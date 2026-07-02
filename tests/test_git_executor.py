"""Phase 7: RepoExecutor / ExecutorPool unit tests.

The executor is the serialization-by-topology device: one worker thread per
(workspace, repo), all git mutation as queued jobs. These tests exercise the
machinery itself with plain callables — ordering, cross-repo concurrency,
drain semantics, exception delivery, lock acquisition, and lifecycle — no git
repos required (jobs are opaque to the executor).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from nightshift.git.executor import ExecutorPool, RepoExecutor
from nightshift.git.locks import repo_lock


_WAIT = 5.0  # generous upper bound; every wait is event-driven, not a sleep


def test_same_repo_jobs_run_serially(tmp_path: Path) -> None:
    """Two jobs on one repo never overlap: the second starts only after the
    first finishes, in submit order."""
    pool = ExecutorPool(tmp_path)
    log: list[str] = []
    first_started = threading.Event()
    release_first = threading.Event()

    def job_a() -> None:
        log.append("a-start")
        first_started.set()
        assert release_first.wait(_WAIT)
        log.append("a-end")

    def job_b() -> None:
        log.append("b")

    fut_a = pool.submit("r1", job_a)
    fut_b = pool.submit("r1", job_b)
    assert first_started.wait(_WAIT)
    # While job_a is parked mid-flight, job_b has not started.
    assert log == ["a-start"]
    assert not fut_b.done()
    release_first.set()
    fut_a.result(timeout=_WAIT)
    fut_b.result(timeout=_WAIT)
    assert log == ["a-start", "a-end", "b"]
    pool.shutdown()


def test_cross_repo_jobs_interleave(tmp_path: Path) -> None:
    """Different repos get different threads: a job on r1 can block on a job
    on r2 finishing — with one shared thread this would deadlock."""
    pool = ExecutorPool(tmp_path)
    r2_done = threading.Event()

    def job_r1() -> str:
        assert r2_done.wait(_WAIT), "r2's job never ran while r1's was blocked"
        return "r1"

    def job_r2() -> str:
        r2_done.set()
        return "r2"

    fut1 = pool.submit("r1", job_r1)
    fut2 = pool.submit("r2", job_r2)
    assert fut1.result(timeout=_WAIT) == "r1"
    assert fut2.result(timeout=_WAIT) == "r2"
    pool.shutdown()


def test_job_holds_the_repo_lock(tmp_path: Path) -> None:
    """The executor acquires the RepoLock around each job (jobs call the
    *_locked pipeline variants, which assert exactly this)."""
    pool = ExecutorPool(tmp_path)
    fut = pool.submit(
        "r1", lambda: repo_lock(tmp_path, "r1").is_held_by_current_thread()
    )
    assert fut.result(timeout=_WAIT) is True
    # And releases it between jobs: the main thread can take it afterwards.
    pool.drain()
    with repo_lock(tmp_path, "r1"):
        pass
    pool.shutdown()


def test_job_exception_rides_future_and_thread_survives(tmp_path: Path) -> None:
    boom = RuntimeError("land exploded")

    def bad_job() -> None:
        raise boom

    pool = ExecutorPool(tmp_path)
    fut = pool.submit("r1", bad_job)
    with pytest.raises(RuntimeError, match="land exploded"):
        fut.result(timeout=_WAIT)
    # The worker thread is still alive and serving the same repo.
    assert pool.submit("r1", lambda: 42).result(timeout=_WAIT) == 42
    pool.shutdown()


def test_drain_blocks_until_all_queued_jobs_finish(tmp_path: Path) -> None:
    """drain() returns only after every job enqueued so far has run: the
    drainer snapshots the completion log at the moment drain returned, and
    that snapshot must already contain all three jobs — including the gated
    one released *after* drain started (deterministic; no wall-clock waits)."""
    pool = ExecutorPool(tmp_path)
    done: list[str] = []
    gate = threading.Event()
    slow_started = threading.Event()

    def slow_job() -> None:
        slow_started.set()
        assert gate.wait(_WAIT)
        done.append("slow")

    pool.submit("r1", slow_job)
    pool.submit("r1", lambda: done.append("after"))
    pool.submit("r2", lambda: done.append("other"))

    seen_at_drain_return: list[str] = []

    def drainer() -> None:
        pool.drain()
        seen_at_drain_return.extend(done)

    t = threading.Thread(target=drainer)
    t.start()
    # The slow job is parked on the gate, so drain is provably still blocked
    # when we release it — drain returning with all three results proves it
    # waited for jobs that finished after it was called.
    assert slow_started.wait(_WAIT)
    gate.set()
    t.join(_WAIT)
    assert not t.is_alive()
    assert sorted(seen_at_drain_return) == ["after", "other", "slow"]
    pool.shutdown()


def test_shutdown_finishes_queued_jobs_and_rejects_new_ones(tmp_path: Path) -> None:
    executor = RepoExecutor(tmp_path, "r1")
    results: list[int] = []
    futures = [executor.submit(lambda i=i: results.append(i)) for i in range(3)]
    executor.shutdown()
    assert all(f.done() for f in futures)
    assert results == [0, 1, 2]
    with pytest.raises(RuntimeError, match="shut down"):
        executor.submit(lambda: None)
    # Idempotent.
    executor.shutdown()


def test_shutdown_before_first_job_is_a_noop(tmp_path: Path) -> None:
    # The thread starts lazily; shutting down a never-used executor must not
    # hang waiting on a thread that was never spawned.
    RepoExecutor(tmp_path, "r1").shutdown()
    ExecutorPool(tmp_path).shutdown()
