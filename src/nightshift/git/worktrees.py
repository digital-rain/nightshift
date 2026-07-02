"""Task worktree lifecycle — naming, setup/teardown, rebase helpers.

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration
(``_worktree_has_commits`` promoted to :func:`has_commits`, ``_queue_slug`` to
:func:`queue_slug`).
"""

from __future__ import annotations

from pathlib import Path

from nightshift.git import GitRunner
from nightshift.git.refs import branch_exists


SYMLINK_TARGETS = [
    ".venv",
    "services/dashboard_ui/node_modules",
    "node_modules",
]


def queue_slug(queue: str | None) -> str:
    """Path/branch-safe token for a queue: ``main`` for the default queue,
    otherwise the queue name (already a slug)."""
    return queue or "main"


def worktree_branch(task: str, queue: str | None = None) -> str:
    """Branch name for a task's local worktree, namespaced by queue so two queues
    holding a same-named task cut distinct branches."""
    return f"task-local/{queue_slug(queue)}/{task}"


def worktree_dir(workspace: Path, repo: str, task: str, queue: str | None = None) -> Path:
    """Worktree directory for a task, placed **outside** the target repo under a
    workspace-level ``<workspace>/.worktrees/<repo>/`` so the target repo stays
    pristine; namespaced by queue (see :func:`worktree_branch`)."""
    return (
        workspace / ".worktrees" / repo / f"task-local-{queue_slug(queue)}-{task}"
    )


def setup_worktree(
    workspace: Path, repo: str, task: str, *, queue: str | None = None, base: str = "HEAD"
) -> Path:
    """Create a git worktree (checked out from the target ``repo_root`` but
    placed outside it under ``<workspace>/.worktrees/<repo>/``) and symlink build
    artifacts from the target repo into it.

    ``base`` is the commit-ish the worktree branch is cut from (default the target
    repo's ``HEAD``). A cross-machine worker passes the work order's ``base_ref``
    so its branch is anchored to the same commit the manager will squash onto;
    the caller must have made ``base`` reachable in ``repo_root`` first (e.g. a
    fetch of the rendezvous remote).

    A failed ``worktree add`` raises a typed :class:`GitError` (task-fatal;
    callers map it to ``failure_kind=worktree_failed``)."""
    repo_root = workspace / repo
    wt_dir = worktree_dir(workspace, repo, task, queue)
    branch = worktree_branch(task, queue)

    git = GitRunner(repo_root)
    # Best-effort teardown of stale state from a previous attempt: the add
    # below is the call that actually has to succeed.
    if wt_dir.exists():
        git.run("worktree", "remove", "--force", str(wt_dir))
    git.run("branch", "-D", branch)

    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    git.must("worktree", "add", str(wt_dir), "-b", branch, base)

    for target in SYMLINK_TARGETS:
        src = repo_root / target
        dst = wt_dir / target
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)

    return wt_dir


def teardown_worktree(
    workspace: Path, repo: str, task: str, *, queue: str | None = None
) -> None:
    """Remove the worktree and its branch unconditionally."""
    repo_root = workspace / repo
    wt_dir = worktree_dir(workspace, repo, task, queue)
    branch = worktree_branch(task, queue)

    # Best-effort: either half may already be gone (a cleanly-landed task, a
    # hand-removed dir) and a failed removal must never fail the caller.
    git = GitRunner(repo_root)
    git.run("worktree", "remove", "--force", str(wt_dir))
    git.run("branch", "-D", branch)


def cleanup_task_worktree(
    workspace: Path, repo: str, task: str, *, queue: str | None = None
) -> bool:
    """Remove a task's *preserved* worktree + branch when present (the artifacts a
    failed-to-land task leaves behind for a later Resolve). Returns True when
    something existed and was removed; a no-op (False) when neither exists — so a
    cleanly-landed task, whose worktree the engine already tore down, is safe to
    pass here. Callers are responsible for the orphan check (no active/other run
    still needs the branch)."""
    repo_root = workspace / repo
    if not worktree_dir(workspace, repo, task, queue).exists() and not branch_exists(
        repo_root, worktree_branch(task, queue)
    ):
        return False
    teardown_worktree(workspace, repo, task, queue=queue)
    return True


def has_commits(
    workspace: Path, repo: str, task: str, *, queue: str | None = None
) -> bool:
    """True if the task's worktree branch has commits beyond ``HEAD``.

    A worker that made no commit (a non-agentic API backend, or an agentic one
    that decided nothing was needed) leaves nothing to validate or squash. When
    we can't tell, err on the side of "yes" so the normal path still runs.
    """
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)
    result = GitRunner(repo_root).run("rev-list", "--count", f"HEAD..{branch}")
    try:
        return int(result.stdout.strip() or "0") > 0
    except ValueError:
        return True


def _link_worktree_artifacts(repo_root: Path, worktree_dir: Path) -> None:
    """Symlink build artifacts (`.venv`, node_modules) from the target repo into
    ``worktree_dir`` so a re-attached worktree can run ``just validate``."""
    for target in SYMLINK_TARGETS:
        src = repo_root / target
        dst = worktree_dir / target
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)


def ensure_worktree_for_branch(
    workspace: Path, repo: str, task: str, *, queue: str | None = None
) -> Path | None:
    """Ensure the task's worktree exists on its preserved branch (re-attaching it
    if its checkout was cleaned up). Returns the dir, or ``None`` if the branch is
    gone. Unlike :func:`setup_worktree` this never deletes the branch."""
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)
    if not branch_exists(repo_root, branch):
        return None
    wt_dir = worktree_dir(workspace, repo, task, queue)
    if not wt_dir.exists():
        wt_dir.parent.mkdir(parents=True, exist_ok=True)
        # Best-effort: success is judged by the dir-existence check below,
        # which turns a failed add into the caller's ``None`` return.
        GitRunner(repo_root).run("worktree", "add", str(wt_dir), branch)
    if not wt_dir.exists():
        return None
    _link_worktree_artifacts(repo_root, wt_dir)
    return wt_dir


def rebase_in_progress(worktree_dir: Path) -> bool:
    """True while a rebase is paused (e.g. on conflicts) in ``worktree_dir``."""
    return GitRunner(worktree_dir).run("rebase", "--show-current-patch").ok


def abort_rebase(worktree_dir: Path) -> None:
    # Best-effort: aborting when no rebase is in progress simply fails.
    GitRunner(worktree_dir).run("rebase", "--abort")


def rebase_onto_main(worktree_dir: Path) -> tuple[str, str]:
    """Rebase the worktree's branch onto ``main``.

    Returns ``("clean", "")`` when it applied with no conflicts, ``("conflict",
    detail)`` when it paused on conflicts (rebase left in progress for the agent
    to resolve), or ``("error", detail)`` for any other failure.
    """
    if rebase_in_progress(worktree_dir):
        abort_rebase(worktree_dir)
    result = GitRunner(worktree_dir).run("rebase", "main")
    if result.ok:
        return "clean", ""
    detail = (result.stdout + "\n" + result.stderr).strip()
    if rebase_in_progress(worktree_dir):
        return "conflict", detail
    return "error", detail
