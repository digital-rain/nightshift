"""Nightshift's git layer (git greenfield §1) — grown module by module.

Phase 2 lands the subprocess seam: :class:`~nightshift.git.runner.GitRunner`,
:class:`~nightshift.git.runner.GitResult`, and
:class:`~nightshift.git.errors.GitError`. Later phases relocate refs/worktrees/
landing/sync/transport logic here.
"""

from nightshift.git.errors import GitError
from nightshift.git.runner import GitResult, GitRunner


__all__ = ["GitError", "GitResult", "GitRunner"]
