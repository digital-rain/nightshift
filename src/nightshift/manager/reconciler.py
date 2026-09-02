"""Reconciler — recovery and hygiene in one periodic loop (Phase 7).

One asyncio task (plus a startup pass) owns every "notice and repair" duty the
worker poll path used to perform inline, so the poll hot path becomes pure
reads (build candidates → pick → create the attempt → return):

1. **Deadline expiry** — every RUNNING attempt past its ``deadline_at`` gets
   an :func:`~nightshift.lifecycle.on_deadline` transition (CAS on RUNNING, so
   an attempt consumed concurrently is never double-expired). LANDING attempts
   are structurally exempt: once the land is enqueued nothing heartbeats the
   attempt anymore, and only the land job (or startup recovery) may consume it.
2. **Hold set/clear** — the ``no_capable_worker`` / bad-repo-reference
   ``blocked`` holds and the ``repo_unavailable`` pauses move here from
   ``worker_poll``, with the same dedup + one-warning-per-queue behavior. The
   reconciler also *clears* its own holds when they no longer apply (a capable
   worker checked in; the repo reappeared) — dispatch-time clearing remains as
   the fast path.
3. **GC** — terminal worktrees/branches: a ``task-local/*`` branch whose brief
   is gone, with no live attempt and no task hold, is provably abandoned (a
   conflicted land always leaves a hold; a queued brief is never GC'd), so its
   worktree+branch are torn down on the repo executor and, cross-machine, its
   consumed WIP ref is pruned best-effort. Finished resolve subprocesses are
   reaped from ``app.state.resolves``.
4. **Worker liveness** — silent workers are marked offline
   (``registry.reap_stale``), moved out of the poll path.
5. **Host-queue auto-import** — a queue bound to a repo's ``.tasks/<host>``
   inbox drains every fresh published brief per pass, on its own cadence, and
   removes each source from the repo's ``main``. See
   :mod:`nightshift.auto_import` for the FIFO and frontmatter rules.

The startup pass additionally *recovers* attempts interrupted mid-land (Phase
8): an attempt stuck in state LANDING can only be a previous process's
abandoned executor job (this process hasn't enqueued anything yet). The
recovery ladder, in order:

1. **Trailer check** — the land's squash commit carries the
   ``Nightshift-Attempt: <id>`` trailer; finding it on the target repo's main
   proves the land completed before the crash → the attempt is completed as
   LANDED (:func:`~nightshift.lifecycle.on_land_recovered`), nothing re-runs.
2. **Re-enqueue** — the task branch (or, cross-machine, the recorded
   ``branch_ref``) survived → the SAME land job is re-enqueued on the repo
   executor and its result applied via
   :func:`~nightshift.lifecycle.on_land_result` under a deliberately
   conservative policy (no watch arming, no queue pause, no auto-resolve).
   The trailer check having run first is the exactly-once guarantee: a re-run
   racing an already-pushed land squashes nothing new (empty squash →
   CONFLICT; the adopt path handles an advanced main).
3. **Park** — neither → conservatively errored (``merge_rejected``) with the
   task held blocked for a resolve
   (:func:`~nightshift.lifecycle.on_land_interrupted`), exactly the pre-Phase-8
   behavior.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from nightshift import playlists as playlists_mod
from nightshift import repos
from nightshift.auto_import import (
    auto_import_repos,
    imported_brief_text,
    pending_imports,
    resolve_host_queue,
)
from nightshift.ci import CiState, CiStatus, check_repo_ci
from nightshift.config.manager import ManagerConfig
from nightshift.git import GitRunner
from nightshift.git.executor import ExecutorPool
from nightshift.git.landing import find_landed_attempt
from nightshift.git.refs import branch_exists
from nightshift.git.store import commit_tasks
from nightshift.git.sync import SyncThrottle
from nightshift.git.transport import prune_rendezvous_branch, wip_ref
from nightshift.git.worktrees import cleanup_task_worktree, worktree_branch
from nightshift.lifecycle import (
    AttemptRef,
    AttemptState,
    Backoff,
    LandingMode,
    LandKind,
    LandOutcome,
    Outcome,
    RetryPolicy,
    RunStatus,
    SubmitPolicy,
    TaskHoldKind,
    Transition,
)
from nightshift.manager.landing import land_locked
from nightshift.manager.registry import Registry
from nightshift.manager.scheduler import (
    UNROUTABLE_REASON_PREFIXES,
    TaskCandidate,
    build_candidates,
    queue_label,
    unroutable,
)
from nightshift.manager.store import NightshiftStore
from nightshift.manager.work_orders import (
    is_ci_fix_task,
    open_ci_fix_briefs,
    task_meta,
)
from nightshift.queue_config import ci_monitoring_enabled
from nightshift.repo_tasks import (
    RepoTask,
    copy_repo_tasks,
    host_inbox,
    list_repo_task_queues,
    remove_repo_tasks_locked,
    scan_repo_inbox,
)
from nightshift.spawn_daily import (
    join_frontmatter,
    load_queue_config,
    resolve_config,
    slugify,
    split_frontmatter,
)
from nightshift.task_files import (
    create_task,
    drop_completed_task,
    frontmatter_held_tasks,
    task_is_evergreen,
)
from nightshift.transitions import (
    on_deadline,
    on_land_interrupted,
    on_land_recovered,
    on_land_result,
)


_log = logging.getLogger("nightshift.manager.reconciler")

# Appended to a trailer-recovered attempt's result line in PR mode: the local
# main CAS precedes ``open_pr`` in the PR pipeline, so the trailer proves the
# squash reached main but NOT that the PR was ever opened.
_PR_UNVERIFIED_NOTE = "; PR not verified — check the remote"


def reap_finished_resolves(resolves: dict[str, dict[str, Any]]) -> None:
    """Drop bookkeeping for resolve subprocesses that have exited. Shared with
    ``create_app``'s resolve spawner (which reaps before the per-repo cap)."""
    for rid in [rid for rid, r in resolves.items() if r["proc"].poll() is not None]:
        resolves.pop(rid, None)


def _tag_ci_resolution(
    tasks_root: Path, tasks_rel: str, task: str, repo: str
) -> None:
    """Add ``kind: ci_resolution`` + ``repo:`` to a freshly created brief's
    frontmatter. This is the tag the attempt record carries into History and
    the Stats page's CI-resolution category (Task 10).
    """
    path = tasks_root / tasks_rel / f"{task}.md"
    meta, body = split_frontmatter(path.read_text())
    meta["kind"] = "ci_resolution"
    meta["repo"] = repo
    path.write_text(join_frontmatter(meta, body))



# Consecutive failed `gh` calls tolerated before a repo's CI state degrades to
# UNKNOWN. Below this the last known state stands, so one timeout cannot churn
# a repo's holds.
_CI_TRANSIENT_TOLERANCE = 3


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
        # Phase 8: land recovery re-enqueues the same land job the submit path
        # runs, so it needs the app's sync throttle and the tasks-repo name
        # (brief drops are content-store jobs).
        sync_throttle: SyncThrottle,
        tasks_repo: str,
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
        self._sync_throttle = sync_throttle
        self._tasks_repo = tasks_repo
        # Per-repo wall-clock of the last gh check (cadences.ci_refresh_seconds).
        self._ci_checked_at: dict[str, float] = {}
        self._ci_transient_misses: dict[str, int] = {}
        # Per-destination-queue wall-clock of the last host-inbox check
        # (cadences.auto_import_seconds).
        self._auto_import_checked_at: dict[str, float] = {}
        # Tasks already announced as "green, but this fix is still in
        # flight" -- the green branch runs every tick, and the notice is
        # worth exactly one event per stale run, not one per poll.
        self._ci_fix_stale_announced: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ #
    # Entry points
    # ------------------------------------------------------------------ #

    async def startup(self) -> None:
        """The startup pass: recover mid-land casualties first (LANDING
        attempts must not be treated as ordinary deadline expiries), then one
        full pass. Isolated like a tick: recovery machinery failing must log
        loudly, not prevent the manager from booting.

        Deliberate trade-off: recovery runs synchronously inside lifespan
        startup — serially per interrupted attempt, with rung 2 running a
        full land job inline. Interrupted lands are rare (a crash inside the
        narrow enqueue→apply window) and bounded (at most one live attempt
        per task, invariant 1), and each attempt stays LANDING until its
        rung applies, so nothing else can touch it; briefly blocking boot is
        simpler and safer than racing the freshly-opened API against a
        background recovery task."""
        await self._run_duty("mid-land recovery", self._recover_interrupted_lands)
        await self.reconcile_once()

    async def run_forever(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            # Each duty is individually isolated inside reconcile_once; the
            # loop itself can only die by cancellation (lifespan shutdown).
            await self.reconcile_once()

    async def reconcile_once(self) -> None:
        await self._run_duty("deadline expiry", self._expire_deadlines)
        await self._run_duty("repo CI refresh", self._refresh_repo_ci)
        # Before hold set/clear so a brief pulled this tick is reconciled as a
        # candidate immediately rather than a full poll interval later.
        await self._run_duty("host queue auto-import", self._auto_import_host_queues)
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
    # Startup: interrupted lands (the Phase 8 recovery ladder)
    # ------------------------------------------------------------------ #

    async def _recover_interrupted_lands(self) -> None:
        store = self._store()
        for attempt in await store.live_attempts():
            if attempt.get("state") != AttemptState.LANDING:
                continue
            queue = self._queue_from_label(attempt.get("queue"))
            task = attempt["task"]
            ref = AttemptRef(id=attempt["id"], queue=queue, task=task)
            repo = attempt.get("repo")
            repo_ok = bool(repo) and repos.repo_available(self._workspace, repo)

            # 1. Trailer check: the squash carries `Nightshift-Attempt: <id>`;
            #    finding it on main proves the land completed before the crash.
            sha = None
            if repo_ok:
                sha = await asyncio.to_thread(
                    find_landed_attempt, self._workspace / repo, ref.id
                )
            if sha:
                await self._recover_landed(ref, sha)
                continue

            # 2. Re-enqueue: the local task branch survived, or (cross-machine)
            #    the recorded WIP ref can be re-fetched from the rendezvous.
            recoverable = repo_ok and (
                await asyncio.to_thread(
                    branch_exists,
                    self._workspace / repo,
                    worktree_branch(task, queue),
                )
                or bool(attempt.get("branch_ref") and self._cfg.rendezvous_remote)
            )
            if recoverable:
                await self._reenqueue_land(attempt, ref, repo)
                continue

            # 3. Park: neither provable nor re-runnable — the pre-Phase-8
            #    conservative error + blocked hold for a resolve.
            await self._apply_and_broadcast(
                on_land_interrupted(ref), expected=AttemptState.LANDING
            )

    def _effective_mode(self, task: str, queue: str | None) -> LandingMode:
        """The landing mode this task's land actually ran under (task meta
        ``make_pr`` overrides the configured default)."""
        meta = task_meta(self._tasks_root, task, queue)
        return LandingMode.PR if meta.get("make_pr") else self._cfg.landing_mode

    async def _recover_landed(self, ref: AttemptRef, sha: str) -> bool:
        """Complete a trailer-verified attempt as LANDED (+ brief drop for
        non-evergreen tasks). In PR mode the result line carries a caveat:
        the trailer proves the squash reached main, but the local CAS
        precedes ``open_pr``, so the PR itself may never have been opened."""
        task, queue = ref.task, ref.queue
        note = (
            _PR_UNVERIFIED_NOTE
            if self._effective_mode(task, queue) is LandingMode.PR
            else None
        )
        applied = await self._apply_and_broadcast(
            on_land_recovered(ref, sha, note=note),
            expected=AttemptState.LANDING,
        )
        if applied and not self._task_evergreen(task, queue):
            await self._drop_brief(task, queue)
        return applied

    async def _reenqueue_land(
        self, attempt: dict[str, Any], ref: AttemptRef, repo: str
    ) -> None:
        """Re-run the interrupted land job (same args as the submit path,
        reconstructed from the attempt row + task meta) and apply its result
        with the same ``on_land_result`` transition — under a deliberately
        conservative policy: no watch arming, no queue pause, no auto-resolve,
        no split (recovery never escalates). The in-memory watch/pause effects
        the transition might carry are ignored here (fresh process, nothing
        armed); the transactional effects (hold, counter) apply as usual."""
        task, queue = ref.task, ref.queue
        meta = task_meta(self._tasks_root, task, queue)
        effective_mode = (
            LandingMode.PR if meta.get("make_pr") else self._cfg.landing_mode
        )
        title = attempt.get("title") or task
        try:
            result = await asyncio.wrap_future(self._executors.submit(repo, partial(
                land_locked,
                self._workspace, repo, task, title,
                queue=queue,
                base_ref=attempt.get("base_ref"),
                landing_mode=effective_mode,
                automerge=bool(meta.get("automerge", True)),
                draft=bool(meta.get("draft", False)),
                branch_ref=attempt.get("branch_ref"),
                head_sha=attempt.get("head_sha"),
                rendezvous_remote=self._cfg.rendezvous_remote,
                git_refresh_seconds=self._cfg.cadences.git_refresh_seconds,
                throttle=self._sync_throttle,
                attempt_id=ref.id,
            )))
        except Exception as exc:
            # Same terminal shape a crashed submit-path land job maps to.
            result = LandOutcome(
                kind=LandKind.TRANSPORT_FAILED,
                detail=f"recovery land job crashed: {exc}",
            )
        if result.kind is LandKind.CONFLICT:
            # Crash-window re-check: integrate_and_push pushes origin BEFORE
            # the local main CAS, so dying between the two leaves the trailer
            # on origin but not on local main — rung 1 misses it, and this
            # re-run's opening sync pulls the already-pushed squash, making
            # the re-squash empty (CONFLICT). Local main has just been synced
            # by the land job, so a second trailer scan now proves that
            # earlier push: recover as landed instead of parking a false
            # conflict.
            sha = await asyncio.to_thread(
                find_landed_attempt, self._workspace / repo, ref.id
            )
            if sha:
                await self._recover_landed(ref, sha)
                return
        task_row = await self._store().get_task_state(queue, task)
        policy = SubmitPolicy(
            retry=RetryPolicy(
                quarantine_after=self._cfg.quarantine_threshold,
                backoff=Backoff(base_seconds=self._cfg.retry_backoff_seconds),
            ),
            attempts_without_progress=(
                int(task_row["attempts_without_progress"]) if task_row else 0
            ),
            evergreen=self._task_evergreen(task, queue),
            pr_mode=effective_mode is LandingMode.PR,
        )
        # Synthetic completed outcome: the worker's original submit was lost
        # with the crash; model/validate_cmd/worktree echo the attempt row so
        # the terminal update doesn't clobber what dispatch recorded.
        outcome = Outcome(
            status=RunStatus.COMPLETED,
            landable=True,
            result_line="recovered: landed (manager restarted mid-land)",
            model=attempt.get("model"),
            validate_cmd=attempt.get("validate_cmd"),
            worktree=attempt.get("worktree"),
        )
        t = on_land_result(ref, outcome, result, policy)
        applied = await self._apply_and_broadcast(
            t, expected=AttemptState.LANDING
        )
        if applied and t.effects.drop_brief:
            await self._drop_brief(task, queue)

    def _task_evergreen(self, task: str, queue: str | None) -> bool:
        meta = task_meta(self._tasks_root, task, queue)
        return task_is_evergreen(
            meta, task,
            resolve_config(
                self._workspace, self._tasks_root, playlists_mod.tasks_rel(queue)
            ),
        )

    async def _drop_brief(self, task: str, queue: str | None) -> None:
        """Consume a landed task's brief (non-evergreen) — a content-store
        mutation, so a tasks-repo executor job. Best-effort like the submit
        path's backstop."""
        try:
            await asyncio.wrap_future(self._executors.submit(
                self._tasks_repo, partial(
                    drop_completed_task,
                    self._tasks_root, task, playlists_mod.tasks_rel(queue),
                    queue=queue,
                ),
            ))
        except Exception:
            _log.warning(
                "reconciler: brief drop failed for %s/%s",
                queue_label(queue), task, exc_info=True,
            )

    # ------------------------------------------------------------------ #
    # 1. Deadline expiry
    # ------------------------------------------------------------------ #

    async def _expire_deadlines(self) -> None:
        store = self._store()
        now = datetime.now(UTC)
        for attempt in await store.live_attempts():
            # LANDING is structurally deadline-exempt: once the worker's
            # submit enqueued the land, nothing heartbeats the attempt anymore
            # — a slow (or queue-delayed) land could outlive the TTL while its
            # push may already have reached origin. Expiring it would make the
            # deferred on_land_result CAS fail, silently dropping a completed
            # land and re-dispatching the task. If the *process* dies
            # mid-land, the startup recovery pass owns exactly these attempts.
            if attempt.get("state") != AttemptState.RUNNING:
                continue
            deadline = attempt.get("deadline_at")
            if deadline is None or deadline >= now:
                continue
            ref = AttemptRef(
                id=attempt["id"],
                queue=self._queue_from_label(attempt.get("queue")),
                task=attempt["task"],
            )
            await self._apply_and_broadcast(
                on_deadline(ref), expected=AttemptState.RUNNING
            )

    async def _apply_and_broadcast(
        self, t: Transition, *, expected: AttemptState
    ) -> bool:
        """CAS-apply a reconciler transition (the attempt must still be in
        ``expected`` state — one consumed concurrently is never
        double-applied) and fan its committed events out like the submit path
        does. False on a lost CAS.

        Contract: unlike the submit path, the reconciler executes NO
        post-commit frontmatter effects. That's unreachable today by
        construction — the transitions applied here (deadline, recovery,
        interruption park, and the recovery ``on_land_result`` whose
        ``land_locked`` never returns NO_CHANGES and whose failure branch
        carries no flags) never produce them — so a non-empty
        ``effects.frontmatter`` is logged as dropped rather than crashing
        the duty (``_run_duty`` isolation is for unexpected errors, not
        deliberate contract gaps)."""
        if t.effects.frontmatter:
            _log.warning(
                "reconciler: dropping %d frontmatter effect(s) for %s — "
                "the reconciler has no frontmatter-write plumbing",
                len(t.effects.frontmatter), t.ref.id,
            )
        event_ids = await self._store().apply_transition(
            t, expected_status=expected
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
    # Repo CI refresh (the CI-state gate's input; runs before hold set/clear
    # so Task 6's holds are written against this tick's freshly-checked state)
    # ------------------------------------------------------------------ #

    def _monitored_repos(self) -> dict[str, set[str | None]]:
        """Repo -> the queues watching it. Two queues bound to one repo share a
        single gh check; a queue with monitoring off contributes nothing."""
        out: dict[str, set[str | None]] = {}
        for q in self._all_queues():
            tasks_rel = playlists_mod.tasks_rel(q)
            config = load_queue_config(self._tasks_root, tasks_rel)
            if not ci_monitoring_enabled(config):
                continue
            repo = config.get("repo")
            if not repo or not repos.repo_available(self._workspace, repo):
                continue
            out.setdefault(repo, set()).add(q)
        return out

    async def _refresh_repo_ci(self) -> None:
        """Refresh GitHub Actions state for every monitored repo.

        Throttled per repo by ``cadences.ci_refresh_seconds``. A workspace with
        no monitored queue makes no subprocess call at all, so a host without
        ``gh`` never pays for the feature. Only a *state change* emits an event.
        """
        monitored = self._monitored_repos()
        if not monitored:
            return
        store = self._store()
        interval = float(self._cfg.cadences.ci_refresh_seconds or 0)
        now = time.time()
        for repo in sorted(monitored):
            last = self._ci_checked_at.get(repo)
            if last is not None and interval > 0 and (now - last) < interval:
                continue
            self._ci_checked_at[repo] = now
            status = await asyncio.to_thread(
                check_repo_ci, repos.repo_root(self._workspace, repo)
            )
            recorded = (await store.repo_ci()).get(repo) or {}
            # Two kinds of non-answer are held rather than written, because
            # writing them would clear the repo's holds and its fix marker on
            # every blip, briefly re-admitting a red repo's tasks:
            #  - gh itself failed (status.transient) -- a timeout or a dead
            #    network says nothing about the branch;
            #  - the list API returned no runs for a sha we already hold a
            #    completed verdict for. Runs cannot un-run, so an empty answer
            #    at the same sha is GitHub list lag (observed live during a
            #    re-run), not a new state.
            # Either way the last known state stands until the miss has
            # repeated _CI_TRANSIENT_TOLERANCE times, then the fresh answer is
            # written through (still fail-open; a deliberately deleted run
            # eventually wins).
            suspect = status.transient or (
                status.no_runs_at_tip
                and recorded.get("head_sha") == status.head_sha
                and recorded.get("state") in (str(CiState.RED), str(CiState.GREEN))
            )
            if suspect:
                misses = self._ci_transient_misses.get(repo, 0) + 1
                self._ci_transient_misses[repo] = misses
                if misses < _CI_TRANSIENT_TOLERANCE:
                    continue
            else:
                self._ci_transient_misses.pop(repo, None)
            previous = await store.set_repo_ci(
                repo,
                state=str(status.state),
                head_sha=status.head_sha,
                url=status.url,
                detail=status.detail,
            )
            # A green main retires the auto-spawned fix work, and does so on
            # every green tick rather than only on the red->green edge: a
            # manager that was down while the repo went green would otherwise
            # never see the edge, and the stale brief would dispatch on the
            # next poll. Idempotent -- no outstanding brief is a no-op.
            if status.state is CiState.GREEN:
                await self._retire_ci_fixes(repo, queues=monitored[repo])
            if previous == str(status.state):
                # No transition, so nothing to announce -- but a still-red repo
                # whose fix brief has been deleted (operator tidy-up, a failed
                # run cleaned away) would otherwise stay red forever with
                # nothing queued to fix it: the marker still names a brief, so
                # the dedupe suppresses a respawn, and only a state change
                # clears the marker. Re-file it.
                if status.state is CiState.RED:
                    if self._ci_fix_brief_missing(
                        recorded.get("fix_task"), monitored[repo]
                    ):
                        await store.set_repo_ci_fix(repo, fix_task="", fix_sha="")
                        await self._spawn_ci_fix(
                            repo, status, queues=monitored[repo]
                        )
                continue
            await self._emit(
                "repo_ci",
                payload={
                    "repo": repo,
                    "state": str(status.state),
                    "previous": previous,
                    "head_sha": status.head_sha,
                    "url": status.url,
                    "detail": status.detail,
                },
            )
            if status.state is CiState.RED:
                await self._spawn_ci_fix(repo, status, queues=monitored[repo])

    def _ci_fix_brief_missing(
        self, fix_task: str | None, queues: set[str | None]
    ) -> bool:
        """True when the recorded fix brief is no longer on disk.

        The marker is bookkeeping; the brief is the work. If the brief is gone
        the marker is stale, and leaving it in place strands a red repo with no
        queued fix.
        """
        if not fix_task:
            return False
        return not any(
            (self._tasks_root / playlists_mod.tasks_rel(q) / f"{fix_task}.md").exists()
            for q in queues
        )

    async def _retire_ci_fixes(
        self, repo: str, *, queues: set[str | None]
    ) -> None:
        """Green main -> every outstanding CI-fix brief for this repo is moot.

        The gate spawns this work; the gate retires it. Without this the fix
        brief outlives the failure it names: one fix lands and turns main
        green, and a second brief filed against an earlier sha is still queued,
        so a worker picks it up and an agent goes looking for a CI failure that
        no longer exists.

        A brief with a live attempt is left alone -- stopping an agent
        mid-flight is the operator's call, not the reconciler's -- but it is
        announced once so a stale run is visible rather than silent.
        """
        outstanding = await asyncio.to_thread(
            open_ci_fix_briefs, self._tasks_root, repo, tuple(queues)
        )
        # Prune this repo's announcements down to what is still outstanding, so
        # a brief that comes back later is announced again.
        announced = self._ci_fix_stale_announced.setdefault(repo, set())
        announced &= {task for task, _ in outstanding}
        if not outstanding:
            return
        live = {a["task"] for a in await self._store().live_attempts()}
        for task, queue in outstanding:
            if task in live:
                if task in announced:
                    continue
                announced.add(task)
                _log.info(
                    "reconciler: %s main is green but %s is still in flight; "
                    "leaving the running attempt alone", repo, task,
                )
                await self._emit(
                    "repo_ci_fix_retired", queue=queue, task=task,
                    payload={"repo": repo, "dropped": False,
                             "reason": "attempt in flight"},
                )
                continue
            await self._drop_brief(task, queue)
            announced.discard(task)
            await self._emit(
                "repo_ci_fix_retired", queue=queue, task=task,
                payload={"repo": repo, "dropped": True},
            )

    async def _spawn_ci_fix(
        self, repo: str, status: CiStatus, *, queues: set[str | None]
    ) -> None:
        """Queue one CI-resolution task for a red main, deduped on the sha.

        The task goes into the monitored queue itself — that queue is already
        bound to this repo, so its validate command and worker routing are the
        right ones. A *paused* queue is a last resort: spawning into one closes
        the gate with no dispatchable fix, which is the deadlock this task
        exists to avoid.

        Idempotent. ``set_repo_ci`` clears the fix marker on any state change,
        so this is re-entered for a repo that goes red -> unknown -> red at the
        SAME sha (one ``gh`` timeout is enough) with the marker already NULL.
        The title is deterministic per sha, so ``create_task`` then collides:
        that collision means "the brief for this sha already exists", and the
        marker is re-adopted rather than dropped — without it the existing
        brief loses its by-name exemption, is held ``ci_red`` itself, and
        nothing can ever turn the repo green again.
        """
        store = self._store()
        # One open fix per repo. The marker is keyed on the failing sha and is
        # cleared by every state change, so a repo that goes red -> pending
        # (main moved) -> red at a NEW sha arrives here with an empty marker
        # while the previous fix brief is still queued or running -- and would
        # spawn a second agent onto the same main. The failure is a property of
        # the branch, not of a sha: adopt the open brief instead. This
        # serializes CI fixes per repo, which is what we want anyway (two fix
        # agents landing on one main collide); a breakage the running fix does
        # not address is picked up by the next poll once its brief is gone.
        open_fixes = await asyncio.to_thread(
            open_ci_fix_briefs, self._tasks_root, repo, tuple(queues)
        )
        if open_fixes:
            existing = open_fixes[0][0]
            _log.info(
                "reconciler: %s already has an open ci fix brief (%s); "
                "adopting it instead of spawning for %s",
                repo, existing, (status.head_sha or "unknown")[:8],
            )
            await store.set_repo_ci_fix(
                repo, fix_task=existing, fix_sha=status.head_sha or ""
            )
            return
        pauses = await store.queue_pauses()
        ordered = sorted(queues, key=lambda q: (q is not None, q or ""))
        unpaused = [q for q in ordered if queue_label(q) not in pauses]
        target = (unpaused or ordered)[0]
        tasks_rel = playlists_mod.tasks_rel(target)
        short = (status.head_sha or "unknown")[:8]
        title = f"fix ci: {repo} main is red at {short}"
        body = (
            f"/fix CI is failing on `{repo}` `main`.\n\n"
            f"- **Failing commit:** `{status.head_sha or 'unknown'}`\n"
            f"- **Detail:** {status.detail or 'no detail reported'}\n"
            f"- **Run:** {status.url or 'no run URL reported'}\n\n"
            "Reproduce the failure locally, find the root cause, fix it, and "
            "verify against the same check that failed. Nightshift is holding "
            "this queue's other tasks until `main` is green again.\n"
        )
        try:
            created = await asyncio.to_thread(
                create_task, self._tasks_root, title, body, tasks_rel
            )
        except FileExistsError as exc:
            # The brief for this sha is already on disk (``create_task`` raises
            # with the stem it refused). Re-adopt it: the marker, not the file,
            # is what carries the exemption.
            existing = str(exc.args[0]) if exc.args else slugify(title)
            _log.info(
                "reconciler: re-adopting existing ci fix brief %s for %s",
                existing, repo,
            )
            await store.set_repo_ci_fix(
                repo, fix_task=existing, fix_sha=status.head_sha or ""
            )
            return
        except ValueError:
            _log.warning(
                "reconciler: could not create a ci fix brief for %s", repo,
                exc_info=True,
            )
            return
        # Tag the brief so History and the Stats page can categorise its runs.
        await asyncio.to_thread(
            _tag_ci_resolution, self._tasks_root, tasks_rel, created["task"], repo
        )
        await store.set_repo_ci_fix(
            repo, fix_task=created["task"], fix_sha=status.head_sha or ""
        )
        await self._emit(
            "repo_ci_fix_spawned",
            queue=target,
            task=created["task"],
            payload={"repo": repo, "head_sha": status.head_sha},
        )

    # ------------------------------------------------------------------ #
    # Host-queue auto-import (see nightshift.auto_import)
    # ------------------------------------------------------------------ #

    async def _auto_import_host_queues(self) -> None:
        """Drain each bound queue's ``.tasks/<host>`` inbox into the queue.

        Throttled per destination queue by ``cadences.auto_import_seconds``,
        and short-circuited when no repo has the switch on — a workspace not
        using the feature makes no git call for it. Every open pass imports
        *all* the fresh briefs the inbox publishes, exactly as a manual import
        would, appending them to the end of the queue's execution order in
        their published order; the cadence makes it a standing drain, so
        briefs published between passes are picked up by the next one.

        A published quarantine is refused outright: the brief stays in the
        host queue, re-skipped every pass, until the publisher clears or
        removes it. The pending map filters the scan, so a source whose
        removal failed after its copy landed is not imported twice.
        """
        enabled = set(auto_import_repos(self._tasks_root))
        if not enabled:
            return
        interval = float(self._cfg.cadences.auto_import_seconds or 0)
        now = time.time()
        for queue in self._all_queues():
            label = queue_label(queue)
            tasks_rel = playlists_mod.tasks_rel(queue)
            config = load_queue_config(self._tasks_root, tasks_rel)
            repo = config.get("repo")
            if not repo or repo not in enabled:
                continue
            last = self._auto_import_checked_at.get(label)
            if last is not None and interval > 0 and (now - last) < interval:
                continue
            if not repos.repo_available(self._workspace, repo):
                continue
            self._auto_import_checked_at[label] = now
            available = await asyncio.to_thread(
                list_repo_task_queues, self._workspace, repo
            )
            host = resolve_host_queue(config, label, available=available)
            if host is None:
                continue
            entries = await asyncio.to_thread(
                scan_repo_inbox,
                self._workspace, repo, host_inbox(host),
                self._tasks_root, tasks_rel,
            )
            if not entries:
                # The steady state: a drained inbox. Don't pay the destination
                # queue's frontmatter scan just to filter an empty list.
                continue
            # Replay guard: a source already imported into this queue is still
            # published only because its removal failed, so skip past it rather
            # than running the same brief twice. A published quarantine is the
            # publisher's hold — skipped and left published.
            pending = pending_imports(self._tasks_root, tasks_rel)
            fresh = [
                e for e in entries
                if not e.quarantined and f"{repo}/{e.source}" not in pending
            ]
            if fresh:
                await self._auto_import_batch(queue, repo, host, fresh)

    async def _auto_import_batch(
        self, queue: str | None, repo: str, host: str, entries: list[RepoTask]
    ) -> None:
        """Move the pass's fresh briefs into the queue: copy them all via the
        manual import's own :func:`copy_repo_tasks` + commit the content store
        once (the briefs are durable from there), then remove every source
        from the repo's ``main`` in one repo-executor job — the manual
        import's copy-then-remove shape, on the importer's clock.

        A brief whose exact text is already in the destination is not copied
        again — the operator drained it by hand at some point — but its source
        is still removed, so the inbox converges instead of re-offering it
        every tick.
        """
        label = queue_label(queue)
        tasks_rel = playlists_mod.tasks_rel(queue)
        # Rewrite every brief up front (normalised frontmatter + provenance
        # stamp), then hand the batch to the manual import's own copy step:
        # one write pass, one order append, one collision policy.
        rewritten = [
            replace(entry, text=imported_brief_text(
                entry.text,
                stem=entry.name,
                repo=repo,
                source=entry.source,
                default_model=self._cfg.default_model,
            ))
            for entry in entries
        ]
        imported = await asyncio.to_thread(
            copy_repo_tasks, self._tasks_root, tasks_rel, rewritten
        )
        stems: dict[str, str | None] = dict.fromkeys(e.source for e in entries)
        stems.update(zip(
            (e.source for e in rewritten if not e.duplicate),
            (t["task"] for t in imported),
            strict=True,
        ))
        if imported:
            await self._commit_store(
                "nightshift: auto-import "
                f"{', '.join(t['task'] for t in imported)} from {repo}/{host}"
            )
        removal = await asyncio.wrap_future(self._executors.submit(repo, partial(
            remove_repo_tasks_locked,
            self._workspace,
            repo,
            [entry.source for entry in entries],
            f"nightshift: auto-import {len(entries)} brief(s) into queue {label}",
        )))
        if removal["warning"]:
            _log.warning("reconciler: auto-import: %s", removal["warning"])
        for entry in entries:
            await self._emit(
                "queue_changed",
                queue=queue,
                task=stems[entry.source],
                payload={
                    "auto_imported": stems[entry.source],
                    "repo": repo,
                    "host_queue": host,
                    "source": entry.source,
                    "removed": removal["removed"],
                    "warning": removal["warning"],
                },
            )

    async def _commit_store(self, message: str) -> None:
        """Commit content-store churn as a tasks-repo executor job, mirroring
        the operator API's ``_commit`` (the tasks repo is a repo like any
        other, so its commits serialize with every job targeting it)."""
        await asyncio.wrap_future(self._executors.submit(
            self._tasks_repo, partial(commit_tasks, self._tasks_root, message)
        ))

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

        monitored = self._monitored_repos()
        ci_rows = await store.repo_ci()
        red_repos = {
            repo for repo, row in ci_rows.items()
            if row.get("state") == "red" and repo in monitored
        }

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
                elif cand.repo and cand.repo in red_repos:
                    # The CI-resolution task must not hold itself: it is the one
                    # task that can turn the repo green, so it is exempt from
                    # the very gate it exists to clear — and from that gate
                    # ONLY: the branches above still apply to it.
                    if is_ci_fix_task(self._tasks_root, cand, ci_rows):
                        continue
                    existing = await store.get_task_state(cand.queue, cand.task)
                    # Never take over a hold this arm does not own: overwriting
                    # an operator-actionable `blocked` row NULLs its reason
                    # (breaking reset) and lets the green-side clear DELETE it,
                    # auto-releasing a task blocked for another reason. A row
                    # with no state is a bare retry counter, which owns no hold.
                    if (existing or {}).get("state") is None:
                        await store.set_task_state(
                            cand.queue, cand.task, TaskHoldKind.CI_RED,
                            repo=cand.repo,
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
        for row in await store.tasks_in_state(TaskHoldKind.CI_RED):
            repo = row.get("repo")
            if not repo or repo not in red_repos:
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
            (self._queue_from_label(a.get("queue")), a["task"])
            for a in await store.live_attempts()
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
                    wip_ref(task, queue, self._cfg.wip_ref_prefix),
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
