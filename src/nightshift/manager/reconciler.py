"""Reconciler — recovery and hygiene in one periodic loop (Phase 7).

One asyncio task (plus a startup pass) owns every "notice and repair" duty the
worker poll path used to perform inline, so the poll hot path becomes pure
reads (build candidates → pick → lease → return):

1. **Deadline expiry** — every live lease past its ``expires_at`` gets an
   :func:`~nightshift.lifecycle.on_deadline` transition (CAS on ``leased``, so
   a lease consumed concurrently is never double-expired). Leases whose run is
   mid-land (``phase="landing"``) are exempt: the land job is the only thing
   that may consume them, and the startup parking pass — not deadline expiry —
   is the recovery path if the process dies with the job in flight.
2. **Hold set/clear** — the ``no_capable_worker`` / bad-repo-reference
   ``blocked`` holds and the ``repo_unavailable`` pauses move here from
   ``worker_poll``, with the same dedup + one-warning-per-queue behavior. The
   reconciler also *clears* its own holds when they no longer apply (a capable
   worker checked in; the repo reappeared) — dispatch-time clearing remains as
   the fast path.
3. **GC** — terminal worktrees/branches: a ``task-local/*`` branch whose brief
   is gone, with no live lease and no task hold, is provably abandoned (a
   conflicted land always leaves a hold; a queued brief is never GC'd), so its
   worktree+branch are torn down on the repo executor and, cross-machine, its
   consumed WIP ref is pruned best-effort. Finished resolve subprocesses are
   reaped from ``app.state.resolves``.
4. **Worker liveness** — silent workers are marked offline
   (``registry.reap_stale``), moved out of the poll path.

The startup pass additionally parks runs interrupted mid-land: a run stuck in
``phase="landing"`` with a live lease can only be a previous process's
abandoned executor job (this process hasn't enqueued anything yet), and
without the Phase 8 land trailer we cannot verify whether its squash reached
main — so it is conservatively errored (``merge_rejected``) and the task held
blocked for a resolve via :func:`~nightshift.lifecycle.on_land_interrupted`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nightshift import playlists as playlists_mod
from nightshift import repos
from nightshift.git import GitRunner
from nightshift.git.executor import ExecutorPool
from nightshift.git.transport import _wip_ref, prune_rendezvous_branch
from nightshift.git.worktrees import cleanup_task_worktree
from nightshift.lifecycle import (
    AttemptRef,
    LeaseStatus,
    RunStatus,
    TaskHoldKind,
    Transition,
    on_deadline,
    on_land_interrupted,
)
from nightshift.manager.config import ManagerConfig
from nightshift.manager.registry import Registry
from nightshift.manager.scheduler import (
    UNROUTABLE_REASON_PREFIXES,
    TaskCandidate,
    build_candidates,
    queue_label,
    unroutable,
)
from nightshift.manager.store import NightshiftStore
from nightshift.task_files import frontmatter_held_tasks


_log = logging.getLogger("nightshift.manager.reconciler")


def _is_mid_land(run: dict[str, Any] | None) -> bool:
    """The mid-land predicate shared by the deadline-expiry exemption and the
    startup parking pass: a live run whose land job is queued/running."""
    return (
        run is not None
        and run.get("status") == RunStatus.RUNNING
        and run.get("phase") == "landing"
    )


def reap_finished_resolves(resolves: dict[str, dict[str, Any]]) -> None:
    """Drop bookkeeping for resolve subprocesses that have exited. Shared with
    ``create_app``'s resolve spawner (which reaps before the per-repo cap)."""
    for rid in [rid for rid, r in resolves.items() if r["proc"].poll() is not None]:
        resolves.pop(rid, None)


class Reconciler:
    """The one periodic recovery/hygiene loop, owned by the manager app."""

    def __init__(
        self,
        *,
        workspace: Path,
        tasks_root: Path,
        cfg: ManagerConfig,
        store: Callable[[], NightshiftStore],
        registry: Callable[[], Registry],
        emit: Callable[..., Awaitable[None]],
        broadcast: Callable[[dict[str, Any]], Awaitable[None]],
        queue_from_label: Callable[[str | None], str | None],
        all_queues: Callable[[], list[str | None]],
        executors: ExecutorPool,
        resolves: dict[str, dict[str, Any]],
        repo_warnings: set[str | None],
    ) -> None:
        self._workspace = workspace
        self._tasks_root = tasks_root
        self._cfg = cfg
        self._store = store
        self._registry = registry
        self._emit = emit
        self._broadcast = broadcast
        self._queue_from_label = queue_from_label
        self._all_queues = all_queues
        self._executors = executors
        self._resolves = resolves
        self._repo_warnings = repo_warnings

    # ------------------------------------------------------------------ #
    # Entry points
    # ------------------------------------------------------------------ #

    async def startup(self) -> None:
        """The startup pass: park mid-land casualties first (their leases must
        not be treated as ordinary deadline expiries), then one full pass.
        Isolated like a tick: recovery machinery failing must log loudly, not
        prevent the manager from booting."""
        await self._run_duty("mid-land parking", self._park_interrupted_lands)
        await self.reconcile_once()

    async def run_forever(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            # Each duty is individually isolated inside reconcile_once; the
            # loop itself can only die by cancellation (lifespan shutdown).
            await self.reconcile_once()

    async def reconcile_once(self) -> None:
        await self._run_duty("deadline expiry", self._expire_deadlines)
        await self._run_duty("hold set/clear", self._reconcile_holds)
        await self._run_duty("worker liveness", self._mark_workers_offline)
        await self._run_duty("resolve reaping", self._reap_resolves)
        await self._run_duty("terminal GC", self._gc_terminal_artifacts)

    async def _run_duty(self, name: str, duty: Callable[[], Awaitable[None]]) -> None:
        """One duty's failure (a git hiccup, a transient store error) is logged
        with its traceback and never starves the remaining duties or kills the
        loop — the next tick retries everything idempotently."""
        try:
            await duty()
        except Exception:
            _log.warning("reconciler: %s failed; continuing", name, exc_info=True)

    # ------------------------------------------------------------------ #
    # Startup: interrupted lands
    # ------------------------------------------------------------------ #

    async def _park_interrupted_lands(self) -> None:
        # TODO(phase 8): branch_ref/head_sha from the worker's submit are not
        # persisted, so parking is the only safe option here. The trailer-based
        # startup pass needs them recorded to verify-or-re-enqueue instead.
        store = self._store()
        for lease in await store.active_leases():
            run_id = lease.get("run_id")
            if not run_id:
                continue
            if not _is_mid_land(await store.get_run(run_id)):
                continue
            ref = AttemptRef(
                run_id=run_id,
                lease_id=lease["id"],
                queue=self._queue_from_label(lease.get("queue")),
                task=lease["task"],
            )
            await self._apply_and_broadcast(on_land_interrupted(ref))

    # ------------------------------------------------------------------ #
    # 1. Deadline expiry
    # ------------------------------------------------------------------ #

    async def _expire_deadlines(self) -> None:
        store = self._store()
        now = datetime.now(UTC)
        for lease in await store.active_leases():
            expires = lease.get("expires_at")
            if expires is None or expires >= now:
                continue
            # Mid-land exemption: once the worker's submit enqueued the land,
            # nothing heartbeats the lease anymore — a slow (or queue-delayed)
            # land could outlive the TTL while its push may already have
            # reached origin. Expiring it here would make the deferred
            # on_land_result CAS fail, silently dropping a completed land and
            # re-dispatching the task (a duplicate-land attempt). So a lease
            # whose run is mid-land is deadline-exempt; if the *process* dies
            # mid-land, the startup parking pass (_park_interrupted_lands) is
            # the recovery path for exactly these leases.
            run_id = lease.get("run_id")
            if run_id and _is_mid_land(await store.get_run(run_id)):
                continue
            ref = AttemptRef(
                run_id=run_id or "",
                lease_id=lease["id"],
                queue=self._queue_from_label(lease.get("queue")),
                task=lease["task"],
            )
            await self._apply_and_broadcast(on_deadline(ref))

    async def _apply_and_broadcast(self, t: Transition) -> bool:
        """CAS-apply a reconciler transition (expected lease state: still
        LEASED — a lease consumed concurrently is never double-applied) and fan
        its committed events out like the submit path does. False on a lost
        CAS."""
        event_ids = await self._store().apply_transition(
            t, expected_status=LeaseStatus.LEASED
        )
        if event_ids is None:
            return False
        for event_id, ev in zip(event_ids, t.events, strict=True):
            await self._broadcast({
                "id": event_id, "kind": ev.kind, "run_id": ev.run_id,
                "queue": ev.queue, "task": ev.task,
                "payload": dict(ev.payload or {}),
            })
        return True

    async def _mark_workers_offline(self) -> None:
        """Silent workers flip to offline (moved out of the poll path)."""
        await self._registry().reap_stale()

    # ------------------------------------------------------------------ #
    # 2. Hold set/clear (moved out of worker_poll)
    # ------------------------------------------------------------------ #

    async def _reconcile_holds(self) -> None:
        store = self._store()
        reg = self._registry()
        pauses = await store.queue_pauses()
        candidates_by_queue: dict[str | None, list[TaskCandidate]] = {
            q: build_candidates(self._tasks_root, q, default_model=self._cfg.default_model)
            for q in self._all_queues()
            if queue_label(q) not in pauses
        }

        # Quarantined tasks are exempt from hold writes (poll-path parity: the
        # frontmatter flag is the stronger, operator-owned state).
        quarantined: set[tuple[str | None, str]] = set()
        for q in self._all_queues():
            tasks_rel = playlists_mod.tasks_rel(q)
            for row in frontmatter_held_tasks(self._tasks_root, tasks_rel):
                if row["state"] == TaskHoldKind.QUARANTINED:
                    quarantined.add(
                        (self._queue_from_label(row["queue"]), row["task"])
                    )

        dedication = await store.queue_dedication()
        available_models = await reg.available_models()
        available_mcps = await reg.available_mcps()
        online_workers = await reg.online_worker_ids()

        # Mark tasks blocked when no live worker can ever currently serve them:
        # an unadvertised pinned model, an unadvertised connector, or a queue
        # dedicated only to offline workers.
        unroutable_pairs = unroutable(
            candidates_by_queue,
            available_models=available_models,
            available_mcps=available_mcps,
            dedication=dedication,
            online_workers=online_workers,
        )
        unroutable_keys = {(c.queue, c.task) for c, _ in unroutable_pairs}
        for cand, reason in unroutable_pairs:
            if (cand.queue, cand.task) in quarantined:
                continue
            existing = await store.get_task_state(cand.queue, cand.task)
            if not existing or existing.get("state") != TaskHoldKind.BLOCKED:
                await store.set_task_state(
                    cand.queue, cand.task, TaskHoldKind.BLOCKED, blocked_reason=reason
                )
                await self._emit(
                    "task_blocked",
                    queue=cand.queue,
                    task=cand.task,
                    payload={"reason": reason},
                )

        # Repo resolution & availability: a malformed/unset repo reference is
        # an authoring error (-> blocked); a well-formed name whose repo is
        # absent pauses the task (-> repo_unavailable) and warns once per
        # queue. Dispatch exclusion stays in worker_poll (read-only).
        candidate_keys: set[tuple[str | None, str]] = set()
        for cands in candidates_by_queue.values():
            for cand in cands:
                key = (cand.queue, cand.task)
                candidate_keys.add(key)
                if key in quarantined:
                    continue
                if cand.repo_error is not None:
                    existing = await store.get_task_state(cand.queue, cand.task)
                    if not existing or existing.get("state") != TaskHoldKind.BLOCKED:
                        await store.set_task_state(
                            cand.queue, cand.task, TaskHoldKind.BLOCKED,
                            blocked_reason=cand.repo_error,
                        )
                        await self._emit(
                            "task_blocked",
                            queue=cand.queue,
                            task=cand.task,
                            payload={"reason": cand.repo_error},
                        )
                elif cand.repo and not repos.repo_available(self._workspace, cand.repo):
                    existing = await store.get_task_state(cand.queue, cand.task)
                    if not existing or existing.get("state") != TaskHoldKind.REPO_UNAVAILABLE:
                        await store.set_task_state(
                            cand.queue, cand.task, TaskHoldKind.REPO_UNAVAILABLE,
                            repo=cand.repo,
                        )
                    if cand.queue not in self._repo_warnings:
                        self._repo_warnings.add(cand.queue)
                        await self._emit(
                            "repo_unavailable",
                            queue=cand.queue,
                            task=cand.task,
                            payload={"repo": cand.repo},
                        )

        # Clear the holds this loop owns once they no longer apply. Silent
        # (matching the dispatch-time clear); operator-set blocks and
        # authoring errors are left for the explicit reset endpoint.
        for row in await store.tasks_in_state(TaskHoldKind.REPO_UNAVAILABLE):
            repo = row.get("repo")
            if repo and repos.repo_available(self._workspace, repo):
                await store.clear_task_state(
                    self._queue_from_label(row.get("queue")), row["task"]
                )
        for row in await store.tasks_in_state(TaskHoldKind.BLOCKED):
            reason = row.get("blocked_reason") or ""
            # Only holds this loop set (recognized by scheduler.unroutable's
            # exported reason vocabulary) are auto-cleared; operator-actionable
            # blocks (validation, resolve, bad repo reference) are not.
            if not reason.startswith(UNROUTABLE_REASON_PREFIXES):
                continue
            queue = self._queue_from_label(row.get("queue"))
            key = (queue, row["task"])
            if key in candidate_keys and key not in unroutable_keys:
                await store.clear_task_state(queue, row["task"])

    # ------------------------------------------------------------------ #
    # 3. GC: resolve reaping + terminal worktrees/branches/WIP refs
    # ------------------------------------------------------------------ #

    async def _reap_resolves(self) -> None:
        reap_finished_resolves(self._resolves)

    async def _gc_terminal_artifacts(self) -> None:
        store = self._store()
        leased = {
            (self._queue_from_label(le.get("queue")), le["task"])
            for le in await store.active_leases()
        }
        worktrees_root = self._workspace / ".worktrees"
        if not worktrees_root.is_dir():
            return
        for repo_dir in sorted(worktrees_root.iterdir()):
            repo = repo_dir.name
            if not repo_dir.is_dir() or not repos.repo_available(self._workspace, repo):
                continue
            # The branch listing is a git subprocess — off the event loop like
            # every other git call (read-only, so no executor serialization).
            branches = await asyncio.to_thread(self._task_local_branches, repo)
            for queue, task in branches:
                if (queue, task) in leased:
                    continue
                brief = (
                    self._tasks_root / playlists_mod.tasks_rel(queue) / f"{task}.md"
                )
                if brief.exists():
                    continue
                # A hold row (blocked for resolve, repo pause) means the branch
                # may still be recovered — never GC live work. NULL-state
                # counter rows don't protect anything.
                state = await store.get_task_state(queue, task)
                if state is not None and state.get("state") is not None:
                    continue
                await asyncio.wrap_future(self._executors.submit(
                    repo,
                    self._gc_job(repo, task, queue),
                ))

    def _gc_job(self, repo: str, task: str, queue: str | None) -> Callable[[], None]:
        """Build the executor job that tears one abandoned task's artifacts
        down (worktree + branch, plus its consumed WIP ref cross-machine)."""
        remote = self._cfg.rendezvous_remote

        def job() -> None:
            removed = cleanup_task_worktree(
                self._workspace, repo, task, queue=queue
            )
            if removed and remote and self._cfg.landing_mode.is_remote:
                # Best-effort: the land path prunes consumed WIP refs itself;
                # this catches refs orphaned by an abandoned/reset task.
                prune_rendezvous_branch(
                    self._workspace, repo, remote,
                    _wip_ref(task, queue, self._cfg.wip_ref_prefix),
                )

        return job

    def _task_local_branches(self, repo: str) -> list[tuple[str | None, str]]:
        """Parse ``task-local/<queue-slug>/<task>`` branches in a target repo
        into (queue, task) pairs (read-only; branch names are unambiguous
        where worktree dir names are not)."""
        res = GitRunner(self._workspace / repo).run(
            "for-each-ref", "--format=%(refname:short)", "refs/heads/task-local/"
        )
        if not res.ok:
            return []
        out: list[tuple[str | None, str]] = []
        for line in res.stdout.splitlines():
            parts = line.strip().split("/", 2)
            if len(parts) != 3 or parts[0] != "task-local":
                continue
            qslug, task = parts[1], parts[2]
            out.append((None if qslug == "main" else qslug, task))
        return out
