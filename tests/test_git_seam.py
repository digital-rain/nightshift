"""The git subprocess seam (Phase 2): GitRunner error policies + the grep-gate.

Covers the three explicit error policies of ``nightshift.git.runner.GitRunner``
(``run`` never raises, ``out`` returns stdout-or-None, ``must`` raises a typed
``GitError``), both against a scripted ``FakeGitRunner`` (the pattern unit
tests fake the seam with) and against real git in a tmp repo. The grep-gate
test enforces the seam: no git subprocess invocation may exist anywhere in
``src/`` outside ``nightshift/git/runner.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from _workspace import build_workspace, git, git_commit_all, git_init
from nightshift.git import GitError, GitResult, GitRunner
from nightshift.git.worktrees import (
    cleanup_task_worktree,
    setup_worktree,
    worktree_branch,
    worktree_dir,
)
from nightshift.lifecycle import FailureKind, RunStatus
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.runner_legacy import run_task
from nightshift.worker.config import WorkerConfig
from nightshift.worker.execute import execute_work_order


class FakeGitRunner(GitRunner):
    """A GitRunner that replays scripted results instead of spawning git.

    Only :meth:`run` is overridden — ``out`` and ``must`` derive their policy
    from it exactly like the real runner, so the policies are tested against
    the real derivation logic.
    """

    def __init__(self, *results: GitResult) -> None:
        super().__init__(Path("/nonexistent"))
        self._results = list(results)
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str) -> GitResult:
        self.calls.append(args)
        return self._results.pop(0)


def _result(rc: int, stdout: str = "", stderr: str = "") -> GitResult:
    return GitResult(argv=("git", "x"), returncode=rc, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------- #
# GitResult
# --------------------------------------------------------------------------- #


def test_result_ok_and_detail() -> None:
    assert _result(0).ok is True
    assert _result(1).ok is False
    # detail prefers stderr, falls back to stdout, strips, and caps at 300.
    assert _result(1, stdout="out", stderr="err\n").detail == "err"
    assert _result(1, stdout="  only stdout \n").detail == "only stdout"
    assert _result(1, stderr="x" * 999).detail == "x" * 300


# --------------------------------------------------------------------------- #
# error policies (one test per policy, on the faked seam)
# --------------------------------------------------------------------------- #


def test_run_policy_returns_result_regardless_of_failure() -> None:
    fake = FakeGitRunner(_result(128, stderr="fatal: boom"))
    res = fake.run("fetch", "origin", "main")
    assert res.ok is False
    assert res.detail == "fatal: boom"
    assert fake.calls == [("fetch", "origin", "main")]


def test_out_policy_returns_stdout_or_none() -> None:
    fake = FakeGitRunner(_result(0, stdout="abc123\n"), _result(128, stderr="bad ref"))
    assert fake.out("rev-parse", "HEAD") == "abc123"
    assert fake.out("rev-parse", "nope") is None


def test_must_policy_raises_typed_git_error() -> None:
    fake = FakeGitRunner(_result(0, stdout="ok"), _result(1, stderr="fatal: nope"))
    assert fake.must("worktree", "add", "x").ok is True
    with pytest.raises(GitError) as exc_info:
        fake.must("worktree", "add", "y")
    err = exc_info.value
    assert "worktree add y" in str(err)
    assert "fatal: nope" in str(err)
    assert err.returncode == 1
    assert err.argv == ("git", "x")


# --------------------------------------------------------------------------- #
# real git smoke (the seam actually runs git)
# --------------------------------------------------------------------------- #


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    git_init(repo)
    (repo / "README.md").write_text("hi\n")
    git_commit_all(repo, "init")
    return repo


def test_real_runner_roundtrip(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    runner = GitRunner(repo)

    head = runner.out("rev-parse", "HEAD")
    assert head == git(repo, "rev-parse", "HEAD")

    assert runner.run("rev-parse", "definitely-not-a-ref").ok is False
    assert runner.out("rev-parse", "definitely-not-a-ref") is None
    with pytest.raises(GitError):
        runner.must("rev-parse", "definitely-not-a-ref")


def test_trace_logs_every_invocation(tmp_path: Path, monkeypatch, caplog) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("NIGHTSHIFT_GIT_TRACE", "1")
    with caplog.at_level("INFO", logger="nightshift.git"):
        GitRunner(repo).run("rev-parse", "HEAD")
    assert any("git rev-parse HEAD" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# setup_worktree: the one deliberate policy change (check=True → typed error)
# --------------------------------------------------------------------------- #


def test_setup_worktree_failure_raises_typed_git_error(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path)
    with pytest.raises(GitError) as exc_info:
        setup_worktree(workspace, "longitude", "10.t", base="not-a-real-ref")
    assert "worktree add" in str(exc_info.value)


def test_run_task_maps_worktree_failure(tmp_path: Path, monkeypatch) -> None:
    """A failed worktree add surfaces as failure_kind=worktree_failed, not a
    traceback."""
    workspace = build_workspace(
        tmp_path, tasks={"10-do": "---\nmodel: auto\n---\nDo it."}
    )

    def boom(*args, **kwargs):
        raise GitError("git worktree add failed (exit 128): fatal: boom")

    monkeypatch.setattr("nightshift.runner_legacy.setup_worktree", boom)
    events = []
    result = run_task(
        workspace, workspace / DEFAULT_TASKS_REPO, "10-do", emit=events.append
    )
    assert result.success is False
    assert result.failure_kind == "worktree_failed"
    task_results = [e for e in events if e.type == "task_result"]
    assert task_results and task_results[-1].payload["failure_kind"] == "worktree_failed"


def test_execute_work_order_maps_worktree_failure(tmp_path: Path, monkeypatch) -> None:
    workspace = build_workspace(tmp_path)

    class _Backend:
        name = "claude-code"
        agentic = True

        def available(self, config=None) -> bool:
            return True

    monkeypatch.setattr(
        "nightshift.backends.require_backend", lambda name: _Backend()
    )

    def boom(*args, **kwargs):
        raise GitError("git worktree add failed (exit 128): fatal: boom")

    monkeypatch.setattr("nightshift.worker.execute.setup_worktree", boom)
    cfg = WorkerConfig(workspace=workspace, worker_id="w", manager_url="http://x")
    outcome = execute_work_order(
        cfg,
        {"task": "10-do", "repo": "longitude", "queue": "main",
         "config": {"validate": "true"}, "body": "brief"},
        on_phase=lambda p: None,
        on_log=lambda s: None,
    )
    assert outcome.status == RunStatus.ERROR
    assert outcome.landable is False
    assert outcome.failure_kind == FailureKind.WORKTREE_FAILED


# --------------------------------------------------------------------------- #
# grep-gate: no git subprocess outside nightshift/git/runner.py
# --------------------------------------------------------------------------- #

# A git invocation through subprocess: `subprocess.<fn>(["git", ...`. The gate
# is scoped to *git* subprocess calls — other tools (`gh`, `ruff`, validate
# commands, worker binaries) may legitimately shell out from src modules.
_GIT_SUBPROCESS = re.compile(
    r"subprocess\.(?:run|check_output|check_call|call|Popen)\(\s*\[\s*['\"]git['\"]"
)


def test_grep_gate_pattern_catches_the_idiom() -> None:
    # Guard the gate itself: the pattern must match the historical call shape.
    assert _GIT_SUBPROCESS.search('subprocess.run(["git", "status"], cwd=root)')
    assert _GIT_SUBPROCESS.search('subprocess.run(\n    ["git", "fetch"],\n)')
    assert _GIT_SUBPROCESS.search('subprocess.check_output(\n    [\n        "git",')
    assert not _GIT_SUBPROCESS.search('subprocess.run(["gh", "pr", "create"])')


def test_no_git_subprocess_outside_runner() -> None:
    src = Path(__file__).resolve().parent.parent / "src" / "nightshift"
    allowed = src / "git" / "runner.py"
    offenders = [
        str(path.relative_to(src))
        for path in sorted(src.rglob("*.py"))
        if path != allowed and _GIT_SUBPROCESS.search(path.read_text())
    ]
    assert offenders == [], (
        "git subprocess calls outside the GitRunner seam "
        f"(nightshift/git/runner.py): {offenders}"
    )


# --------------------------------------------------------------------------- #
# Path-safety guard: task/queue/repo names reach worktree paths and branch
# names from request payloads and stored run records, and flow into
# destructive git operations — traversal attempts must be rejected.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "..", ".", "", "a\0b"])
def test_worktree_dir_rejects_unsafe_task_names(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        worktree_dir(tmp_path, "repo", bad)


@pytest.mark.parametrize("bad", ["../escape", "q/x", ".."])
def test_worktree_dir_rejects_unsafe_queue_and_repo(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        worktree_dir(tmp_path, "repo", "task", bad)
    with pytest.raises(ValueError, match="unsafe"):
        worktree_dir(tmp_path, bad, "task")


def test_worktree_branch_rejects_unsafe_task_names() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        worktree_branch("x/../../y")


def test_worktree_naming_accepts_normal_slugs(tmp_path: Path) -> None:
    assert worktree_branch("add-feature", "nightly") == "task-local/nightly/add-feature"
    assert worktree_dir(tmp_path, "proj", "add-feature") == (
        tmp_path / ".worktrees" / "proj" / "task-local-main-add-feature"
    )


def test_cleanup_task_worktree_refuses_traversal(tmp_path: Path) -> None:
    # A poisoned task name in a stored run record must not reach the
    # `worktree remove --force` / `branch -D` teardown path.
    (tmp_path / "escape").mkdir()
    with pytest.raises(ValueError, match="unsafe"):
        cleanup_task_worktree(tmp_path, "repo", "x/../../escape")
    assert (tmp_path / "escape").exists()
