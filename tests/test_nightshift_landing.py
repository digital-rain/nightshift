"""Landing tests (Phase 2/3): squash land, merge-tree drift detection, conflict.

Co-located mode: the worker's branch lives in the target repo (a workspace
child), so landing degenerates to a local squash-to-main that the manager owns.
The two-root model means every landing primitive is addressed as
``(workspace, repo)`` and resolves the target repo at ``workspace / repo``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _workspace import build_workspace, git_commit_all
from nightshift.engine import setup_worktree, worktree_branch
from nightshift.manager.landing import (
    base_ref_drifted,
    canonical_head,
    land,
    merge_tree_conflicts,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(tmp_path: Path) -> tuple[Path, str, Path]:
    """Build a workspace with one target repo holding ``file.txt``.

    Returns ``(workspace, repo, repo_root)``. Landing resolves the target repo
    as ``workspace / repo``; ``file.txt`` gives the drift/conflict tests a line
    to fight over.
    """
    workspace = build_workspace(tmp_path, repos=("longitude",), main_repo="longitude")
    repo = "longitude"
    repo_root = workspace / repo
    (repo_root / "file.txt").write_text("base\n")
    git_commit_all(repo_root, "add file")
    return workspace, repo, repo_root


def _make_branch_commit(
    workspace: Path, repo: str, task: str, *, path: str, content: str
) -> None:
    """Create a task worktree branch with a single commit editing ``path``."""
    wt = setup_worktree(workspace, repo, task)
    (wt / path).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"work {task}"], cwd=wt, check=True, capture_output=True
    )


def test_clean_land_succeeds(tmp_path: Path) -> None:
    workspace, repo, repo_root = _init_repo(tmp_path)
    _make_branch_commit(workspace, repo, "10.add", path="new.txt", content="hello\n")
    base = canonical_head(repo_root)
    result = land(workspace, repo, "10.add", "add new file", queue=None, base_ref=base)
    assert result.landed is True
    assert result.sha
    assert (repo_root / "new.txt").exists()
    # The branch was reclaimed after landing.
    branches = _git(repo_root, "branch", "--list", worktree_branch("10.add"))
    assert branches.strip() == ""


def test_merge_tree_detects_conflict(tmp_path: Path) -> None:
    workspace, repo, repo_root = _init_repo(tmp_path)
    # Branch edits file.txt one way...
    _make_branch_commit(workspace, repo, "20.edit", path="file.txt", content="branch change\n")
    # ...main edits the same line another way (drift).
    (repo_root / "file.txt").write_text("main change\n")
    _git(repo_root, "commit", "-am", "main edits file")

    conflicts = merge_tree_conflicts(repo_root, worktree_branch("20.edit"))
    assert "file.txt" in conflicts


def test_land_refuses_on_drift_conflict(tmp_path: Path) -> None:
    workspace, repo, repo_root = _init_repo(tmp_path)
    base = canonical_head(repo_root)
    _make_branch_commit(workspace, repo, "20.edit", path="file.txt", content="branch change\n")
    # main advances past base_ref with a conflicting edit.
    (repo_root / "file.txt").write_text("main change\n")
    _git(repo_root, "commit", "-am", "main edits file")

    assert base_ref_drifted(repo_root, base) is True
    result = land(workspace, repo, "20.edit", "edit file", queue=None, base_ref=base)
    assert result.landed is False
    assert result.conflict is True
    # Branch preserved for resolution.
    assert _git(repo_root, "branch", "--list", worktree_branch("20.edit")).strip() != ""
