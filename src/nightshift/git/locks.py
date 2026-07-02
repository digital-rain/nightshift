"""RepoLock — one mutual-exclusion device per (workspace, repo) target repo
(git greenfield §4).

Everything that mutates the canonical repo — landing, origin sync, transport
fetch/prune — serializes on the SAME lock, held at orchestration boundaries
(``land()``, ``push_resolved_main``, the public sync entry points, the CLI
land path). The git primitives themselves are lock-free and *assert* the
caller holds it (:meth:`RepoLock.is_held_by_current_thread`), which makes a
forgotten lock a loud test failure instead of a silent race.

Two layers, like the locks this replaces:

* an in-process :class:`threading.Lock` keyed per ``(workspace, repo)`` in a
  registry — NOT module-global, so lands on different repos never serialize;
* a cross-process ``flock`` at ``<workspace>/.worktrees/<repo>/.lock`` so a
  manager land and an out-of-process resolve push (or a CLI land) can't
  collide.

Re-entry raises ``RuntimeError``: the flock is not reentrant, and nesting the
lock on one thread is always a layering bug (a primitive trying to act like
an orchestrator). The pre-Phase-6 ``landing_lock``/``integrate_lock`` pair —
one module-global mutex each, serializing across unrelated repos — is gone.
"""

from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path


class RepoLock:
    """The per-(workspace, repo) repo mutation lock. Obtain instances via
    :func:`repo_lock` (the registry) — constructing one directly bypasses the
    per-repo keying and only makes sense in tests."""

    def __init__(self, workspace: Path, repo: str) -> None:
        self._lock = threading.Lock()
        self._owner: int | None = None
        self._fd: int | None = None
        self._path = workspace / ".worktrees" / repo / ".lock"

    def is_held_by_current_thread(self) -> bool:
        return self._owner == threading.get_ident()

    def acquire(self) -> None:
        if self.is_held_by_current_thread():
            raise RuntimeError(
                f"RepoLock({self._path}) re-entered on the same thread — "
                "primitives must not re-acquire the orchestration lock"
            )
        self._lock.acquire()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._path), os.O_CREAT | os.O_WRONLY, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                os.close(fd)
                raise
        except OSError:
            self._lock.release()
            raise
        self._fd = fd
        self._owner = threading.get_ident()

    def release(self) -> None:
        if not self.is_held_by_current_thread():
            raise RuntimeError(f"RepoLock({self._path}) released by a non-owner")
        fd = self._fd
        self._owner = None
        self._fd = None
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self._lock.release()

    def __enter__(self) -> RepoLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


# Registry: one lock object per (workspace, repo), created on first use. The
# registry mutex only guards the dict itself (creation races), never the repo.
_REGISTRY: dict[tuple[str, str], RepoLock] = {}
_REGISTRY_MUTEX = threading.Lock()


def repo_lock(workspace: Path, repo: str) -> RepoLock:
    """The one :class:`RepoLock` for a target repo (registry-keyed by resolved
    workspace + repo name, so every code path in the process shares it)."""
    key = (str(workspace.resolve()), repo)
    with _REGISTRY_MUTEX:
        lock = _REGISTRY.get(key)
        if lock is None:
            lock = RepoLock(workspace, repo)
            _REGISTRY[key] = lock
        return lock
