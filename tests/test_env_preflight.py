"""Environment preflight — keep the worker's shared venv in step with the
committed lockfile *before* any model spend, so a dependency that landed on
another machine but is missing here fails cheaply up front instead of after the
agent runs (at validate time, as an import error).

Covers three layers:

* the lockfile fingerprint + marker gate (``lock_fingerprint`` /
  ``ensure_env_synced``) — the cheap common-path skip and the self-heal,
* the fast-forward invalidation signal (``lock_changed_between`` +
  ``maybe_sync_main_to_origin``), and
* the worker seam (``execute_work_order``) — a failing preflight returns
  ``preflight_failed`` and the backend is never invoked (no tokens burned).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import nightshift.backends as backends_mod
from _workspace import build_workspace, git, git_commit_all
from nightshift.backends import WorkerResult
from nightshift.engine import (
    DEFAULT_PREFLIGHT_CMD,
    ensure_env_synced,
    lock_changed_between,
    lock_fingerprint,
    maybe_sync_main_to_origin,
    preflight_cmd_from_blob,
    resolve_preflight_cmd,
)
from nightshift.worker.config import WorkerConfig
from nightshift.worker.execute import execute_work_order


# --------------------------------------------------------------------------- #
# config resolution — mirrors the validate_cmd semantics
# --------------------------------------------------------------------------- #


def test_preflight_absent_key_inherits_default() -> None:
    assert resolve_preflight_cmd({}) == DEFAULT_PREFLIGHT_CMD.split()


def test_preflight_empty_string_opts_out() -> None:
    # An explicit empty string disables the preflight (never falls back).
    assert resolve_preflight_cmd({"preflight": ""}) is None
    assert resolve_preflight_cmd({"preflight": "   "}) is None


def test_preflight_custom_command_split() -> None:
    assert resolve_preflight_cmd({"preflight": "uv sync --extra x"}) == [
        "uv", "sync", "--extra", "x",
    ]


def test_preflight_cmd_from_blob_prefers_explicit_cmd() -> None:
    # The manager sends an authoritative preflight_cmd string (empty = disabled).
    assert preflight_cmd_from_blob({"preflight_cmd": ""}) == (None, None)
    argv, display = preflight_cmd_from_blob({"preflight_cmd": "uv sync --frozen"})
    assert argv == ["uv", "sync", "--frozen"]
    assert display == "uv sync --frozen"


# --------------------------------------------------------------------------- #
# fingerprint
# --------------------------------------------------------------------------- #


def test_fingerprint_none_without_lockfile(tmp_path: Path) -> None:
    assert lock_fingerprint(tmp_path) is None


def test_fingerprint_changes_with_contents(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("a = 1\n")
    first = lock_fingerprint(tmp_path)
    assert first is not None
    (tmp_path / "uv.lock").write_text("a = 2\n")
    assert lock_fingerprint(tmp_path) != first


# --------------------------------------------------------------------------- #
# ensure_env_synced — the marker gate
# --------------------------------------------------------------------------- #


def _venv(tmp_path: Path) -> Path:
    """A repo root with a lockfile and a (fake) .venv dir the marker lives in."""
    (tmp_path / "uv.lock").write_text("dep = 1\n")
    (tmp_path / ".venv").mkdir()
    return tmp_path


def test_ensure_disabled_is_noop(tmp_path: Path) -> None:
    r = ensure_env_synced(_venv(tmp_path), preflight_argv=None)
    assert (r.ok, r.synced) == (True, False)


def test_ensure_no_lockfile_is_noop(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    r = ensure_env_synced(tmp_path, preflight_argv=["false"])  # would fail if run
    assert (r.ok, r.synced) == (True, False)


def test_ensure_syncs_then_fast_paths(tmp_path: Path) -> None:
    repo = _venv(tmp_path)
    calls = _counting_marker(repo)

    # First call: marker missing -> sync runs, marker written.
    r1 = ensure_env_synced(repo, preflight_argv=["true"])
    assert (r1.ok, r1.synced) == (True, True)
    assert calls() == 1  # sync happened once (marker now present)

    # Second call: fingerprint matches marker -> fast path, nothing runs.
    r2 = ensure_env_synced(repo, preflight_argv=["true"])
    assert (r2.ok, r2.synced) == (True, False)


def test_ensure_resyncs_when_lock_changes(tmp_path: Path) -> None:
    repo = _venv(tmp_path)
    assert ensure_env_synced(repo, preflight_argv=["true"]).synced is True
    assert ensure_env_synced(repo, preflight_argv=["true"]).synced is False  # cached

    # The lockfile changes (a new dependency landed) -> the gate re-fires.
    (repo / "uv.lock").write_text("dep = 2\n")
    assert ensure_env_synced(repo, preflight_argv=["true"]).synced is True


def test_ensure_failed_sync_reports_and_leaves_marker_unset(tmp_path: Path) -> None:
    repo = _venv(tmp_path)
    r = ensure_env_synced(
        repo, preflight_argv=["sh", "-c", "echo boom >&2; exit 3"]
    )
    assert r.ok is False
    assert r.synced is True
    assert "boom" in r.detail
    # No marker written on failure -> the next attempt retries rather than
    # falsely believing the env is in sync.
    assert not (repo / ".venv" / ".nightshift-lock-hash").exists()
    # And indeed the next run tries again (would sync).
    assert ensure_env_synced(repo, preflight_argv=["true"]).synced is True


def _counting_marker(repo: Path):
    marker = repo / ".venv" / ".nightshift-lock-hash"

    def count() -> int:
        return 1 if marker.exists() else 0

    return count


# --------------------------------------------------------------------------- #
# fast-forward invalidation signal
# --------------------------------------------------------------------------- #


def test_lock_changed_between_detects_lockfile_edit(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "uv.lock").write_text("dep = 1\n")
    old = git_commit_all(repo, "lock v1")
    # A non-lock change does not trip the signal.
    (repo / "README.md").write_text("hi\n")
    mid = git_commit_all(repo, "docs")
    assert lock_changed_between(repo, old, mid) is False
    # A lock change does.
    (repo / "uv.lock").write_text("dep = 2\n")
    new = git_commit_all(repo, "lock v2")
    assert lock_changed_between(repo, mid, new) is True


def test_ff_invalidates_marker_when_lock_changes(tmp_path: Path) -> None:
    """A fast-forward that pulls in a changed lockfile drops the venv marker so
    the next preflight re-syncs; a ff with no lock change leaves it intact."""
    from _workspace import add_remote, make_bare_remote

    workspace = build_workspace(tmp_path, repos=("longitude",))
    repo_root = workspace / "longitude"
    (repo_root / "uv.lock").write_text("dep = 1\n")
    git_commit_all(repo_root, "add lockfile")
    bare = make_bare_remote(tmp_path / "origin.git")
    add_remote(repo_root, "origin", bare)

    # Establish a marker: pretend the venv is synced to the current lockfile.
    (repo_root / ".venv").mkdir()
    marker = repo_root / ".venv" / ".nightshift-lock-hash"
    marker.write_text((lock_fingerprint(repo_root) or "") + "\n")

    # Another actor advances origin/main WITHOUT touching the lockfile.
    _advance_origin(tmp_path, bare, {"notes.txt": "hello\n"}, "docs")
    maybe_sync_main_to_origin(workspace, "longitude", "origin", force=True)
    assert marker.exists(), "non-lock ff must not invalidate the marker"

    # Now origin advances WITH a lockfile change -> ff must drop the marker.
    _advance_origin(tmp_path, bare, {"uv.lock": "dep = 2\n"}, "bump dep")
    maybe_sync_main_to_origin(workspace, "longitude", "origin", force=True)
    assert not marker.exists(), "lock-changing ff must invalidate the marker"


def _advance_origin(
    tmp_path: Path, bare: Path, files: dict[str, str], tag: str
) -> None:
    """Push a commit to ``bare``'s main from a throwaway clone (simulating
    another machine landing work while our clone sits behind)."""
    other = tmp_path / f"other-{tag}".replace(" ", "-")
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "o@o"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "o"], check=True)
    for rel, content in files.items():
        (other / rel).write_text(content)
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(other), "commit", "-m", f"origin: {tag}"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "push", "origin", "main"], check=True, capture_output=True
    )


# --------------------------------------------------------------------------- #
# worker seam — the whole point: fail before spending on the model
# --------------------------------------------------------------------------- #


class _SpyBackend:
    """Records whether the backend was ever asked to run."""

    name = "ollama-cloud"
    agentic = False

    def __init__(self) -> None:
        self.ran = False

    def available(self, config: Any = None) -> bool:
        return True

    def run(self, spec, emit_log, should_abort, on_worker_start=None) -> WorkerResult:
        self.ran = True
        emit_log("ran\n")
        return WorkerResult(returncode=0, turns=1)


def _order(preflight_cmd: str) -> dict[str, Any]:
    return {
        "task": "00.demo",
        "repo": "longitude",
        "queue": "main",
        "body": "Do a thing.",
        "base_ref": "HEAD",
        "config": {
            "model": "ollama-cloud/gpt-oss:120b",
            "validate": "",
            "preflight_cmd": preflight_cmd,
        },
    }


def test_execute_preflight_failure_skips_backend(tmp_path: Path, monkeypatch) -> None:
    """A failing preflight returns preflight_failed and the backend never runs —
    no model tokens are spent on an environment that can't even import."""
    workspace = build_workspace(tmp_path, tasks={"00.demo": "Do a thing."})
    repo_root = workspace / "longitude"
    # A lockfile + venv so the gate engages (marker missing -> preflight runs).
    (repo_root / "uv.lock").write_text("dep = 1\n")
    (repo_root / ".venv").mkdir()

    spy = _SpyBackend()
    monkeypatch.setattr(backends_mod, "require_backend", lambda _p: spy)
    cfg = WorkerConfig(
        workspace=workspace, worker_id="w", manager_url="http://x",
        models=["ollama-cloud/gpt-oss:120b"],
    )

    outcome = execute_work_order(
        cfg, _order("sh -c 'echo missing cvxpy >&2; exit 1'"),
        on_phase=lambda _p: None, on_log=lambda _l: None,
    )

    assert spy.ran is False, "backend must not run when the preflight fails"
    assert outcome.status == "error"
    assert outcome.failure_kind == "preflight_failed"
    assert outcome.landable is False
    assert "missing cvxpy" in (outcome.failure_reason or "")


def test_execute_preflight_success_runs_backend(tmp_path: Path, monkeypatch) -> None:
    """A passing preflight (here a no-op ``true``) lets the run proceed to the
    backend, and the venv marker is written for the fast path next time."""
    workspace = build_workspace(tmp_path, tasks={"00.demo": "Do a thing."})
    repo_root = workspace / "longitude"
    (repo_root / "uv.lock").write_text("dep = 1\n")
    (repo_root / ".venv").mkdir()

    spy = _SpyBackend()
    monkeypatch.setattr(backends_mod, "require_backend", lambda _p: spy)
    cfg = WorkerConfig(
        workspace=workspace, worker_id="w", manager_url="http://x",
        models=["ollama-cloud/gpt-oss:120b"],
    )

    outcome = execute_work_order(
        cfg, _order("true"), on_phase=lambda _p: None, on_log=lambda _l: None,
    )

    assert spy.ran is True
    assert outcome.failure_kind != "preflight_failed"
    assert (repo_root / ".venv" / ".nightshift-lock-hash").exists()


def test_execute_preflight_disabled_runs_backend(tmp_path: Path, monkeypatch) -> None:
    """With the preflight disabled (empty command) the backend runs and no
    marker is written — the feature is entirely opt-out per queue."""
    workspace = build_workspace(tmp_path, tasks={"00.demo": "Do a thing."})
    repo_root = workspace / "longitude"
    (repo_root / "uv.lock").write_text("dep = 1\n")
    (repo_root / ".venv").mkdir()

    spy = _SpyBackend()
    monkeypatch.setattr(backends_mod, "require_backend", lambda _p: spy)
    cfg = WorkerConfig(
        workspace=workspace, worker_id="w", manager_url="http://x",
        models=["ollama-cloud/gpt-oss:120b"],
    )

    outcome = execute_work_order(
        cfg, _order(""), on_phase=lambda _p: None, on_log=lambda _l: None,
    )

    assert spy.ran is True
    assert outcome.failure_kind != "preflight_failed"
    assert not (repo_root / ".venv" / ".nightshift-lock-hash").exists()
