"""Tests for ``nightshift.run_local`` — the one-shot local runner, ported in
Phase 9 to drive the production manager/worker split.

These are end-to-end: each test invokes ``run_local.main()`` which stands up a
real in-process manager (uvicorn on a loopback port, ephemeral store) plus a
worker loop, and drains the queue through the production wire — brief →
dispatch → execute (fake backend) → validate → land → brief consumed.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import nightshift.backends as backends_mod
from _workspace import build_workspace, git
from nightshift.backends import WorkerResult
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.run_local import _Tee, main, open_run_log
from nightshift.spawn_daily import split_frontmatter


REPO = "longitude"


# --------------------------------------------------------------------------- #
# Tee / log plumbing
# --------------------------------------------------------------------------- #


def test_tee_writes_to_all_streams() -> None:
    a, b = io.StringIO(), io.StringIO()
    tee = _Tee(a, b)
    n = tee.write("hello")
    tee.write(" world")
    assert n == 5
    assert a.getvalue() == "hello world"
    assert b.getvalue() == "hello world"
    assert tee.isatty() is False


def test_open_run_log_creates_timestamped_log(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, repos=(), main_repo=None)
    tasks_root = workspace / DEFAULT_TASKS_REPO
    log = open_run_log(tasks_root, "main")
    try:
        path = Path(log.name)
        # Run logs are gitignored runtime state under the queue in the store.
        assert path.parent == tasks_root / "main" / "logs"
        assert path.suffix == ".log"
        assert path.name.startswith("nightshift-local-")
        log.write("progress line\n")
        log.flush()
    finally:
        log.close()
    assert path.read_text() == "progress line\n"


# --------------------------------------------------------------------------- #
# End-to-end drains through the in-process manager + worker loop
# --------------------------------------------------------------------------- #


class _CommittingBackend:
    """A fake agentic backend that writes a file and commits it in the
    worktree, so the worker produces a landable branch the manager squashes."""

    name = "claude-code"
    agentic = True

    def __init__(self, on_run=None) -> None:
        self.calls: list[str] = []
        self._on_run = on_run

    def available(self, config=None) -> bool:
        return True

    def run(self, spec, emit_log, should_abort, on_worker_start=None) -> WorkerResult:
        self.calls.append(spec.task)
        emit_log(f"fake backend working on {spec.task}\n")
        (spec.cwd / f"GENERATED-{spec.task}.txt").write_text(f"done by {spec.task}\n")
        subprocess.run(["git", "add", "-A"], cwd=spec.cwd, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"work for {spec.task}"],
            cwd=spec.cwd, check=True, capture_output=True,
        )
        if self._on_run is not None:
            self._on_run(spec.task)
        return WorkerResult(returncode=0)


class _FailingBackend:
    name = "claude-code"
    agentic = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def available(self, config=None) -> bool:
        return True

    def run(self, spec, emit_log, should_abort, on_worker_start=None) -> WorkerResult:
        self.calls.append(spec.task)
        emit_log("fake backend exploding\n")
        return WorkerResult(returncode=1, error="worker exploded")


def _prep(workspace: Path, monkeypatch, backend) -> Path:
    """Satisfy run_local's pre-flight (claude bin + API key + a passing
    `just validate` in the target repo) and install the fake backend."""
    repo_root = workspace / REPO
    monkeypatch.setattr("nightshift.preflight.shutil.which", lambda _n: "/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    (repo_root / "justfile").write_text("validate:\n\t@true\n")
    git(repo_root, "add", "-A")
    git(repo_root, "commit", "-m", "add justfile")
    monkeypatch.setattr(backends_mod, "require_backend", lambda _name: backend)
    monkeypatch.setattr(backends_mod, "get_backend", lambda _name=None: backend)
    return repo_root


def test_run_local_drains_queue_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """Two briefs → both dispatched, executed, validated, landed on the target
    repo's main, and consumed from the queue; exit code 0."""
    workspace = build_workspace(tmp_path, tasks={
        "10.a": "---\nmodel: auto\n---\nDo a.",
        "20.b": "Do b.",
    })
    backend = _CommittingBackend()
    repo_root = _prep(workspace, monkeypatch, backend)
    tasks_root = workspace / DEFAULT_TASKS_REPO

    rc = main(["--workspace", str(workspace)])

    assert rc == 0
    assert sorted(backend.calls) == ["10.a", "20.b"]
    log = git(repo_root, "log", "--oneline")
    assert "task: " in log
    assert (repo_root / "GENERATED-10.a.txt").exists()
    assert (repo_root / "GENERATED-20.b.txt").exists()
    # Briefs consumed: a landed regular task leaves the queue.
    assert not (tasks_root / "main" / "10.a.md").exists()
    assert not (tasks_root / "main" / "20.b.md").exists()
    # The run transcript was tee'd to a local log under the store.
    logs = list((tasks_root / "main" / "logs").glob("nightshift-local-*.log"))
    assert len(logs) == 1
    assert "fake backend working on 10.a" in logs[0].read_text()


def test_run_local_single_task_runs_only_selection(tmp_path: Path, monkeypatch) -> None:
    """--task <name> runs exactly that task; the rest of the queue is held and
    its briefs are untouched."""
    workspace = build_workspace(tmp_path, tasks={
        "10.a": "Do a.",
        "20.b": "Do b.",
    })
    backend = _CommittingBackend()
    repo_root = _prep(workspace, monkeypatch, backend)
    tasks_root = workspace / DEFAULT_TASKS_REPO

    rc = main(["--workspace", str(workspace), "--task", "20.b"])

    assert rc == 0
    assert backend.calls == ["20.b"]
    assert (repo_root / "GENERATED-20.b.txt").exists()
    assert not (repo_root / "GENERATED-10.a.txt").exists()
    # The unselected brief is still queued; the selected one was consumed.
    assert (tasks_root / "main" / "10.a.md").exists()
    assert not (tasks_root / "main" / "20.b.md").exists()


def test_run_local_picks_up_midrun_addition(tmp_path: Path, monkeypatch) -> None:
    """A brief added while the run is draining is dispatched in the same
    invocation (parity with the legacy follow-queue drain)."""
    workspace = build_workspace(tmp_path, tasks={"10.a": "Do a."})
    tasks_root = workspace / DEFAULT_TASKS_REPO

    def _add_brief(task: str) -> None:
        if task == "10.a":
            (tasks_root / "main" / "20.late.md").write_text("Do the late thing.\n")

    backend = _CommittingBackend(on_run=_add_brief)
    repo_root = _prep(workspace, monkeypatch, backend)

    rc = main(["--workspace", str(workspace)])

    assert rc == 0
    assert backend.calls == ["10.a", "20.late"]
    assert (repo_root / "GENERATED-20.late.txt").exists()
    assert not (tasks_root / "main" / "20.late.md").exists()


def test_run_local_failure_returns_nonzero(tmp_path: Path, monkeypatch) -> None:
    """A failing worker yields exit code 1, lands nothing, and each task is
    attempted at most once per run (the failed task's retry backoff keeps it
    out of dispatch for the rest of the drain)."""
    workspace = build_workspace(tmp_path, tasks={"10.a": "Do a."})
    backend = _FailingBackend()
    repo_root = _prep(workspace, monkeypatch, backend)
    tasks_root = workspace / DEFAULT_TASKS_REPO

    rc = main(["--workspace", str(workspace)])

    assert rc == 1
    assert backend.calls == ["10.a"]  # one attempt per task per run
    assert not (repo_root / "GENERATED-10.a.txt").exists()
    # Production failure bookkeeping: the brief stays queued, held via
    # frontmatter/state rather than deleted.
    brief = tasks_root / "main" / "10.a.md"
    assert brief.exists()
    meta, _body = split_frontmatter(brief.read_text())
    assert meta.get("failed") is True or meta.get("quarantined") is True


def test_run_local_declined_reoffer_does_not_strand_queue(
    tmp_path: Path, monkeypatch
) -> None:
    """An evergreen task lands, keeps its brief, and is immediately re-offered
    (holds cleared, progress reset, no backoff, first in sort order). The
    one-shot decline must block it in the ephemeral store and keep draining,
    so the task behind it still runs (regression: the decline used to stop
    the whole drain)."""
    workspace = build_workspace(tmp_path, tasks={
        "10.a": "---\nevergreen: true\n---\nDo a.",
        "20.b": "Do b.",
    })
    backend = _CommittingBackend()
    repo_root = _prep(workspace, monkeypatch, backend)
    tasks_root = workspace / DEFAULT_TASKS_REPO

    rc = main(["--workspace", str(workspace)])

    assert rc == 0
    # 10.a ran once (its immediate re-offer declined + held), then 20.b ran.
    assert backend.calls == ["10.a", "20.b"]
    assert (repo_root / "GENERATED-10.a.txt").exists()
    assert (repo_root / "GENERATED-20.b.txt").exists()
    # The evergreen brief is kept (and untouched by the ephemeral hold); the
    # regular one was consumed by its land.
    assert (tasks_root / "main" / "10.a.md").exists()
    assert not (tasks_root / "main" / "20.b.md").exists()


def test_run_local_empty_queue_is_a_noop(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = build_workspace(tmp_path, tasks={})
    backend = _CommittingBackend()
    _prep(workspace, monkeypatch, backend)

    rc = main(["--workspace", str(workspace)])

    assert rc == 0
    assert backend.calls == []
    assert "No tasks to run." in capsys.readouterr().out


def test_run_local_unknown_single_task_reports(tmp_path: Path, monkeypatch, capsys) -> None:
    """Naming a task with no brief runs nothing: the manager never dispatches
    a nonexistent stem, so the run drains empty and reports it."""
    workspace = build_workspace(tmp_path, tasks={"10.a": "Do a."})
    backend = _CommittingBackend()
    _prep(workspace, monkeypatch, backend)
    tasks_root = workspace / DEFAULT_TASKS_REPO

    rc = main(["--workspace", str(workspace), "--task", "99.ghost"])

    assert rc == 0
    assert backend.calls == []
    assert "No tasks to run." in capsys.readouterr().out
    # Nothing was consumed.
    assert (tasks_root / "main" / "10.a.md").exists()


def test_run_local_respects_alternate_queue(tmp_path: Path, monkeypatch) -> None:
    """--queue drains an alternate queue only; main is left alone."""
    workspace = build_workspace(
        tmp_path,
        tasks={"10.main-task": "Do main."},
        queues={
            "sidework": {
                "tasks": {"10.side": "Do side."},
                "config": {"order": [], "repo": REPO, "validate": "true"},
            }
        },
    )
    backend = _CommittingBackend()
    repo_root = _prep(workspace, monkeypatch, backend)
    tasks_root = workspace / DEFAULT_TASKS_REPO

    rc = main(["--workspace", str(workspace), "--queue", "sidework"])

    assert rc == 0
    assert backend.calls == ["10.side"]
    assert (repo_root / "GENERATED-10.side.txt").exists()
    assert (tasks_root / "main" / "10.main-task.md").exists()
    assert not (tasks_root / "sidework" / "10.side.md").exists()
