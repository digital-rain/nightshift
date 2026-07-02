"""Typed git-layer errors (git greenfield §2/§3).

``GitError`` is what :meth:`nightshift.git.runner.GitRunner.must` raises when a
git command whose failure is exceptional exits non-zero. It carries the argv,
returncode, and the trimmed stderr/stdout detail so callers can map it to a
typed failure (e.g. ``failure_kind=worktree_failed``) instead of letting a raw
``CalledProcessError`` traceback escape.
"""

from __future__ import annotations


class GitError(RuntimeError):
    """A git command failed where failure is exceptional (``GitRunner.must``)."""

    def __init__(self, message: str, *, argv: tuple[str, ...] = (), returncode: int = 0):
        super().__init__(message)
        self.argv = argv
        self.returncode = returncode
