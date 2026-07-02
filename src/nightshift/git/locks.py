"""Landing / integrate locks for a target repo's shared working tree.

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import threading
from pathlib import Path


# Serialize every mutation of a target repo's index/HEAD/stash so concurrent
# queue runners (and a stray CLI process) can never interleave on the shared
# working tree. Two layers: a process-local lock across registry runner threads,
# and a cross-process file lock (per workspace+repo) so a server land and a CLI
# land can't collide.
_LANDING_LOCK = threading.Lock()
_INTEGRATE_LOCK = threading.Lock()


@contextlib.contextmanager
def landing_lock(workspace: Path, repo: str):
    """Hold the in-process + cross-process landing lock for a short critical
    section (a squash-merge + commit) on a target repo. The cross-process lock
    file lives at ``<workspace>/.worktrees/<repo>/.nightshift-landing.lock``. Not
    reentrant — never nest ``landing_lock`` calls on one thread."""
    with _LANDING_LOCK:
        path = workspace / ".worktrees" / repo / ".nightshift-landing.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


@contextlib.contextmanager
def integrate_lock(workspace: Path, repo: str):
    """Serialize a whole *integrate-and-push* section (sync origin -> preview ->
    squash -> push, with retries) across every land path and the out-of-process
    resolve runner, so concurrent merges to a target repo are strictly
    serialized while the long-running work that *precedes* the merge stays
    unlocked.

    This is a DIFFERENT lock from :func:`landing_lock`: the inner git primitives
    (``sync_main_to_origin``, ``squash_to_main``, the push) each still take
    ``landing_lock`` for their own critical section, so this outer lock must
    never be the landing lock (that would self-deadlock on the non-reentrant
    flock). The cross-process lock file lives at
    ``<workspace>/.worktrees/<repo>/.nightshift-integrate.lock``. Not reentrant.
    """
    with _INTEGRATE_LOCK:
        path = workspace / ".worktrees" / repo / ".nightshift-integrate.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
