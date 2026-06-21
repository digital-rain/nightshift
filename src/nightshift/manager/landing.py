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
    fetch_rendezvous_branch,
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

    # PR mode is origin/main-authoritative: resync local main to origin/main so the
    # ephemeral squash sits on the latest merged state and cannot diverge (the next
    # dispatch resync replaces it with GitHub's merge commit). See remote-landing.md.
    if landing_mode == "pr" and rendezvous_remote:
        sync_main_to_origin(workspace, repo, rendezvous_remote, autostash=autostash)

    # Pre-check: if main drifted past base_ref and the merge would conflict,
    # refuse cleanly so the caller resolves rather than the squash failing deep.
    if base_ref_drifted(repo_root, base_ref):
        conflicts = merge_tree_conflicts(repo_root, branch)
        if conflicts:
            shown = "\n".join(f"    {p}" for p in conflicts[:20])
            return LandingResult(
                landed=False,
                conflict=True,
                recoverable=False,
                detail=(
                    f"canonical main advanced past base_ref {base_ref[:8] if base_ref else '?'}; "
                    f"squash of '{branch}' conflicts on {len(conflicts)} file(s):\n{shown}"
                ),
            )

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

    # The branch has landed on canonical main; reclaim its worktree + branch
    # (mirrors the engine's post-squash teardown in the single-process path).
    teardown_worktree(workspace, repo, task, queue=queue)

    # Cross-machine: the WIP ref is transport-only and now consumed (in PR mode
    # the PR head is the separate task/* branch), so prune it best-effort. A
    # conflict/rejection returns earlier and keeps the ref for a resolve re-fetch.
    if cross_machine and rendezvous_remote and branch_ref:
        prune_rendezvous_branch(workspace, repo, rendezvous_remote, branch_ref)

    if landing_mode == "push":
        result.remote = "push"
        _push_main(workspace, repo, result)
    elif landing_mode == "pr":
        result.remote = "pr"
        _open_pr(
            workspace, repo, task, title,
            queue=queue, automerge=automerge, draft=draft, result=result,
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
        result.detail = (
            f"{result.detail}\nlocal land ok ({result.sha}); push to GitHub main failed: "
            f"{(res.stderr or res.stdout).strip()[:300]}"
        ).strip()


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
        result.detail = (
            f"{result.detail}\nlocal land ok ({result.sha}); PR branch push failed: "
            f"{(push.stderr or push.stdout).strip()[:300]}"
        ).strip()
        return
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
