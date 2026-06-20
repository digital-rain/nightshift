"""Nightshift worker — owns a clone, a backend, and the poll loop.

A worker checks in with the manager, polls for a leased work order, executes it
in a git worktree with its chosen backend, validates, and submits a single
squashed result back to the manager (which is the git authority). The worker
knows nothing about PRs, automerge, or GitHub; landing policy is manager-side.
"""

from __future__ import annotations
