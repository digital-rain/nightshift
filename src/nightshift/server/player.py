"""Per-queue runners + a registry that owns one runner per active queue.

Each :class:`QueueRunner` drives one queue's play-throughs with transport
control (the "MP3 player" semantics layered on a single play-through):

- transport modes: ``oneshot`` (one task), ``auto`` (whole queue once),
  ``repeat`` (loop the queue every ``repeat_interval``);
- a play cursor (select a track to start from);
- pause / resume / stop / skip.

A runner is fixed to its queue for its lifetime — focus (which queue the UI is
viewing) is a registry concern, never a runner one. :class:`PlayerRegistry`
owns the runners, a shared :class:`ConcurrencyGate` that caps simultaneously
running workers across queues, and the UI's focused-queue pointer. It exposes a
back-compatible single-context facade (``state``/``store``/transport) that
follows an active run when one exists, so the existing API/UI keep working until
Phase 3 generalises them to per-queue surfaces.

Live state is reflected on disk through the engine's run records, which the
server tails for SSE; runners only track the lightweight player-state summary
(idle / playing / paused, mode, now-playing, cursor).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from nightshift import playlists, repos
from nightshift.events import (
    RUN_FINISHED,
    RUN_STARTED,
    TASK_RESULT,
    TASK_STARTED,
    Event,
    RunStore,
    fan_out,
)
from nightshift.preflight import enough_free_disk
from nightshift.runner_legacy import Controller, resolve_task, run_queue
from nightshift.server.settings import load_settings, parse_duration
from nightshift.slack import listener_for_queue
from nightshift.spawn_daily import load_config, load_queue_config, resolve_config
from nightshift.task_files import build_task_list, read_task


def resolve_tasks_repo(workspace: Path) -> str:
    """Name of the content-store repo (``tasks_root = workspace / tasks_repo``).

    Read from ``<workspace>/config.json`` (key ``tasks_repo``), with the
    ``NIGHTSHIFT_TASKS_REPO`` env var winning and
    :data:`nightshift.repos.DEFAULT_TASKS_REPO` as the fallback — matching the
    manager/worker resolution order so every entry point names the same store.
    """
    env = os.environ.get("NIGHTSHIFT_TASKS_REPO")
    if env:
        return env
    try:
        cfg = load_config(workspace)
    except (FileNotFoundError, ValueError, OSError):
        cfg = {}
    name = cfg.get("tasks_repo") if isinstance(cfg, dict) else None
    return str(name or repos.DEFAULT_TASKS_REPO)


def resolve_tasks_root(workspace: Path) -> Path:
    """The content store ``<workspace>/<tasks_repo>`` — briefs + queue config."""
    return workspace / resolve_tasks_repo(workspace)


class ConcurrencyGate:
    """Caps the number of simultaneously-running workers (tasks) across every
    queue runner. The limit is read live on each acquire (via ``limit_fn``) so a
    Settings change takes effect for the next task without a restart."""

    def __init__(self, limit_fn: Callable[[], int]) -> None:
        self._cond = threading.Condition()
        self._active = 0
        self._limit_fn = limit_fn

    @contextlib.contextmanager
    def slot(self) -> Iterator[None]:
        with self._cond:
            while self._active >= max(1, self._limit_fn()):
                self._cond.wait()
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify()

    def active(self) -> int:
        with self._cond:
            return self._active


class QueueRunner:
    """Drives a single queue's play-throughs with transport control.

    Constructed with an explicit ``queue`` (``None`` = the main queue) and
    pinned to it for its lifetime: its run store, tasks dir, and every
    ``run_task``/``squash``/``resolve`` call use that queue. The runner threads
    the two roots — ``workspace`` (where git ops + worktrees live, per task) and
    ``tasks_root`` (the content store holding briefs + queue config + the
    gitignored ``runs/``) — into every engine call. Multiple runners run
    independently; a shared :class:`ConcurrencyGate` bounds total workers."""

    def __init__(
        self,
        workspace: Path,
        tasks_root: Path,
        queue: str | None,
        gate: ConcurrencyGate,
    ) -> None:
        self.workspace = workspace
        self.tasks_root = tasks_root
        self.queue = queue  # None = main queue, else a playlist name.
        self._gate = gate
        # Run records live under the queue's gitignored ``runs/`` in the content
        # store (never committed), so the store is rooted at ``tasks_root``.
        self.store = RunStore(tasks_root, playlists.runs_rel(queue))
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._controller: Controller | None = None
        self._state = "idle"  # idle | playing | paused
        self._mode = "auto"
        self._now_playing: str | None = None
        self._cursor: str | None = None
        self._current_run_id: str | None = None

    @property
    def _tasks_rel(self) -> str:
        return playlists.tasks_rel(self.queue)

    def tasks_rel(self) -> str:
        return self._tasks_rel

    # ----- introspection ----------------------------------------------- #

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "mode": self._mode,
                "now_playing": self._now_playing,
                "cursor": self._cursor,
                "run_id": self._current_run_id,
                # This runner's own queue, and whether it is the running queue.
                "active_playlist": self.queue,
                "running_playlist": self.queue if self._running() else None,
            }

    def running(self) -> bool:
        with self._lock:
            return self._running()

    def _running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def now_playing(self) -> str | None:
        with self._lock:
            return self._now_playing if self._running() else None

    def run_id(self) -> str | None:
        with self._lock:
            return self._current_run_id if self._running() else None

    # ----- transport --------------------------------------------------- #

    def select(self, task: str | None) -> None:
        """Move the play cursor without starting playback."""
        with self._lock:
            self._cursor = task

    def play(self, mode: str | None = None, task: str | None = None) -> None:
        """Start (or resume) playback of this runner's queue."""
        with self._lock:
            if self._state == "paused" and self._controller is not None:
                self._controller.resume()
                self._state = "playing"
                return
            if self._running():
                return
            if mode:
                self._mode = mode
            if task is not None:
                self._cursor = task
            start_task = task if task is not None else self._cursor
            self._state = "playing"
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(self._mode, start_task),
                daemon=True,
            )
            self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if self._controller is not None and self._state == "playing":
                self._controller.pause()
                self._state = "paused"

    def stop(self) -> None:
        """Stop playback immediately.

        Flips state to idle right away (so the UI reflects the stop even while
        the worker/validate winds down) and signals the controller — honoured
        mid-worker and mid-validate by the engine, and at the next task boundary
        otherwise. Safe to call when no run is active (e.g. repeat-interval
        sleep)."""
        with self._lock:
            if self._controller is not None:
                self._controller.stop()
            self._state = "idle"
            self._now_playing = None

    def skip(self) -> None:
        with self._lock:
            if self._controller is not None:
                self._controller.skip()

    # ----- resolve ----------------------------------------------------- #

    def resolve(self, run_id: str, task: str) -> dict[str, Any]:
        """Resolve a task that validated but failed to land on ``main``.

        Runs as a tracked job on this runner's thread (serialised against its
        playback, shown in Now/History with a live log + ``resolve`` phase). The
        engine diagnoses first — a cheap re-squash for a transient blocker,
        otherwise an agent rebases onto ``main`` and resolves the conflicts.
        Returns immediately; progress streams over SSE."""
        with self._lock:
            if self._running():
                return {"ok": False, "error": "a run is in progress — stop it first"}
            rec = self.store.find_task(run_id, task)
            if rec is None:
                return {"ok": False, "error": "task not found in this run"}
            # The resolve runs git ops in the task's target repo, so resolve it
            # (task override → queue default) up front. A missing repo binding is
            # an authoring error reported synchronously rather than crashing the
            # job thread.
            repo = self._resolve_repo(task)
            if repo is None:
                return {
                    "ok": False,
                    "error": "no target repo configured for this queue",
                }
            title = rec.get("title") or task
            self._state = "playing"
            self._now_playing = task
            self._thread = threading.Thread(
                target=self._resolve_loop,
                args=(run_id, task, title, repo),
                daemon=True,
            )
            self._thread.start()
        return {"ok": True, "started": True, "task": task}

    def _resolve_repo(self, task: str) -> str | None:
        """Resolve the target repo for ``task`` (frontmatter override → queue
        default). Returns ``None`` when no repo is configured (an authoring
        error the caller surfaces) instead of raising."""
        tasks_rel = self._tasks_rel
        task_repo: str | None = None
        if (self.tasks_root / tasks_rel / f"{task}.md").is_file():
            try:
                task_repo = read_task(self.tasks_root, task, tasks_rel)[
                    "frontmatter_raw"
                ].get("repo")
            except (FileNotFoundError, ValueError, KeyError):
                task_repo = None
        try:
            return repos.resolve_repo(
                task_repo, load_queue_config(self.tasks_root, tasks_rel).get("repo")
            )
        except repos.RepoConfigError:
            return None

    def _resolve_loop(
        self, origin_run_id: str, task: str, title: str, repo: str
    ) -> None:
        try:
            controller = Controller()
            with self._lock:
                self._controller = controller
            run_store = self.store
            writer = run_store.start(launched_by="resolve", playlist=self.queue)
            with self._lock:
                self._current_run_id = writer.run_id
            backend_name = load_settings(self.workspace).get("worker_backend")
            slack_listener = listener_for_queue(
                self.workspace, self.tasks_root, tasks_rel=self._tasks_rel, queue=self.queue
            )
            emit = fan_out([writer.emit, self._make_tracker(), slack_listener])
            emit(Event(RUN_STARTED, {"run_id": writer.run_id, "tasks": [task]}))
            try:
                result = resolve_task(
                    self.workspace,
                    repo,
                    self.tasks_root,
                    task,
                    title,
                    emit=emit,
                    config=resolve_config(
                        self.workspace, self.tasks_root, self._tasks_rel
                    ),
                    backend_name=backend_name,
                    abort_reason=controller.abort_reason,
                    queue=self.queue,
                )
            finally:
                emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
                writer.close()
            # Reflect a successful landing on the original run's record too, so
            # the task that failed there now reads as completed in History.
            if result.success and origin_run_id != writer.run_id:
                run_store.append_task_result(
                    origin_run_id,
                    task,
                    status="completed",
                    result_line=result.result_line
                    or f"resolved: landed ({result.commit_sha})",
                    commit_sha=result.commit_sha,
                    loc=result.loc,
                    recoverable=False,
                )
        finally:
            with self._lock:
                self._state = "idle"
                self._now_playing = None
                self._controller = None
                self._current_run_id = None

    # ----- run loop ---------------------------------------------------- #

    def _build_tasks(self, mode: str, start_task: str | None) -> list[str]:
        tasks_rel = self._tasks_rel
        if mode == "oneshot":
            target = start_task or self._cursor
            if not target:
                return []
            return build_task_list(self.tasks_root, target, tasks_rel)
        full = build_task_list(self.tasks_root, "all", tasks_rel)
        if start_task and start_task in full:
            return full[full.index(start_task):]
        return full

    def _make_tracker(self) -> Callable[[Event], None]:
        def track(event: Event) -> None:
            if event.type == TASK_STARTED:
                with self._lock:
                    self._now_playing = event.payload.get("task")
            elif event.type == TASK_RESULT:
                with self._lock:
                    if self._now_playing == event.payload.get("task"):
                        self._now_playing = None
        return track

    def _disk_admit(self) -> str | None:
        """Disk admission for the governor: ``None`` to admit, else a message.

        Keeps the server from thrashing a too-full tree with new worktrees; the
        run pauses with a clear ``disk`` failure instead."""
        if enough_free_disk(self.workspace):
            return None
        return (
            "insufficient free disk to start another task — free space and "
            "re-run this queue."
        )

    def _run_loop(self, mode: str, start_task: str | None) -> None:
        first = True
        try:
            while True:
                tasks = self._build_tasks(mode, start_task if first else None)
                first = False
                controller = Controller()
                with self._lock:
                    self._controller = controller
                tasks_rel = self._tasks_rel
                run_store = self.store
                writer = run_store.start(launched_by="ui", playlist=self.queue)
                with self._lock:
                    self._current_run_id = writer.run_id
                backend_name = load_settings(self.workspace).get("worker_backend")
                slack_listener = listener_for_queue(
                    self.workspace, self.tasks_root, tasks_rel=tasks_rel, queue=self.queue
                )
                try:
                    run_queue(
                        self.workspace,
                        self.tasks_root,
                        tasks,
                        listeners=[
                            writer.emit,
                            self._make_tracker(),
                            slack_listener,
                        ],
                        controller=controller,
                        run_id=writer.run_id,
                        backend_name=backend_name,
                        tasks_rel=tasks_rel,
                        # Queue/repeat runs follow the live queue so tasks added
                        # mid-run execute now; oneshot does not drain siblings.
                        follow_queue=(mode != "oneshot"),
                        # Governor: cap concurrent workers + gate on free disk.
                        task_slot=self._gate.slot,
                        admit_task=self._disk_admit,
                    )
                finally:
                    writer.close()

                if mode != "repeat" or controller.stopped:
                    break
                # repeat: wait the interval, but bail out promptly on stop.
                if not self._interruptible_sleep(self._repeat_seconds(), controller):
                    break
        finally:
            with self._lock:
                self._state = "idle"
                self._now_playing = None
                self._controller = None
                self._current_run_id = None

    def _repeat_seconds(self) -> int:
        try:
            return parse_duration(
                load_settings(self.workspace).get("repeat_interval", "30m")
            )
        except ValueError:
            return 1800

    @staticmethod
    def _interruptible_sleep(seconds: int, controller: Controller) -> bool:
        """Sleep up to ``seconds``; return False if stopped during the wait."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if controller.stopped:
                return False
            time.sleep(0.2)
        return not controller.stopped


def _queue_key(queue: str | None) -> str:
    """Stable map key for a queue: ``main`` for the main queue, else its name."""
    return queue or "main"


class PlayerRegistry:
    """Owns one :class:`QueueRunner` per active queue plus the shared concurrency
    governor, and tracks the UI's *focused* queue (which queue the screen shows).

    Focus never gates execution — any queue can run regardless of focus, and two
    queues can run at once (bounded by the gate). The single-context facade
    (``state``/``store``/transport/``resolve``) follows an active run when one
    exists so the existing API and UI keep working; Phase 3 adds the per-queue
    surfaces (``states`` etc.)."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        # The content store ``<workspace>/<tasks_repo>`` — briefs, queue config,
        # and the gitignored per-queue ``runs/`` all live here.
        self.tasks_root = resolve_tasks_root(workspace)
        self._lock = threading.RLock()
        self._runners: dict[str | None, QueueRunner] = {}
        self._focused: str | None = None
        self._gate = ConcurrencyGate(
            lambda: int(
                resolve_config(self.workspace, self.tasks_root).get(
                    "max_concurrent_queues", 2
                )
            )
        )

    # ----- runners ----------------------------------------------------- #

    def runner(self, queue: str | None) -> QueueRunner:
        with self._lock:
            r = self._runners.get(queue)
            if r is None:
                r = QueueRunner(
                    self.workspace, self.tasks_root, queue, self._gate
                )
                self._runners[queue] = r
            return r

    def _running_runners(self) -> list[QueueRunner]:
        with self._lock:
            return [r for r in self._runners.values() if r.running()]

    def _primary_running(self) -> QueueRunner | None:
        """The runner the single-context view should follow: a running one if
        any (so Now/SSE/guards track execution), else ``None``."""
        running = self._running_runners()
        return running[0] if running else None

    # ----- focus (UI-only) --------------------------------------------- #

    def active_playlist(self) -> str | None:
        with self._lock:
            return self._focused

    def set_active(self, playlist: str | None) -> dict[str, Any]:
        """Switch the *focused* queue (or back to main when ``None``).

        Always allowed, even mid-run: it only moves the UI's view/edit focus and
        never touches any runner, so a live play-through keeps draining its own
        queue."""
        with self._lock:
            self._focused = playlist
        return {"ok": True, "active_playlist": playlist}

    def rename_queue(self, old: str, new: str) -> None:
        """Forget any cached runner for ``old`` (its on-disk dir has moved under
        the new name, so a fresh runner is lazily recreated pointing at it) and
        carry the UI focus across when the renamed queue was focused.

        The caller renames the directory only after confirming the queue is not
        running, so no live thread is dropped here."""
        if not old or old == new:
            return
        with self._lock:
            self._runners.pop(old, None)
            if self._focused == old:
                self._focused = new

    @property
    def store(self) -> RunStore:
        """The store the single-context view reads/browses: always the *focused*
        queue's (history follows focus). The live ``state()`` separately follows
        the running queue, mirroring the Phase 0 split where editing/history
        follow focus while Now follows the run."""
        return self.runner(self._focused).store

    def tasks_rel(self) -> str:
        return playlists.tasks_rel(self._focused)

    def running_playlist(self) -> str | None:
        running = self._primary_running()
        return running.queue if running is not None else None

    # ----- single-context facade (back-compat) ------------------------- #

    def state(self) -> dict[str, Any]:
        """Single-context state: follow an active run when one exists (so Now,
        SSE, and the edit guard track execution), else show the focused queue.
        ``active_playlist`` is always the focused queue."""
        with self._lock:
            focused = self._focused
        running = self._primary_running()
        runner = running if running is not None else self.runner(focused)
        st = runner.state()
        st["active_playlist"] = focused
        return st

    def select(self, task: str | None) -> None:
        self.runner(self._focused).select(task)

    def play(self, mode: str | None = None, task: str | None = None) -> None:
        self.runner(self._focused).play(mode=mode, task=task)

    def pause(self) -> None:
        runner = self._primary_running() or self.runner(self._focused)
        runner.pause()

    def stop(self) -> None:
        runner = self._primary_running() or self.runner(self._focused)
        runner.stop()

    def skip(self) -> None:
        runner = self._primary_running() or self.runner(self._focused)
        runner.skip()

    def resolve(self, run_id: str, task: str) -> dict[str, Any]:
        return self.runner(self._focused).resolve(run_id, task)

    # ----- multi-queue introspection (used by app for reconcile) ------- #

    def live_task(self, queue: str | None) -> str | None:
        """The now-playing task of ``queue``'s runner, or ``None`` when that
        queue isn't running. Queue-correct edit-guard input."""
        with self._lock:
            r = self._runners.get(queue)
        return r.now_playing() if r is not None else None

    def is_running(self, queue: str | None) -> bool:
        with self._lock:
            r = self._runners.get(queue)
        return r is not None and r.running()

    def active_run_ids(self) -> set[str]:
        """Every running runner's live run id — fed to per-store reconciliation
        so a healthy run in any queue is never aborted as stale."""
        ids: set[str] = set()
        for r in self._running_runners():
            rid = r.run_id()
            if rid:
                ids.add(rid)
        return ids

    def active_run_ids_by_queue(self) -> dict[str | None, set[str]]:
        """Per-queue live run ids, so each queue's store reconciles against only
        *its own* live run (a queue with no live run gets an empty set, which
        correctly reaps its phantom runs without touching another queue's)."""
        with self._lock:
            runners = dict(self._runners)
        out: dict[str | None, set[str]] = {}
        for queue, r in runners.items():
            rid = r.run_id()
            out[queue] = {rid} if rid else set()
        return out

    def active_runners(self) -> dict[str | None, QueueRunner]:
        """Runners with a live thread, keyed by queue — the SSE fan-out set."""
        with self._lock:
            return {q: r for q, r in self._runners.items() if r.running()}

    def store_for(self, queue: str | None) -> RunStore:
        """The run store for a specific queue (lazy-creates its runner)."""
        return self.runner(queue).store

    def states(self) -> dict[str, dict[str, Any]]:
        """Per-queue state map keyed by queue (``main``/playlist name) — the
        Phase 3 multi-queue surface. Always includes the focused queue so the UI
        has a card for what it is viewing even before that queue has run."""
        with self._lock:
            self.runner(self._focused)  # ensure focused queue is present
            runners = dict(self._runners)
        return {_queue_key(q): r.state() for q, r in runners.items()}


# Back-compat alias: the registry is the player the server constructs and the
# single-context tests drive. ``QueueRunner`` is the per-queue unit underneath.
Player = PlayerRegistry
