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

from nightshift.engine import (
    _queue_slug,
    _worktree_has_commits,
    fetch_rendezvous_branch,
    integrate_lock,
    landing_lock,
    prune_rendezvous_branch,
    squash_to_main,
    sync_main_to_origin,
    teardown_worktree,
    worktree_branch,
    worktree_dir,
)


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
    res = subprocess.run(
        ["git", "rev-parse", ref], cwd=repo_root, capture_output=True, text=True
    )
    return res.stdout.strip() if res.returncode == 0 else None


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
    res = subprocess.run(
        ["git", "merge-tree", "--write-tree", "--name-only", base, branch],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
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
    landing_mode: str = "none",
    automerge: bool = True,
    draft: bool = False,
    autostash: bool = True,
    branch_ref: str | None = None,
    head_sha: str | None = None,
    rendezvous_remote: str | None = None,
    max_push_retries: int = 3,
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
        landing_mode=landing_mode, automerge=automerge, draft=draft,
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
    do_sync = bool(rendezvous_remote) and landing_mode in ("push", "pr")
    push_remote = rendezvous_remote or "origin"

    with integrate_lock(workspace, repo):
        for attempt in range(max_push_retries + 1):
            # 1. Integrate the latest origin/main so the squash replays on top of
            #    everyone else's merged work (a no-op when already current).
            if do_sync:
                sync_main_to_origin(
                    workspace, repo, rendezvous_remote, autostash=autostash
                )

            # 2. Preview the squash against the fresh main. A genuine content
            #    conflict is refused cleanly (branch preserved) so the caller
            #    issues a resolve work-order instead of squashing deep.
            conflicts = merge_tree_conflicts(repo_root, branch)
            if conflicts:
                shown = "\n".join(f"    {p}" for p in conflicts[:20])
                return LandingResult(
                    landed=False,
                    conflict=True,
                    recoverable=False,
                    detail=(
                        f"squash of '{branch}' conflicts with current main on "
                        f"{len(conflicts)} file(s):\n{shown}"
                    ),
                )

            # 3. Squash the branch onto fresh main (local authority).
            sha, squash_error, recoverable = squash_to_main(
                workspace, repo, task, title, queue=queue, autostash=autostash
            )
            if sha is None:
                # squash_to_main reports a conflict as recoverable=False.
                return LandingResult(
                    landed=False,
                    detail=squash_error or "squash-merge to main failed",
                    recoverable=recoverable,
                    conflict=not recoverable,
                )

            result = LandingResult(landed=True, sha=sha, detail=squash_error)

            # 4. Apply the remote policy.
            if landing_mode == "push":
                result.remote = "push"
                pushed, push_detail = _push_head_to_main(
                    workspace, repo, push_remote
                )
                if pushed:
                    result.pushed = True
                    break
                # Non-fast-forward: origin advanced under us. The branch is still
                # intact (teardown happens only after a confirmed push), so the
                # next pass re-syncs (resetting local main to origin, which drops
                # this just-made squash) and re-squashes onto the new tip. Retry
                # only helps when we actually re-sync; without a rendezvous remote
                # a rejection is terminal.
                result.pushed = False
                if do_sync and attempt < max_push_retries:
                    continue
                # Exhausted retries: leave the branch for a resolve and report a
                # recoverable rejection (NOT a content conflict).
                return LandingResult(
                    landed=False,
                    recoverable=True,
                    conflict=False,
                    remote="push",
                    pushed=False,
                    detail=(
                        f"push to origin main rejected after {max_push_retries + 1} "
                        f"attempt(s) (origin keeps advancing):\n{push_detail}"
                    ),
                )
            elif landing_mode == "pr":
                result.remote = "pr"
                _open_pr(
                    workspace, repo, task, title, queue=queue,
                    automerge=automerge, draft=draft, result=result,
                )
                break
            else:
                # landing_mode == "none": the local squash is the whole land.
                break

    # The branch has landed on canonical main; reclaim its worktree + branch
    # (mirrors the engine's post-squash teardown in the single-process path).
    teardown_worktree(workspace, repo, task, queue=queue)

    # Cross-machine: the WIP ref is transport-only and now consumed (in PR mode
    # the PR head is the separate task/* branch), so prune it best-effort. A
    # conflict/rejection returns earlier and keeps the ref for a resolve re-fetch.
    if cross_machine and rendezvous_remote and branch_ref:
        prune_rendezvous_branch(workspace, repo, rendezvous_remote, branch_ref)

    return result


def _apply_remote_policy(
    workspace: Path,
    repo: str,
    task: str,
    title: str,
    *,
    queue: str | None,
    landing_mode: str,
    automerge: bool,
    draft: bool,
    result: LandingResult,
) -> None:
    if landing_mode == "push":
        result.remote = "push"
        _push_main(workspace, repo, result)
    elif landing_mode == "pr":
        result.remote = "pr"
        _open_pr(
            workspace, repo, task, title,
            queue=queue, automerge=automerge, draft=draft, result=result,
        )


def _adopt_agent_land_on_main(
    workspace: Path,
    repo: str,
    task: str,
    title: str,
    *,
    queue: str | None,
    base_ref: str | None,
    landing_mode: str,
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
        res = subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    if res.returncode != 0:
        result.pushed = False
        result.detail = (
            f"{result.detail}\nlocal land ok ({result.sha}); push to GitHub main failed: "
            f"{(res.stderr or res.stdout).strip()[:300]}"
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
        res = subprocess.run(
            ["git", "push", remote, "HEAD:main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    if res.returncode != 0:
        return False, (res.stderr or res.stdout).strip()[:300]
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
    last_detail = ""
    with integrate_lock(workspace, repo):
        for _attempt in range(max_retries + 1):
            # Reset local main to the freshest origin/main. ``sync_main_to_origin``
            # takes ``landing_lock`` itself, so it must run OUTSIDE the landing_lock
            # block below (the flock is not reentrant).
            if remote:
                sync_main_to_origin(workspace, repo, remote, autostash=autostash)
            with landing_lock(workspace, repo):
                # Replay the resolved commit on top of the fresh main (a no-op
                # fast-forward when main already is ``sha``), then push.
                head = _rev_parse(repo_root, "HEAD")
                if head != sha:
                    cp = subprocess.run(
                        ["git", "cherry-pick", "--allow-empty", sha],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                    )
                    if cp.returncode != 0:
                        subprocess.run(
                            ["git", "cherry-pick", "--abort"],
                            cwd=repo_root,
                            capture_output=True,
                            text=True,
                        )
                        return False, (
                            f"resolved commit {sha[:8]} conflicts with current "
                            f"origin/main:\n{(cp.stderr or cp.stdout).strip()[:300]}"
                        )
                    head = _rev_parse(repo_root, "HEAD") or sha
                push = subprocess.run(
                    ["git", "push", remote or "origin", "HEAD:main"],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                )
            if push.returncode == 0:
                return True, head
            last_detail = (push.stderr or push.stdout).strip()[:300]
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
        push = subprocess.run(
            ["git", "push", "-f", "origin", f"HEAD:refs/heads/{pr_branch}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    if push.returncode != 0:
        result.pushed = False
        result.detail = (
            f"{result.detail}\nlocal land ok ({result.sha}); PR branch push failed: "
            f"{(push.stderr or push.stdout).strip()[:300]}"
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
