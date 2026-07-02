"""Origin/main synchronisation — throttled fetch, ref-level fast-forward, and
divergence rescue by plumbing replay (git greenfield §5.1/§7).

Phase 6 removed every working-tree mutation from this path. Local ``main`` is
moved with an atomic ``update-ref`` CAS; over a divergence the unpushed local
commits are first replayed onto the fresh origin tip as *plumbing* cherry-picks
(``merge-tree --merge-base`` + ``commit-tree`` — no checkout is ever touched),
and only then is the ref swapped to the rebuilt tip. A replay that conflicts
is dropped (preserved unreachable in the reflog) and reported via the caller's
``dropped_commits`` list — never silently. A redundant replay (its content
already on the new base, e.g. the manager's own pr-mode squash that GitHub
re-squashed) collapses to a no-op instead of being re-introduced, which is why
the old ``drop_shas`` bookkeeping is gone.

The operator checkout is advanced best-effort afterwards (the ``reset --keep``
semantics of :func:`nightshift.git.refs.advance_checkout`): uncommitted work is
carried forward when clean and never clobbered — an overlapping edit leaves
the checkout behind ``main`` instead. The autostash machinery is deleted.

The public entry points (:func:`maybe_sync_main_to_origin`,
:func:`sync_main_to_origin`) are orchestration boundaries: they take the
:class:`~nightshift.git.locks.RepoLock`. :func:`sync_main_locked` is the
primitive for callers already holding it (the land pipeline).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from nightshift.git import GitRunner
from nightshift.git.locks import repo_lock
from nightshift.git.refs import (
    advance_checkout,
    checkout_state,
    is_ancestor,
    main_sha,
    replay_commit,
    rev_parse,
    update_main_cas,
)
from nightshift.preflight import invalidate_lock_marker, lock_changed_between


class SyncThrottle:
    """Per-target-repo throttle for origin/main refresh (monotonic timestamps).

    Phase 7 (git greenfield §7): an injected object owned by the manager app —
    the pre-Phase-7 module-global dict (and its tests-only reset hook) is gone.
    Callers that pass no throttle are simply unthrottled. Thread-safe: the
    repo executors and the event loop both consult it.
    """

    def __init__(self) -> None:
        self._last_check: dict[tuple[str, str], float] = {}
        self._mutex = threading.Lock()

    @staticmethod
    def _key(workspace: Path, repo: str) -> tuple[str, str]:
        return (str(workspace.resolve()), repo)

    def due(self, workspace: Path, repo: str, min_interval_seconds: float) -> bool:
        """True when a sync check for this repo is due (never checked, or the
        last check is older than ``min_interval_seconds``)."""
        if min_interval_seconds <= 0:
            return True
        with self._mutex:
            last = self._last_check.get(self._key(workspace, repo))
        return last is None or (time.monotonic() - last) >= min_interval_seconds

    def mark(self, workspace: Path, repo: str) -> None:
        """Record that a sync check just ran for this repo."""
        with self._mutex:
            self._last_check[self._key(workspace, repo)] = time.monotonic()


def maybe_sync_main_to_origin(
    workspace: Path,
    repo: str,
    remote: str,
    *,
    min_interval_seconds: float = 15.0,
    force: bool = False,
    reset_divergence: bool = False,
    dropped_commits: list[str] | None = None,
    throttle: SyncThrottle | None = None,
) -> str | None:
    """Refresh local ``main`` from ``<remote>/main`` when due.

    When ``force`` is false, a ``throttle`` is given, and its last check for
    this repo was less than ``min_interval_seconds`` ago, skip the network
    round-trip and return the current local ``main``. Otherwise:

    1. ``git fetch <remote> main``
    2. Compare local ``main`` to ``FETCH_HEAD`` (no work when already current)
    3. When local ``main`` is **strictly behind**, fast-forward the ref (CAS)
       and advance the checkout best-effort (operator WIP is carried, never
       clobbered — an overlap leaves the checkout behind ``main``)
    4. When local ``main`` is **ahead of or diverged from** ``origin/main``,
       leave it alone unless ``reset_divergence=True`` (land retries). Even
       then the move is surgical: unpushed commits are replayed onto the fresh
       tip first, so operator cherry-picks survive; a replay that conflicts is
       dropped and appended to ``dropped_commits`` (when given) so the caller
       can report it.

    Returns the local ``main`` after the check, or ``None`` when the remote
    has no ``main`` yet (callers fall back to local ``main``). See
    ``docs/spec/remote-landing.md``, Proposal 1.
    """
    with repo_lock(workspace, repo):
        return sync_main_locked(
            workspace,
            repo,
            remote,
            min_interval_seconds=min_interval_seconds,
            force=force,
            reset_divergence=reset_divergence,
            dropped_commits=dropped_commits,
            throttle=throttle,
        )


def sync_main_to_origin(
    workspace: Path,
    repo: str,
    remote: str,
    *,
    reset_divergence: bool = True,
    dropped_commits: list[str] | None = None,
) -> str | None:
    """Force an immediate origin/main refresh (bypasses the git-refresh throttle).

    Used when correctness matters more than pacing — e.g. a push-rejected land
    retry or an out-of-process resolve about to rebase. By default
    ``reset_divergence=True`` so a divergence is reconciled by replay; pass
    ``reset_divergence=False`` for a fetch + fast-forward-only check.

    Any unpushed commit on local ``main`` (e.g. an operator cherry-pick) is
    replayed onto the fresh ``origin/main`` and preserved; a replay that
    conflicts is dropped and appended to ``dropped_commits`` (when given) so
    the caller can report the casualty. A commit whose content origin already
    carries collapses to a redundant no-op replay.
    """
    with repo_lock(workspace, repo):
        return sync_main_locked(
            workspace,
            repo,
            remote,
            min_interval_seconds=0,
            force=True,
            reset_divergence=reset_divergence,
            dropped_commits=dropped_commits,
        )


def sync_main_locked(
    workspace: Path,
    repo: str,
    remote: str,
    *,
    min_interval_seconds: float = 0.0,
    force: bool = True,
    reset_divergence: bool = False,
    dropped_commits: list[str] | None = None,
    throttle: SyncThrottle | None = None,
) -> str | None:
    """The sync primitive: caller must hold the repo's :class:`RepoLock`
    (asserted). Throttle semantics match :func:`maybe_sync_main_to_origin`."""
    assert repo_lock(workspace, repo).is_held_by_current_thread(), (
        "sync_main_locked requires the caller to hold the RepoLock"
    )
    repo_root = workspace / repo
    if (
        not force
        and throttle is not None
        and not throttle.due(workspace, repo, min_interval_seconds)
    ):
        return main_sha(repo_root)

    result = _sync_main_to_origin_impl(
        workspace,
        repo,
        remote,
        reset_divergence=reset_divergence,
        dropped_commits=dropped_commits,
    )
    if throttle is not None:
        throttle.mark(workspace, repo)
    return result


def _unpushed_commits(repo_root: Path, target: str, head: str) -> list[str]:
    """Commits on local ``head`` not reachable from ``target``, oldest-first.

    These are the commits a plain ref swap to ``target`` would abandon (e.g.
    an operator cherry-pick on ``main``). Returned oldest-first so a replay
    re-applies them in order."""
    res = GitRunner(repo_root).run("rev-list", "--reverse", f"{target}..{head}")
    if not res.ok:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def _sync_main_to_origin_impl(
    workspace: Path,
    repo: str,
    remote: str,
    *,
    reset_divergence: bool = False,
    dropped_commits: list[str] | None = None,
) -> str | None:
    """Fetch ``<remote> main`` and move local ``main`` when safe (see the
    module docstring for the fast-forward / divergence-replay model)."""
    repo_root = workspace / repo
    git = GitRunner(repo_root)
    fetch = git.run("fetch", remote, "main")
    if not fetch.ok:
        return None
    target = rev_parse(repo_root, "FETCH_HEAD")
    if target is None:
        return None
    head = main_sha(repo_root)
    if head is None:
        return None
    if head == target:
        return head

    behind = is_ancestor(repo_root, head, target)
    if not behind and not reset_divergence:
        # Local main carries unpushed or divergent commits (e.g. a direct
        # cherry-pick) — periodic/poll sync must not clobber them.
        return head

    # Over a divergence, rebuild the new tip by replaying every unpushed local
    # commit onto the fresh origin tip (plumbing only; nothing is checked out).
    # A conflicting/unreplayable commit is dropped and reported as a casualty
    # (it stays reachable from the reflog for manual recovery).
    new_tip = target
    dropped: list[str] = []
    if not behind:
        for sha in _unpushed_commits(repo_root, target, head):
            replayed = replay_commit(git, new_tip, sha)
            if replayed.sha is None:
                dropped.append(sha)
            else:
                new_tip = replayed.sha

    checkout = checkout_state(repo_root)
    cas = update_main_cas(repo_root, new_tip, head)
    if not cas.ok:
        # main moved mid-section (external actor despite the lock) — report
        # the actual state rather than fight over it.
        return main_sha(repo_root)
    advance_checkout(repo_root, checkout, new_tip)
    if dropped_commits is not None:
        dropped_commits.extend(dropped)
    # Eager preflight signal: local main just moved — if a lockfile changed
    # in the range, drop the venv marker so the next task re-syncs before
    # spending model budget. The marker fingerprint stays authoritative for
    # clones that never fast-forward through here (e.g. a worker box).
    if lock_changed_between(repo_root, head, new_tip):
        invalidate_lock_marker(repo_root)
    return new_tip
