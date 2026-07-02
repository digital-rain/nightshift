"""Worker-facing manager API — checkin / poll / heartbeat / run events /
submit / resolve-result — plus the work-order assembly helpers.

Split out of ``manager/app.py`` in Phase 3 of the rebuild-in-place migration;
handler logic is unchanged. Endpoints are registered onto the shared FastAPI
app by :func:`register_worker_api`; the app wiring (store, registry, event
emitter, shared queue state) is injected by ``create_app``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nightshift import playlists as playlists_mod
from nightshift import repos
from nightshift.events import new_run_id
from nightshift.git.squash import compute_code_loc
from nightshift.git.store import commit_tasks
from nightshift.git.sync import maybe_sync_main_to_origin
from nightshift.git.worktrees import has_commits
from nightshift.lifecycle import (
    RUN_RESOLVABLE_STATUSES,
    FailureKind,
    LandingMode,
    LeaseStatus,
    Outcome,
    RunStatus,
    TaskHoldKind,
)
from nightshift.manager import failure_policy
from nightshift.manager.config import ManagerConfig
from nightshift.manager.landing import canonical_head, land, main_advanced_sha
from nightshift.manager.registry import Registry
from nightshift.manager.scheduler import (
    WorkerFilter,
    build_candidates,
    parse_required_mcps,
    pick_next,
    queue_label,
    unroutable,
)
from nightshift.manager.store import NightshiftStore
from nightshift.model_id import provider_of
from nightshift.preflight import resolve_preflight_cmd
from nightshift.queue_config import format_validate_cmd, resolve_validate_cmd
from nightshift.spawn_daily import (
    is_failed,
    load_queue_config,
    resolve_config,
    split_frontmatter,
)
from nightshift.task_files import (
    drop_completed_task,
    failed_tasks,
    frontmatter_held_tasks,
    harvest_split_output,
    resolve_title,
    set_task_meta,
    task_is_evergreen,
)


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
    exclude_queues: list[str] | None = None


class HeartbeatBody(BaseModel):
    worker_id: str
    lease_id: str | None = None
    phase: str | None = None


class RunEventsBody(BaseModel):
    events: list[dict[str, Any]]


class SubmitBody(Outcome):
    """The worker's submit body: the unified :class:`Outcome` embedded flat
    (same wire keys as ever) plus the lease/task envelope."""

    worker_id: str
    lease_id: str
    task: str
    queue: str | None = None
    title: str
    # Wire-compat defaults kept from the pre-Outcome SubmitBody: a bare submit
    # is a completed, landable run with an optional backend.
    status: RunStatus = RunStatus.COMPLETED
    landable: bool = True
    backend: str | None = None  # type: ignore[assignment]
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


class EmitFn(Protocol):
    """``create_app``'s event emitter: persist a state-change event and fan it
    out to every connected browser."""

    def __call__(
        self,
        kind: str,
        *,
        run_id: str | None = None,
        queue: str | None = None,
        task: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Awaitable[None]: ...


class StartResolveFn(Protocol):
    """``create_app``'s resolve spawner: create a resolve run + launch the
    out-of-process resolver, returning ``(started, child_run_id, error)``."""

    def __call__(
        self,
        origin_run_id: str,
        *,
        task: str,
        queue: str | None,
        repo: str,
        title: str,
    ) -> Awaitable[tuple[bool, str | None, str | None]]: ...


def register_worker_api(
    app: FastAPI,
    *,
    cfg: ManagerConfig,
    workspace: Path,
    tasks_root: Path,
    _store: Callable[[], NightshiftStore],
    _registry: Callable[[], Registry],
    _emit: EmitFn,
    _queue_from_label: Callable[[str | None], str | None],
    _all_queues: Callable[[], list[str | None]],
    # Shared mutable pause state, owned by create_app: queue label -> pause
    # reason ("operator" | "consecutive_failures" | "retry_failed").
    _paused_queues: dict[str, str],
    _failure_state: Callable[[str], failure_policy.QueueFailureState],
    _start_resolve: StartResolveFn,
) -> None:
    """Register the worker endpoints. Shared wiring (store/registry accessors,
    the event emitter, queue-pause state, and the resolve spawner) is injected
    by ``create_app`` under the same names the handler bodies always used."""
    def _set_frontmatter_flag(
        queue: str | None, task: str, key: str, value: bool,
        *, reason_key: str | None = None, reason: str | None = None,
    ) -> None:
        """Write a boolean frontmatter field (quarantined/failed) and an
        optional companion reason field directly to the task's .md file.

        This is the single writer for quarantine/failed state — the .md file
        is the source of truth, not the DB overlay.
        """
        changes: dict[str, object | None] = {key: value}
        if reason_key:
            changes[reason_key] = reason if value else None
        tasks_rel = playlists_mod.tasks_rel(queue)
        set_task_meta(tasks_root, task, changes, tasks_rel)
        commit_tasks(tasks_root, f"nightshift: {key} {task}")

    def _task_is_failed_in_frontmatter(queue: str | None, task: str) -> bool:
        """Check if a task is currently marked ``failed: true`` in frontmatter."""
        tasks_rel = playlists_mod.tasks_rel(queue)
        task_path = tasks_root / tasks_rel / f"{task}.md"
        if not task_path.exists():
            return False
        text = task_path.read_text(errors="replace")
        if not text.startswith("---"):
            return False
        meta = split_frontmatter(text)[0]
        return is_failed(meta)

    async def _quarantine_if_looping(
        queue: str | None, task: str, run_id: str, detail: str
    ) -> bool:
        """Quarantine a task that is stuck re-executing without progress.

        Counts the most recent consecutive runs of ``task`` that landed nothing
        (see :func:`no_progress_streak`); once that streak reaches the configured
        ``quarantine_threshold`` the task is pinned to the ``quarantined`` state
        **in frontmatter** (the single source of truth for quarantine).
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
        _set_frontmatter_flag(
            queue, task, "quarantined", True,
            reason_key="quarantine_reason", reason=reason,
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
        """Quarantine a task on the first failure (worker quarantine mode)."""
        reason = (
            f"quarantined by worker on first failure ({detail}); "
            f"review the run logs and edit or delete the task to release it"
        )
        _set_frontmatter_flag(
            queue, task, "quarantined", True,
            reason_key="quarantine_reason", reason=reason,
        )
        await _emit(
            "task_quarantined",
            run_id=run_id,
            queue=queue,
            task=task,
            payload={"reason": reason, "streak": 1},
        )
        return True

    async def _quarantine_retry_failure(
        queue: str | None, task: str, run_id: str, detail: str
    ) -> None:
        """Phase B: the retried task failed again. Quarantine it (frontmatter)
        and pause the queue."""
        reason = (
            f"quarantined after failing again on retry ({detail}); "
            f"review the run logs and edit or delete the task to release it"
        )
        _set_frontmatter_flag(
            queue, task, "quarantined", True,
            reason_key="quarantine_reason", reason=reason,
        )
        await _emit(
            "task_quarantined",
            run_id=run_id,
            queue=queue,
            task=task,
            payload={"reason": reason, "streak": 2},
        )
        label = queue_label(queue)
        _paused_queues[label] = "retry_failed"
        await _emit(
            "queue_paused", queue=queue, payload={"reason": "retry_failed", "task": task},
        )

    async def _record_failure_outcome(
        queue: str | None, task: str, *, state: TaskHoldKind,
        blocked_reason: str, retry_eligible: bool
    ) -> None:
        """Mark a task failed (frontmatter) or blocked (DB overlay) and apply
        the phase-A two-in-a-row pause."""
        if state is TaskHoldKind.FAILED:
            _set_frontmatter_flag(
                queue, task, "failed", True,
                reason_key="failed_reason", reason=blocked_reason,
            )
        else:
            await _store().set_task_state(
                queue, task, state, blocked_reason=blocked_reason,
                retry_eligible=retry_eligible,
            )
        label = queue_label(queue)
        should_pause = failure_policy.record_outcome(_failure_state(label), is_failure=True)
        if should_pause and label not in _paused_queues:
            _paused_queues[label] = "consecutive_failures"
            await _emit(
                "queue_paused", queue=queue,
                payload={"reason": "consecutive_failures", "task": task},
            )

    def _record_success_outcome(queue: str | None) -> None:
        failure_policy.record_outcome(_failure_state(queue_label(queue)), is_failure=False)

    # ----- worker auth (optional shared secret) ---------------------------- #

    def _require_secret(x_nightshift_secret: str | None = Header(default=None)) -> None:
        if cfg.shared_secret and x_nightshift_secret != cfg.shared_secret:
            raise HTTPException(status_code=401, detail="bad or missing worker secret")

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
                "worker": jsonable(worker),
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
        # Queues paused via the transport controls or locally backed-off by this
        # worker are excluded from dispatch.
        exclude = set(body.exclude_queues or [])
        candidates_by_queue = {
            q: build_candidates(tasks_root, q, default_model=cfg.default_model)
            for q in _all_queues()
            if queue_label(q) not in _paused_queues and queue_label(q) not in exclude
        }

        # Manager-side queue dedication (queue label -> bound worker ids).
        dedication = await store.queue_dedication()

        # Quarantined/failed tasks are sourced from frontmatter (the single
        # source of truth). live_ordered_queue already skips them so they
        # won't appear in candidates, but we still need the sets for the
        # unroutable / repo-check guards below and for Phase B retry.
        quarantined: set[tuple[str | None, str]] = set()
        failed: set[tuple[str | None, str]] = set()
        for q in _all_queues():
            tasks_rel = playlists_mod.tasks_rel(q)
            for row in frontmatter_held_tasks(tasks_root, tasks_rel):
                key = (_queue_from_label(row["queue"]), row["task"])
                if row["state"] == TaskHoldKind.QUARANTINED:
                    quarantined.add(key)
                elif row["state"] == TaskHoldKind.FAILED:
                    failed.add(key)

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
            if not existing or existing.get("state") != TaskHoldKind.BLOCKED:
                await store.set_task_state(
                    cand.queue, cand.task, TaskHoldKind.BLOCKED, blocked_reason=reason
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
                    if not existing or existing.get("state") != TaskHoldKind.BLOCKED:
                        await store.set_task_state(
                            cand.queue, cand.task, TaskHoldKind.BLOCKED,
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
                    if not existing or existing.get("state") != TaskHoldKind.REPO_UNAVAILABLE:
                        await store.set_task_state(
                            cand.queue, cand.task, TaskHoldKind.REPO_UNAVAILABLE,
                            repo=cand.repo,
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
        # Never dispatch a paused (repo_unavailable), repo-blocked,
        # quarantined (re-execution loop), or failed task.
        blocked |= repo_excluded
        blocked |= quarantined
        blocked |= failed

        # Phase B: once a queue has no active leases and no ready (non-failed)
        # candidate left, let its earliest failed/blocked-retryable task back
        # into dispatch -- one at a time, never two failed tasks concurrently.
        # Failed tasks come from frontmatter; blocked-retryable come from DB.
        for q in list(candidates_by_queue):
            label = queue_label(q)
            if label in _paused_queues:
                continue
            if any(queue_label(le.get("queue")) == label for le in active):
                continue
            cands = candidates_by_queue[q]
            ready_exists = any((c.queue, c.task) not in blocked for c in cands)
            if ready_exists:
                continue
            tasks_rel = playlists_mod.tasks_rel(q)
            fm_failed = failed_tasks(tasks_root, tasks_rel)
            db_retryable = await store.retryable_tasks(q)
            retryable = [*fm_failed, *db_retryable]
            if not retryable:
                continue
            order_list = load_queue_config(tasks_root, tasks_rel).get("order") or []
            pick = failure_policy.pick_retry(retryable, order=order_list)
            if pick is not None:
                blocked.discard((q, pick))
                failed.discard((q, pick))

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
            return JSONResponse({"work": None, "queue_pauses": dict(_paused_queues)}, status_code=200)

        # The chosen candidate is repo-available by construction; clear any prior
        # paused/blocked overlay it may carry (e.g. a now-resolved repo) so the
        # dispatch is clean, and pin the target repo's HEAD as base_ref.
        repo = chosen.repo
        prior = await store.get_task_state(chosen.queue, chosen.task)
        if prior and prior.get("state") in (
            TaskHoldKind.REPO_UNAVAILABLE, TaskHoldKind.BLOCKED,
        ):
            await store.clear_task_state(chosen.queue, chosen.task)
        # Origin-aware dispatch: for any remote-landing mode (push or pr), resync
        # local main to origin/main before pinning base_ref so the worker starts
        # from the freshest merged state in a multi-actor repo (and an orphaned
        # ephemeral pr-mode squash is dropped). Best-effort: a transient fetch
        # failure must not fail the poll — base_ref then pins the local HEAD and
        # the land re-syncs anyway. See remote-landing.md.
        poll_meta = _task_meta(tasks_root, chosen.task, chosen.queue)
        effective_mode = LandingMode.PR if poll_meta.get("make_pr") else cfg.landing_mode
        if effective_mode.is_remote and cfg.rendezvous_remote:
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
        await store.set_lease_status(lease["id"], LeaseStatus.LEASED, run_id=run_id)
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
        return JSONResponse({"work": order, "queue_pauses": dict(_paused_queues)})

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
        # Fence: a submit is only honored while its lease is live AND owned by
        # the submitting worker. A reclaimed (expired), cancelled, or already
        # consumed lease means the task may have been re-leased elsewhere —
        # honoring the stale submit would double-land it. 409, no writes.
        if (
            lease is None
            or lease.get("status") != LeaseStatus.LEASED
            or lease.get("worker_id") != body.worker_id
        ):
            raise HTTPException(
                status_code=409,
                detail="stale submit: lease is not live for this worker",
            )
        base_ref = lease.get("base_ref")
        # The target repo the worker ran against is recorded on the run (and is
        # workspace-relative); landing materialises ``workspace / repo``.
        repo = run.get("repo")

        # Agent telemetry recorded on every outcome (a failed/no-change run still
        # burned turns + tokens), so the per-task rollups stay accurate. The
        # dict derives from the shared Telemetry model — no hand-synced re-pack.
        telemetry = body.telemetry.model_dump()

        # An honest block from the worker (no commits): record the outcome, hold
        # the task in the DB ``blocked`` state with its reason, do NOT land and do
        # NOT drop the brief, so a Resolve can pick it up later.
        if body.status == RunStatus.BLOCKED:
            reason = body.failure_reason or body.result_line or "blocked"
            await store.update_run(
                run_id,
                status=RunStatus.BLOCKED,
                result_line=body.result_line,
                failure_kind=body.failure_kind or FailureKind.BLOCKED,
                failure_reason=body.failure_reason,
                model=body.model,
                **telemetry,
            )
            await store.set_lease_status(body.lease_id, LeaseStatus.RELEASED)
            was_retry = _task_is_failed_in_frontmatter(queue, body.task)
            await _record_failure_outcome(
                queue, body.task, state=TaskHoldKind.BLOCKED, blocked_reason=reason,
                retry_eligible=True,
            )
            if was_retry:
                await _quarantine_retry_failure(queue, body.task, run_id, reason)
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
        if body.status != RunStatus.COMPLETED:
            await store.update_run(
                run_id,
                status=body.status,
                result_line=body.result_line,
                failure_kind=body.failure_kind,
                failure_reason=body.failure_reason,
                model=body.model,
                **telemetry,
            )
            await store.set_lease_status(body.lease_id, LeaseStatus.RELEASED)
            # A worker *error* marks the task ``failed`` so it is excluded from
            # immediate dispatch. The failure-policy state machine watches for
            # two unrelated failures in a row and pauses the queue. Repeated
            # errors on the *same* task still trigger the existing quarantine
            # guard. Operator-driven stops (aborted) are intentional and never
            # quarantine or count as failures.
            quarantined = False
            if body.status == RunStatus.ERROR:
                was_retry = _task_is_failed_in_frontmatter(queue, body.task)
                if body.failure_kind == FailureKind.VALIDATION_ERROR:
                    # Validation failures mean the agent DID produce commits
                    # but the validate command rejected them. The branch is
                    # preserved and the work is recoverable — treat this as a
                    # block (needs resolve) rather than a policy failure so it
                    # doesn't arm the two-in-a-row watch.
                    await store.set_task_state(
                        queue, body.task, TaskHoldKind.BLOCKED,
                        blocked_reason="validation failed: " + (
                            body.result_line or "validate command returned non-zero"
                        ),
                    )
                    await _emit(
                        "task_blocked", queue=queue, task=body.task,
                        payload={"reason": "validation_error",
                                 "detail": body.failure_reason},
                    )
                elif body.quarantine:
                    quarantined = await _quarantine_immediate(
                        queue, body.task, run_id, "worker error"
                    )
                elif await _quarantine_if_looping(queue, body.task, run_id, "worker error"):
                    quarantined = True
                    if was_retry:
                        label = queue_label(queue)
                        _paused_queues[label] = "retry_failed"
                        await _emit(
                            "queue_paused", queue=queue,
                            payload={"reason": "retry_failed", "task": body.task},
                        )
                else:
                    reason = body.result_line or body.failure_reason or "worker error"
                    await _record_failure_outcome(
                        queue, body.task, state=TaskHoldKind.FAILED,
                        blocked_reason=reason, retry_eligible=True,
                    )
                    if was_retry:
                        await _quarantine_retry_failure(queue, body.task, run_id, reason)
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
                    run_id, status=RunStatus.COMPLETED,
                    result_line=result_line, model=body.model,
                    **telemetry,
                )
                await store.set_lease_status(body.lease_id, LeaseStatus.RELEASED)
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
                and not has_commits(workspace, repo, body.task, queue=queue)
            ):
                body = body.model_copy(update={
                    "landable": True,
                    "result_line": "agent landed on main",
                })
            else:
                await store.update_run(
                    run_id, status=RunStatus.COMPLETED,
                    result_line=body.result_line or "no changes", model=body.model,
                    **telemetry,
                )
                await store.set_lease_status(body.lease_id, LeaseStatus.RELEASED)
                if body.quarantine:
                    quarantined = await _quarantine_immediate(
                        queue, body.task, run_id, "no changes produced"
                    )
                else:
                    quarantined = await _quarantine_if_looping(
                        queue, body.task, run_id, "no changes produced"
                    )
                if not quarantined:
                    was_retry = _task_is_failed_in_frontmatter(queue, body.task)
                    await _record_failure_outcome(
                        queue, body.task, state=TaskHoldKind.FAILED,
                        blocked_reason="no changes produced",
                        retry_eligible=True,
                    )
                    if was_retry:
                        await _quarantine_retry_failure(
                            queue, body.task, run_id, "no changes produced"
                        )
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
        effective_mode = LandingMode.PR if meta.get("make_pr") else cfg.landing_mode
        # Landing shells out to git (and possibly the network) repeatedly; run
        # it off the event loop so polls/heartbeats/SSE stay live. The lease
        # fence above makes this safe: a task re-leased while a slow land runs
        # gets its stale submit rejected, not raced.
        result = await asyncio.to_thread(
            land,
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
            status = RunStatus.ERROR
            failure_kind = (
                FailureKind.MERGE_CONFLICT if result.conflict
                else FailureKind.MERGE_REJECTED
            )
            await store.update_run(
                run_id,
                status=status,
                result_line=result.detail.splitlines()[0][:200] if result.detail else "land failed",
                failure_kind=failure_kind,
                failure_reason=result.detail,
                model=body.model,
                **telemetry,
            )
            await store.set_lease_status(body.lease_id, LeaseStatus.RELEASED)
            await _registry().set_idle(body.worker_id)
            # The branch is preserved (squash_to_main / land only teardown after a
            # confirmed land), so the conflict or rejection is resolvable. Hold the
            # task blocked so it doesn't re-lease while a resolve is pending.
            if result.conflict or result.recoverable:
                land_reason = "needs resolve: " + (
                    result.detail.splitlines()[0] if result.detail
                    else failure_kind
                )
                await _record_failure_outcome(
                    queue, body.task, state=TaskHoldKind.BLOCKED,
                    blocked_reason=land_reason, retry_eligible=False,
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
                and effective_mode is not LandingMode.PR
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
                await asyncio.to_thread(
                    drop_completed_task, tasks_root, body.task, tasks_rel, queue=queue
                )

        loc = None
        if result.sha:
            with contextlib.suppress(Exception):
                loc = await asyncio.to_thread(
                    compute_code_loc, workspace / repo, result.sha
                )
        await store.update_run(
            run_id,
            status=RunStatus.COMPLETED,
            result_line=body.result_line or result.detail or "landed",
            commit_sha=result.sha,
            loc=loc,
            model=body.model,
            remote=result.remote,
            pushed=result.pushed,
            **telemetry,
        )
        await store.set_lease_status(body.lease_id, LeaseStatus.LANDED)
        await store.clear_task_state(queue, body.task)
        _record_success_outcome(queue)
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
        # Fence (mirrors the submit fence): the origin run must still be waiting
        # on a resolve. If it already reached a terminal outcome by another route
        # (e.g. the task was re-leased and landed), this report is stale —
        # honoring it would double-land / wrongly re-block the task. 409, no
        # writes.
        if body.origin_run_id:
            origin = await store.get_run(body.origin_run_id)
            if origin is None or origin.get("status") not in RUN_RESOLVABLE_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail="stale resolve result: origin run is not resolvable",
                )
        telemetry = {
            "turns": body.turns,
            "input_tokens": body.input_tokens,
            "output_tokens": body.output_tokens,
            "cost_usd": body.cost_usd,
        }
        if body.landed and body.status == RunStatus.COMPLETED:
            await store.update_run(
                run_id,
                status=RunStatus.COMPLETED,
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
                        status=RunStatus.COMPLETED,
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
                status=RunStatus.ERROR,
                result_line=body.result_line or "resolve failed",
                failure_kind=body.failure_kind or FailureKind.MERGE_CONFLICT,
                failure_reason=body.failure_reason,
                **telemetry,
            )
            reason = "needs resolve: " + (
                body.result_line or body.failure_reason or "resolve failed"
            )
            await store.set_task_state(
                queue, body.task, TaskHoldKind.BLOCKED, blocked_reason=reason,
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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def jsonable(row: dict[str, Any] | None) -> dict[str, Any]:
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
        if status == RunStatus.COMPLETED and run.get("commit_sha"):
            break
        if status == RunStatus.ERROR or (
            status == RunStatus.COMPLETED and not run.get("commit_sha")
        ):
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
