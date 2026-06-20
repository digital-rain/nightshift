"""Landing tests (Phase 2/3): squash land, merge-tree drift detection, conflict.

Co-located mode: the worker's branch lives in the manager's repo, so landing
degenerates to a local squash-to-main that the manager owns.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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


def _init_repo(tmp_path: Path) -> Path:
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "file.txt").write_text("base\n")
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@test")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def _make_branch_commit(root: Path, task: str, *, path: str, content: str) -> None:
    """Create a task worktree branch with a single commit editing ``path``."""
    wt = setup_worktree(root, task)
    (wt / path).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"work {task}"], cwd=wt, check=True, capture_output=True
    )


def test_clean_land_succeeds(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _make_branch_commit(root, "10.add", path="new.txt", content="hello\n")
    base = canonical_head(root)
    result = land(root, "10.add", "add new file", queue=None, base_ref=base)
    assert result.landed is True
    assert result.sha
    assert (root / "new.txt").exists()
    # The branch was reclaimed after landing.
    branches = _git(root, "branch", "--list", worktree_branch("10.add"))
    assert branches.strip() == ""


def test_merge_tree_detects_conflict(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    # Branch edits file.txt one way...
    _make_branch_commit(root, "20.edit", path="file.txt", content="branch change\n")
    # ...main edits the same line another way (drift).
    (root / "file.txt").write_text("main change\n")
    _git(root, "commit", "-am", "main edits file")

    conflicts = merge_tree_conflicts(root, worktree_branch("20.edit"))
    assert "file.txt" in conflicts


def test_land_refuses_on_drift_conflict(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    base = canonical_head(root)
    _make_branch_commit(root, "20.edit", path="file.txt", content="branch change\n")
    # main advances past base_ref with a conflicting edit.
    (root / "file.txt").write_text("main change\n")
    _git(root, "commit", "-am", "main edits file")

    assert base_ref_drifted(root, base) is True
    result = land(root, "20.edit", "edit file", queue=None, base_ref=base)
    assert result.landed is False
    assert result.conflict is True
    # Branch preserved for resolution.
    assert _git(root, "branch", "--list", worktree_branch("20.edit")).strip() != ""
