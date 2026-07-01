"""Nightshift manager FastAPI app.

Serves two audiences over one HTTP surface:

* **workers** — ``/api/worker/*`` (checkin, poll, heartbeat, run events, submit).
  The manager parses briefs, arbitrates the next task, hands out a leased
  *work order*, and is the sole git authority on submit.
* **operators** — ``/api/*`` (queue / tasks / runs / workers / stats / settings)
  plus the ``/api/events`` SSE stream (snapshot-on-connect + live deltas) and the
  static operator UI.

State lives in the ``nightshift`` Postgres schema via the injected store; briefs
stay canonical on disk. Every mutation publishes to the broadcast hub so all
browsers converge as state changes, not on navigation.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from nightshift import playlists as playlists_mod
from nightshift import repos
from nightshift._paths import UI_DIR
from nightshift.config.validate import build_get_response, validate_delta, write_delta
from nightshift.engine import (
    _worktree_has_commits,
    commit_tasks,
    compute_code_loc,
    create_task,
    delete_task,
    drop_completed_task,
    format_validate_cmd,
    harvest_split_output,
    list_queue,
    load_play_priorities,
    load_sort_mode,
    maybe_sync_main_to_origin,
    normalize_validate_command,
    read_task,
    reorder_queue,
    resolve_preflight_cmd,
    resolve_title,
    resolve_validate_cmd,
    save_play_priorities,
    save_queue_config_value,
    save_sort_mode,
    set_task_meta,
    task_is_evergreen,
)
from nightshift.events import new_run_id
from nightshift.manager.config import ManagerConfig, load_manager_config
from nightshift.manager.hub import Hub
from nightshift.manager.landing import canonical_head, land, main_advanced_sha
from nightshift.manager.registry import Registry
from nightshift.manager.scheduler import (
    SchedulerState,
    WorkerFilter,
    build_candidates,
    parse_required_mcps,
    pick_next,
    queue_label,
    unroutable,
)
from nightshift.manager.store import NightshiftStore, open_store
from nightshift.model_id import provider_of
from nightshift.spawn_daily import (
    MAX_PRIORITY,
    MIN_PRIORITY,
    load_queue_config,
    resolve_config,
    resolve_frontmatter,
    split_frontmatter,
)


# UI assets ship inside the package (see nightshift._paths.UI_DIR).


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class CheckinBody(BaseModel):
    worker_id: str
    backend: str | None = None
    queues: list[str] | None = None
    priorities: list[int] | None = None
    # Advertised capabilities (operator-declared on the worker). ``models`` are
    # the request-facing model ids this worker can serve; ``mcps`` are the MCP
    # connectors wired into its harness. Both feed capability-based routing.
    models: list[str] | None = None
    mcps: list[str] | None = None
    meta: dict[str, Any] | None = None


class PollBody(BaseModel):
    worker_id: str
    backend: str | None = None
    queues: list[str] | None = None
    priorities: list[int] | None = None
    # The poll request *is* the routing filter: the manager returns the first
    # runnable task whose pinned model is in ``models`` (or is auto/max) and
    # whose required MCP set is a subset of ``mcps``.
    models: list[str] | None = None
    mcps: list[str] | None = None


class HeartbeatBody(BaseModel):
    worker_id: str
    lease_id: str | None = None
    phase: str | None = None


class RunEventsBody(BaseModel):
    events: list[dict[str, Any]]


class SubmitBody(BaseModel):
    worker_id: str
    lease_id: str
    task: str
    queue: str | None = None
    title: str
    status: str = "completed"
    result_line: str = ""
    backend: str | None = None
    model: str | None = None
    # False when the worker completed but produced no commit to land (e.g. a
    # non-agentic backend, or an agentic one that decided nothing was needed).
    landable: bool = True
    # Cross-machine landing (transport B): the WIP ref the worker published and
    # the branch tip SHA the manager fetches + verifies before squashing. Both
    # None for a co-located worker that shares the manager's workspace.
    branch_ref: str | None = None
    head_sha: str | None = None
    failure_kind: str | None = None
    failure_reason: str | None = None
    # Best-effort agent telemetry (None when the backend can't report it).
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    # The validate command the worker actually ran (None when validation was
    # skipped or never reached).
    validate_cmd: str | None = None
    # The worktree directory the worker used for this task.
    worktree: str | None = None
    # Worker-side quarantine flag: when the worker has quarantine mode enabled,
    # it sets this to True so the manager quarantines on the first failure
    # instead of waiting for the streak threshold.
    quarantine: bool = False


class ResolveResultBody(BaseModel):
    """Final outcome reported by an out-of-process resolve subprocess (see
    nightshift.manager.resolve_job)."""

    task: str
    queue: str | None = None
    # The original run that conflicted; updated alongside the resolve run so the
    # task's history reflects the eventual land.
    origin_run_id: str | None = None
    status: str = "error"
    landed: bool = False
    sha: str | None = None
    result_line: str | None = None
    failure_kind: str | None = None
    failure_reason: str | None = None
    loc: int | None = None
    remote: str | None = None
    pushed: bool | None = None
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class QueueOrder(BaseModel):
    order: list[str]


class QueueSort(BaseModel):
    sort: str


class QueuePlayPriorities(BaseModel):
    priorities: list[int]


class QueueDedication(BaseModel):
    # Worker ids this queue is dedicated to (empty list clears dedication).
    worker_ids: list[str]


class QueueConfig(BaseModel):
    # Per-queue default target repo (workspace-relative child name). ``None``
    # clears the binding (a queue with no repo is an authoring error on dispatch).
    repo: str | None = None


class ActivePlaylistRequest(BaseModel):
    playlist: str | None = None


class TransportRequest(BaseModel):
    action: str
    mode: str | None = None
    task: str | None = None
    queue: str | None = None


class PlaylistCreate(BaseModel):
    name: str


class PlaylistUpdate(BaseModel):
    """Edit a playlist from its info page. ``name`` renames the queue (its
    on-disk dir + every queue-keyed DB row); ``repository`` is the alias the UI
    shows for the queue's default ``repo`` binding; ``validate`` is the queue's
    validate command. All optional; only the fields present in the request are
    applied."""

    # ``validate`` on the wire; the field is named ``validate_cmd`` to avoid
    # shadowing ``BaseModel.validate``.
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = None
    repository: str | None = None
    validate_cmd: str | None = Field(default=None, alias="validate")
    # Hide the playlist from the default Playlists view and exclude it from the
    # scheduler's candidate set; ``False`` re-enables it. ``None`` leaves it
    # untouched.
    disabled: bool | None = None


class TaskCreate(BaseModel):
    title: str
    text: str
    quarantined: bool | None = None
    # Optional per-task repo override (defaults to the queue's repo). Written as
    # an editable frontmatter meta key on the new brief.
    repo: str | None = None
    loop: bool | None = None
    loop_max_iterations: int | None = None


class TaskUpdate(BaseModel):
    """Partial edit from the detail-view pane. Unset fields are left untouched;
    ``model`` set to "" or "default" clears the field so the task inherits the
    config default. ``title`` (headline) and ``body`` (spec prose) are content
    edits saved alongside the frontmatter toggles in one PATCH. The accepted keys
    mirror :data:`engine._EDITABLE_META_KEYS` ∪ :data:`engine._EDITABLE_CONTENT_KEYS`
    — any field declared here but absent from the model is silently dropped by
    ``model_dump(exclude_unset=True)`` and yields a spurious "no fields to update".
    """

    disabled: bool | None = None
    quarantined: bool | None = None
    completed: bool | None = None
    evergreen: bool | None = None
    automerge: bool | None = None
    draft: bool | None = None
    model: str | None = None
    priority: int | None = None
    title: str | None = None
    body: str | None = None
    # Per-task target-repo override; "" / "default" clears it (inherit queue).
    repo: str | None = None
    loop: bool | None = None
    loop_max_iterations: int | None = None


def _normalize_repo(value: object) -> str | None:
    """Validate an optional per-task repo override from a request payload.

    ``None`` / ``""`` / ``"default"`` clear the override (the task then inherits
    the queue default); any other value must be a bare workspace-child slug or
    it is rejected as a 400 (the path-traversal guard) — surfaced at edit time
    rather than silently written and only caught later at dispatch. Mirrors the
    legacy server's guard so the shared UI behaves identically on both backends.
    """
    if value in (None, "", "default"):
        return None
    repo = str(value).strip()
    if not repo:
        return None
    if not repos.is_valid_repo_ref(repo):
        raise ValueError(
            f"invalid repo reference {repo!r}: a repo must be a bare workspace "
            "child name matching [a-z0-9][a-z0-9-]* (no paths, '..', '/', or "
            "absolute paths)"
        )
    return repo


def _validate_priority(value: object) -> int:
    """Coerce a request's priority to an int in ``[MIN_PRIORITY, MAX_PRIORITY]``,
    raising ``ValueError`` (surfaced as a 400) for out-of-range/non-int input."""
    try:
        priority = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("priority must be an integer 0-5")
    if not (MIN_PRIORITY <= priority <= MAX_PRIORITY):
        raise ValueError(f"priority must be between {MIN_PRIORITY} and {MAX_PRIORITY}")
    return priority


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #


def _workspace_tier(workspace: Path) -> dict:
    """Build a read-only 'Workspace' tier for the /api/settings response."""
    env_var = "NIGHTSHIFT_WORKSPACE"
    env_val = os.environ.get(env_var)
    return {
        "surface": "workspace",
        "categories": [
            {
                "name": "Workspace",
                "fields": [
                    {
                        "key": "location",
                        "label": "Location",
                        "desc": (
                            "The workspace directory Nightshift reads and writes repos "
                            "and config from. Set via --workspace flag or "
                            f"{env_var} env var; resolved at launch."
                        ),
                        "type": "readonly",
                        "apply": "restart",
                        "store": "—",
                        "default": None,
                        "secret": False,
                        "stored": str(workspace),
                        "effective": str(workspace),
                        "env": env_var,
                        "env_shadowed": bool(env_val),
                    }
                ],
            }
        ],
    }


def create_app(workspace: Path, *, store: NightshiftStore | None = None) -> FastAPI:
    workspace = Path(workspace).resolve()
    cfg: ManagerConfig = load_manager_config(workspace)
    # The two roots: briefs/queue config live in the content store
    # (``tasks_root``); git ops resolve a target repo per task under the
    # workspace. ``tasks_repo`` is the content-store repo's bare child name.
    tasks_repo = cfg.tasks_repo
    tasks_root = workspace / tasks_repo

    async def _origin_sync_loop() -> None:
        """Periodically refresh origin/main for every queue's target repo.

        Each repo is checked at most once per ``cadences.git_refresh_seconds``.
        A check fetches origin/main and fast-forwards local main only when the
        remote tip moved; otherwise the repo is left alone until the next
        interval. ``0`` disables the background loop (dispatch/land still refresh
        on their own throttle)."""
        interval = cfg.cadences.git_refresh_seconds
        remote = cfg.rendezvous_remote
        if not interval or interval <= 0 or not remote:
            return
        if cfg.landing_mode not in ("push", "pr"):
            return
        while True:
            seen: set[str] = set()
            queues: list[str | None] = [None]
            with contextlib.suppress(Exception):
                queues += [
                    p["name"]
                    for p in playlists_mod.list_playlists(tasks_root)
                    if not p.get("disabled")
                ]
            for q in queues:
                with contextlib.suppress(Exception):
                    target = load_queue_config(
                        tasks_root, playlists_mod.tasks_rel(q)
                    ).get("repo")
                    if not target or target in seen:
                        continue
                    seen.add(target)
                    await asyncio.to_thread(
                        maybe_sync_main_to_origin,
                        workspace,
                        target,
                        remote,
                        min_interval_seconds=interval,
                    )
            await asyncio.sleep(interval)

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI):
        if app.state.store is None:
            app.state.store = await open_store(cfg.dsn)
        app.state.registry = Registry(
            app.state.store, stale_seconds=cfg.cadences.worker_stale_seconds
        )
        sync_task = asyncio.create_task(_origin_sync_loop())
        app.state.origin_sync_task = sync_task
        try:
            yield
        finally:
            sync_task.cancel()
            # CancelledError is a BaseException (not Exception), so suppress it
            # explicitly alongside any teardown error from the sync loop.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sync_task
            with contextlib.suppress(Exception):
                await app.state.store.close()

    app = FastAPI(title="Nightshift Manager", lifespan=_lifespan)

    app.state.workspace = workspace
    app.state.tasks_root = tasks_root
    app.state.tasks_repo = tasks_repo
    app.state.cfg = cfg
    app.state.hub = Hub()
    app.state.sched_state = SchedulerState()
    # One repo_unavailable warning per queue (deduped by queue key); cleared on
    # rescan so a re-cloned repo re-warns if it disappears again.
    app.state.repo_warnings = set()
    # Live out-of-process resolve subprocesses, keyed by their (child) run id:
    # {run_id: {"proc", "repo", "task", "queue", "origin_run_id"}}. Used to cap
    # concurrency per repo and to reap finished jobs.
    app.state.resolves = {}
    app.state.store = store  # may be None until lifespan opens one

    def _store() -> NightshiftStore:
        return app.state.store

    def _registry() -> Registry:
        return app.state.registry

    async def _emit(
        kind: str,
        *,
        run_id: str | None = None,
        queue: str | None = None,
        task: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist a state-change event and fan it out to all browsers."""
        store = _store()
        event_id = await store.append_event(
            kind, run_id=run_id, queue=queue, task=task, payload=payload
        )
        await app.state.hub.publish(
            {
                "id": event_id,
                "kind": kind,
                "run_id": run_id,
                "queue": queue,
                "task": task,
                "payload": payload or {},
            }
        )

    async def _quarantine_if_looping(
        queue: str | None, task: str, run_id: str, detail: str
    ) -> bool:
        """Quarantine a task that is stuck re-executing without progress.

        Counts the most recent consecutive runs of ``task`` that landed nothing
        (see :func:`no_progress_streak`); once that streak reaches the configured
        ``quarantine_threshold`` the task is pinned to the ``quarantined`` state.
        A quarantined task stays in the queue (so the operator sees it) but is
        excluded from dispatch by every worker — the budget-protection stop. The
        offending runs (and their logs, kept as ``task_log`` events) are retained
        for later analysis. Returns ``True`` when it quarantined the task.
        """
        if cfg.quarantine_threshold <= 0:
            return False
        runs = await _store().list_runs(queue=queue, limit=50)
        streak = no_progress_streak(runs, task)
        if streak < cfg.quarantine_threshold:
            return False
        reason = (
            f"quarantined after {streak} consecutive runs with no progress "
            f"({detail}); execution halted to protect budget — review the run "
            f"logs and edit or delete the task to release it"
        )
        await _store().set_task_state(
            queue, task, "quarantined", blocked_reason=reason
        )
        await _emit(
            "task_quarantined",
            run_id=run_id,
            queue=queue,
            task=task,
            payload={"reason": reason, "streak": streak},
        )
        return True

    async def _quarantine_immediate(
        queue: str | None, task: str, run_id: str, detail: str
    ) -> bool:
        """Quarantine a task on the first failure (worker quarantine mode).

        Unlike :func:`_quarantine_if_looping` which waits for a streak to
        reach the configured threshold, this quarantines unconditionally —
        called when the submitting worker has quarantine mode enabled.
        """
        reason = (
            f"quarantined by worker on first failure ({detail}); "
            f"review the run logs and edit or delete the task to release it"
        )
        await _store().set_task_state(
            queue, task, "quarantined", blocked_reason=reason
        )
        await _emit(
            "task_quarantined",
            run_id=run_id,
            queue=queue,
            task=task,
            payload={"reason": reason, "streak": 1},
        )
        return True

    # ----- worker auth (optional shared secret) ---------------------------- #

    def _require_secret(x_nightshift_secret: str | None = Header(default=None)) -> None:
        if cfg.shared_secret and x_nightshift_secret != cfg.shared_secret:
            raise HTTPException(status_code=401, detail="bad or missing worker secret")

    # ----- queue helpers --------------------------------------------------- #

    def _queue_from_label(label: str | None) -> str | None:
        """Map a worker/UI queue label ('main' or playlist) to internal name."""
        if label in (None, "", "main"):
            return None
        return label

    def _resolve_queue(queue: str | None) -> str | None:
        """Operator API queue resolution: absent param (None) falls back to the
        focused queue so ``GET /api/queue`` returns the active playlist's tasks.
        An empty string targets main explicitly."""
        if queue is None:
            return _active_playlist
        if queue == "":
            return None
        return _queue_from_label(queue)

    def _queue_exists(queue: str | None) -> bool:
        return queue is None or playlists_mod.exists(tasks_root, queue)

    def _all_queues() -> list[str | None]:
        # Disabled playlists are hidden + parked: excluded here so the scheduler
        # never builds candidates from them. The main queue is always included.
        return [
            None,
            *[
                p["name"]
                for p in playlists_mod.list_playlists(tasks_root)
                if not p.get("disabled")
            ],
        ]

    def _queue_repo(queue: str | None) -> str | None:
        """The queue's configured default target repo (or ``None`` when unset)."""
        return load_queue_config(
            tasks_root, playlists_mod.tasks_rel(queue)
        ).get("repo")

    def _reap_resolves() -> None:
        """Drop bookkeeping for resolve subprocesses that have exited."""
        for rid in [
            rid for rid, r in app.state.resolves.items()
            if r["proc"].poll() is not None
        ]:
            app.state.resolves.pop(rid, None)

    def _active_resolves(repo: str) -> int:
        return sum(
            1 for r in app.state.resolves.values()
            if r["repo"] == repo and r["proc"].poll() is None
        )

    def _spawn_resolve(
        child_run_id: str,
        *,
        task: str,
        queue: str | None,
        repo: str,
        title: str,
        origin_run_id: str | None,
    ) -> bool:
        """Launch the out-of-process resolver. Returns True if it started.

        Stored on ``app.state.spawn_resolve`` so tests can substitute a stub
        without launching a real subprocess (which can't reach an in-process
        TestClient anyway)."""
        argv = [
            sys.executable, "-m", "nightshift.manager.resolve_job",
            "--workspace", str(workspace),
            "--repo", repo,
            "--task", task,
            "--title", title,
            "--tasks-repo", tasks_repo,
            "--run-id", child_run_id,
            "--manager-url", f"http://127.0.0.1:{cfg.port}",
            "--landing-mode", cfg.landing_mode,
            "--max-push-retries", str(cfg.max_push_retries),
        ]
        if queue:
            argv += ["--queue", queue]
        if origin_run_id:
            argv += ["--origin-run-id", origin_run_id]
        if cfg.rendezvous_remote:
            argv += ["--rendezvous-remote", cfg.rendezvous_remote]
        env = dict(os.environ)
        if cfg.shared_secret:
            env["NIGHTSHIFT_SHARED_SECRET"] = cfg.shared_secret
        try:
            proc = subprocess.Popen(argv, env=env)  # noqa: S603 — fixed argv
        except OSError:
            return False
        app.state.resolves[child_run_id] = {
            "proc": proc, "repo": repo, "task": task,
            "queue": queue, "origin_run_id": origin_run_id,
        }
        return True

    app.state.spawn_resolve = _spawn_resolve

    async def _start_resolve(
        origin_run_id: str,
        *,
        task: str,
        queue: str | None,
        repo: str,
        title: str,
    ) -> tuple[bool, str | None, str | None]:
        """Create a resolve run + spawn the resolver, honoring the per-repo cap.

        Returns ``(started, child_run_id, error)``. Used by both the explicit
        Resolve endpoint and the auto-escalation path on a landing conflict."""
        _reap_resolves()
        if _active_resolves(repo) >= max(1, cfg.max_concurrent_resolves):
            return False, None, "a resolve is already running for this repo"
        store = _store()
        child_run_id = new_run_id()
        await store.create_run(
            child_run_id,
            task=task,
            queue=queue,
            worker_id="manager:resolve",
            backend=cfg.raw.get("resolve_backend"),
            model=cfg.raw.get("resolve_model"),
            title=title,
            repo=repo,
        )
        started = app.state.spawn_resolve(
            child_run_id, task=task, queue=queue, repo=repo,
            title=title, origin_run_id=origin_run_id,
        )
        if not started:
            await store.update_run(
                child_run_id, status="error",
                result_line="failed to launch resolver process",
                failure_kind="worker_launch",
            )
            return False, child_run_id, "failed to launch resolver process"
        await _emit(
            "run_started", run_id=child_run_id, queue=queue, task=task,
            payload={"task": task, "resolve": True},
        )
        return True, child_run_id, None

    # ===================================================================== #
    # Worker API
    # ===================================================================== #

    @app.post("/api/worker/checkin", dependencies=[Depends(_require_secret)])
    async def worker_checkin(body: CheckinBody) -> JSONResponse:
        worker = await _registry().checkin(
            body.worker_id,
            backend=body.backend,
            queues=body.queues,
            priorities=body.priorities,
            models=body.models,
            mcps=body.mcps,
            meta=body.meta,
        )
        await _emit("worker_registered", payload={"worker_id": body.worker_id})
        return JSONResponse(
            {
                "ok": True,
                "worker": _jsonable(worker),
                "cadences": {
                    "poll_seconds": cfg.cadences.poll_seconds,
                    "heartbeat_seconds": cfg.cadences.heartbeat_seconds,
                    "lease_ttl_seconds": cfg.cadences.lease_ttl_seconds,
                    "refresh_ms": cfg.cadences.refresh_ms,
                },
            }
        )

    @app.post("/api/worker/poll", dependencies=[Depends(_require_secret)])
    async def worker_poll(body: PollBody) -> JSONResponse:
        store = _store()
        await store.reclaim_expired_leases()
        await _registry().reap_stale()

        # Build candidates across every queue from the canonical briefs in the
        # content store (each candidate already carries its resolved target repo).
        # Queues paused via the transport controls are excluded from dispatch.
        candidates_by_queue = {
            q: build_candidates(tasks_root, q, default_model=cfg.default_model)
            for q in _all_queues()
            if queue_label(q) not in _paused_queues
        }

        # Manager-side queue dedication (queue label -> bound worker ids).
        dedication = await store.queue_dedication()

        # Tasks quarantined for re-execution looping are held: excluded from
        # dispatch and never re-stated by the overlays below, so a quarantine
        # reason is never clobbered by a (lower-priority) blocked/repo overlay.
        quarantined = {
            (_queue_from_label(row["queue"]), row["task"])
            for row in await store.tasks_in_state("quarantined")
        }

        # Mark tasks blocked when no live worker can ever currently serve them:
        # an unadvertised pinned model, an unadvertised connector, or a queue
        # dedicated only to offline workers.
        reg = _registry()
        available_models = await reg.available_models()
        available_mcps = await reg.available_mcps()
        online_workers = await reg.online_worker_ids()
        for cand, reason in unroutable(
            candidates_by_queue,
            available_models=available_models,
            available_mcps=available_mcps,
            dedication=dedication,
            online_workers=online_workers,
        ):
            if (cand.queue, cand.task) in quarantined:
                continue
            existing = await store.get_task_state(cand.queue, cand.task)
            if not existing or existing.get("state") != "blocked":
                await store.set_task_state(
                    cand.queue, cand.task, "blocked", blocked_reason=reason
                )
                await _emit(
                    "task_blocked",
                    queue=cand.queue,
                    task=cand.task,
                    payload={"reason": reason},
                )

        # Repo resolution & availability: a malformed/unset repo reference is an
        # authoring error (-> blocked); a well-formed name whose repo is absent
        # pauses the task (-> repo_unavailable) and warns once per queue. Both
        # are excluded from dispatch; neither records a failed run.
        repo_excluded: set[tuple[str | None, str]] = set()
        for cands in candidates_by_queue.values():
            for cand in cands:
                key = (cand.queue, cand.task)
                if key in quarantined:
                    continue
                if cand.repo_error is not None:
                    existing = await store.get_task_state(cand.queue, cand.task)
                    if not existing or existing.get("state") != "blocked":
                        await store.set_task_state(
                            cand.queue, cand.task, "blocked",
                            blocked_reason=cand.repo_error,
                        )
                        await _emit(
                            "task_blocked",
                            queue=cand.queue,
                            task=cand.task,
                            payload={"reason": cand.repo_error},
                        )
                    repo_excluded.add(key)
                elif cand.repo and not repos.repo_available(workspace, cand.repo):
                    existing = await store.get_task_state(cand.queue, cand.task)
                    if not existing or existing.get("state") != "repo_unavailable":
                        await store.set_task_state(
                            cand.queue, cand.task, "repo_unavailable", repo=cand.repo
                        )
                    repo_excluded.add(key)
                    if cand.queue not in app.state.repo_warnings:
                        app.state.repo_warnings.add(cand.queue)
                        await _emit(
                            "repo_unavailable",
                            queue=cand.queue,
                            task=cand.task,
                            payload={"repo": cand.repo},
                        )

        active = await store.active_leases()
        leased = {(_queue_from_label(le["queue"]), le["task"]) for le in active}
        blocked_rows = await store.list_blocked()
        blocked = {(_queue_from_label(b["queue"]), b["task"]) for b in blocked_rows}
        # Never dispatch a paused (repo_unavailable), repo-blocked, or
        # quarantined (re-execution loop) task.
        blocked |= repo_excluded
        blocked |= quarantined

        worker = WorkerFilter(
            worker_id=body.worker_id,
            queues=body.queues,
            priorities=body.priorities,
            models=body.models,
            mcps=body.mcps,
        )
        chosen = pick_next(
            candidates_by_queue,
            worker=worker,
            leased=leased,
            blocked=blocked,
            state=app.state.sched_state,
            dedication=dedication,
        )
        if chosen is None:
            return JSONResponse({"work": None}, status_code=200)

        # The chosen candidate is repo-available by construction; clear any prior
        # paused/blocked overlay it may carry (e.g. a now-resolved repo) so the
        # dispatch is clean, and pin the target repo's HEAD as base_ref.
        repo = chosen.repo
        prior = await store.get_task_state(chosen.queue, chosen.task)
        if prior and prior.get("state") in ("repo_unavailable", "blocked"):
            await store.clear_task_state(chosen.queue, chosen.task)
        # Origin-aware dispatch: for any remote-landing mode (push or pr), resync
        # local main to origin/main before pinning base_ref so the worker starts
        # from the freshest merged state in a multi-actor repo (and an orphaned
        # ephemeral pr-mode squash is dropped). Best-effort: a transient fetch
        # failure must not fail the poll — base_ref then pins the local HEAD and
        # the land re-syncs anyway. See remote-landing.md.
        poll_meta = _task_meta(tasks_root, chosen.task, chosen.queue)
        effective_mode = "pr" if poll_meta.get("make_pr") else cfg.landing_mode
        if effective_mode in ("push", "pr") and cfg.rendezvous_remote:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    maybe_sync_main_to_origin,
                    workspace,
                    repo,
                    cfg.rendezvous_remote,
                    min_interval_seconds=cfg.cadences.git_refresh_seconds,
                )
        base_ref = canonical_head(workspace / repo)
        lease = await store.acquire_lease(
            task=chosen.task,
            queue=chosen.queue,
            worker_id=body.worker_id,
            model=chosen.model,
            base_ref=base_ref,
            ttl_seconds=cfg.cadences.lease_ttl_seconds,
        )
        if lease is None:
            # Lost a race for this task; let the worker poll again shortly.
            return JSONResponse({"work": None}, status_code=200)

        run_id = new_run_id()
        order = _build_work_order(
            workspace, tasks_root, chosen.task, chosen.queue, repo,
            lease["id"], run_id, base_ref, cfg,
        )
        planned_validate = order["config"].get("validate_cmd") or None
        await store.create_run(
            run_id,
            task=chosen.task,
            queue=chosen.queue,
            worker_id=body.worker_id,
            backend=provider_of(order["config"]["model"]) or body.backend,
            model=order["config"]["model"],
            title=order["title"],
            body=order["body"],
            required_mcps=list(chosen.required_mcps),
            repo=repo,
            validate_cmd=planned_validate or None,
        )
        await store.set_lease_status(lease["id"], "leased", run_id=run_id)
        await _registry().set_busy(
            body.worker_id, task=chosen.task, queue=chosen.queue, run_id=run_id
        )
        await _emit(
            "lease_acquired",
            run_id=run_id,
            queue=chosen.queue,
            task=chosen.task,
            payload={"worker_id": body.worker_id, "lease_id": lease["id"]},
        )
        await _emit(
            "run_started",
            run_id=run_id,
            queue=chosen.queue,
            task=chosen.task,
            payload={"title": order["title"], "worker_id": body.worker_id},
        )
        return JSONResponse({"work": order})

    @app.post("/api/worker/heartbeat", dependencies=[Depends(_require_secret)])
    async def worker_heartbeat(body: HeartbeatBody) -> JSONResponse:
        await _registry().heartbeat(body.worker_id)
        if body.lease_id:
            await _store().heartbeat_lease(body.lease_id, cfg.cadences.lease_ttl_seconds)
        return JSONResponse({"ok": True})

    @app.post("/api/worker/runs/{run_id}/events", dependencies=[Depends(_require_secret)])
    async def worker_run_events(run_id: str, body: RunEventsBody) -> JSONResponse:
        store = _store()
        run = await store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="unknown run")
        for ev in body.events:
            kind = ev.get("type", "task_log")
            await _emit(
                kind,
                run_id=run_id,
                queue=_queue_from_label(run.get("queue")),
                task=ev.get("task") or run.get("task"),
                payload=ev,
            )
            if kind == "task_status" and ev.get("phase"):
                await store.update_run(run_id, phase=ev["phase"])
        return JSONResponse({"ok": True})

    @app.post("/api/worker/runs/{run_id}/submit", dependencies=[Depends(_require_secret)])
    async def worker_submit(run_id: str, body: SubmitBody) -> JSONResponse:
        store = _store()
        run = await store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="unknown run")
        queue = _queue_from_label(body.queue)
        lease = await store.get_lease(body.lease_id)
        base_ref = lease.get("base_ref") if lease else None
        # The target repo the worker ran against is recorded on the run (and is
        # workspace-relative); landing materialises ``workspace / repo``.
        repo = run.get("repo")

        # Agent telemetry recorded on every outcome (a failed/no-change run still
        # burned turns + tokens), so the per-task rollups stay accurate.
        telemetry = {
            "turns": body.turns,
            "input_tokens": body.input_tokens,
            "output_tokens": body.output_tokens,
            "cost_usd": body.cost_usd,
            "validate_cmd": body.validate_cmd,
            "worktree": body.worktree,
        }

        # An honest block from the worker (no commits): record the outcome, hold
        # the task in the DB ``blocked`` state with its reason, do NOT land and do
        # NOT drop the brief, so a Resolve can pick it up later.
        if body.status == "blocked":
            reason = body.failure_reason or body.result_line or "blocked"
            await store.update_run(
                run_id,
                status="blocked",
                result_line=body.result_line,
                failure_kind=body.failure_kind or "blocked",
                failure_reason=body.failure_reason,
                model=body.model,
                **telemetry,
            )
            await store.set_lease_status(body.lease_id, "released")
            await store.set_task_state(queue, body.task, "blocked", blocked_reason=reason)
            await _registry().set_idle(body.worker_id)
            await _emit(
                "task_blocked", queue=queue, task=body.task, payload={"reason": reason},
            )
            await _emit(
                "task_result", run_id=run_id, queue=queue, task=body.task,
                payload={"status": "blocked", "result_line": body.result_line},
            )
            return JSONResponse({"landed": False, "status": "blocked"})

        # A non-completed submit (worker failed/aborted before producing a branch)
        # records the outcome and releases the lease without touching main.
        if body.status != "completed":
            await store.update_run(
                run_id,
                status=body.status,
                result_line=body.result_line,
                failure_kind=body.failure_kind,
                failure_reason=body.failure_reason,
                model=body.model,
                **telemetry,
            )
            await store.set_lease_status(body.lease_id, "released")
            # A worker *error* leaves the brief in the queue with no blocking
            # overlay, so it re-leases on the next poll. Repeated errors are a
            # re-execution loop → quarantine. Operator-driven stops (aborted) are
            # intentional and never quarantine. When the worker has quarantine
            # mode enabled it requests immediate quarantine on the first failure.
            quarantined = False
            if body.status == "error":
                if body.quarantine:
                    quarantined = await _quarantine_immediate(
                        queue, body.task, run_id, "worker error"
                    )
                else:
                    quarantined = await _quarantine_if_looping(
                        queue, body.task, run_id, "worker error"
                    )
            await _registry().set_idle(body.worker_id)
            await _emit(
                "task_result",
                run_id=run_id,
                queue=queue,
                task=body.task,
                payload={"status": body.status, "result_line": body.result_line},
            )
            return JSONResponse(
                {"landed": False, "status": body.status, "quarantined": quarantined}
            )

        # Completed but nothing to land (no commit): record success, no git.
        # Unless the agent landed on main directly during the run — main advanced
        # past the lease's base_ref while the task branch has nothing to squash.
        if not body.landable:
            # Split (decomposition) runs: harvest subtask briefs from the
            # split output dir and enqueue them, then retire the parent.
            split_meta = _task_meta(tasks_root, body.task, queue)
            if split_meta.get("split"):
                tasks_rel = playlists_mod.tasks_rel(queue)
                created = harvest_split_output(
                    workspace, tasks_root, repo, body.task, split_meta,
                    queue=queue, tasks_rel=tasks_rel,
                )
                if created:
                    result_line = (
                        f"decomposed into {len(created)} subtask(s): "
                        + ", ".join(created)
                    )
                else:
                    result_line = "decomposition run produced no subtasks"
                await store.update_run(
                    run_id, status="completed",
                    result_line=result_line, model=body.model,
                    **telemetry,
                )
                await store.set_lease_status(body.lease_id, "released")
                await store.clear_task_state(queue, body.task)
                await _registry().set_idle(body.worker_id)
                await _emit(
                    "task_result", run_id=run_id, queue=queue, task=body.task,
                    payload={
                        "status": "completed",
                        "result_line": result_line,
                        "subtasks": created,
                    },
                )
                await _emit("queue_changed", queue=queue)
                return JSONResponse({
                    "landed": False, "status": "completed",
                    "split": True, "subtasks": created,
                })

            repo_root = workspace / repo
            if (
                base_ref
                and main_advanced_sha(repo_root, base_ref)
                and not _worktree_has_commits(workspace, repo, body.task, queue=queue)
            ):
                body = body.model_copy(update={
                    "landable": True,
                    "result_line": "agent landed on main",
                })
            else:
                await store.update_run(
                    run_id, status="completed",
                    result_line=body.result_line or "no changes", model=body.model,
                    **telemetry,
                )
                await store.set_lease_status(body.lease_id, "released")
                if body.quarantine:
                    quarantined = await _quarantine_immediate(
                        queue, body.task, run_id, "no changes produced"
                    )
                else:
                    quarantined = await _quarantine_if_looping(
                        queue, body.task, run_id, "no changes produced"
                    )
                if not quarantined:
                    await store.clear_task_state(queue, body.task)
                await _registry().set_idle(body.worker_id)
                await _emit(
                    "task_result", run_id=run_id, queue=queue, task=body.task,
                    payload={"status": "completed", "result_line": body.result_line or "no changes"},
                )
                await _emit("queue_changed", queue=queue)
                return JSONResponse(
                    {"landed": False, "status": "completed", "no_changes": True,
                     "quarantined": quarantined}
                )

        tasks_rel = playlists_mod.tasks_rel(queue)
        meta = _task_meta(tasks_root, body.task, queue)
        # A task may force PR mode with `make_pr: true`; otherwise the manager's
        # configured landing_mode applies. make_pr never forces squash/push.
        effective_mode = "pr" if meta.get("make_pr") else cfg.landing_mode
        result = land(
            workspace,
            repo,
            body.task,
            body.title,
            queue=queue,
            base_ref=base_ref,
            landing_mode=effective_mode,
            automerge=bool(meta.get("automerge", True)),
            draft=bool(meta.get("draft", False)),
            autostash=bool(
                resolve_config(workspace, tasks_root, tasks_rel).get(
                    "autostash_operator_work", True
                )
            ),
            branch_ref=body.branch_ref,
            head_sha=body.head_sha,
            rendezvous_remote=cfg.rendezvous_remote,
            git_refresh_seconds=cfg.cadences.git_refresh_seconds,
        )

        if not result.landed:
            status = "error"
            failure_kind = "merge_conflict" if result.conflict else "merge_rejected"
            await store.update_run(
                run_id,
                status=status,
                result_line=result.detail.splitlines()[0][:200] if result.detail else "land failed",
                failure_kind=failure_kind,
                failure_reason=result.detail,
                model=body.model,
                **telemetry,
            )
            await store.set_lease_status(body.lease_id, "released")
            await _registry().set_idle(body.worker_id)
            # The branch is preserved (squash_to_main / land only teardown after a
            # confirmed land), so the conflict or rejection is resolvable. Hold the
            # task blocked so it doesn't re-lease while a resolve is pending.
            if result.conflict or result.recoverable:
                await store.set_task_state(
                    queue, body.task, "blocked",
                    blocked_reason="needs resolve: " + (
                        result.detail.splitlines()[0] if result.detail
                        else failure_kind
                    ),
                )
                await _emit(
                    "task_blocked", queue=queue, task=body.task,
                    payload={"reason": failure_kind, "detail": result.detail},
                )
            await _emit(
                "task_result", run_id=run_id, queue=queue, task=body.task,
                payload={"status": status, "failure_kind": failure_kind},
            )
            # Auto-escalate: when enabled, immediately spawn the out-of-process
            # resolver instead of waiting for an operator to click Resolve. PR
            # mode lands via GitHub, so it isn't escalated here.
            resolving = False
            if (
                cfg.auto_resolve
                and (result.conflict or result.recoverable)
                and effective_mode != "pr"
            ):
                resolving, _child, _err = await _start_resolve(
                    run_id, task=body.task, queue=queue, repo=repo, title=body.title,
                )
            return JSONResponse(
                {
                    "landed": False,
                    "conflict": result.conflict,
                    "detail": result.detail,
                    "resolving": resolving,
                }
            )

        # Backstop the worker's queue removal: a completed regular task must
        # leave the content store. Evergreen tasks keep their file and re-run.
        if not task_is_evergreen(
            meta, body.task, resolve_config(workspace, tasks_root, tasks_rel)
        ):
            with contextlib.suppress(Exception):
                drop_completed_task(tasks_root, body.task, tasks_rel, queue=queue)

        loc = None
        if result.sha:
            with contextlib.suppress(Exception):
                loc = compute_code_loc(workspace / repo, result.sha)
        await store.update_run(
            run_id,
            status="completed",
            result_line=body.result_line or result.detail or "landed",
            commit_sha=result.sha,
            loc=loc,
            model=body.model,
            remote=result.remote,
            pushed=result.pushed,
            **telemetry,
        )
        await store.set_lease_status(body.lease_id, "landed")
        await store.clear_task_state(queue, body.task)
        await _registry().set_idle(body.worker_id)
        await _emit(
            "task_result",
            run_id=run_id,
            queue=queue,
            task=body.task,
            payload={
                "status": "completed",
                "commit_sha": result.sha,
                "remote": result.remote,
                "pushed": result.pushed,
                "pr_url": result.pr_url,
            },
        )
        await _emit("queue_changed", queue=queue)
        return JSONResponse(
            {
                "landed": True,
                "sha": result.sha,
                "remote": result.remote,
                "pushed": result.pushed,
                "pr_url": result.pr_url,
            }
        )

    @app.post(
        "/api/worker/runs/{run_id}/resolve-result",
        dependencies=[Depends(_require_secret)],
    )
    async def worker_resolve_result(
        run_id: str, body: ResolveResultBody
    ) -> JSONResponse:
        """Record the outcome of an out-of-process resolve (see resolve_job).

        On a landed resolve: complete the resolve run, reflect the land onto the
        original run, clear the task overlay, and drop the brief (non-evergreen).
        Otherwise: re-block the task so it stays resolvable."""
        store = _store()
        queue = _queue_from_label(body.queue)
        telemetry = {
            "turns": body.turns,
            "input_tokens": body.input_tokens,
            "output_tokens": body.output_tokens,
            "cost_usd": body.cost_usd,
        }
        if body.landed and body.status == "completed":
            await store.update_run(
                run_id,
                status="completed",
                result_line=body.result_line or "resolved: landed",
                commit_sha=body.sha,
                loc=body.loc,
                remote=body.remote,
                pushed=body.pushed,
                **telemetry,
            )
            if body.origin_run_id:
                with contextlib.suppress(Exception):
                    await store.update_run(
                        body.origin_run_id,
                        status="completed",
                        result_line=body.result_line or "resolved",
                        commit_sha=body.sha,
                    )
            await store.clear_task_state(queue, body.task)
            tasks_rel = playlists_mod.tasks_rel(queue)
            meta = _task_meta(tasks_root, body.task, queue)
            if not task_is_evergreen(
                meta, body.task, resolve_config(workspace, tasks_root, tasks_rel)
            ):
                with contextlib.suppress(Exception):
                    drop_completed_task(tasks_root, body.task, tasks_rel, queue=queue)
            await _emit(
                "task_result", run_id=run_id, queue=queue, task=body.task,
                payload={
                    "status": "completed", "commit_sha": body.sha,
                    "remote": body.remote, "pushed": body.pushed,
                },
            )
            await _emit("queue_changed", queue=queue)
        else:
            await store.update_run(
                run_id,
                status="error",
                result_line=body.result_line or "resolve failed",
                failure_kind=body.failure_kind or "merge_conflict",
                failure_reason=body.failure_reason,
                **telemetry,
            )
            reason = "needs resolve: " + (
                body.result_line or body.failure_reason or "resolve failed"
            )
            await store.set_task_state(
                queue, body.task, "blocked", blocked_reason=reason,
            )
            await _emit(
                "task_blocked", queue=queue, task=body.task,
                payload={"reason": body.failure_kind or "merge_conflict"},
            )
            await _emit(
                "task_result", run_id=run_id, queue=queue, task=body.task,
                payload={"status": "error", "failure_kind": body.failure_kind},
            )
        app.state.resolves.pop(run_id, None)
        return JSONResponse({"ok": True})

    # ===================================================================== #
    # Operator API
    # ===================================================================== #

    @app.post("/api/runs/{run_id}/{task}/resolve")
    @app.post("/api/runs/{run_id}/{task}/recover")
    async def resolve_run_task(
        run_id: str, task: str, queue: str | None = None
    ) -> JSONResponse:
        """Operator-triggered resolve of a conflicted task. Spawns the resolver
        out-of-process (non-blocking) so the manager stays responsive and other
        tasks keep dispatching while the agent works."""
        store = _store()
        origin = await store.get_run(run_id)
        if origin is None:
            raise HTTPException(status_code=404, detail="unknown run")
        target_queue = (
            _queue_from_label(queue) if queue is not None
            else _queue_from_label(origin.get("queue"))
        )
        repo = origin.get("repo") or _queue_repo(target_queue)
        if not repo:
            raise HTTPException(
                status_code=400, detail="no target repo for this task"
            )
        title = origin.get("title") or task
        started, child_run_id, error = await _start_resolve(
            run_id, task=task, queue=target_queue, repo=repo, title=title,
        )
        if not started:
            return JSONResponse({"ok": False, "error": error}, status_code=409)
        return JSONResponse(
            {"ok": True, "run_id": child_run_id, "task": task}, status_code=202
        )

    @app.get("/api/queue")
    def get_queue(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse(list_queue(tasks_root, playlists_mod.tasks_rel(target)))

    @app.get("/api/main/tasks")
    def get_main_tasks() -> JSONResponse:
        """The main queue's tasks, surfaced in the Playlists screen as the
        library row count and in the Add-from picker."""
        return JSONResponse(list_queue(tasks_root, playlists_mod.tasks_rel(None)))

    @app.get("/api/tasks/{task}")
    async def get_task(task: str, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        try:
            info = read_task(tasks_root, task, playlists_mod.tasks_rel(target))
        except FileNotFoundError:
            return JSONResponse({"error": "task not found"}, status_code=404)
        label = queue_label(target)
        info["model_options"] = await _registry().models_for_queue(label)
        return JSONResponse(info)

    @app.get("/api/task-defaults")
    async def get_task_defaults(queue: str | None = None) -> JSONResponse:
        """Brief-shaped defaults for a new task: effective model/draft/automerge
        for the target queue, plus live model choices from registered workers."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        config = resolve_config(workspace, tasks_root, playlists_mod.tasks_rel(target))
        resolved = resolve_frontmatter({}, config)
        label = queue_label(target)
        models = await _registry().models_for_queue(label)
        return JSONResponse(
            {
                "task": None,
                "title": "",
                "body": "",
                "frontmatter": {
                    "model": resolved["model"],
                    "draft": resolved["draft"],
                    "automerge": resolved["automerge"],
                    "priority": 3,
                },
                "frontmatter_raw": {},
                "evergreen": False,
                "disabled": False,
                "model_options": models,
            }
        )

    @app.post("/api/tasks")
    async def post_task(body: TaskCreate, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        target_rel = playlists_mod.tasks_rel(target)
        # Validate the optional repo override *before* writing the brief so a
        # malformed ref is a clean 400 that never orphans a file in the content
        # store (matches the legacy server and the contract's edit-time guard).
        try:
            repo_override = _normalize_repo(body.repo)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            created = create_task(tasks_root, body.title, body.text, target_rel)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileExistsError as exc:
            return JSONResponse({"error": f"task already exists: {exc}"}, status_code=409)
        # Apply optional frontmatter fields from the create pane (repo
        # override, loop mode) to the freshly created file.
        meta_changes: dict[str, object | None] = {}
        if repo_override is not None:
            meta_changes["repo"] = repo_override
        if body.loop is not None:
            meta_changes["loop"] = body.loop
        if body.loop_max_iterations is not None:
            meta_changes["loop_max_iterations"] = body.loop_max_iterations
        if meta_changes:
            with contextlib.suppress(FileNotFoundError, ValueError):
                set_task_meta(
                    tasks_root, created["task"], meta_changes, target_rel
                )
        commit_tasks(tasks_root, f"nightshift: create task {created['task']}")
        await _emit("queue_changed", queue=target, task=created.get("task"))
        return JSONResponse(created)

    @app.patch("/api/tasks/{task}")
    async def patch_task(
        task: str, body: TaskUpdate, queue: str | None = None
    ) -> JSONResponse:
        target = _resolve_queue(queue)
        target_rel = playlists_mod.tasks_rel(target)
        # Partial edit: only the fields the detail pane actually sent reach the
        # frontmatter writer. ``model``/``priority``/``repo`` are normalised the
        # same way as the legacy server so the two backends behave identically.
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            return JSONResponse({"error": "no fields to update"}, status_code=400)
        changes: dict[str, object | None] = {}
        try:
            for key, value in fields.items():
                if key == "model":
                    # "" / "default" clears the key so the task inherits the default.
                    changes["model"] = None if value in (None, "", "default") else value
                elif key == "priority":
                    if value is not None:
                        changes["priority"] = _validate_priority(value)
                elif key == "repo":
                    # "" / "default" clears the override → inherit the queue repo.
                    changes["repo"] = _normalize_repo(value)
                else:
                    changes[key] = value
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            updated = set_task_meta(tasks_root, task, changes, target_rel)
        except FileNotFoundError:
            return JSONResponse({"error": "task not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        commit_tasks(tasks_root, f"nightshift: edit task {task}")
        # When the operator sets the task back to "ready" (not disabled,
        # not quarantined, not completed), clear the manager's state
        # overlay so it is no longer excluded from dispatch.
        now_ready = (
            not updated.get("disabled")
            and not updated.get("quarantined")
            and not updated.get("completed")
        )
        if now_ready:
            store = _store()
            prior = await store.get_task_state(target, task)
            if prior and prior.get("state") in ("quarantined", "blocked"):
                await store.clear_task_state(target, task)
                await _emit(
                    "task_released",
                    queue=target,
                    task=task,
                    payload={"prior_state": prior.get("state")},
                )
        await _emit("queue_changed", queue=target, task=task)
        return JSONResponse(updated)

    @app.delete("/api/tasks/{task}")
    async def remove_task(task: str, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        result = delete_task(tasks_root, task, playlists_mod.tasks_rel(target))
        commit_tasks(tasks_root, f"nightshift: delete task {task}")
        await _emit("queue_changed", queue=target, task=task)
        return JSONResponse(result)

    @app.put("/api/queue/order")
    async def put_queue_order(req: QueueOrder, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        target_rel = playlists_mod.tasks_rel(target)
        order = reorder_queue(tasks_root, req.order, target_rel)
        commit_tasks(tasks_root, f"nightshift: reorder queue {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"order": order})
        return JSONResponse({"order": order})

    @app.get("/api/queue/sort")
    def get_queue_sort(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        return JSONResponse({"sort": load_sort_mode(tasks_root, playlists_mod.tasks_rel(target))})

    @app.put("/api/queue/sort")
    async def put_queue_sort(req: QueueSort, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        target_rel = playlists_mod.tasks_rel(target)
        sort = save_sort_mode(tasks_root, req.sort, target_rel)
        commit_tasks(tasks_root, f"nightshift: set sort {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"sort": sort})
        return JSONResponse({"sort": sort})

    @app.get("/api/queue/play-priorities")
    def get_play_priorities(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        return JSONResponse(
            {"priorities": load_play_priorities(tasks_root, playlists_mod.tasks_rel(target))}
        )

    @app.put("/api/queue/play-priorities")
    async def put_play_priorities(
        req: QueuePlayPriorities, queue: str | None = None
    ) -> JSONResponse:
        target = _resolve_queue(queue)
        target_rel = playlists_mod.tasks_rel(target)
        priorities = save_play_priorities(tasks_root, req.priorities, target_rel)
        commit_tasks(tasks_root, f"nightshift: set play-priorities {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"priorities": priorities})
        return JSONResponse({"priorities": priorities})

    @app.get("/api/queue/config")
    def get_queue_config(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse({"repo": _queue_repo(target)})

    async def _set_queue_repo(target: str | None, req: QueueConfig) -> JSONResponse:
        """Persist a queue's default target repo into its ``config.json`` and
        commit the content store. A null/empty value clears the binding; a
        malformed name is rejected (the path-traversal guard — a per-task ``repo``
        override still wins at dispatch, but a bad queue default is an authoring
        error we surface here rather than at poll time)."""
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        repo_value = req.repo.strip() if isinstance(req.repo, str) else None
        repo_value = repo_value or None
        if repo_value is not None and not repos.is_valid_repo_ref(repo_value):
            return JSONResponse(
                {"error": (
                    f"invalid repo reference {repo_value!r}: a repo must be a bare "
                    "workspace child name (no paths, '..', '/', or absolute paths)"
                )},
                status_code=400,
            )
        target_rel = playlists_mod.tasks_rel(target)
        save_queue_config_value(tasks_root, "repo", repo_value, target_rel)
        commit_tasks(tasks_root, f"nightshift: set repo {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"repo": repo_value})
        return JSONResponse({"repo": repo_value})

    @app.put("/api/queue/config")
    async def put_queue_config(req: QueueConfig, queue: str | None = None) -> JSONResponse:
        return await _set_queue_repo(_resolve_queue(queue), req)

    @app.get("/api/queue/repo")
    def get_queue_repo(queue: str | None = None) -> JSONResponse:
        """The target queue's default repo (mirrors the server's dedicated
        ``/api/queue/repo`` so the shared UI has one binding endpoint)."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse({"repo": _queue_repo(target)})

    @app.put("/api/queue/repo")
    async def put_queue_repo(req: QueueConfig, queue: str | None = None) -> JSONResponse:
        return await _set_queue_repo(_resolve_queue(queue), req)

    @app.get("/api/queue/dedication")
    async def get_queue_dedication(queue: str | None = None) -> JSONResponse:
        dedication = await _store().queue_dedication()
        if queue is None:
            return JSONResponse({"dedication": dedication})
        label = queue_label(_queue_from_label(queue))
        return JSONResponse({"worker_ids": dedication.get(label, [])})

    @app.put("/api/queue/dedication")
    async def put_queue_dedication(
        req: QueueDedication, queue: str | None = None
    ) -> JSONResponse:
        target = _queue_from_label(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        label = queue_label(target)
        await _store().set_queue_dedication(label, req.worker_ids)
        await _emit(
            "queue_changed", queue=target, payload={"dedication": req.worker_ids}
        )
        return JSONResponse({"worker_ids": req.worker_ids})

    # ----- active playlist (UI focus) ----------------------------------- #

    _active_playlist: str | None = None

    @app.get("/api/active")
    def get_active() -> dict:
        return {"active_playlist": _active_playlist}

    @app.post("/api/active")
    def set_active(req: ActivePlaylistRequest) -> JSONResponse:
        nonlocal _active_playlist
        if req.playlist is not None and not playlists_mod.exists(tasks_root, req.playlist):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        _active_playlist = req.playlist
        return JSONResponse({"active_playlist": _active_playlist})

    # ----- transport (play/pause/stop/skip) -------------------------------- #

    _VALID_ACTIONS = {"play", "pause", "stop", "skip", "select"}
    _VALID_MODES = {"oneshot", "auto", "repeat"}

    _paused_queues: set[str] = set()
    _queue_modes: dict[str, str] = {}
    _queue_cursors: dict[str, str | None] = {}

    def _queue_state(queue: str | None, leases: list[dict[str, Any]]) -> dict[str, Any]:
        """Build a per-queue state dict in the shape the UI expects."""
        key = queue_label(queue)
        lease = next((le for le in leases if le.get("queue", "main") == key), None)
        paused = key in _paused_queues
        if paused:
            st = "paused"
        elif lease:
            st = "playing"
        else:
            st = "idle"
        return {
            "state": st,
            "mode": _queue_modes.get(key, "auto"),
            "now_playing": lease["task"] if lease else None,
            "cursor": _queue_cursors.get(key),
            "run_id": lease.get("run_id") if lease else None,
            "active_playlist": queue,
            "running_playlist": queue if lease else None,
        }

    async def _state_payload() -> dict[str, Any]:
        leases = await _store().active_leases()
        focused = _active_playlist
        focused_state = _queue_state(focused, leases)
        queues: dict[str, dict[str, Any]] = {}
        queues[queue_label(focused)] = focused_state
        for q in _all_queues():
            key = queue_label(q)
            if key not in queues:
                queues[key] = _queue_state(q, leases)
        return {**focused_state, "active_playlist": focused, "queues": queues}

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        return JSONResponse(await _state_payload())

    @app.post("/api/transport")
    async def post_transport(req: TransportRequest) -> JSONResponse:
        if req.action not in _VALID_ACTIONS:
            return JSONResponse(
                {"error": f"unknown action: {req.action}"}, status_code=400
            )
        if req.mode is not None and req.mode not in _VALID_MODES:
            return JSONResponse(
                {"error": f"unknown mode: {req.mode}"}, status_code=400
            )
        target = req.queue if req.queue not in (None, "") else _active_playlist
        key = queue_label(target)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        if req.mode is not None:
            _queue_modes[key] = req.mode
        if req.action == "play":
            _paused_queues.discard(key)
        elif req.action == "pause":
            _paused_queues.add(key)
        elif req.action == "stop":
            _paused_queues.add(key)
            store = _store()
            for le in await store.active_leases():
                if le.get("queue", "main") == key:
                    await store.set_lease_status(le["id"], "cancelled")
                    await _emit("run_finished", run_id=le.get("run_id"), queue=target)
            _paused_queues.discard(key)
        elif req.action == "skip":
            store = _store()
            for le in await store.active_leases():
                if le.get("queue", "main") == key:
                    await store.set_lease_status(le["id"], "cancelled")
                    await _emit("run_finished", run_id=le.get("run_id"), queue=target)
        elif req.action == "select":
            _queue_cursors[key] = req.task
        return JSONResponse(await _state_payload())

    # ----- playlists ------------------------------------------------------ #

    @app.get("/api/playlists")
    def get_playlists() -> JSONResponse:
        return JSONResponse(playlists_mod.list_playlists(tasks_root))

    @app.post("/api/playlists")
    async def post_playlist(req: PlaylistCreate) -> JSONResponse:
        try:
            created = playlists_mod.create_playlist(tasks_root, req.name)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileExistsError as exc:
            return JSONResponse(
                {"error": f"playlist already exists: {exc}"}, status_code=409
            )
        commit_tasks(tasks_root, f"nightshift: create playlist {created['name']}")
        await _emit("queue_changed", queue=created["name"])
        return JSONResponse(created, status_code=201)

    def _playlist_info(name: str) -> dict[str, Any]:
        """The playlist-info payload: its name, task count, the ``repo`` binding
        aliased to ``repository``, and the queue's ``validate`` command for the
        info page. ``validate`` is the raw stored value: ``None`` when the queue
        inherits the engine default, ``""`` when validation is explicitly
        disabled, else the custom command."""
        cfg = load_queue_config(tasks_root, playlists_mod.tasks_rel(name))
        count = len(list((tasks_root / name).glob("*.md")))
        return {
            "name": name,
            "task_count": count,
            "repository": cfg.get("repo"),
            "validate": cfg.get("validate"),
            "disabled": playlists_mod.is_disabled(tasks_root, name),
        }

    @app.get("/api/playlists/{name}")
    def get_playlist(name: str) -> JSONResponse:
        if not playlists_mod.exists(tasks_root, name):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        return JSONResponse(_playlist_info(name))

    @app.put("/api/playlists/{name}")
    async def put_playlist(name: str, req: PlaylistUpdate) -> JSONResponse:
        if not playlists_mod.exists(tasks_root, name):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        current = name
        # An active lease on this queue means a worker is mid-run against it;
        # renaming the dir + DB rows under it would strand that run.
        if req.name is not None and playlists_mod.slugify_name(req.name) != name:
            active = await _store().active_leases()
            if any(_queue_from_label(le["queue"]) == name for le in active):
                return JSONResponse(
                    {"error": "playlist has a running task; stop it first"},
                    status_code=409,
                )
            try:
                new_name = playlists_mod.rename_playlist(tasks_root, name, req.name)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            except FileExistsError as exc:
                return JSONResponse(
                    {"error": f"playlist already exists: {exc}"}, status_code=409
                )
            except FileNotFoundError:
                return JSONResponse({"error": "playlist not found"}, status_code=404)
            await _store().rename_queue(name, new_name)
            commit_tasks(tasks_root, f"nightshift: rename playlist {name} -> {new_name}")
            await _emit(
                "queue_changed",
                queue=new_name,
                payload={"renamed_from": name},
            )
            current = new_name
        # ``repository`` aliases the queue's default repo binding. A sent value
        # (incl. "" -> cleared) is normalized + persisted; an unset field is left
        # untouched (PATCH-like semantics on a PUT body of optional fields).
        if "repository" in req.model_dump(exclude_unset=True):
            try:
                repo_value = _normalize_repo(req.repository)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            save_queue_config_value(
                tasks_root, "repo", repo_value, playlists_mod.tasks_rel(current)
            )
            commit_tasks(tasks_root, f"nightshift: set repo {queue_label(current)}")
            await _emit("queue_changed", queue=current, payload={"repo": repo_value})
        # ``validate`` is the queue's validate command. A whitespace-only value
        # (or the empty-quote literals) normalizes to "" — a deliberate "disable
        # validation" signal that never falls back to the inherited default; any
        # other value is stored stripped. An unset field is left untouched.
        if "validate_cmd" in req.model_dump(exclude_unset=True):
            cmd = normalize_validate_command(str(req.validate_cmd or ""))
            save_queue_config_value(
                tasks_root, "validate", cmd, playlists_mod.tasks_rel(current)
            )
            commit_tasks(tasks_root, f"nightshift: set validate {queue_label(current)}")
            await _emit("queue_changed", queue=current, payload={"validate": cmd})
        # Disabling hides the queue and drops it from the scheduler's candidate
        # set; a no-op for an in-flight lease, which keeps draining until done.
        if req.disabled is not None:
            playlists_mod.set_playlist_disabled(tasks_root, current, req.disabled)
            verb = "disable" if req.disabled else "enable"
            commit_tasks(tasks_root, f"nightshift: {verb} playlist {current}")
            await _emit(
                "queue_changed", queue=current, payload={"disabled": req.disabled}
            )
        return JSONResponse(_playlist_info(current))

    @app.delete("/api/playlists/{name}")
    async def remove_playlist(name: str) -> JSONResponse:
        active = await _store().active_leases()
        if any(_queue_from_label(le["queue"]) == name for le in active):
            return JSONResponse(
                {"error": "playlist has a running task; stop it first"},
                status_code=409,
            )
        if not playlists_mod.delete_playlist(tasks_root, name):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        commit_tasks(tasks_root, f"nightshift: delete playlist {name}")
        await _emit("queue_changed", queue=name)
        return JSONResponse({"name": name, "deleted": True})

    @app.post("/api/playlists/rescan")
    async def rescan_playlists() -> JSONResponse:
        """Scan the workspace's immediate children for git repos and materialise
        one playlist per repo (name = repo dir name), binding each playlist's
        default repo to the discovered repo. The content-store repo is skipped.
        """
        repo_names = repos.known_repos(workspace)
        result = playlists_mod.rescan_into_playlists(
            tasks_root, repo_names, skip={tasks_repo}
        )
        if result["created"] or result["configured"]:
            commit_tasks(tasks_root, "nightshift: rescan workspace repos into playlists")
        await _emit("queue_changed", payload=result)
        return JSONResponse(
            {**result, "playlists": playlists_mod.list_playlists(tasks_root)}
        )

    # ----- repos (multi-repo workspace) ----------------------------------- #

    def _repos_payload() -> dict[str, Any]:
        """The known-repos set, per-queue repo bindings, and warnings.

        The known set is the workspace's direct children with ``.git``; per-queue
        repo comes from each queue's ``config.json``. A queue whose configured
        repo is set but absent surfaces a single warning (matching the
        one-warning-per-queue pause rule)."""
        known = repos.known_repos(workspace)
        queues_payload: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        for q in _all_queues():
            label = queue_label(q)
            repo = _queue_repo(q)
            available = bool(repo) and repos.repo_available(workspace, repo)
            queues_payload.append({"queue": label, "repo": repo, "available": available})
            if repo and not available:
                warnings.append({"queue": label, "repo": repo})
        return {
            "workspace": str(workspace),
            "tasks_repo": tasks_repo,
            "repos": [{"name": name, "available": True} for name in known],
            "queues": queues_payload,
            "warnings": warnings,
        }

    @app.get("/api/repos")
    def get_repos() -> JSONResponse:
        return JSONResponse(_repos_payload())

    @app.post("/api/repos/rescan")
    async def rescan_repos() -> JSONResponse:
        """Recompute the known-repos set and auto-resume any paused
        (``repo_unavailable``) task whose repo is now present, then re-warn from
        scratch on the next poll."""
        store = _store()
        resumed: list[dict[str, Any]] = []
        for row in await store.tasks_in_state("repo_unavailable"):
            repo = row.get("repo")
            if repo and repos.repo_available(workspace, repo):
                queue = _queue_from_label(row.get("queue"))
                await store.clear_task_state(queue, row["task"])
                resumed.append({"queue": queue_label(queue), "task": row["task"]})
                await _emit("queue_changed", queue=queue, task=row["task"])
        # Reset the per-queue warning dedupe so a still-missing repo re-warns.
        app.state.repo_warnings = set()
        await _emit("repos_changed", payload={"resumed": resumed})
        return JSONResponse(_repos_payload())

    @app.get("/api/runs")
    async def get_runs(queue: str | None = None, limit: int = 200) -> JSONResponse:
        target = _queue_from_label(queue) if queue is not None else None
        runs = await _store().list_runs(limit=limit, queue=target if queue is not None else None)
        return JSONResponse([_jsonable(r) for r in runs])

    @app.get("/api/runs/{run_id}/events")
    async def get_run_events(run_id: str) -> JSONResponse:
        return JSONResponse([_jsonable(e) for e in await _store().run_events(run_id)])

    @app.get("/api/workers")
    async def get_workers() -> JSONResponse:
        return JSONResponse([_jsonable(w) for w in await _registry().snapshot()])

    @app.get("/api/models")
    async def get_models(queue: str | None = None) -> JSONResponse:
        """Models advertised by live workers that can serve the given queue.

        Used by the task detail dropdown so the operator sees only models
        actually routable to this queue's workers."""
        label = queue_label(_resolve_queue(queue) if queue else None)
        models = await _registry().models_for_queue(label)
        return JSONResponse({"models": models})

    @app.get("/api/stats")
    async def get_stats() -> JSONResponse:
        store = _store()
        return JSONResponse(
            {
                "overall": _jsonable(await store.stats_overall()),
                "by_worker": [_jsonable(r) for r in await store.stats_by_worker()],
                "by_backend": [_jsonable(r) for r in await store.stats_by_backend()],
                "by_model": [_jsonable(r) for r in await store.stats_by_model()],
                "by_queue": [_jsonable(r) for r in await store.stats_by_queue()],
            }
        )

    @app.get("/api/leases")
    async def get_leases() -> JSONResponse:
        return JSONResponse([_jsonable(le) for le in await _store().active_leases()])

    @app.get("/api/blocked")
    async def get_blocked() -> JSONResponse:
        store = _store()
        # Quarantined tasks are surfaced alongside blocked ones so the operator
        # sees them in the same "needs attention" surface; each carries its own
        # ``blocked_reason`` (the quarantine explanation) and a ``state`` tag.
        rows = [*await store.list_blocked(), *await store.tasks_in_state("quarantined")]
        return JSONResponse([_jsonable(b) for b in rows])

    @app.get("/api/runs/{run_id}/{task}/log")
    async def get_run_log(
        run_id: str, task: str, offset: int = 0, queue: str | None = None
    ) -> JSONResponse:
        """Reconstruct a run's log from its persisted ``task_log`` events.

        The worker streams stdout to the manager as ``task_log`` events, which
        are stored durably (Postgres ``nightshift.events``). This re-assembles
        them into the plain-text payload the shared UI's log panel expects, so a
        finished run's output is viewable after the fact (parity with the
        single-process server's on-disk ``/log`` endpoint)."""
        events = await _store().run_events(run_id)
        text = "".join(
            str((ev.get("payload") or {}).get("line", ""))
            for ev in events
            if ev.get("kind") == "task_log"
        )
        return JSONResponse(
            {"text": text[offset:], "offset": len(text), "eof": True}
        )

    _MANAGER_SURFACES = ["manager", "player"]

    @app.get("/api/settings")
    def get_settings() -> JSONResponse:
        response = build_get_response(workspace, _MANAGER_SURFACES)
        response["tiers"].insert(0, _workspace_tier(workspace))
        return JSONResponse(response)

    @app.put("/api/settings")
    async def put_settings(body: dict[str, Any]) -> JSONResponse:
        allowed = set(_MANAGER_SURFACES)
        resolved, errors = validate_delta(body, allowed)
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=400)

        applied_live, restart_required = write_delta(workspace, resolved)
        response = build_get_response(workspace, _MANAGER_SURFACES)
        await _emit("settings_changed", payload={})

        return JSONResponse({
            "ok": True,
            "applied_live": applied_live,
            "restart_required": restart_required,
            **response,
        })

    # ----- SSE ------------------------------------------------------------- #

    async def _snapshot() -> dict[str, Any]:
        store = _store()
        return {
            "cursor": await store.max_event_id(),
            "workers": [_jsonable(w) for w in await store.list_workers()],
            "leases": [_jsonable(le) for le in await store.active_leases()],
            "runs": [_jsonable(r) for r in await store.list_runs(limit=50)],
            "blocked": [
                _jsonable(b)
                for b in (
                    *await store.list_blocked(),
                    *await store.tasks_in_state("quarantined"),
                )
            ],
        }

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        server = getattr(request.app.state, "uvicorn_server", None)

        def is_shutting_down() -> bool:
            # End the stream once the server starts shutting down (Ctrl-C) so this
            # long-lived connection doesn't block graceful shutdown. None under
            # the test client / uvicorn.run (no handle) → never shutting down.
            return server is not None and bool(server.should_exit)

        async def gen():
            async for frame in app.state.hub.stream(
                _snapshot,
                heartbeat_seconds=cfg.cadences.heartbeat_seconds,
                is_shutting_down=is_shutting_down,
            ):
                if await request.is_disconnected():
                    break
                yield frame

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ----- branding -------------------------------------------------------- #

    @app.get("/api/info")
    def info() -> JSONResponse:
        # The operator UI is shared with the single-process server; this lets the
        # frontend retitle itself "Nightshift Manager" when served by the manager
        # (the single-process server has no /api/info, so it keeps "Nightshift").
        return JSONResponse({"brand_name": "Nightshift Manager"})

    # ----- static UI ------------------------------------------------------- #

    if UI_DIR.exists():
        app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")

    return app


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _jsonable(row: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce datetimes/UUIDs/Decimals to JSON-safe values.

    Postgres hands ``numeric`` columns (cost_usd, avg_turns, …) back as
    ``Decimal``, which ``json.dumps`` can't serialize — coerce those to float so
    the stats/runs endpoints don't 500 under the PgStore.
    """
    if row is None:
        return {}
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def no_progress_streak(runs: list[dict[str, Any]], task: str) -> int:
    """Most-recent consecutive runs of ``task`` that made no progress.

    ``runs`` is in the order :meth:`NightshiftStore.list_runs` returns them
    (newest first). The scan stops at the first run that *landed* (a completed
    run carrying a ``commit_sha``), so real progress resets the count. A
    completed run with no commit ("worker emitted output only") or a worker
    ``error`` is a no-progress run and increments the streak. Operator-driven
    outcomes (``aborted``/``skipped``) and explicit holds (``blocked``) are
    neutral — they neither count nor reset — so a manual stop in the middle of a
    loop neither masks nor amplifies it. Pure given its inputs (unit-testable
    without a store).
    """
    streak = 0
    for run in runs:
        if run.get("task") != task:
            continue
        status = run.get("status")
        if status == "completed" and run.get("commit_sha"):
            break
        if status == "error" or (status == "completed" and not run.get("commit_sha")):
            streak += 1
    return streak


def _task_meta(tasks_root: Path, task: str, queue: str | None) -> dict[str, Any]:
    path = tasks_root / playlists_mod.tasks_rel(queue) / f"{task}.md"
    try:
        return split_frontmatter(path.read_text(errors="replace"))[0]
    except OSError:
        return {}


def _build_work_order(
    workspace: Path,
    tasks_root: Path,
    task: str,
    queue: str | None,
    repo: str,
    lease_id: str,
    run_id: str,
    base_ref: str | None,
    cfg: ManagerConfig,
) -> dict[str, Any]:
    """Assemble the JSON work order handed to a worker.

    The brief is read from the content store (``tasks_root``) and its body is
    embedded (frontmatter stripped) so the brief never enters the target repo.
    Every path is **workspace-relative**: ``repo`` is a bare child name and
    ``task_path`` is ``<tasks_repo>/<queue>/<task>.md``. ``base_ref`` is the
    target repo's canonical HEAD. Landing policy (landing/automerge/draft) and
    backend choice are intentionally *not* included — landing is manager-side,
    backend is worker-owned.
    """
    tasks_rel = playlists_mod.tasks_rel(queue)
    tasks_repo = tasks_root.name
    path = tasks_root / tasks_rel / f"{task}.md"
    text = path.read_text(errors="replace") if path.exists() else ""
    meta, body = split_frontmatter(text)
    merged = resolve_config(workspace, tasks_root, tasks_rel)

    model = meta.get("model") or cfg.default_model
    raw_turns = meta.get("turns", merged.get("max_turns"))
    validate_argv = resolve_validate_cmd(merged)
    preflight_argv = resolve_preflight_cmd(merged)
    config_blob = {
        "model": str(model).strip() or cfg.default_model,
        "validate": merged.get("validate"),
        "validate_cmd": format_validate_cmd(validate_argv),
        # Environment preflight (default `uv sync --frozen`); empty string in a
        # queue's config opts out. Formatted like validate_cmd for the worker.
        "preflight": merged.get("preflight"),
        "preflight_cmd": format_validate_cmd(preflight_argv),
        "diff_cap_lines": merged.get("diff_cap_lines"),
        "forbidden_paths": merged.get("forbidden_paths"),
        "max_turns": int(raw_turns) if raw_turns is not None else None,
        # MCP connectors the brief declares (informational for the worker; the
        # manager already routed to a worker that advertises this superset).
        "required_mcps": list(parse_required_mcps(meta)),
        # WIP namespace the worker publishes its cross-machine branch under. The
        # worker never reads the centralized config, so the manager hands it the
        # operator-configured prefix here (co-located workers ignore it).
        "wip_ref_prefix": cfg.wip_ref_prefix,
        # Ralph-loop mode: when true, the worker uses the iterative ralph-loop
        # prompt instead of the standard single-pass nightshift-local prompt.
        "loop": bool(meta.get("loop", False)),
        "loop_max_iterations": int(meta.get("loop_max_iterations", 0)),
        # Split (decomposition) mode: the worker writes subtask briefs into a
        # dedicated split output directory instead of implementing directly.
        "split": bool(meta.get("split", False)),
    }
    return {
        "lease_id": lease_id,
        "run_id": run_id,
        "task": task,
        "queue": queue_label(queue),
        "priority": int(meta.get("priority", 5)) if str(meta.get("priority", "")).strip() != "" else 5,
        "title": resolve_title(task, meta),
        "body": body.strip(),
        "repo": repo,
        "task_path": f"{tasks_repo}/{tasks_rel}/{task}.md",
        "base_ref": base_ref,
        "config": config_blob,
    }
