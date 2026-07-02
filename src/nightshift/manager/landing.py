"""Git authority — the manager is the only component that mutates canonical main.

A worker has exactly one landing action: produce a squashed result on its task
branch and submit. The manager then, holding the repo's
:class:`~nightshift.git.locks.RepoLock` for the whole orchestration:

1. cross-machine: fetches the worker's WIP ref and verifies ``head_sha``
   (fail-closed → TRANSPORT_FAILED);
2. adopts a commit an agent landed on ``main`` directly (when the task branch
   has nothing to squash);
3. otherwise runs the ONE integrate loop
   (:func:`nightshift.git.landing.integrate_and_push_locked`): sync origin →
   squash as a pure commit object → push/CAS → best-effort checkout advance.

Landing is a ref operation (git greenfield §3): a refused land leaves the
branch intact and the working tree untouched, so the manager can hand out a
resolve work-order instead of losing the validated work. Every result is a
:class:`~nightshift.lifecycle.LandOutcome`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import assert_never

from nightshift.git.landing import (
    PrSpec,
    RepoContext,
    cherry_produce,
    integrate_and_push,
    integrate_and_push_locked,
    merge_tree_conflicts,
    open_pr,
    push_main,
    squash_produce,
)
from nightshift.git.locks import repo_lock
from nightshift.git.refs import main_sha, rev_parse
from nightshift.git.transport import fetch_rendezvous_branch, prune_rendezvous_branch
from nightshift.git.worktrees import (
    has_commits,
    teardown_worktree,
    worktree_branch,
    worktree_dir,
)
from nightshift.lifecycle import (
    LAND_SUCCESS_KINDS,
    LandingMode,
    LandKind,
    LandOutcome,
)


__all__ = [
    "adopt_or_nothing",
    "base_ref_drifted",
    "canonical_head",
    "land",
    "main_advanced_sha",
    "merge_tree_conflicts",
    "push_resolved_main",
]


def canonical_head(repo_root: Path) -> str | None:
    """Current canonical ``main`` SHA of a target repo — the ``base_ref`` handed
    to a worker. The branch ref is authoritative: a checkout left behind by a
    refused advance (CHECKOUT_BEHIND) must not make main look stale."""
    return main_sha(repo_root)


def base_ref_drifted(repo_root: Path, base_ref: str | None) -> bool:
    """True when canonical ``main`` has advanced past the worker's pinned base."""
    if not base_ref:
        return False
    head = canonical_head(repo_root)
    return head is not None and head != base_ref and rev_parse(repo_root, base_ref) is not None


def main_advanced_sha(repo_root: Path, base_ref: str | None) -> str | None:
    """Return canonical ``main``'s tip when it advanced past ``base_ref``.

    Used to adopt a commit an agent landed directly on ``main`` during its run
    (squash-merge in the worktree) when the task branch carries no commits for
    the manager to squash.
    """
    if not base_ref:
        return None
    head = canonical_head(repo_root)
    if not head or head == base_ref:
        return None
    if rev_parse(repo_root, base_ref) is None:
        return None
    return head


def land(
    workspace: Path,
    repo: str,
    task: str,
    title: str,
    *,
    queue: str | None,
    base_ref: str | None = None,
    landing_mode: LandingMode | str = LandingMode.NONE,
    automerge: bool = True,
    draft: bool = False,
    branch_ref: str | None = None,
    head_sha: str | None = None,
    rendezvous_remote: str | None = None,
    max_push_retries: int = 3,
    git_refresh_seconds: float = 15.0,
) -> LandOutcome:
    """Land a submitted task branch onto canonical ``main`` of the target
    ``repo``, then sync remotely.

    ``repo`` is the workspace-relative child name; the target repo path
    (``repo_root = workspace / repo``) is materialised here only for the git
    calls. Returns a :class:`LandOutcome`. On a content conflict (whether or
    not the base drifted) the branch is left intact and CONFLICT is returned so
    the caller can issue a resolve work-order.

    Cross-machine (transport B): when the worker ran on another box, the task
    branch is absent here — only ``branch_ref``/``head_sha`` arrive over the API.
    With no local worktree dir, ``land`` fetches ``branch_ref`` from
    ``rendezvous_remote`` and verifies it matches ``head_sha`` (fail-closed →
    TRANSPORT_FAILED) before the existing flow. The whole fetch → adopt →
    integrate section holds the repo's RepoLock once.
    """
    branch = worktree_branch(task, queue)
    # Callers inside the manager hand in a parsed LandingMode; the str form is
    # accepted for direct/legacy callers and validated here (fail loudly).
    mode = LandingMode(landing_mode)
    pr = PrSpec(task=task, title=title, queue=queue, automerge=automerge, draft=draft)

    # Cross-machine obtain: detected by an absent worktree dir + a submitted
    # branch_ref (gating on the dir, not branch presence, forces a re-fetch +
    # re-verify on every retry so stale/unverified content is never squashed).
    cross_machine = branch_ref is not None and not worktree_dir(
        workspace, repo, task, queue
    ).exists()

    with repo_lock(workspace, repo):
        if cross_machine:
            if not rendezvous_remote or not head_sha:
                return LandOutcome(
                    kind=LandKind.TRANSPORT_FAILED,
                    detail="cross-machine land requires a rendezvous remote and head_sha",
                )
            fetched = fetch_rendezvous_branch(
                workspace, repo, rendezvous_remote, branch_ref, task, queue=queue
            )
            if fetched is None:
                return LandOutcome(
                    kind=LandKind.TRANSPORT_FAILED,
                    retryable=True,
                    detail=f"failed to fetch '{branch_ref}' from '{rendezvous_remote}'",
                )
            if fetched != head_sha:
                return LandOutcome(
                    kind=LandKind.TRANSPORT_FAILED,
                    detail=(
                        f"head_sha mismatch: worker published {head_sha[:8]}, "
                        f"fetched {fetched[:8]} — refusing to land unverified content"
                    ),
                )

        adopted = _adopt_agent_land_on_main(
            workspace, repo, task, base_ref=base_ref, queue=queue,
            landing_mode=mode, pr=pr, remote=rendezvous_remote,
        )
        if adopted is not None:
            outcome = adopted
        else:
            # Sync is gated on an explicitly configured rendezvous remote (a
            # ``none`` mode or an unconfigured remote stays purely local). The
            # push target itself defaults to ``origin`` inside the loop so
            # ``push`` mode works without a separate rendezvous remote,
            # matching the historical direct-push behavior.
            ctx = RepoContext(
                workspace=workspace,
                repo=repo,
                remote=rendezvous_remote,
                sync=bool(rendezvous_remote) and mode.is_remote,
                git_refresh_seconds=git_refresh_seconds,
                pr=pr,
            )
            outcome = integrate_and_push_locked(
                ctx,
                squash_produce(workspace / repo, branch, title),
                mode=mode,
                max_retries=max_push_retries,
            )
            if outcome.kind not in LAND_SUCCESS_KINDS:
                # Refused: the branch (and, cross-machine, the WIP ref) is
                # preserved for a resolve.
                return outcome

            # The branch has landed on canonical main; reclaim its worktree +
            # branch (mirrors the engine's post-squash teardown).
            teardown_worktree(workspace, repo, task, queue=queue)

        # Cross-machine: the WIP ref is transport-only and now consumed (in PR
        # mode the PR head is the separate task/* branch), so prune it
        # best-effort. A conflict/rejection returns earlier and keeps the ref.
        if cross_machine and rendezvous_remote and branch_ref:
            prune_rendezvous_branch(workspace, repo, rendezvous_remote, branch_ref)

    return outcome


def _adopt_agent_land_on_main(
    workspace: Path,
    repo: str,
    task: str,
    *,
    base_ref: str | None,
    queue: str | None,
    landing_mode: LandingMode,
    pr: PrSpec,
    remote: str | None = None,
) -> LandOutcome | None:
    """When an agent landed on ``main`` during the worker run, adopt the tip.

    Returns an ADOPTED :class:`LandOutcome` when ``main`` advanced past
    ``base_ref`` and the task branch has nothing to squash; otherwise ``None``.
    Caller must hold the RepoLock (the remote policy uses the push primitives).
    """
    repo_root = workspace / repo
    sha = main_advanced_sha(repo_root, base_ref)
    if not sha or has_commits(workspace, repo, task, queue=queue):
        return None
    teardown_worktree(workspace, repo, task, queue=queue)
    outcome = LandOutcome(
        kind=LandKind.ADOPTED, sha=sha, detail="adopted agent land on main",
    )
    return _apply_remote_policy(
        workspace, repo, sha, landing_mode=landing_mode, pr=pr, outcome=outcome,
        remote=remote,
    )


def _apply_remote_policy(
    workspace: Path,
    repo: str,
    sha: str,
    *,
    landing_mode: LandingMode,
    pr: PrSpec,
    outcome: LandOutcome,
    remote: str | None = None,
) -> LandOutcome:
    """Remote policy for an already-on-main commit (the adopt path): best-effort
    — a push/PR failure is recorded on ``detail`` but never unwinds the land."""
    match landing_mode:
        case LandingMode.PUSH:
            # Same push-target resolution as the integrate pipeline.
            push = push_main(workspace, repo, remote or "origin", sha)
            if not push.ok:
                return replace(
                    outcome,
                    remote="push",
                    pushed=False,
                    detail=(
                        f"{outcome.detail}\nlocal land ok ({outcome.sha}); "
                        f"push to GitHub main failed: {push.detail}"
                    ).strip(),
                )
            return replace(outcome, remote="push", pushed=True)
        case LandingMode.PR:
            return open_pr(workspace, repo, pr, sha, replace(outcome, remote="pr"))
        case LandingMode.NONE:
            return outcome  # local-only: the commit already on main is the land
        case _:
            assert_never(landing_mode)


def adopt_or_nothing(
    workspace: Path,
    repo: str,
    task: str,
    title: str,
    *,
    queue: str | None,
    base_ref: str | None,
    landing_mode: LandingMode | str = LandingMode.NONE,
    automerge: bool = True,
    draft: bool = False,
) -> LandOutcome:
    """The completed-but-nothing-landable check: adopt an agent land on main,
    or report NO_CHANGES.

    This is :func:`land`'s adopt phase exposed on its own, so a submit with no
    landable branch stays cheap (two rev-parses and a branch existence check —
    never an origin sync or a squash attempt)."""
    pr = PrSpec(task=task, title=title, queue=queue, automerge=automerge, draft=draft)
    with repo_lock(workspace, repo):
        adopted = _adopt_agent_land_on_main(
            workspace, repo, task, base_ref=base_ref, queue=queue,
            landing_mode=LandingMode(landing_mode), pr=pr,
        )
    if adopted is not None:
        return adopted
    return LandOutcome(kind=LandKind.NO_CHANGES)


def push_resolved_main(
    workspace: Path,
    repo: str,
    remote: str,
    sha: str,
    *,
    max_retries: int = 3,
) -> tuple[bool, str]:
    """Land an already-squashed *resolved* commit (``sha``) on origin ``main``.

    The out-of-process resolve runner produces a single squash commit on local
    ``main`` (``runner_legacy.resolve_task``); origin may have advanced while
    its agent worked, so this is :func:`integrate_and_push` with the cherry
    producer: re-sync origin (rescuing the divergence the resolved commit
    deliberately created — hence ``rescue_divergence_on_first_sync``), replay
    the commit onto the fresh tip in pure plumbing, and push, bounded by
    ``max_retries``. The long agent work that produced ``sha`` ran unlocked;
    only this section holds the RepoLock.

    Returns ``(True, new_sha)`` on a confirmed push, or ``(False, detail)`` when
    the replay hits a content conflict (re-escalate) or the push keeps being
    rejected.
    """
    ctx = RepoContext(
        workspace=workspace,
        repo=repo,
        remote=remote,
        sync=bool(remote),
        rescue_divergence_on_first_sync=True,
    )
    outcome = integrate_and_push(
        ctx,
        cherry_produce(workspace / repo, sha),
        mode=LandingMode.PUSH,
        max_retries=max_retries,
    )
    if outcome.kind in LAND_SUCCESS_KINDS:
        return True, outcome.sha or sha
    return False, outcome.detail
