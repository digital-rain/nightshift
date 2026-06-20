"""Unit coverage for ``nightshift.repos`` — workspace repo addressing.

The two-root model addresses every target repo by a **bare workspace child
slug**. This module's job is to (a) resolve a task's repo by precedence (task
frontmatter → queue default), (b) reject malformed/unsafe references as an
authoring-time error, and (c) answer availability + known-set questions against
a real workspace on disk. Those three concerns are exercised here without a
manager or a worker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _workspace import build_workspace, make_target_repo
from nightshift.repos import (
    DEFAULT_TASKS_REPO,
    RepoConfigError,
    is_valid_repo_ref,
    known_repos,
    repo_available,
    repo_root,
    resolve_repo,
)


# References that are NOT a bare child slug — the path-traversal guard must
# reject every one of these (paths, ``..``, ``/``, absolute paths, and the
# reserved ``runs`` infra name + non-slug shapes).
MALFORMED_REFS = [
    "../evil",
    "..",
    "a/b",
    "sub/dir",
    "/abs",
    "/abs/path",
    "/",
    "./x",
    "a/",
    "Upper",
    "with space",
    "weird.name",
    "_leading",
    "-leading",
    "runs",  # reserved content-store infra dir, never a target repo
]


def test_default_tasks_repo_name() -> None:
    assert DEFAULT_TASKS_REPO == "nightshift-tasks"


# --------------------------------------------------------------------------- #
# is_valid_repo_ref edge cases
# --------------------------------------------------------------------------- #


def test_is_valid_repo_ref_accepts_bare_slugs() -> None:
    assert is_valid_repo_ref("longitude") is True
    assert is_valid_repo_ref("a") is True
    assert is_valid_repo_ref("a-b-c") is True
    assert is_valid_repo_ref("repo1") is True
    assert is_valid_repo_ref("0starts-with-digit") is True


def test_is_valid_repo_ref_rejects_empty_and_none() -> None:
    assert is_valid_repo_ref(None) is False
    assert is_valid_repo_ref("") is False


@pytest.mark.parametrize("bad", MALFORMED_REFS)
def test_is_valid_repo_ref_rejects_malformed(bad: str) -> None:
    assert is_valid_repo_ref(bad) is False


# --------------------------------------------------------------------------- #
# resolve_repo precedence + malformed rejection
# --------------------------------------------------------------------------- #


def test_resolve_repo_task_overrides_queue() -> None:
    assert resolve_repo("longitude", "atlas") == "longitude"


def test_resolve_repo_falls_back_to_queue() -> None:
    assert resolve_repo(None, "atlas") == "atlas"
    # An empty/blank task ref is treated as unset and falls back to the queue.
    assert resolve_repo("", "atlas") == "atlas"
    assert resolve_repo("   ", "atlas") == "atlas"


def test_resolve_repo_raises_when_neither_set() -> None:
    for task_repo, queue_repo in [(None, None), ("", ""), ("   ", None), (None, "  ")]:
        with pytest.raises(RepoConfigError):
            resolve_repo(task_repo, queue_repo)


@pytest.mark.parametrize("bad", MALFORMED_REFS)
def test_resolve_repo_rejects_malformed_task_ref(bad: str) -> None:
    # A malformed task ref is rejected even when the queue default is valid
    # (the task ref wins by precedence, then fails the path-traversal guard).
    with pytest.raises(RepoConfigError):
        resolve_repo(bad, "longitude")


@pytest.mark.parametrize("bad", MALFORMED_REFS)
def test_resolve_repo_rejects_malformed_queue_ref(bad: str) -> None:
    with pytest.raises(RepoConfigError):
        resolve_repo(None, bad)


# --------------------------------------------------------------------------- #
# repo_root
# --------------------------------------------------------------------------- #


def test_repo_root_is_workspace_child(tmp_path: Path) -> None:
    assert repo_root(tmp_path, "longitude") == tmp_path / "longitude"


# --------------------------------------------------------------------------- #
# repo_available
# --------------------------------------------------------------------------- #


def test_repo_available_present_with_git(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, repos=("longitude",), main_repo="longitude")
    assert repo_available(workspace, "longitude") is True


def test_repo_available_absent(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, repos=("longitude",), main_repo="longitude")
    assert repo_available(workspace, "ghost") is False


def test_repo_available_present_without_git(tmp_path: Path) -> None:
    # A plain (non-git) child directory is not an available repo.
    workspace = build_workspace(tmp_path, repos=(), main_repo=None)
    plain = workspace / "plain"
    plain.mkdir()
    (plain / "README.md").write_text("hi\n")
    assert repo_available(workspace, "plain") is False


def test_repo_available_malformed_is_false(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, repos=("longitude",), main_repo="longitude")
    # The path-traversal guard short-circuits availability for unsafe refs.
    assert repo_available(workspace, "../longitude") is False
    assert repo_available(workspace, "/abs") is False
    assert repo_available(workspace, "a/b") is False


# --------------------------------------------------------------------------- #
# known_repos
# --------------------------------------------------------------------------- #


def test_known_repos_sorted_children_with_git_including_tasks(tmp_path: Path) -> None:
    workspace = build_workspace(
        tmp_path, repos=("longitude", "atlas"), main_repo="longitude"
    )
    # A plain (non-git) child and a hidden dir are excluded; the content-store
    # repo (nightshift-tasks) is a workspace child with .git like any other.
    (workspace / "scratch").mkdir()
    (workspace / ".hidden").mkdir()

    assert known_repos(workspace) == ["atlas", "longitude", "nightshift-tasks"]


def test_known_repos_picks_up_a_freshly_created_repo(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, repos=("longitude",), main_repo="longitude")
    assert "ghost" not in known_repos(workspace)
    make_target_repo(workspace, "ghost")
    assert known_repos(workspace) == ["ghost", "longitude", "nightshift-tasks"]


def test_known_repos_empty_when_workspace_missing(tmp_path: Path) -> None:
    assert known_repos(tmp_path / "does-not-exist") == []
