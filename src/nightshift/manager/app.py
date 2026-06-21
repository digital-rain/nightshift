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

import contextlib
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nightshift import playlists as playlists_mod
from nightshift import repos
from nightshift._paths import UI_DIR
from nightshift.engine import (
    WIP_REF_PREFIX,
    commit_tasks,
    compute_code_loc,
    create_task,
    delete_task,
    drop_completed_task,
    list_queue,
    load_play_priorities,
    load_sort_mode,
    normalize_wip_prefix,
    read_task,
    reorder_queue,
    resolve_title,
    save_play_priorities,
    save_queue_config_value,
    save_sort_mode,
    set_task_meta,
    sync_main_to_origin,
    task_is_evergreen,
)
from nightshift.events import new_run_id
from nightshift.manager.config import ManagerConfig, load_manager_config
from nightshift.manager.hub import Hub
from nightshift.manager.landing import canonical_head, land
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
from nightshift.server import settings as settings_mod
from nightshift.spawn_daily import (
    load_config,
    load_queue_config,
    resolve_config,
    save_config_value,
    split_frontmatter,
)


# UI assets ship inside the package (see nightshift._paths.UI_DIR).


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class CheckinBody(BaseModel):
    worker_id: str
    backend: str
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
    backend: str
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


class TaskCreate(BaseModel):
    title: str
    text: str
    # Optional per-task repo override (defaults to the queue's repo). Written as
    # an editable frontmatter meta key on the new brief.
    repo: str | None = None


class TaskUpdate(BaseModel):
    # Partial edit; unset fields are left untouched. ``repo`` is an editable
    # frontmatter meta key (the per-task target-repo override).
    repo: str | None = None


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


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #


def create_app(workspace: Path, *, store: NightshiftStore | None = None) -> FastAPI:
    workspace = Path(workspace).resolve()
    cfg: ManagerConfig = load_manager_config(workspace)
    # The two roots: briefs/queue config live in the content store
    # (``tasks_root``); git ops resolve a target repo per task under the
    # workspace. ``tasks_repo`` is the content-store repo's bare child name.
    tasks_repo = cfg.tasks_repo
    tasks_root = workspace / tasks_repo

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI):
        if app.state.store is None:
            app.state.store = await open_store(cfg.dsn)
        app.state.registry = Registry(
            app.state.store, stale_seconds=cfg.cadences.worker_stale_seconds
        )
        yield
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

    def _queue_exists(queue: str | None) -> bool:
        return queue is None or playlists_mod.exists(tasks_root, queue)

    def _all_queues() -> list[str | None]:
        return [None, *[p["name"] for p in playlists_mod.list_playlists(tasks_root)]]

    def _queue_repo(queue: str | None) -> str | None:
        """The queue's configured default target repo (or ``None`` when unset)."""
        return load_queue_config(
            tasks_root, playlists_mod.tasks_rel(queue)
        ).get("repo")

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
        candidates_by_queue = {
            q: build_candidates(tasks_root, q, default_model=cfg.default_model)
            for q in _all_queues()
        }

        # Manager-side queue dedication (queue label -> bound worker ids).
        dedication = await store.queue_dedication()

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
        # Never dispatch a paused (repo_unavailable) or repo-blocked task.
        blocked |= repo_excluded

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
        # PR mode is origin/main-authoritative: resync local main to origin/main
        # before pinning base_ref, so an orphaned ephemeral pr-mode squash is
        # dropped and cross-PR divergence cannot accumulate (see remote-landing.md).
        poll_meta = _task_meta(tasks_root, chosen.task, chosen.queue)
        if (
            ("pr" if poll_meta.get("make_pr") else cfg.landing_mode) == "pr"
            and cfg.rendezvous_remote
        ):
            sync_main_to_origin(workspace, repo, cfg.rendezvous_remote)
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
        await store.create_run(
            run_id,
            task=chosen.task,
            queue=chosen.queue,
            worker_id=body.worker_id,
            backend=body.backend,
            model=order["config"]["model"],
            title=order["title"],
            body=order["body"],
            required_mcps=list(chosen.required_mcps),
            repo=repo,
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
            await _registry().set_idle(body.worker_id)
            await _emit(
                "task_result",
                run_id=run_id,
                queue=queue,
                task=body.task,
                payload={"status": body.status, "result_line": body.result_line},
            )
            return JSONResponse({"landed": False, "status": body.status})

        # Completed but nothing to land (no commit): record success, no git.
        if not body.landable:
            await store.update_run(
                run_id, status="completed",
                result_line=body.result_line or "no changes", model=body.model,
                **telemetry,
            )
            await store.set_lease_status(body.lease_id, "released")
            await store.clear_task_state(queue, body.task)
            await _registry().set_idle(body.worker_id)
            await _emit(
                "task_result", run_id=run_id, queue=queue, task=body.task,
                payload={"status": "completed", "result_line": body.result_line or "no changes"},
            )
            await _emit("queue_changed", queue=queue)
            return JSONResponse({"landed": False, "status": "completed", "no_changes": True})

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
            if result.conflict:
                # Hand the operator/worker a resolve work-order signal: mark the
                # task so the conflict is visible and the branch (preserved by
                # squash_to_main) can be resolved.
                await store.set_task_state(
                    queue, body.task, "blocked",
                    blocked_reason="needs resolve: " + (result.detail.splitlines()[0] if result.detail else "merge conflict"),
                )
                await _emit(
                    "task_blocked", queue=queue, task=body.task,
                    payload={"reason": "merge_conflict", "detail": result.detail},
                )
            await _emit(
                "task_result", run_id=run_id, queue=queue, task=body.task,
                payload={"status": status, "failure_kind": failure_kind},
            )
            return JSONResponse(
                {"landed": False, "conflict": result.conflict, "detail": result.detail}
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
                "pr_url": result.pr_url,
            },
        )
        await _emit("queue_changed", queue=queue)
        return JSONResponse(
            {
                "landed": True,
                "sha": result.sha,
                "remote": result.remote,
                "pr_url": result.pr_url,
            }
        )

    # ===================================================================== #
    # Operator API
    # ===================================================================== #

    @app.get("/api/queue")
    def get_queue(queue: str | None = None) -> JSONResponse:
        target = _queue_from_label(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse(list_queue(tasks_root, playlists_mod.tasks_rel(target)))

    @app.get("/api/tasks/{task}")
    def get_task(task: str, queue: str | None = None) -> JSONResponse:
        target = _queue_from_label(queue)
        try:
            return JSONResponse(read_task(tasks_root, task, playlists_mod.tasks_rel(target)))
        except FileNotFoundError:
            return JSONResponse({"error": "task not found"}, status_code=404)

    @app.post("/api/tasks")
    async def post_task(body: TaskCreate, queue: str | None = None) -> JSONResponse:
        target = _queue_from_label(queue)
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
        # Optional per-task repo override is written as an editable meta key.
        if repo_override is not None:
            with contextlib.suppress(FileNotFoundError, ValueError):
                set_task_meta(
                    tasks_root, created["task"], {"repo": repo_override}, target_rel
                )
        commit_tasks(tasks_root, f"nightshift: create task {created['task']}")
        await _emit("queue_changed", queue=target, task=created.get("task"))
        return JSONResponse(created)

    @app.patch("/api/tasks/{task}")
    async def patch_task(
        task: str, body: TaskUpdate, queue: str | None = None
    ) -> JSONResponse:
        target = _queue_from_label(queue)
        target_rel = playlists_mod.tasks_rel(target)
        # ``repo`` is the per-task target-repo override (an editable meta key).
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            return JSONResponse({"error": "no fields to update"}, status_code=400)
        # A malformed override is a 400 here (edit-time guard); a sent ``null``
        # clears it so the task falls back to the queue default.
        if "repo" in changes:
            try:
                changes["repo"] = _normalize_repo(changes["repo"])
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            updated = set_task_meta(tasks_root, task, changes, target_rel)
        except FileNotFoundError:
            return JSONResponse({"error": "task not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        commit_tasks(tasks_root, f"nightshift: edit task {task}")
        await _emit("queue_changed", queue=target, task=task)
        return JSONResponse(updated)

    @app.delete("/api/tasks/{task}")
    async def remove_task(task: str, queue: str | None = None) -> JSONResponse:
        target = _queue_from_label(queue)
        result = delete_task(tasks_root, task, playlists_mod.tasks_rel(target))
        commit_tasks(tasks_root, f"nightshift: delete task {task}")
        await _emit("queue_changed", queue=target, task=task)
        return JSONResponse(result)

    @app.put("/api/queue/order")
    async def put_queue_order(req: QueueOrder, queue: str | None = None) -> JSONResponse:
        target = _queue_from_label(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        target_rel = playlists_mod.tasks_rel(target)
        order = reorder_queue(tasks_root, req.order, target_rel)
        commit_tasks(tasks_root, f"nightshift: reorder queue {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"order": order})
        return JSONResponse({"order": order})

    @app.get("/api/queue/sort")
    def get_queue_sort(queue: str | None = None) -> JSONResponse:
        target = _queue_from_label(queue)
        return JSONResponse({"sort": load_sort_mode(tasks_root, playlists_mod.tasks_rel(target))})

    @app.put("/api/queue/sort")
    async def put_queue_sort(req: QueueSort, queue: str | None = None) -> JSONResponse:
        target = _queue_from_label(queue)
        target_rel = playlists_mod.tasks_rel(target)
        sort = save_sort_mode(tasks_root, req.sort, target_rel)
        commit_tasks(tasks_root, f"nightshift: set sort {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"sort": sort})
        return JSONResponse({"sort": sort})

    @app.get("/api/queue/play-priorities")
    def get_play_priorities(queue: str | None = None) -> JSONResponse:
        target = _queue_from_label(queue)
        return JSONResponse(
            {"priorities": load_play_priorities(tasks_root, playlists_mod.tasks_rel(target))}
        )

    @app.put("/api/queue/play-priorities")
    async def put_play_priorities(
        req: QueuePlayPriorities, queue: str | None = None
    ) -> JSONResponse:
        target = _queue_from_label(queue)
        target_rel = playlists_mod.tasks_rel(target)
        priorities = save_play_priorities(tasks_root, req.priorities, target_rel)
        commit_tasks(tasks_root, f"nightshift: set play-priorities {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"priorities": priorities})
        return JSONResponse({"priorities": priorities})

    @app.get("/api/queue/config")
    def get_queue_config(queue: str | None = None) -> JSONResponse:
        target = _queue_from_label(queue)
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
        return await _set_queue_repo(_queue_from_label(queue), req)

    @app.get("/api/queue/repo")
    def get_queue_repo(queue: str | None = None) -> JSONResponse:
        """The target queue's default repo (mirrors the server's dedicated
        ``/api/queue/repo`` so the shared UI has one binding endpoint)."""
        target = _queue_from_label(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse({"repo": _queue_repo(target)})

    @app.put("/api/queue/repo")
    async def put_queue_repo(req: QueueConfig, queue: str | None = None) -> JSONResponse:
        return await _set_queue_repo(_queue_from_label(queue), req)

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

    @app.get("/api/playlists")
    def get_playlists() -> JSONResponse:
        return JSONResponse(playlists_mod.list_playlists(tasks_root))

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
        return JSONResponse([_jsonable(b) for b in await _store().list_blocked()])

    # The WIP-namespace prefix is a global, launch-time knob persisted in
    # ``<workspace>/config.json`` (not a player setting), surfaced here so the
    # operator edits it from the same Settings UI. It is read into ``cfg`` at
    # launch and baked into work orders, so a change applies on the next restart.
    _WIP_PREFIX_FIELD = {
        "key": "wip_ref_prefix",
        "label": "Branch prefix",
        "description": (
            "Namespace a cross-machine worker publishes its validated branch "
            "under (refs/heads/<prefix>/<queue>/<task>). Scope worker push "
            "credentials to '<prefix>/*'. Saved to config.json; applies on the "
            "next manager restart."
        ),
        "type": "string",
        "default": WIP_REF_PREFIX,
    }

    @app.get("/api/settings")
    def get_settings() -> JSONResponse:
        settings = {
            **settings_mod.load_settings(workspace),
            "wip_ref_prefix": cfg.wip_ref_prefix,
        }
        return JSONResponse(
            {
                "settings": settings,
                "schema": [*settings_mod.SCHEMA, _WIP_PREFIX_FIELD],
                "cadences": {
                    "poll_seconds": cfg.cadences.poll_seconds,
                    "heartbeat_seconds": cfg.cadences.heartbeat_seconds,
                    "refresh_ms": cfg.cadences.refresh_ms,
                },
                "landing_mode": cfg.landing_mode,
                "default_model": cfg.default_model,
            }
        )

    @app.put("/api/settings")
    async def put_settings(body: dict[str, Any]) -> JSONResponse:
        # ``wip_ref_prefix`` is a config.json knob, not a player setting: pull it
        # out, validate strictly (a 400 at edit time, writing nothing), and
        # persist it to config.json. The running manager keeps its launch-time
        # value until restart.
        body = dict(body)
        wip_prefix = body.pop("wip_ref_prefix", None)
        if wip_prefix is not None:
            try:
                save_config_value(
                    workspace, "wip_ref_prefix", normalize_wip_prefix(wip_prefix)
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            merged = settings_mod.save_settings(workspace, body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        merged["wip_ref_prefix"] = str(
            load_config(workspace).get("wip_ref_prefix", cfg.wip_ref_prefix)
        )
        await _emit("settings_changed", payload={"settings": merged})
        return JSONResponse({"ok": True, "settings": merged})

    # ----- SSE ------------------------------------------------------------- #

    async def _snapshot() -> dict[str, Any]:
        store = _store()
        return {
            "cursor": await store.max_event_id(),
            "workers": [_jsonable(w) for w in await store.list_workers()],
            "leases": [_jsonable(le) for le in await store.active_leases()],
            "runs": [_jsonable(r) for r in await store.list_runs(limit=50)],
            "blocked": [_jsonable(b) for b in await store.list_blocked()],
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
    config_blob = {
        "model": str(model).strip() or cfg.default_model,
        "validate": merged.get("validate"),
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
