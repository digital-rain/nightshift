"""Tests for ``nightshift.resolve_runner`` — the conflict-resolve driver the
manager's out-of-process resolve job runs (extracted from the legacy runner in
Phase 9): diagnose a failed land, then re-squash or drive an agent to resolve.

Relocated from the legacy ``test_run_local.py`` suite; behavior unchanged.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import nightshift.backends as backends_mod
from _workspace import build_workspace, git, git_commit_all
from nightshift.backends import WorkerResult
from nightshift.events import TASK_STATUS
from nightshift.git.squash import squash_failure_kind, squash_to_main
from nightshift.git.worktrees import setup_worktree
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.resolve_runner import resolve_task, write_failure_log


REPO = "longitude"


def _full(
    tmp_path: Path,
    *,
    tasks: dict[str, str] | None = None,
    config: dict[str, object] | None = None,
) -> tuple[Path, Path, Path]:
    workspace = build_workspace(tmp_path, tasks=tasks, config=config)
    return workspace, workspace / DEFAULT_TASKS_REPO, workspace / REPO


def _commit_work_on_branch(workspace: Path, repo: str, task: str) -> Path:
    """Cut a worktree for ``task`` and add a single committed file on its branch."""
    worktree = setup_worktree(workspace, repo, task)
    (worktree / "new_file.py").write_text("print('hello')\n")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "work")
    return worktree


def _seed_failed_land(workspace: Path, repo_root: Path) -> str:
    """Drive a real failed land: main gains a commit that add/add-conflicts with
    the branch's ``new_file.py``, so the squash refuses and preserves the branch.
    Returns the pre-conflict main sha (reset main to it to clear the blocker)."""
    _commit_work_on_branch(workspace, REPO, "10.hello")
    clean = git(repo_root, "rev-parse", "HEAD")
    (repo_root / "new_file.py").write_text("print('conflicting')\n")
    git(repo_root, "add", ".")
    git(repo_root, "commit", "-m", "conflicting main edit")

    sha, detail, recoverable = squash_to_main(workspace, REPO, "10.hello", "hello world")
    assert sha is None
    assert recoverable is False and "conflict" in detail.lower()
    return clean


def test_squash_failure_kind_classifies_conflict_vs_rejected() -> None:
    # A real 3-way conflict is a content conflict; a dirty main or a failed
    # commit is a (transient) rejection.
    assert squash_failure_kind(False, "merge conflict in foo.py") == "merge_conflict"
    assert squash_failure_kind(True, "main has uncommitted changes") == "merge_rejected"
    assert squash_failure_kind(False, "commit failed: nothing to commit") == "merge_rejected"


def test_write_failure_log_captures_error_and_diff(tmp_path: Path) -> None:
    workspace, _tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})

    worktree = setup_worktree(workspace, REPO, "10.hello")

    log_path = write_failure_log(
        workspace, REPO, worktree, "10.hello", "just validate failed: syntax error",
        validate_stderr="broken.py:1: SyntaxError: invalid syntax\n",
    )

    # Failure logs live under the workspace worktree area for the repo, never in
    # the target repo.
    assert str(log_path).startswith(str(workspace / ".worktrees" / REPO))
    assert log_path.exists()
    content = log_path.read_text()
    assert "10.hello" in content
    assert "just validate failed" in content
    assert "SyntaxError" in content
    lines = content.strip().splitlines()
    assert len(lines) <= 5


class _StubBackend:
    """A worker backend that runs an injected callback in the worktree instead
    of launching a real agent (used to drive the resolve agent path)."""

    def __init__(self, resolve_fn) -> None:
        self.resolve_fn = resolve_fn
        self.calls = 0

    def run(self, spec, emit_log, should_abort, on_worker_start=None) -> WorkerResult:
        self.calls += 1
        self.resolve_fn(spec.cwd)
        return WorkerResult(returncode=0)


def _seed_conflict_repo(
    tmp_path: Path,
    *,
    branch_content: str,
    main_content: str,
    max_attempts: int = 2,
) -> tuple[Path, Path, Path]:
    """Seed a workspace where the task branch and the target repo's main make
    overlapping edits to a shared file, so the squash hits a real content
    conflict. The seeded operator config disables validation friction via the
    shared builder (``validate`` resolves to a trivially-passing command), so the
    resolve path can validate cleanly. Returns ``(workspace, tasks_root,
    repo_root)``."""
    workspace, tasks_root, repo_root = _full(
        tmp_path,
        tasks={"10.hello": "Do something."},
        config={"max_resolve_attempts": max_attempts},
    )
    (repo_root / "shared.txt").write_text("base\n")
    git_commit_all(repo_root, "add shared.txt")

    worktree = setup_worktree(workspace, REPO, "10.hello")
    (worktree / "shared.txt").write_text(branch_content)
    git(worktree, "add", "shared.txt")
    git(worktree, "commit", "-m", "branch edit")

    (repo_root / "shared.txt").write_text(main_content)
    git(repo_root, "add", "shared.txt")
    git(repo_root, "commit", "-m", "main edit")
    return workspace, tasks_root, repo_root


def test_resolve_task_without_branch_reports_clearly(tmp_path: Path) -> None:
    workspace, tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    result = resolve_task(
        workspace, REPO, tasks_root, "10.hello", "hello world", emit=lambda _e: None
    )
    assert not result.success
    assert "nothing to resolve" in (result.error or "")
    assert result.failure_kind == "merge_rejected"


def test_resolve_task_lands_cleared_blocker_via_cheap_path(tmp_path: Path) -> None:
    """A blocker that has since cleared lands on the cheap re-squash path — no
    agent involved."""
    workspace, tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    clean = _seed_failed_land(workspace, repo_root)
    git(repo_root, "reset", "--hard", clean)  # the operator clears the blocker

    result = resolve_task(
        workspace, REPO, tasks_root, "10.hello", "hello world", emit=lambda _e: None
    )
    assert result.success
    assert result.commit_sha
    assert (repo_root / "new_file.py").exists()
    assert "task-local/main/10.hello" not in git(repo_root, "branch")


def test_resolve_task_agent_resolves_content_conflict(tmp_path: Path) -> None:
    """A content conflict routes to the agent path: rebase onto main, the worker
    resolves + continues the rebase, validate passes, and the work squashes in."""
    workspace, tasks_root, repo_root = _seed_conflict_repo(
        tmp_path, branch_content="branch\n", main_content="main\n",
    )

    def _resolver(cwd: Path) -> None:
        (Path(cwd) / "shared.txt").write_text("resolved\n")
        subprocess.run(
            ["git", "add", "shared.txt"], cwd=cwd, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "rebase", "--continue"],
            cwd=cwd, check=True, capture_output=True,
            env={**os.environ, "GIT_EDITOR": "true"},
        )

    stub = _StubBackend(_resolver)
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: stub
    try:
        events: list = []
        result = resolve_task(
            workspace, REPO, tasks_root, "10.hello", "hello world", emit=events.append
        )
    finally:
        backends_mod.get_backend = original

    assert result.success, result.error
    assert stub.calls == 1
    assert (repo_root / "shared.txt").read_text() == "resolved\n"
    assert "task-local/main/10.hello" not in git(repo_root, "branch")
    phases = [e.payload.get("phase") for e in events if e.type == TASK_STATUS]
    assert "resolve" in phases


def test_resolve_task_bounded_attempts_preserves_branch(tmp_path: Path) -> None:
    """When the agent can't resolve, the run is bounded by max_resolve_attempts
    and the branch is preserved for manual resolution."""
    workspace, tasks_root, repo_root = _seed_conflict_repo(
        tmp_path, branch_content="branch\n", main_content="main\n", max_attempts=1,
    )

    def _gives_up(cwd: Path) -> None:
        # Leave the rebase conflicted — the resolver should abort and stop.
        pass

    stub = _StubBackend(_gives_up)
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: stub
    try:
        result = resolve_task(
            workspace, REPO, tasks_root, "10.hello", "hello world", emit=lambda _e: None
        )
    finally:
        backends_mod.get_backend = original

    assert not result.success
    assert result.failure_kind == "merge_conflict"
    assert stub.calls == 1  # bounded by max_resolve_attempts
    assert "task-local/main/10.hello" in git(repo_root, "branch")


def test_resolve_deterministic_rebase_after_main_fixed(
    tmp_path: Path, monkeypatch,
) -> None:
    """When main was broken then fixed out-of-band, deterministic resolve rebases
    the preserved branch, re-validates, and lands without an agent."""
    import nightshift.resolve_runner as resolve_mod
    from nightshift.git import squash as squash_mod

    validate = "true"
    workspace, tasks_root, repo_root = _full(
        tmp_path,
        tasks={"10.hello": "Do something."},
        config={"validate": validate},
    )
    (repo_root / "main_ok").write_text("ok\n")
    git_commit_all(repo_root, "healthy main")

    worktree = setup_worktree(workspace, REPO, "10.hello")
    (worktree / "branch_ok").write_text("ok\n")
    git(worktree, "add", "branch_ok")
    git(worktree, "commit", "-m", "agent work")

    # Main breaks after the branch was cut (pre-existing validate failure).
    (repo_root / "main_ok").unlink()
    git(repo_root, "add", "main_ok")
    git(repo_root, "commit", "-m", "main broke")

    # Operator fixes main; the branch still holds the agent's work.
    (repo_root / "main_ok").write_text("ok\n")
    git(repo_root, "add", "main_ok")
    git(repo_root, "commit", "-m", "main fixed")

    real_squash = squash_mod.squash_to_main
    squash_calls: list[int] = []

    def _squash_once_then_real(*args, **kwargs):
        squash_calls.append(1)
        if len(squash_calls) == 1:
            return None, "simulated stale-base squash failure", False
        return real_squash(*args, **kwargs)

    monkeypatch.setattr(resolve_mod, "squash_to_main", _squash_once_then_real)

    events: list = []
    result = resolve_task(
        workspace, REPO, tasks_root, "10.hello", "hello world", emit=events.append
    )
    assert result.success, result.error
    assert len(squash_calls) == 2  # cheap path failed, deterministic path landed
    assert (repo_root / "branch_ok").exists()
    assert (repo_root / "main_ok").exists()
    assert "task-local/main/10.hello" not in git(repo_root, "branch")
    phases = [e.payload.get("phase") for e in events if e.type == TASK_STATUS]
    assert "validate" in phases
