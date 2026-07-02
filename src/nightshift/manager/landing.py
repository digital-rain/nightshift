"""Git authority — the manager is the only component that mutates canonical main.

A worker has exactly one landing action: produce a squashed result on its task
branch and submit. The manager then, under the global ``landing_lock``:

1. detects whether canonical ``main`` advanced past the worker's pinned
   ``base_ref`` and would now conflict (``git merge-tree --write-tree`` preview);
2. always fast-forwards canonical ``main`` locally by reusing the engine's
   ``squash_to_main`` (``git merge --squash`` + ``git commit``);
3. applies the configured remote policy (``none`` | ``push`` | ``pr``).

On a genuine content conflict the land is refused with ``conflict=True`` and the
branch is preserved, so the manager can hand out a resolve work-order instead of
losing the validated work.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from nightshift.engine import (
    _queue_slug,
    _worktree_has_commits,
    fetch_rendezvous_branch,
    integrate_lock,
    landing_lock,
    maybe_sync_main_to_origin,
    prune_rendezvous_branch,
    squash_to_main,
    sync_main_to_origin,
    teardown_worktree,
    worktree_branch,
    worktree_dir,
)
from nightshift.git import GitRunner
from nightshift.lifecycle import LandingMode


@dataclass
class LandingResult:
    landed: bool
    sha: str | None = None
    detail: str = ""
    recoverable: bool = False
    conflict: bool = False          # base_ref drift / content conflict → resolve
    remote: str | None = None       # remote action taken: 'push' | 'pr' | None
    pr_url: str | None = None
    # Whether the configured remote step succeeded. ``None`` when no remote
    # action was attempted (``landing_mode=none``).
    pushed: bool | None = None


def _rev_parse(repo_root: Path, ref: str) -> str | None:
    return GitRunner(repo_root).out("rev-parse", ref)


def canonical_head(repo_root: Path) -> str | None:
    """Current canonical ``main`` SHA of a target repo — the ``base_ref`` handed
    to a worker."""
    return _rev_parse(repo_root, "HEAD")


def merge_tree_conflicts(repo_root: Path, branch: str, *, base: str = "HEAD") -> list[str]:
    """Preview a squash of ``branch`` onto ``base`` without touching the tree.

    Uses ``git merge-tree --write-tree`` (git ≥ 2.38). Returns the conflicting
    paths (empty when the merge is clean). Any failure to run the preview returns
    ``[]`` so detection never blocks a land that ``squash_to_main`` would handle.
    """
    res = GitRunner(repo_root).run("merge-tree", "--write-tree", "--name-only", base, branch)
    if res.ok:
        return []  # clean merge
    # Non-zero exit = conflicts. Output is: <tree-oid>\n<conflicted paths...>.
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    return lines[1:] if len(lines) > 1 else lines


def base_ref_drifted(repo_root: Path, base_ref: str | None) -> bool:
    """True when canonical ``main`` has advanced past the worker's pinned base."""
    if not base_ref:
        return False
    head = canonical_head(repo_root)
    return head is not None and head != base_ref and _rev_parse(repo_root, base_ref) is not None


def main_advanced_sha(repo_root: Path, base_ref: str | None) -> str | None:
    """Return canonical ``main``'s HEAD when it advanced past ``base_ref``.

    Used to adopt a commit an agent landed directly on ``main`` during its run
    (squash-merge in the worktree) when the task branch carries no commits for
    the manager to squash.
    """
    if not base_ref:
        return None
    head = canonical_head(repo_root)
    if not head or head == base_ref:
        return None
    if _rev_parse(repo_root, base_ref) is None:
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
    autostash: bool = True,
    branch_ref: str | None = None,
    head_sha: str | None = None,
    rendezvous_remote: str | None = None,
    max_push_retries: int = 3,
    git_refresh_seconds: float = 15.0,
) -> LandingResult:
    """Land a submitted task branch onto canonical ``main`` of the target
    ``repo``, then sync remotely.

    ``repo`` is the workspace-relative child name; the target repo path
    (``repo_root = workspace / repo``) is materialised here only for the git
    calls. Returns a :class:`LandingResult`. On a content conflict (whether or
    not the base drifted) the branch is left intact and ``conflict=True`` is
    returned so the caller can issue a resolve work-order.

    Cross-machine (transport B): when the worker ran on another box, the task
    branch is absent here — only ``branch_ref``/``head_sha`` arrive over the API.
    With no local worktree dir, ``land`` fetches ``branch_ref`` from
    ``rendezvous_remote`` and verifies it matches ``head_sha`` (fail-closed)
    before the existing flow. In PR mode it also resyncs local ``main`` to
    ``origin/main`` so the squash stays origin-authoritative.
    """
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)
    # Callers inside the manager hand in a parsed LandingMode; the str form is
    # accepted for direct/legacy callers and validated here (fail loudly).
    mode = LandingMode(landing_mode)

    # Cross-machine obtain: detected by an absent worktree dir + a submitted
    # branch_ref (gating on the dir, not branch presence, forces a re-fetch +
    # re-verify on every retry so stale/unverified content is never squashed).
    cross_machine = branch_ref is not None and not worktree_dir(
        workspace, repo, task, queue
    ).exists()
    if cross_machine:
        if not rendezvous_remote or not head_sha:
            return LandingResult(
                landed=False,
                recoverable=False,
                detail="cross-machine land requires a rendezvous remote and head_sha",
            )
        fetched = fetch_rendezvous_branch(
            workspace, repo, rendezvous_remote, branch_ref, task, queue=queue
        )
        if fetched is None:
            return LandingResult(
                landed=False,
                recoverable=True,
                detail=f"failed to fetch '{branch_ref}' from '{rendezvous_remote}'",
            )
        if fetched != head_sha:
            return LandingResult(
                landed=False,
                recoverable=False,
                detail=(
                    f"head_sha mismatch: worker published {head_sha[:8]}, "
                    f"fetched {fetched[:8]} — refusing to land unverified content"
                ),
            )

    adopted = _adopt_agent_land_on_main(
        workspace, repo, task, title, queue=queue, base_ref=base_ref,
        landing_mode=mode, automerge=automerge, draft=draft,
    )
    if adopted is not None:
        return adopted

    # A remote-capable land integrates origin/main into the local clone before
    # every squash so the result sits on the freshest merged state, and retries
    # the whole sync->preview->squash->push when origin races us between the
    # squash and the push. Modes without a remote (or ``none``) land locally.
    # Sync is gated on an explicitly configured rendezvous remote (a ``none``
    # mode or an unconfigured remote stays purely local). The push target itself
    # defaults to ``origin`` so ``push`` mode works without a separate rendezvous
    # remote, matching the historical direct-push behavior.
    do_sync = bool(rendezvous_remote) and mode.is_remote
    push_remote = rendezvous_remote or "origin"

    # The squash commit a rejected push leaves on local main: the next re-sync
    # must drop exactly this (and nothing else, so an operator commit on main is
    # preserved). Captured as a full SHA after each squash attempt.
    orphan_squash: str | None = None
    # Rescued-but-conflicting local commits a re-sync had to drop (preserved
    # only in the reflog) — surfaced on the returned result's detail so the
    # casualty is reported, not buried.
    dropped_rescues: list[str] = []

    def _noting_drops(result: LandingResult) -> LandingResult:
        if dropped_rescues:
            note = (
                "origin re-sync dropped conflicting local commit(s): "
                + ", ".join(sha[:12] for sha in dropped_rescues)
                + " (preserved unreachable in the reflog for manual recovery)"
            )
            result.detail = f"{result.detail}\n{note}".strip()
        return result

    with integrate_lock(workspace, repo):
        for attempt in range(max_push_retries + 1):
            # 1. Integrate the latest origin/main so the squash replays on top of
            #    everyone else's merged work (a no-op when already current).
            if do_sync:
                drop = frozenset({orphan_squash}) if orphan_squash else None
                if attempt == 0:
                    maybe_sync_main_to_origin(
                        workspace,
                        repo,
                        rendezvous_remote,
                        min_interval_seconds=git_refresh_seconds,
                        autostash=autostash,
                        drop_shas=drop,
                        dropped_commits=dropped_rescues,
                    )
                else:
                    sync_main_to_origin(
                        workspace,
                        repo,
                        rendezvous_remote,
                        autostash=autostash,
                        drop_shas=drop,
                        dropped_commits=dropped_rescues,
                    )

            # 2. Preview the squash against the fresh main. A genuine content
            #    conflict is refused cleanly (branch preserved) so the caller
            #    issues a resolve work-order instead of squashing deep.
            conflicts = merge_tree_conflicts(repo_root, branch)
            if conflicts:
                shown = "\n".join(f"    {p}" for p in conflicts[:20])
                return _noting_drops(LandingResult(
                    landed=False,
                    conflict=True,
                    recoverable=False,
                    detail=(
                        f"squash of '{branch}' conflicts with current main on "
                        f"{len(conflicts)} file(s):\n{shown}"
                    ),
                ))

            # 3. Squash the branch onto fresh main (local authority).
            sha, squash_error, recoverable = squash_to_main(
                workspace, repo, task, title, queue=queue, autostash=autostash
            )
            if sha is None:
                # squash_to_main reports a conflict as recoverable=False.
                return _noting_drops(LandingResult(
                    landed=False,
                    detail=squash_error or "squash-merge to main failed",
                    recoverable=recoverable,
                    conflict=not recoverable,
                ))

            # Record the full SHA of the squash we just made; if the push below
            # is rejected, the next re-sync drops exactly this commit (the next
            # iteration re-squashes onto the freshly advanced origin) while any
            # operator commit on main is rescued + replayed.
            orphan_squash = _rev_parse(repo_root, "HEAD")
            result = LandingResult(landed=True, sha=sha, detail=squash_error)

            # 4. Apply the remote policy.
            match mode:
                case LandingMode.PUSH:
                    result.remote = "push"
                    pushed, push_detail = _push_head_to_main(
                        workspace, repo, push_remote
                    )
                    if pushed:
                        result.pushed = True
                        break
                    # Non-fast-forward: origin advanced under us. The branch is
                    # still intact (teardown happens only after a confirmed
                    # push), so the next pass re-syncs (resetting local main to
                    # origin, which drops this just-made squash) and
                    # re-squashes onto the new tip. Retry only helps when we
                    # actually re-sync; without a rendezvous remote a rejection
                    # is terminal.
                    result.pushed = False
                    if do_sync and attempt < max_push_retries:
                        continue
                    # Exhausted retries: leave the branch for a resolve and
                    # report a recoverable rejection (NOT a content conflict).
                    return _noting_drops(LandingResult(
                        landed=False,
                        recoverable=True,
                        conflict=False,
                        remote="push",
                        pushed=False,
                        detail=(
                            f"push to origin main rejected after {max_push_retries + 1} "
                            f"attempt(s) (origin keeps advancing):\n{push_detail}"
                        ),
                    ))
                case LandingMode.PR:
                    result.remote = "pr"
                    _open_pr(
                        workspace, repo, task, title, queue=queue,
                        automerge=automerge, draft=draft, result=result,
                    )
                    break
                case LandingMode.NONE:
                    # The local squash is the whole land.
                    break
                case _:
                    assert_never(mode)

    # The branch has landed on canonical main; reclaim its worktree + branch
    # (mirrors the engine's post-squash teardown in the single-process path).
    teardown_worktree(workspace, repo, task, queue=queue)

    # Cross-machine: the WIP ref is transport-only and now consumed (in PR mode
    # the PR head is the separate task/* branch), so prune it best-effort. A
    # conflict/rejection returns earlier and keeps the ref for a resolve re-fetch.
    if cross_machine and rendezvous_remote and branch_ref:
        prune_rendezvous_branch(workspace, repo, rendezvous_remote, branch_ref)

    return _noting_drops(result)


def _apply_remote_policy(
    workspace: Path,
    repo: str,
    task: str,
    title: str,
    *,
    queue: str | None,
    landing_mode: LandingMode,
    automerge: bool,
    draft: bool,
    result: LandingResult,
) -> None:
    match landing_mode:
        case LandingMode.PUSH:
            result.remote = "push"
            _push_main(workspace, repo, result)
        case LandingMode.PR:
            result.remote = "pr"
            _open_pr(
                workspace, repo, task, title,
                queue=queue, automerge=automerge, draft=draft, result=result,
            )
        case LandingMode.NONE:
            pass  # local-only: the squash already on main is the whole land
        case _:
            assert_never(landing_mode)


def _adopt_agent_land_on_main(
    workspace: Path,
    repo: str,
    task: str,
    title: str,
    *,
    queue: str | None,
    base_ref: str | None,
    landing_mode: LandingMode,
    automerge: bool,
    draft: bool,
) -> LandingResult | None:
    """When an agent landed on ``main`` during the worker run, adopt HEAD.

    Returns a :class:`LandingResult` when ``main`` advanced past ``base_ref``
    and the task branch has nothing to squash; otherwise ``None``.
    """
    repo_root = workspace / repo
    sha = main_advanced_sha(repo_root, base_ref)
    if not sha or _worktree_has_commits(workspace, repo, task, queue=queue):
        return None
    teardown_worktree(workspace, repo, task, queue=queue)
    result = LandingResult(
        landed=True,
        sha=sha,
        detail="adopted agent land on main",
    )
    _apply_remote_policy(
        workspace, repo, task, title, queue=queue,
        landing_mode=landing_mode, automerge=automerge, draft=draft, result=result,
    )
    return result


def _push_main(workspace: Path, repo: str, result: LandingResult) -> None:
    """Fast-forward GitHub ``main`` directly (no PR). Requires the manager token
    on main's protected-branch bypass list. Best-effort: a push failure is
    recorded in ``detail`` but never unwinds the already-landed local commit."""
    repo_root = workspace / repo
    with landing_lock(workspace, repo):
        res = GitRunner(repo_root).run("push", "origin", "HEAD:main")
    if not res.ok:
        result.pushed = False
        result.detail = (
            f"{result.detail}\nlocal land ok ({result.sha}); push to GitHub main failed: "
            f"{res.detail}"
        ).strip()
    else:
        result.pushed = True


def _push_head_to_main(
    workspace: Path, repo: str, remote: str
) -> tuple[bool, str]:
    """Push local ``HEAD`` to ``<remote>/main`` (non-force) under the landing
    lock. Returns ``(ok, detail)``; a non-fast-forward rejection is ``ok=False``
    with the trimmed git stderr so the caller can retry or escalate."""
    repo_root = workspace / repo
    with landing_lock(workspace, repo):
        res = GitRunner(repo_root).run("push", remote, "HEAD:main")
    if not res.ok:
        return False, res.detail
    return True, ""


def push_resolved_main(
    workspace: Path,
    repo: str,
    remote: str,
    sha: str,
    *,
    max_retries: int = 3,
    autostash: bool = True,
) -> tuple[bool, str]:
    """Land an already-squashed *resolved* commit (``sha``) on origin ``main``.

    The out-of-process resolve runner produces a single squash commit locally
    (``engine.resolve_task``); origin may have advanced while its agent worked,
    so this replays that commit onto the freshest origin/main and pushes,
    bounded by ``max_retries``. The whole section holds :func:`integrate_lock`
    so it serializes against every other land on the repo, while the long agent
    work that produced ``sha`` ran unlocked.

    Returns ``(True, new_sha)`` on a confirmed push, or ``(False, detail)`` when
    the replay hits a content conflict (re-escalate) or the push keeps being
    rejected. Operates in ``repo_root`` (clean after the squash), isolated from
    the resolve worktree.
    """
    repo_root = workspace / repo
    git = GitRunner(repo_root)
    last_detail = ""
    with integrate_lock(workspace, repo):
        for _attempt in range(max_retries + 1):
            # Reset local main to the freshest origin/main. ``sync_main_to_origin``
            # takes ``landing_lock`` itself, so it must run OUTSIDE the landing_lock
            # block below (the flock is not reentrant). Drop our own resolved
            # commit on the re-sync — it is re-applied by the explicit cherry-pick
            # below — so the rescue does not double-apply it; any *operator* commit
            # on main is still rescued + replayed.
            if remote:
                drop = frozenset({sha}) if sha else None
                sync_main_to_origin(
                    workspace, repo, remote, autostash=autostash, drop_shas=drop
                )
            with landing_lock(workspace, repo):
                # Replay the resolved commit on top of the fresh main (a no-op
                # fast-forward when main already is ``sha``), then push.
                head = _rev_parse(repo_root, "HEAD")
                if head != sha:
                    cp = git.run("cherry-pick", "--allow-empty", sha)
                    if not cp.ok:
                        # Best-effort unwind of the conflicted pick before bailing.
                        git.run("cherry-pick", "--abort")
                        return False, (
                            f"resolved commit {sha[:8]} conflicts with current "
                            f"origin/main:\n{cp.detail}"
                        )
                    head = _rev_parse(repo_root, "HEAD") or sha
                push = git.run("push", remote or "origin", "HEAD:main")
            if push.ok:
                return True, head
            last_detail = push.detail
            # Rejected: origin advanced again — re-sync and replay on the next pass.
    return False, f"push of resolved commit rejected after retries:\n{last_detail}"


def _open_pr(
    workspace: Path,
    repo: str,
    task: str,
    title: str,
    *,
    queue: str | None,
    automerge: bool,
    draft: bool,
    result: LandingResult,
) -> None:
    """Push ``task/<stem>`` at the freshly-landed commit and open a PR.

    Defaults ``automerge=true`` / ``draft=false`` (overridable per task). The
    branch points at the squash commit on main so the PR is exactly the landed
    change. GitHub auth lives only on the manager (``gh``)."""
    repo_root = workspace / repo
    pr_branch = f"task/{_queue_slug(queue)}-{task}"
    with landing_lock(workspace, repo):
        push = GitRunner(repo_root).run("push", "-f", "origin", f"HEAD:refs/heads/{pr_branch}")
    if not push.ok:
        result.pushed = False
        result.detail = (
            f"{result.detail}\nlocal land ok ({result.sha}); PR branch push failed: "
            f"{push.detail}"
        ).strip()
        return
    result.pushed = True
    argv = [
        "gh", "pr", "create",
        "--head", pr_branch,
        "--base", "main",
        "--title", f"task: {title}",
        "--body", f"Automated nightshift land for `{task}`.",
    ]
    if draft:
        argv.append("--draft")
    create = subprocess.run(argv, cwd=repo_root, capture_output=True, text=True)
    if create.returncode != 0:
        result.detail = (
            f"{result.detail}\nlocal land ok ({result.sha}); gh pr create failed: "
            f"{(create.stderr or create.stdout).strip()[:300]}"
        ).strip()
        return
    result.pr_url = create.stdout.strip().splitlines()[-1] if create.stdout.strip() else None
    if automerge and not draft and result.pr_url:
        subprocess.run(
            ["gh", "pr", "merge", "--auto", "--squash", result.pr_url],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
