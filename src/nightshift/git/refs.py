"""Ref-level git reads — rev-parse, ancestry, branch existence.

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration
(promoted from ``_rev_parse`` / ``_is_ancestor`` / ``_branch_exists``).
"""

from __future__ import annotations

from pathlib import Path

from nightshift.git import GitRunner


def rev_parse(repo_root: Path, ref: str) -> str | None:
    return GitRunner(repo_root).out("rev-parse", ref)


def is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant`` (inclusive)."""
    return GitRunner(repo_root).run("merge-base", "--is-ancestor", ancestor, descendant).ok


def branch_exists(repo_root: Path, branch: str) -> bool:
    return GitRunner(repo_root).run(
        "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
    ).ok
