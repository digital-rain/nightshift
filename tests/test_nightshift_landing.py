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
from nightshift.manager import landing as landing_mod
from nightshift.manager.landing import (
    base_ref_drifted,
    canonical_head,
    land,
    main_advanced_sha,
    merge_tree_conflicts,
    push_resolved_main,
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


def test_main_advanced_sha_detects_agent_land(tmp_path: Path) -> None:
    workspace, repo, repo_root = _init_repo(tmp_path)
    base = canonical_head(repo_root)
    assert main_advanced_sha(repo_root, base) is None
    (repo_root / "file.txt").write_text("agent landed\n")
    _git(repo_root, "commit", "-am", "feat: agent landed on main")
    assert main_advanced_sha(repo_root, base) == canonical_head(repo_root)


def test_adopt_agent_land_on_main_without_branch(tmp_path: Path) -> None:
    """When an agent squash-merges to main directly, adopt HEAD instead of
    reporting no changes."""
    workspace, repo, repo_root = _init_repo(tmp_path)
    base = canonical_head(repo_root)
    (repo_root / "file.txt").write_text("agent landed\n")
    _git(repo_root, "commit", "-am", "feat: agent landed on main")
    result = land(workspace, repo, "10.adopt", "adopt agent land", queue=None, base_ref=base)
    assert result.landed is True
    assert result.sha == canonical_head(repo_root)
    assert "adopted agent land" in result.detail
    assert "agent landed" in (repo_root / "file.txt").read_text()


def test_adopt_does_not_trigger_when_branch_has_commits(tmp_path: Path) -> None:
    workspace, repo, repo_root = _init_repo(tmp_path)
    base = canonical_head(repo_root)
    _make_branch_commit(workspace, repo, "10.add", path="new.txt", content="branch\n")
    # main also advanced independently (unrelated to adopt path).
    (repo_root / "file.txt").write_text("other\n")
    _git(repo_root, "commit", "-am", "other main commit")
    result = land(workspace, repo, "10.add", "add new file", queue=None, base_ref=base)
    assert result.landed is True
    assert (repo_root / "new.txt").exists()


def test_push_mode_records_pushed_on_success(tmp_path: Path) -> None:
    from _workspace import add_remote, make_bare_remote

    workspace, repo, repo_root = _init_repo(tmp_path)
    bare = make_bare_remote(tmp_path / "origin.git")
    add_remote(repo_root, "origin", bare)
    _make_branch_commit(workspace, repo, "10.add", path="new.txt", content="hello\n")
    base = canonical_head(repo_root)
    result = land(
        workspace, repo, "10.add", "add new file", queue=None, base_ref=base,
        landing_mode="push",
    )
    assert result.landed is True
    assert result.remote == "push"
    assert result.pushed is True


def _advance_origin(
    tmp_path: Path, bare: Path, *, path: str, content: str, tag: str
) -> None:
    """Push a commit to ``bare``'s main from a throwaway clone, simulating
    another actor advancing origin/main between our dispatch and our land."""
    other = tmp_path / f"other-{tag}"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "o@o"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "o"], check=True)
    (other / path).write_text(content)
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(other), "commit", "-m", f"origin: {tag}"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "push", "origin", "main"], check=True, capture_output=True
    )


def test_push_mode_integrates_origin_advance(tmp_path: Path) -> None:
    """Integrate-first: a non-conflicting origin advance is pulled in before the
    squash, so the land replays on top of it and ships both changes."""
    from _workspace import add_remote, make_bare_remote

    workspace, repo, repo_root = _init_repo(tmp_path)
    bare = make_bare_remote(tmp_path / "origin.git")
    add_remote(repo_root, "origin", bare)
    base = canonical_head(repo_root)
    _make_branch_commit(workspace, repo, "10.add", path="new.txt", content="hello\n")
    # Origin advances (a different file) while local main is untouched.
    _advance_origin(tmp_path, bare, path="other.txt", content="from origin\n", tag="adv")

    result = land(
        workspace, repo, "10.add", "add new file", queue=None, base_ref=base,
        landing_mode="push", rendezvous_remote="origin",
    )
    assert result.landed is True
    assert result.pushed is True
    # Local main now carries both the origin advance and our squash.
    assert (repo_root / "other.txt").exists()
    assert (repo_root / "new.txt").exists()


def test_push_retry_after_nonfastforward_rejection(
    tmp_path: Path, monkeypatch
) -> None:
    """A rejected push triggers a re-sync + re-squash + re-push under the bounded
    retry loop; the branch is preserved across the retry so it can re-squash."""
    from _workspace import add_remote, make_bare_remote

    workspace, repo, repo_root = _init_repo(tmp_path)
    bare = make_bare_remote(tmp_path / "origin.git")
    add_remote(repo_root, "origin", bare)
    base = canonical_head(repo_root)
    _make_branch_commit(workspace, repo, "10.add", path="new.txt", content="hello\n")

    calls = {"n": 0}
    real = landing_mod._push_head_to_main

    def flaky(ws: Path, r: str, remote: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "non-fast-forward (simulated)"
        return real(ws, r, remote)

    monkeypatch.setattr(landing_mod, "_push_head_to_main", flaky)
    result = land(
        workspace, repo, "10.add", "add new file", queue=None, base_ref=base,
        landing_mode="push", rendezvous_remote="origin", max_push_retries=3,
    )
    assert result.landed is True
    assert result.pushed is True
    assert calls["n"] == 2  # one rejection, then success
    assert (repo_root / "new.txt").exists()


def test_push_resolved_main_replays_onto_advanced_origin(tmp_path: Path) -> None:
    from _workspace import add_remote, make_bare_remote

    workspace, repo, repo_root = _init_repo(tmp_path)
    bare = make_bare_remote(tmp_path / "origin.git")
    add_remote(repo_root, "origin", bare)
    # A resolved squash commit sits on local main but was never pushed.
    (repo_root / "resolved.txt").write_text("resolved\n")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-m", "resolved squash")
    sha = canonical_head(repo_root)
    # Origin advances (non-conflicting) in the meantime.
    _advance_origin(tmp_path, bare, path="other.txt", content="from origin\n", tag="r")

    ok, new_sha = push_resolved_main(workspace, repo, "origin", sha, max_retries=3)
    assert ok is True
    assert new_sha
    assert (repo_root / "resolved.txt").exists()
    assert (repo_root / "other.txt").exists()


def test_push_resolved_main_reports_conflict(tmp_path: Path) -> None:
    from _workspace import add_remote, make_bare_remote

    workspace, repo, repo_root = _init_repo(tmp_path)
    bare = make_bare_remote(tmp_path / "origin.git")
    add_remote(repo_root, "origin", bare)
    # Resolved commit edits file.txt one way...
    (repo_root / "file.txt").write_text("resolved change\n")
    _git(repo_root, "commit", "-am", "resolved squash")
    sha = canonical_head(repo_root)
    # ...origin advances the same line another way → cherry-pick conflicts.
    _advance_origin(tmp_path, bare, path="file.txt", content="origin change\n", tag="c")

    ok, detail = push_resolved_main(workspace, repo, "origin", sha, max_retries=2)
    assert ok is False
    assert "conflict" in detail.lower()
