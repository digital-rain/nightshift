"""Origin/main synchronisation — throttled fetch, fast-forward, surgical
divergence reset with commit rescue.

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration.
"""

from __future__ import annotations

import time
from pathlib import Path

from nightshift.git import GitRunner
from nightshift.git.locks import landing_lock
from nightshift.git.refs import is_ancestor, rev_parse
from nightshift.git.squash import (
    conflicted_paths,
    landing_blockers,
    porcelain_path,
    restore_operator_work,
    stash_operator_work,
)
from nightshift.preflight import invalidate_lock_marker, lock_changed_between


# Per-target-repo throttle for origin/main refresh (monotonic timestamps).
_LAST_ORIGIN_SYNC_CHECK: dict[tuple[str, str], float] = {}


def _origin_sync_key(workspace: Path, repo: str) -> tuple[str, str]:
    return (str(workspace.resolve()), repo)


def reset_origin_sync_throttle(workspace: Path | None = None, repo: str | None = None) -> None:
    """Clear origin-sync throttle state (tests only)."""
    if workspace is None and repo is None:
        _LAST_ORIGIN_SYNC_CHECK.clear()
        return
    if workspace is None or repo is None:
        raise ValueError("workspace and repo must both be set or both omitted")
    _LAST_ORIGIN_SYNC_CHECK.pop(_origin_sync_key(workspace, repo), None)


def maybe_sync_main_to_origin(
    workspace: Path,
    repo: str,
    remote: str,
    *,
    min_interval_seconds: float = 15.0,
    autostash: bool = True,
    force: bool = False,
    reset_divergence: bool = False,
    drop_shas: frozenset[str] | None = None,
    dropped_commits: list[str] | None = None,
) -> str | None:
    """Refresh local ``main`` from ``<remote>/main`` when due.

    When ``force`` is false and the last fetch for this repo was less than
    ``min_interval_seconds`` ago, skip the network round-trip and return the
    current local ``HEAD``. Otherwise:

    1. ``git fetch <remote> main``
    2. Compare local ``HEAD`` to ``FETCH_HEAD`` (no work when already current)
    3. When local ``main`` is **strictly behind** ``origin/main``, fast-forward
       with ``reset --hard`` (operator dirty-tree WIP is autostashed when enabled)
    4. When local ``main`` is **ahead of or diverged from** ``origin/main``,
       leave it alone unless ``reset_divergence=True`` (land retries / orphan
       pr-mode cleanup). Even then the reset is surgical: unpushed commits are
       replayed onto the fresh ``origin/main`` afterwards, so operator
       cherry-picks survive. ``drop_shas`` names commits the caller deliberately
       discards (its own orphan squash) so they are *not* replayed. Rescued
       commits whose replay conflicts are appended to ``dropped_commits`` (when
       given) so the caller can report them.

    Returns the local ``HEAD`` after the check, or ``None`` when the remote has
    no ``main`` yet (callers fall back to local ``HEAD``). See
    ``docs/spec/remote-landing.md``, Proposal 1.
    """
    repo_root = workspace / repo
    key = _origin_sync_key(workspace, repo)
    now = time.monotonic()
    if (
        not force
        and min_interval_seconds > 0
        and key in _LAST_ORIGIN_SYNC_CHECK
        and (now - _LAST_ORIGIN_SYNC_CHECK[key]) < min_interval_seconds
    ):
        return rev_parse(repo_root, "HEAD")

    result = _sync_main_to_origin_impl(
        workspace,
        repo,
        remote,
        autostash=autostash,
        reset_divergence=reset_divergence,
        drop_shas=drop_shas,
        dropped_commits=dropped_commits,
    )
    _LAST_ORIGIN_SYNC_CHECK[key] = time.monotonic()
    return result


def sync_main_to_origin(
    workspace: Path,
    repo: str,
    remote: str,
    *,
    autostash: bool = True,
    reset_divergence: bool = True,
    drop_shas: frozenset[str] | None = None,
    dropped_commits: list[str] | None = None,
) -> str | None:
    """Force an immediate origin/main refresh (bypasses the git-refresh throttle).

    Used when correctness matters more than pacing — e.g. a push-rejected land
    retry or an out-of-process resolve about to rebase. By default
    ``reset_divergence=True`` so an orphaned pr-mode squash can be dropped; pass
    ``reset_divergence=False`` for a fetch + fast-forward-only check.

    ``drop_shas`` names commits the caller deliberately discards (its own orphan
    squash). Any *other* unpushed commit on local ``main`` (e.g. an operator
    cherry-pick) is replayed onto the fresh ``origin/main`` and preserved; a
    replay that conflicts is dropped and appended to ``dropped_commits`` (when
    given) so the caller can report the casualty.
    """
    return maybe_sync_main_to_origin(
        workspace,
        repo,
        remote,
        min_interval_seconds=0,
        autostash=autostash,
        force=True,
        reset_divergence=reset_divergence,
        drop_shas=drop_shas,
        dropped_commits=dropped_commits,
    )


def _unpushed_commits(repo_root: Path, target: str, head: str) -> list[str]:
    """Commits on local ``head`` not reachable from ``target``, oldest-first.

    These are the commits a ``reset --hard target`` over a divergence would drop
    (e.g. an operator cherry-pick on ``main`` plus the manager's own orphan
    squash). Returned oldest-first so a replay re-applies them in order."""
    res = GitRunner(repo_root).run("rev-list", "--reverse", f"{target}..{head}")
    if not res.ok:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def _replay_commits(repo_root: Path, shas: list[str]) -> list[str]:
    """Cherry-pick ``shas`` (oldest-first) onto the current ``HEAD``.

    Each commit is replayed individually so a redundant one (its content already
    present on the new base — e.g. the manager's own squash that origin already
    carries) collapses to an empty cherry-pick and is skipped rather than
    re-introduced. A commit that genuinely conflicts with the new base is skipped
    too (best-effort rescue): its content is preserved unreachable in the reflog
    for manual recovery rather than left as a half-applied conflict on ``main``.

    Returns the SHAs that were **dropped** (their pick conflicted with the new
    base) so callers can surface the casualty to the operator — the reflog is
    not a UI. Redundant (empty) picks are not drops: their content is present.
    """
    git = GitRunner(repo_root)
    dropped: list[str] = []
    for sha in shas:
        if git.run("cherry-pick", sha).ok:
            continue
        # Unmerged index entries distinguish a genuine conflict (content lost —
        # report it) from an empty, already-applied pick (content present).
        if conflicted_paths(repo_root):
            dropped.append(sha)
        # Empty (already-applied) or conflicted: abort this pick and move on.
        # ``--skip`` advances past an empty pick; ``--abort`` unwinds a conflict
        # (best-effort — a failed abort leaves nothing further to unwind).
        # Try skip first (the common, redundant-squash case), then abort.
        if not git.run("cherry-pick", "--skip").ok:
            git.run("cherry-pick", "--abort")
    return dropped


def _sync_main_to_origin_impl(
    workspace: Path,
    repo: str,
    remote: str,
    *,
    autostash: bool = True,
    reset_divergence: bool = False,
    drop_shas: frozenset[str] | None = None,
    dropped_commits: list[str] | None = None,
) -> str | None:
    """Fetch ``<remote> main`` and move local ``main`` when safe.

    When ``reset_divergence`` is set and local ``main`` has diverged (carries
    unpushed commits), the reset to ``origin/main`` is *surgical*: any unpushed
    commit is rescued and replayed on top of the fresh tip afterwards, so an
    operator cherry-pick sitting on ``main`` survives a land retry instead of
    being silently dropped. ``drop_shas`` names commits the caller intends to
    discard (the manager's own orphan squash from a rejected push), which are
    excluded from the replay; redundant picks (content already on the new base)
    collapse to empty and are skipped automatically. A rescued commit whose
    replay *conflicts* with the fresh tip is dropped (preserved only in the
    reflog) and appended to ``dropped_commits`` when the caller passes a list,
    so the casualty can be reported instead of vanishing silently.
    """
    drop = drop_shas or frozenset()
    repo_root = workspace / repo
    git = GitRunner(repo_root)
    with landing_lock(workspace, repo):
        fetch = git.run("fetch", remote, "main")
        if not fetch.ok:
            return None
        target = rev_parse(repo_root, "FETCH_HEAD")
        if target is None:
            return None
        head = rev_parse(repo_root, "HEAD")
        if head is None:
            return None
        if head == target:
            return head

        behind = is_ancestor(repo_root, head, target)
        if not behind and not reset_divergence:
            # Local main carries unpushed or divergent commits (e.g. a direct
            # cherry-pick) — periodic/poll sync must not clobber them.
            return head

        # Over a divergence, capture the unpushed commits so we can replay the
        # ones the caller did not ask to drop after fast-forwarding to origin.
        rescue: list[str] = []
        if not behind:
            rescue = [
                sha
                for sha in _unpushed_commits(repo_root, target, head)
                if sha not in drop
            ]

        blockers = landing_blockers(repo_root)
        wip_sha: str | None = None
        blocker_paths: list[str] = []
        if blockers:
            if not autostash:
                return head
            blocker_paths = [porcelain_path(line) for line in blockers]
            wip_sha = stash_operator_work(repo_root, blocker_paths)
            if wip_sha is None:
                # ``git stash create`` failed to capture the operator's WIP.
                # Refuse the sync (mirrors squash_to_main's wip_sha guard) —
                # proceeding to ``reset --hard`` would destroy the uncommitted
                # work. The next sync retries once the tree is capturable.
                return head

        try:
            reset = git.run("reset", "--hard", target)
            if reset.ok and rescue:
                lost = _replay_commits(repo_root, rescue)
                if dropped_commits is not None:
                    dropped_commits.extend(lost)
        finally:
            if wip_sha:
                restore_operator_work(repo_root, wip_sha, blocker_paths)

        if not reset.ok:
            return rev_parse(repo_root, "HEAD")
        # Eager preflight signal: local main just moved — if a lockfile changed
        # in the range, drop the venv marker so the next task re-syncs before
        # spending model budget. The marker fingerprint stays authoritative for
        # clones that never fast-forward through here (e.g. a worker box).
        if lock_changed_between(repo_root, head, target):
            invalidate_lock_marker(repo_root)
        return rev_parse(repo_root, "HEAD")
