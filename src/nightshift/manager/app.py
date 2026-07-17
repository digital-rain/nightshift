"""Nightshift manager FastAPI app — shared wiring.

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

Phase 3 of the rebuild-in-place migration split the endpoint handlers into
:mod:`nightshift.manager.api_worker` and :mod:`nightshift.manager.api_operator`;
this module keeps the app factory, lifespan, shared state, and the helpers both
API surfaces need.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from nightshift import playlists as playlists_mod
from nightshift._paths import UI_DIR
from nightshift.config.manager import ManagerConfig, load_manager_config
from nightshift.events import new_run_id
from nightshift.git.executor import ExecutorPool
from nightshift.git.sync import SyncThrottle, sync_main_locked
from nightshift.lifecycle import RESOLVE_WORKER_ID, AttemptState, FailureKind
from nightshift.manager import failure_policy
from nightshift.manager.api_operator import register_operator_api
from nightshift.manager.api_worker import register_worker_api
from nightshift.manager.api_workflows import register_workflow_editor_api
from nightshift.manager.hub import Hub
from nightshift.manager.reconciler import Reconciler, reap_finished_resolves
from nightshift.manager.registry import Registry
from nightshift.manager.scheduler import SchedulerState
from nightshift.manager.store import NightshiftStore, open_store
from nightshift.spawn_daily import load_queue_config
from nightshift.workflows import load_workflows


# UI assets ship inside the package (see nightshift._paths.UI_DIR).


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

    # Phase 7: per-repo serialized git executors + the app-owned sync throttle.
    # Every git mutation in the manager (land, sync, resolved-push, adopt,
    # content-store commits) is a job on the target repo's executor.
    executors = ExecutorPool(workspace)
    sync_throttle = SyncThrottle()

    async def _origin_sync_loop() -> None:
        """Periodically refresh origin/main for every queue's target repo.

        Each repo is checked at most once per ``cadences.git_refresh_seconds``.
        A check fetches origin/main and fast-forwards local main only when the
        remote tip moved; otherwise the repo is left alone until the next
        interval. ``0`` disables the background loop (dispatch/land still refresh
        on their own throttle). The sync runs as a repo-executor job so it can
        never interleave with a land on the same repo."""
        interval = cfg.cadences.git_refresh_seconds
        remote = cfg.rendezvous_remote
        if not interval or interval <= 0 or not remote:
            return
        if not cfg.landing_mode.is_remote:
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
                    if not sync_throttle.due(workspace, target, interval):
                        continue
                    await asyncio.wrap_future(executors.submit(target, partial(
                        sync_main_locked,
                        workspace, target, remote,
                        min_interval_seconds=interval,
                        force=False,
                        throttle=sync_throttle,
                    )))
            await asyncio.sleep(interval)

    async def _drain_git_jobs() -> None:
        """Test seam / shutdown helper: block until every queued git job *and*
        every pending async-land completion has finished. Loops because a land
        completion itself submits content-store jobs."""
        while True:
            await asyncio.to_thread(executors.drain)
            pending = {t for t in app.state.land_completions if not t.done()}
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI):
        if app.state.store is None:
            app.state.store = await open_store(cfg.dsn)
        app.state.registry = Registry(
            app.state.store, stale_seconds=cfg.cadences.worker_stale_seconds
        )
        # Reconciler: startup pass first (park mid-land casualties, expire
        # deadlines, reconcile holds), then the periodic loop. The interval
        # reuses poll_seconds — the cadence these duties were noticed at when
        # they lived inline in worker_poll.
        await app.state.reconciler.startup()
        reconcile_task = asyncio.create_task(
            app.state.reconciler.run_forever(cfg.cadences.poll_seconds)
        )
        sync_task = asyncio.create_task(_origin_sync_loop())
        app.state.origin_sync_task = sync_task
        try:
            yield
        finally:
            sync_task.cancel()
            reconcile_task.cancel()
            # CancelledError is a BaseException (not Exception), so suppress it
            # explicitly alongside any teardown error from the loops.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sync_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reconcile_task
            # Drain rather than abandon: land jobs are bounded (push retries
            # cap them) and an abandoned mid-land job would need the startup
            # parking on next boot. A hard kill still recovers via that pass.
            with contextlib.suppress(Exception):
                await _drain_git_jobs()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(executors.shutdown)
            with contextlib.suppress(Exception):
                await app.state.store.close()

    app = FastAPI(title="Nightshift Manager", lifespan=_lifespan)

    app.state.workspace = workspace
    app.state.tasks_root = tasks_root
    app.state.tasks_repo = tasks_repo
    app.state.cfg = cfg
    app.state.hub = Hub()
    app.state.sched_state = SchedulerState()
    # Workflow definitions (spec §3): shipped assets shadowed by operator
    # ``.nightshift/workflows/*.json``. Loaded at startup — a malformed
    # definition or dangling prompt reference fails the load loudly
    # (WorkflowError propagates) — and hot-reloaded by the workflow-editor
    # API on every successful definition write/delete (editor spec §3.1).
    app.state.workflows = load_workflows(workspace)
    # One repo_unavailable warning per queue (deduped by queue key); cleared on
    # rescan so a re-cloned repo re-warns if it disappears again.
    app.state.repo_warnings = set()
    # Live out-of-process resolve subprocesses, keyed by their (child) run id:
    # {run_id: {"proc", "repo", "task", "queue", "origin_run_id"}}. Used to cap
    # concurrency per repo and to reap finished jobs.
    app.state.resolves = {}
    # Environment-failure cooldowns: (worker_id, queue label) -> expiry. A
    # cooled-down worker isn't offered that queue until expiry; other workers
    # still are. Deliberately in-memory (a restart clears a cooldown early —
    # harmless); queue pause/mode state is the durable piece (in the store).
    app.state.worker_cooldowns = {}
    app.state.store = store  # may be None until lifespan opens one
    app.state.git_executors = executors
    app.state.sync_throttle = sync_throttle
    # Pending async-land completion tasks (event-loop coroutines applying
    # on_land_result once the executor job finishes); tracked so the drain
    # seam and lifespan shutdown can await them.
    app.state.land_completions = set()
    app.state.drain_git_jobs = _drain_git_jobs

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
        await _broadcast(
            {
                "id": event_id,
                "kind": kind,
                "run_id": run_id,
                "queue": queue,
                "task": task,
                "payload": payload or {},
            }
        )

    async def _broadcast(event: dict[str, Any]) -> None:
        """Fan an already-persisted event out to all browsers (the outbox half
        of ``_emit`` — used for events committed inside ``apply_transition``)."""
        await app.state.hub.publish(event)

    # ----- queue helpers --------------------------------------------------- #

    def _queue_from_label(label: str | None) -> str | None:
        """Map a worker/UI queue label ('main' or playlist) to internal name."""
        if label in (None, "", "main"):
            return None
        return label

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
        reap_finished_resolves(app.state.resolves)
        if _active_resolves(repo) >= max(1, cfg.max_concurrent_resolves):
            return False, None, "a resolve is already running for this repo"
        store = _store()
        child_run_id = new_run_id()
        # Resolve children are RESOLVING attempts: never live (they must not
        # block re-dispatch of the task they repair) and deadline-less (the
        # subprocess reaper owns their lifetime, not the TTL sweep).
        await store.create_attempt(
            child_run_id,
            task=task,
            queue=queue,
            worker_id=RESOLVE_WORKER_ID,
            backend=cfg.raw.get("resolve_backend"),
            model=cfg.raw.get("resolve_model"),
            base_ref=None,
            ttl_seconds=0,
            title=title,
            repo=repo,
            state=AttemptState.RESOLVING,
        )
        started = app.state.spawn_resolve(
            child_run_id, task=task, queue=queue, repo=repo,
            title=title, origin_run_id=origin_run_id,
        )
        if not started:
            await store.update_attempt(
                child_run_id, state=AttemptState.FAILED,
                result_line="failed to launch resolver process",
                failure_kind=FailureKind.WORKER_LAUNCH,
            )
            return False, child_run_id, "failed to launch resolver process"
        await _emit(
            "run_started", run_id=child_run_id, queue=queue, task=task,
            payload={"task": task, "resolve": True},
        )
        return True, child_run_id, None

    _queue_failure_state: dict[str, failure_policy.QueueFailureState] = {}

    def _failure_state(label: str) -> failure_policy.QueueFailureState:
        return _queue_failure_state.setdefault(label, failure_policy.QueueFailureState())

    app.state.reconciler = Reconciler(
        workspace=workspace,
        tasks_root=tasks_root,
        cfg=cfg,
        store=_store,
        registry=_registry,
        emit=_emit,
        broadcast=_broadcast,
        queue_from_label=_queue_from_label,
        all_queues=_all_queues,
        executors=executors,
        resolves=app.state.resolves,
        repo_warnings=app.state.repo_warnings,
        sync_throttle=sync_throttle,
        tasks_repo=tasks_repo,
    )

    register_worker_api(
        app,
        cfg=cfg,
        workspace=workspace,
        tasks_root=tasks_root,
        tasks_repo=tasks_repo,
        _store=_store,
        _registry=_registry,
        _emit=_emit,
        _queue_from_label=_queue_from_label,
        _all_queues=_all_queues,
        _failure_state=_failure_state,
        _start_resolve=_start_resolve,
        _broadcast=_broadcast,
        _executors=executors,
        _sync_throttle=sync_throttle,
    )
    register_operator_api(
        app,
        cfg=cfg,
        workspace=workspace,
        tasks_root=tasks_root,
        tasks_repo=tasks_repo,
        _store=_store,
        _registry=_registry,
        _emit=_emit,
        _queue_from_label=_queue_from_label,
        _all_queues=_all_queues,
        _queue_failure_state=_queue_failure_state,
        _start_resolve=_start_resolve,
        _executors=executors,
    )
    register_workflow_editor_api(
        app,
        workspace=workspace,
        _emit=_emit,
    )

    # ----- static UI ------------------------------------------------------- #

    if UI_DIR.exists():
        app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")

    return app
