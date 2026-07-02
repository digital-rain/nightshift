"""Worker-facing manager API — checkin / poll / heartbeat / run events /
submit / resolve-result.

Split out of ``manager/app.py`` in Phase 3 of the rebuild-in-place migration;
Phase 4 rewrote ``worker_submit`` around the pure transition core; Phase 7 made
landing asynchronous. The submit handler parses the outcome, computes a
:class:`~nightshift.lifecycle.Transition` or a :class:`~nightshift.lifecycle.
GitPhase`, and branches:

* no git work (blocked/error/no-change) — apply the transition synchronously,
  exactly as Phase 4 did;
* adopt-check / split harvest — bounded git jobs on the per-repo executor,
  awaited inline (the response reports their result);
* land — mark the run ``phase="landing"``, enqueue the serialized land job,
  and return ``{"queued": true}`` immediately so heartbeats and polls keep
  flowing during a slow land. The job's completion applies the same
  ``on_land_result`` transition via ``store.apply_transition``, whose CAS
  (lease still LEASED, still this worker's) is the stale-result fence; a
  refused result is logged and traced on the run's event log.

``worker_poll`` is a read-only hot path (candidates → pick → lease → return);
lease reclaim, stale-worker reaping, and hold writes live in the reconciler.
Endpoints are registered onto the shared FastAPI app by
:func:`register_worker_api`; the app wiring (store, registry, event emitter,
SSE broadcaster, executor pool, sync throttle) is injected by ``create_app``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, Protocol, assert_never

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nightshift import playlists as playlists_mod
from nightshift import repos
from nightshift.events import new_run_id
from nightshift.git.executor import ExecutorPool
from nightshift.git.squash import compute_code_loc
from nightshift.git.store import commit_tasks
from nightshift.git.sync import SyncThrottle, sync_main_locked
from nightshift.lifecycle import (
    LAND_SUCCESS_KINDS,
    RUN_RESOLVABLE_STATUSES,
    AttemptRef,
    FailureKind,
    GitPhase,
    LandingMode,
    LandKind,
    LandOutcome,
    LeaseStatus,
    Outcome,
    RetryPolicy,
    RunStatus,
    SubmitPolicy,
    TaskHoldKind,
    Transition,
    on_land_result,
    on_split_result,
    on_submit,
)
from nightshift.manager import failure_policy
from nightshift.manager.config import ManagerConfig
from nightshift.manager.landing import (
    adopt_or_nothing_locked,
    canonical_head,
    land_locked,
    push_resolved_main_locked,
)
from nightshift.manager.registry import Registry
from nightshift.manager.scheduler import (
    WorkerFilter,
    build_candidates,
    pick_next,
    queue_label,
)
from nightshift.manager.store import NightshiftStore
from nightshift.manager.work_orders import build_work_order, task_meta
from nightshift.model_id import provider_of
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
    set_task_meta,
    task_is_evergreen,
)


# A submit is only honored while its lease is live AND owned by the submitting
# worker. A reclaimed (expired), cancelled, or already consumed lease means the
# task may have been re-leased elsewhere — honoring the stale submit would
# double-land it. 409, no writes (enforced by apply_transition's CAS).
_STALE_SUBMIT = "stale submit: lease is not live for this worker"

_log = logging.getLogger("nightshift.manager.api_worker")

# How long an environment failure cools the submitting worker down for its
# queue (Phase 5): long enough that a broken box stops eating the queue,
# short enough that a transient outage self-heals without operator action.
WORKER_COOLDOWN_SECONDS = 300.0


def _cooldown_exclusions(
    cooldowns: dict[tuple[str, str], datetime], worker_id: str
) -> set[str]:
    """Reap expired worker cooldowns and return the queue labels this worker
    is currently cooled down for (other workers keep seeing those queues)."""
    now = datetime.now(UTC)
    excluded: set[str] = set()
    for (wid, label), expiry in list(cooldowns.items()):
        if expiry <= now:
            cooldowns.pop((wid, label), None)
        elif wid == worker_id:
            excluded.add(label)
    return excluded


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
    # instead of waiting for the counter threshold
    # (RetryPolicy.immediate_quarantine).
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


class BroadcastFn(Protocol):
    """``create_app``'s SSE fan-out: publish one already-persisted event to
    every connected browser (no store write — the outbox half of ``_emit``)."""

    def __call__(self, event: dict[str, Any]) -> Awaitable[None]: ...


def register_worker_api(
    app: FastAPI,
    *,
    cfg: ManagerConfig,
    workspace: Path,
    tasks_root: Path,
    tasks_repo: str,
    _store: Callable[[], NightshiftStore],
    _registry: Callable[[], Registry],
    _emit: EmitFn,
    _queue_from_label: Callable[[str | None], str | None],
    _all_queues: Callable[[], list[str | None]],
    _failure_state: Callable[[str], failure_policy.QueueFailureState],
    _start_resolve: StartResolveFn,
    _broadcast: BroadcastFn,
    # Phase 7: the per-repo git executor pool and the app-owned sync throttle
    # (all git mutation routes through the pool; the throttle replaces the
    # old module-global sync state).
    _executors: ExecutorPool,
    _sync_throttle: SyncThrottle,
) -> None:
    """Register the worker endpoints. Shared wiring (store/registry accessors,
    the event emitter, the SSE broadcaster, the executor pool, and the resolve
    spawner) is injected by ``create_app`` under the same names the handler
    bodies always used."""
    def _set_frontmatter_flag_job(
        queue: str | None, task: str, key: str, value: bool,
        *, reason_key: str | None = None, reason: str | None = None,
    ) -> None:
        """Write a boolean frontmatter field (quarantined/failed) and an
        optional companion reason field directly to the task's .md file.
        Runs as a tasks-repo executor job (content-store mutation).

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
        # Phase 7: the poll hot path is pure reads — build candidates → pick →
        # lease → return. Lease reclaim, stale-worker reaping, and the
        # unroutable / repo-availability hold *writes* all moved to the
        # reconciler; a no-work poll performs zero store writes.

        # Build candidates across every queue from the canonical briefs in the
        # content store (each candidate already carries its resolved target repo).
        # Queues paused via the transport controls or locally backed-off by this
        # worker are excluded from dispatch.
        exclude = set(body.exclude_queues or [])
        # Environment-failure cooldowns: a worker that just env-failed in a
        # queue isn't offered that queue until the cooldown expires.
        exclude |= _cooldown_exclusions(app.state.worker_cooldowns, body.worker_id)
        queue_pauses = await store.queue_pauses()
        candidates_by_queue = {
            q: build_candidates(tasks_root, q, default_model=cfg.default_model)
            for q in _all_queues()
            if queue_label(q) not in queue_pauses and queue_label(q) not in exclude
        }

        # Manager-side queue dedication (queue label -> bound worker ids).
        dedication = await store.queue_dedication()

        # Quarantined/failed tasks are sourced from frontmatter (the single
        # source of truth). live_ordered_queue already skips them so they
        # won't appear in candidates, but we still need the sets for the
        # repo-check guard below and for Phase B retry.
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

        # Dispatch exclusion for bad/absent repo references stays here (read
        # only — never hand out undispatchable work); the corresponding hold
        # writes and warnings are the reconciler's.
        repo_excluded: set[tuple[str | None, str]] = set()
        for cands in candidates_by_queue.values():
            for cand in cands:
                if cand.repo_error is not None or (
                    cand.repo and not repos.repo_available(workspace, cand.repo)
                ):
                    repo_excluded.add((cand.queue, cand.task))

        active = await store.active_leases()
        leased = {(_queue_from_label(le["queue"]), le["task"]) for le in active}
        blocked_rows = await store.list_blocked()
        blocked = {(_queue_from_label(b["queue"]), b["task"]) for b in blocked_rows}
        # Never dispatch a paused (repo_unavailable), repo-blocked,
        # quarantined (re-execution loop), or failed task.
        blocked |= repo_excluded
        blocked |= quarantined
        blocked |= failed
        # Retry backoff (Phase 5): a task whose next_eligible_at hasn't
        # elapsed is not dispatchable — by any worker, on any path.
        backing_off = {
            (_queue_from_label(b["queue"]), b["task"])
            for b in await store.tasks_backing_off()
        }
        blocked |= backing_off

        # Phase B: once a queue has no active leases and no ready (non-failed)
        # candidate left, let its earliest failed/blocked-retryable task back
        # into dispatch -- one at a time, never two failed tasks concurrently.
        # Failed tasks come from frontmatter; blocked-retryable come from DB.
        for q in list(candidates_by_queue):
            label = queue_label(q)
            if label in queue_pauses:
                continue
            if any(le.get("queue", "main") == label for le in active):
                continue
            cands = candidates_by_queue[q]
            ready_exists = any((c.queue, c.task) not in blocked for c in cands)
            if ready_exists:
                continue
            tasks_rel = playlists_mod.tasks_rel(q)
            fm_failed = failed_tasks(tasks_root, tasks_rel)
            db_retryable = await store.retryable_tasks(q)
            # Backoff applies to the retry path too: a failed task isn't
            # retryable until its next_eligible_at elapses.
            retryable = [
                r for r in [*fm_failed, *db_retryable]
                if (q, r["task"]) not in backing_off
            ]
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
            return JSONResponse({"work": None, "queue_pauses": dict(queue_pauses)}, status_code=200)

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
        poll_meta = task_meta(tasks_root, chosen.task, chosen.queue)
        effective_mode = LandingMode.PR if poll_meta.get("make_pr") else cfg.landing_mode
        if (
            effective_mode.is_remote
            and cfg.rendezvous_remote
            # Throttle pre-check keeps the common (recently-synced) case from
            # even enqueuing an executor job behind a possibly-slow land.
            and _sync_throttle.due(workspace, repo, cfg.cadences.git_refresh_seconds)
        ):
            with contextlib.suppress(Exception):
                await asyncio.wrap_future(_executors.submit(repo, partial(
                    sync_main_locked,
                    workspace, repo, cfg.rendezvous_remote,
                    min_interval_seconds=cfg.cadences.git_refresh_seconds,
                    force=False,
                    throttle=_sync_throttle,
                )))
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
        order = build_work_order(
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
        return JSONResponse({"work": order, "queue_pauses": dict(queue_pauses)})

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
        # Advisory fast-fail on the same predicate the CAS below enforces, so a
        # stale submit never reaches git work. The *authoritative* fence is
        # apply_transition's CAS — a lease consumed between this read and the
        # apply still yields 409 with no writes.
        if (
            lease is None
            or lease.get("status") != LeaseStatus.LEASED
            or lease.get("worker_id") != body.worker_id
        ):
            raise HTTPException(status_code=409, detail=_STALE_SUBMIT)
        # The target repo the worker ran against is recorded on the run (and is
        # workspace-relative); landing materialises ``workspace / repo``.
        repo = run.get("repo")
        tasks_rel = playlists_mod.tasks_rel(queue)
        meta = task_meta(tasks_root, body.task, queue)
        # A task may force PR mode with `make_pr: true`; otherwise the manager's
        # configured landing_mode applies. make_pr never forces squash/push.
        effective_mode = LandingMode.PR if meta.get("make_pr") else cfg.landing_mode
        label = queue_label(queue)
        # The persisted counter replaces the run-history streak scan: the task
        # row carries attempts_without_progress *before* this outcome; the
        # transition's Progress op adds the current outcome itself.
        task_row = await store.get_task_state(queue, body.task)
        policy = SubmitPolicy(
            retry=RetryPolicy(
                quarantine_after=cfg.quarantine_threshold,
                immediate_quarantine=body.quarantine,
            ),
            attempts_without_progress=(
                int(task_row["attempts_without_progress"]) if task_row else 0
            ),
            was_retry=_task_is_failed_in_frontmatter(queue, body.task),
            watch_armed=_failure_state(label).watch_armed,
            queue_paused=label in await store.queue_pauses(),
            split=bool(meta.get("split")),
            evergreen=task_is_evergreen(
                meta, body.task, resolve_config(workspace, tasks_root, tasks_rel)
            ),
            auto_resolve=cfg.auto_resolve,
            pr_mode=effective_mode is LandingMode.PR,
        )
        ref = AttemptRef(
            run_id=run_id, lease_id=body.lease_id, queue=queue, task=body.task
        )

        async def _finish(t: Transition, *, set_idle: bool) -> dict[str, Any] | None:
            """Apply the transition and run its post-commit side effects.

            The CAS (lease still LEASED, still this worker's) is the
            authoritative fence: a lease consumed while a queued land ran means
            the result is stale — return ``None``, write nothing. Content-store
            mutations (frontmatter flags, brief drop) route through the
            tasks-repo executor. ``set_idle`` is false on the deferred land
            path, where the worker went idle at enqueue time.
            """
            event_ids = await store.apply_transition(
                t, expected_status=LeaseStatus.LEASED, expected_worker_id=body.worker_id
            )
            if event_ids is None:
                return None
            response = dict(t.response)
            for flag in t.effects.frontmatter:
                await _run_tasks_repo_job(_executors, tasks_repo, partial(
                    _set_frontmatter_flag_job,
                    queue, body.task, flag.key, flag.value,
                    reason_key=flag.reason_key, reason=flag.reason,
                ))
            if t.effects.watch_armed is not None:
                _failure_state(label).watch_armed = t.effects.watch_armed
            if t.effects.pause_queue is not None:
                # Phase 7: pauses are durable — a manager restart no longer
                # silently unpauses a failure-tripped queue.
                await store.set_queue_pause(label, t.effects.pause_queue)
            if t.effects.worker_cooldown:
                app.state.worker_cooldowns[(body.worker_id, label)] = (
                    datetime.now(UTC) + timedelta(seconds=WORKER_COOLDOWN_SECONDS)
                )
            if t.effects.drop_brief:
                # Backstop the worker's queue removal: a landed regular task must
                # leave the content store (evergreen tasks keep their file).
                with contextlib.suppress(Exception):
                    await _run_tasks_repo_job(_executors, tasks_repo, partial(
                        drop_completed_task, tasks_root, body.task, tasks_rel,
                        queue=queue,
                    ))
            if set_idle:
                await _registry().set_idle(body.worker_id)
            for event_id, ev in zip(event_ids, t.events, strict=True):
                await _broadcast({
                    "id": event_id, "kind": ev.kind, "run_id": ev.run_id,
                    "queue": ev.queue, "task": ev.task, "payload": dict(ev.payload or {}),
                })
            if t.effects.start_resolve:
                # Auto-escalate: immediately spawn the out-of-process resolver
                # instead of waiting for an operator to click Resolve.
                started, _child, _err = await _start_resolve(
                    run_id, task=body.task, queue=queue, repo=repo, title=body.title,
                )
                response["resolving"] = started
            return response

        async def _with_loc(result: LandOutcome) -> LandOutcome:
            """Annotate a landed outcome with its LOC delta (read-only git)."""
            if result.kind in LAND_SUCCESS_KINDS and result.sha:
                with contextlib.suppress(Exception):
                    return replace(result, loc=await asyncio.to_thread(
                        compute_code_loc, workspace / repo, result.sha
                    ))
            return result

        computed = on_submit(ref, body, policy)
        if isinstance(computed, Transition):
            # No git work (blocked/error/validation-failed/...): fully
            # synchronous, exactly as before.
            response = await _finish(computed, set_idle=True)
            if response is None:
                raise HTTPException(status_code=409, detail=_STALE_SUBMIT)
            return JSONResponse(response)

        match computed:
            case GitPhase.HARVEST_SPLIT:
                # Decomposition run: harvest subtask briefs from the split
                # output dir and enqueue them, then retire the parent. A
                # content-store mutation -> the tasks-repo executor,
                # synchronous-wait (cheap, and the response reports `created`).
                created = await asyncio.wrap_future(_executors.submit(
                    tasks_repo, partial(
                        harvest_split_output,
                        workspace, tasks_root, repo, body.task, meta,
                        queue=queue, tasks_rel=tasks_rel,
                    ),
                ))
                t = on_split_result(ref, body, created)
            case GitPhase.ADOPT_CHECK:
                # Nothing landable: the cheap adopt-or-nothing detection (never
                # an origin sync or squash attempt). Its adopt path applies
                # remote policy (a push/PR) = target-repo mutation, so it runs
                # on the repo executor too — synchronous-wait, because it is
                # bounded (no integrate loop) and its response is consumed
                # inline.
                result = await asyncio.wrap_future(_executors.submit(
                    repo, partial(
                        adopt_or_nothing_locked,
                        workspace, repo, body.task, body.title,
                        queue=queue,
                        base_ref=lease.get("base_ref"),
                        landing_mode=effective_mode,
                        automerge=bool(meta.get("automerge", True)),
                        draft=bool(meta.get("draft", False)),
                    ),
                ))
                t = on_land_result(ref, body, await _with_loc(result), policy)
            case GitPhase.LAND:
                # Async land (Phase 7): mark the run's phase, enqueue the
                # serialized land job, and return immediately — heartbeats and
                # polls keep flowing while a slow land runs. The result arrives
                # as the same on_land_result transition the synchronous path
                # applied, CAS-fenced against a lease consumed mid-land.
                await store.update_run(run_id, phase="landing")
                land_future = _executors.submit(repo, partial(
                    land_locked,
                    workspace, repo, body.task, body.title,
                    queue=queue,
                    base_ref=lease.get("base_ref"),
                    landing_mode=effective_mode,
                    automerge=bool(meta.get("automerge", True)),
                    draft=bool(meta.get("draft", False)),
                    branch_ref=body.branch_ref,
                    head_sha=body.head_sha,
                    rendezvous_remote=cfg.rendezvous_remote,
                    git_refresh_seconds=cfg.cadences.git_refresh_seconds,
                    throttle=_sync_throttle,
                ))

                async def _complete_land() -> None:
                    try:
                        result = await asyncio.wrap_future(land_future)
                    except Exception as exc:
                        # A crashed land job maps to the same terminal shape an
                        # in-band pipeline error would produce; the branch is
                        # preserved for a resolve.
                        result = LandOutcome(
                            kind=LandKind.TRANSPORT_FAILED,
                            detail=f"land job crashed: {exc}",
                        )
                    else:
                        result = await _with_loc(result)
                    applied = await _finish(
                        on_land_result(ref, body, result, policy), set_idle=False
                    )
                    if applied is None:
                        # The lease was consumed (cancelled/expired) while the
                        # land job ran and the CAS refused the result. The git
                        # work may already be on origin — never let that vanish
                        # silently: leave an operator-visible trace on the run
                        # (task_log rides the existing wire kind).
                        line = (
                            "land result discarded: lease no longer live "
                            f"(kind={result.kind}, sha={result.sha or '-'})"
                        )
                        _log.warning("run %s %s/%s: %s",
                                     run_id, label, body.task, line)
                        with contextlib.suppress(Exception):
                            await _emit(
                                "task_log", run_id=run_id, queue=queue,
                                task=body.task,
                                payload={
                                    "line": line,
                                    "land_kind": str(result.kind),
                                    "sha": result.sha,
                                },
                            )

                # Completion runs on the event loop (never blocks the executor
                # thread — a land targeting the tasks repo itself would
                # otherwise self-deadlock on the content-store side effects).
                # The tracking set is the drain seam's second half.
                completion = asyncio.create_task(_complete_land())
                app.state.land_completions.add(completion)
                completion.add_done_callback(app.state.land_completions.discard)
                await _registry().set_idle(body.worker_id)
                return JSONResponse(
                    {"landed": None, "status": "landing", "queued": True}
                )
            case _:
                assert_never(computed)

        response = await _finish(t, set_idle=True)
        if response is None:
            raise HTTPException(status_code=409, detail=_STALE_SUBMIT)
        return JSONResponse(response)

    @app.post(
        "/api/worker/runs/{run_id}/resolve-result",
        dependencies=[Depends(_require_secret)],
    )
    async def worker_resolve_result(
        run_id: str, body: ResolveResultBody
    ) -> JSONResponse:
        """Record the outcome of an out-of-process resolve (see resolve_job).

        Phase 7: the resolver subprocess stops at the resolved squash SHA on
        its local main — push authority is the manager's. A push-mode resolve
        reports ``pushed: None`` and the origin push runs here, on the repo
        executor (the last cross-process integrate-lock consumer is gone).

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
        landed = bool(body.landed and body.status == RunStatus.COMPLETED)
        sha, remote_kind, pushed = body.sha, body.remote, body.pushed
        result_line = body.result_line
        failure_kind, failure_reason = body.failure_kind, body.failure_reason
        if (
            landed
            and sha
            and pushed is None
            and cfg.landing_mode is LandingMode.PUSH
            and cfg.rendezvous_remote
        ):
            run = await store.get_run(run_id)
            repo = (run or {}).get("repo")
            if repo:
                ok, info = await asyncio.wrap_future(_executors.submit(
                    repo, partial(
                        push_resolved_main_locked,
                        workspace, repo, cfg.rendezvous_remote, sha,
                        max_retries=cfg.max_push_retries,
                        throttle=_sync_throttle,
                    ),
                ))
                if ok:
                    sha, remote_kind, pushed = info, "push", True
                else:
                    # Same terminal shape the subprocess used to report on its
                    # own push failure: the resolved commit stays on local main
                    # and the task re-blocks for another resolve.
                    landed = False
                    result_line = result_line or "resolve failed"
                    failure_kind = failure_kind or FailureKind.MERGE_CONFLICT
                    failure_reason = failure_reason or info
        if landed:
            await store.update_run(
                run_id,
                status=RunStatus.COMPLETED,
                result_line=result_line or "resolved: landed",
                commit_sha=sha,
                loc=body.loc,
                remote=remote_kind,
                pushed=pushed,
                **telemetry,
            )
            if body.origin_run_id:
                with contextlib.suppress(Exception):
                    await store.update_run(
                        body.origin_run_id,
                        status=RunStatus.COMPLETED,
                        result_line=result_line or "resolved",
                        commit_sha=sha,
                    )
            # A landed resolve is real progress — the retry counter resets.
            await store.clear_task_state(queue, body.task, reset_progress=True)
            tasks_rel = playlists_mod.tasks_rel(queue)
            meta = task_meta(tasks_root, body.task, queue)
            if not task_is_evergreen(
                meta, body.task, resolve_config(workspace, tasks_root, tasks_rel)
            ):
                with contextlib.suppress(Exception):
                    await asyncio.wrap_future(_executors.submit(tasks_repo, partial(
                        drop_completed_task, tasks_root, body.task, tasks_rel,
                        queue=queue,
                    )))
            await _emit(
                "task_result", run_id=run_id, queue=queue, task=body.task,
                payload={
                    "status": "completed", "commit_sha": sha,
                    "remote": remote_kind, "pushed": pushed,
                },
            )
            await _emit("queue_changed", queue=queue)
        else:
            await store.update_run(
                run_id,
                status=RunStatus.ERROR,
                result_line=result_line or "resolve failed",
                failure_kind=failure_kind or FailureKind.MERGE_CONFLICT,
                failure_reason=failure_reason,
                **telemetry,
            )
            reason = "needs resolve: " + (
                result_line or failure_reason or "resolve failed"
            )
            await store.set_task_state(
                queue, body.task, TaskHoldKind.BLOCKED, blocked_reason=reason,
            )
            await _emit(
                "task_blocked", queue=queue, task=body.task,
                payload={"reason": failure_kind or "merge_conflict"},
            )
            await _emit(
                "task_result", run_id=run_id, queue=queue, task=body.task,
                payload={"status": "error", "failure_kind": failure_kind},
            )
        app.state.resolves.pop(run_id, None)
        return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _run_tasks_repo_job(
    executors: ExecutorPool, tasks_repo: str, fn: Callable[[], Any]
) -> Any:
    """Run a content-store mutation as a job on the tasks repo's executor —
    the tasks repo is a repo like any other target (Phase 7)."""
    return await asyncio.wrap_future(executors.submit(tasks_repo, fn))


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


