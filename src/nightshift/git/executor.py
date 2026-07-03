"""Per-repo git executor — serialization by topology (Phase 7).

One :class:`RepoExecutor` per (workspace, repo): a single daemon thread
draining a FIFO job queue, so every mutation of one target repo is strictly
serialized while different repos proceed concurrently. The executor acquires
the repo's :class:`~nightshift.git.locks.RepoLock` around each job, which
means jobs call the ``*_locked`` pipeline variants directly — the in-process
half of the lock is now owned by exactly one thread per repo, and the
cross-process ``flock`` layer stays solely as the CLI/legacy guard.

Jobs are plain callables; results (or exceptions) are delivered on a
:class:`concurrent.futures.Future`, which asyncio callers bridge with
``asyncio.wrap_future``. A job that raises never kills the worker thread —
the exception rides the future.

:class:`ExecutorPool` is the app-owned registry (``app.state.git_executors``):
``submit(repo, fn)`` routes to (lazily starting) the repo's executor;
``drain()`` blocks until every queued job has finished (the deterministic test
seam for the async land path); ``shutdown()`` drains and stops the threads.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any, TypeVar

from nightshift.git.locks import repo_lock


T = TypeVar("T")


class RepoExecutor:
    """One serialized worker thread for one (workspace, repo) target repo."""

    def __init__(self, workspace: Path, repo: str) -> None:
        self._workspace = workspace
        self._repo = repo
        self._queue: queue.Queue[tuple[Callable[[], Any], Future[Any]] | None] = (
            queue.Queue()
        )
        self._thread: threading.Thread | None = None
        self._start_mutex = threading.Lock()
        self._stopped = False

    def submit(self, fn: Callable[[], T]) -> Future[T]:
        """Enqueue ``fn`` for serialized execution; the returned future carries
        its result or exception."""
        future: Future[T] = Future()
        with self._start_mutex:
            if self._stopped:
                raise RuntimeError(f"RepoExecutor({self._repo}) is shut down")
            self._ensure_thread()
            self._queue.put((fn, future))
        return future

    def drain(self) -> None:
        """Block until every job enqueued so far has finished."""
        self._queue.join()

    def shutdown(self) -> None:
        """Finish the queued jobs, then stop the thread. Idempotent."""
        with self._start_mutex:
            if self._stopped:
                return
            self._stopped = True
            thread = self._thread
            if thread is None:
                return
            self._queue.put(None)
        thread.join()

    def _ensure_thread(self) -> None:
        # Caller holds _start_mutex. Daemon: an abandoned executor (an app
        # torn down without lifespan) must never block interpreter exit.
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run,
                name=f"repo-executor-{self._repo}",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            fn, future = item
            try:
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    with repo_lock(self._workspace, self._repo):
                        result = fn()
                except BaseException as exc:  # noqa: BLE001 — delivered on the future
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._queue.task_done()


class ExecutorPool:
    """App-owned registry of :class:`RepoExecutor` instances, keyed by repo
    name within one workspace. All git mutation in the manager routes through
    ``submit`` — the "only the repo executor mutates a target repo" invariant.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)
        self._executors: dict[str, RepoExecutor] = {}
        self._mutex = threading.Lock()

    def executor(self, repo: str) -> RepoExecutor:
        with self._mutex:
            ex = self._executors.get(repo)
            if ex is None:
                ex = RepoExecutor(self._workspace, repo)
                self._executors[repo] = ex
            return ex

    def submit(self, repo: str, fn: Callable[[], T]) -> Future[T]:
        return self.executor(repo).submit(fn)

    def drain(self) -> None:
        """Block until every executor's queue is empty (test seam for the
        async land path). Must be called from a thread that is NOT the app's
        event loop — a queued job may be waiting to schedule its completion
        coroutine there."""
        for ex in self._snapshot():
            ex.drain()

    def shutdown(self) -> None:
        """Drain and stop every executor. Called from the app lifespan via
        ``asyncio.to_thread`` so in-flight completion callbacks can still
        reach the (free) event loop while we wait."""
        for ex in self._snapshot():
            ex.shutdown()

    def _snapshot(self) -> list[RepoExecutor]:
        with self._mutex:
            return list(self._executors.values())
