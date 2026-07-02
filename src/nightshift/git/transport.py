"""Cross-machine landing transport (transport B — git rendezvous remote).

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration.
"""

from __future__ import annotations

import re
from pathlib import Path

from nightshift.git import GitRunner
from nightshift.git.refs import rev_parse
from nightshift.git.worktrees import queue_slug, worktree_branch


# --------------------------------------------------------------------------
# Cross-machine landing transport (transport B — git rendezvous remote).
#
# A worker on a different box publishes its validated task branch to a git
# remote it has scoped access to; the manager fetches it into its own clone and
# runs the existing land() path. The WIP namespace (``nightshift-wip/*``) is
# kept distinct from the manager's PR branch (``task/*``) so a worker credential
# can be restricted to it. See docs/spec/remote-landing.md.
# --------------------------------------------------------------------------

WIP_REF_PREFIX = "nightshift-wip"


def normalize_wip_prefix(value: object) -> str:
    """Normalize the WIP-namespace prefix — the ``<prefix>`` segment of the
    rendezvous ref ``refs/heads/<prefix>/<queue>/<task>``.

    Returns a git-ref-safe namespace (one or more ``/``-joined segments).
    Raises ``ValueError`` on an unsafe value (empty, a leading ``-``, ``..``,
    ``//``, or characters outside ``[A-Za-z0-9._/-]``) so a bad operator value
    is surfaced at edit time rather than corrupting a push refspec.
    """
    text = str(value or "").strip().strip("/")
    if not text:
        raise ValueError("branch prefix must not be empty")
    if text.startswith("-") or ".." in text or "//" in text:
        raise ValueError(f"invalid branch prefix {text!r}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", text):
        raise ValueError(
            f"invalid branch prefix {text!r}: use letters, digits, '.', '_', "
            "'-', or '/' (no spaces, leading '-', '..', or '//')"
        )
    return text


def _wip_ref(task: str, queue: str | None, prefix: str = WIP_REF_PREFIX) -> str:
    """Remote ref a worker publishes its task branch to (transport-only). The
    ``prefix`` (the WIP namespace) defaults to :data:`WIP_REF_PREFIX` and is
    operator-configurable (manager ``wip_ref_prefix``, threaded via the work
    order)."""
    return f"refs/heads/{prefix or WIP_REF_PREFIX}/{queue_slug(queue)}/{task}"


def publish_task_branch(
    workspace: Path,
    repo: str,
    task: str,
    remote: str,
    *,
    queue: str | None = None,
    prefix: str | None = None,
) -> tuple[str, str]:
    """Force-push the task's local worktree branch to ``<remote>`` as its WIP ref.

    ``prefix`` is the WIP namespace (the manager's ``wip_ref_prefix``, delivered
    in the work order); ``None`` falls back to :data:`WIP_REF_PREFIX`.

    Returns ``(wip_ref, head_sha)`` where ``head_sha`` is the full SHA of the
    pushed branch tip (the manager re-verifies it after fetching). Raises
    ``RuntimeError`` on a push failure or a missing branch so the worker can
    surface ``publish_failed`` and land nothing.
    """
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)
    wip_ref = _wip_ref(task, queue, prefix or WIP_REF_PREFIX)

    head_sha = rev_parse(repo_root, branch)
    if head_sha is None:
        raise RuntimeError(f"no task branch '{branch}' to publish")

    push = GitRunner(repo_root).run("push", "-f", remote, f"{branch}:{wip_ref}")
    if not push.ok:
        raise RuntimeError(
            f"publish of '{branch}' to {remote} {wip_ref} failed: {push.detail}"
        )
    return wip_ref, head_sha


def fetch_rendezvous_branch(
    workspace: Path,
    repo: str,
    remote: str,
    wip_ref: str,
    task: str,
    *,
    queue: str | None = None,
) -> str | None:
    """Force-fetch a worker's published WIP ref into ``repo_root`` as the local
    ``worktree_branch``. Returns the fetched tip SHA, or ``None`` on a fetch
    error (the caller maps that to a recoverable land failure)."""
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)
    fetch = GitRunner(repo_root).run("fetch", "-f", remote, f"{wip_ref}:refs/heads/{branch}")
    if not fetch.ok:
        return None
    return rev_parse(repo_root, branch)


def prune_rendezvous_branch(
    workspace: Path, repo: str, remote: str, wip_ref: str
) -> None:
    """Best-effort delete of a consumed WIP ref on the rendezvous remote. A
    failure is swallowed: the ref is transport-only and a leftover is harmless
    (and eligible for a future scheduled GC)."""
    repo_root = workspace / repo
    # Best-effort by contract (see docstring): the result is deliberately discarded.
    GitRunner(repo_root).run("push", remote, "--delete", wip_ref)


def prepare_worktree_base(
    workspace: Path, repo: str, remote: str, base_ref: str | None
) -> str:
    """Cross-machine: make the manager's pinned ``base_ref`` reachable in the
    worker's clone, then return the commit-ish ``setup_worktree`` should cut from.

    Fetches ``<remote> main`` (``base_ref`` is the manager's ``origin/main`` HEAD,
    so it is reachable as an ancestor) and returns ``base_ref`` when it is now
    present, else falls back to ``HEAD`` (best-effort; a stale clone still lands
    co-located-style, and any real divergence surfaces as a land-time conflict).
    """
    if not base_ref:
        return "HEAD"
    repo_root = workspace / repo
    # Best-effort fetch: the reachability check below is what actually decides.
    GitRunner(repo_root).run("fetch", remote, "main")
    return base_ref if rev_parse(repo_root, base_ref) is not None else "HEAD"
