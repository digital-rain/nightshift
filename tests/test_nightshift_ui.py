"""Tests for the Nightshift UI: engine extras, run records, player, server."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from _workspace import build_workspace, make_target_repo
from nightshift import engine, prompts, runner_legacy
from nightshift.engine import (
    Controller,
    TaskResult,
    create_task,
    delete_task,
    extract_result_line,
    list_queue,
    live_ordered_queue,
    reorder_queue,
    run_queue,
)
from nightshift.events import (
    ABORT_REASON_INTERRUPTED,
    ABORT_REASON_NO_RUNNER,
    ABORTED,
    RUN_FINISHED,
    TASK_LOG,
    TASK_RESULT,
    TASK_STARTED,
    TASK_STATUS,
    WORKER_STARTED,
    Event,
    RunStore,
)
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.server.player import ConcurrencyGate, Player, PlayerRegistry
from nightshift.server.settings import (
    load_settings,
    parse_duration,
    save_settings,
    validate_settings,
)
from nightshift.spawn_daily import load_config, save_config_value


def _seed(workspace: Path, tasks: dict[str, str] | None = None, **kw: object) -> Path:
    """Scaffold a two-root workspace via the shared builder and return its content
    store (``tasks_root = <workspace>/nightshift-tasks``).

    The builder seeds the default ``main`` queue (briefs at ``main/``, runs at
    ``main/runs``) bound to a real ``longitude`` target repo. We then pin the
    operator ``config.json`` to the legacy shape — a ``claude-sonnet-4-6`` model
    default and the ``00._todo`` evergreen list, and crucially *no* ``validate``
    key — so the migrated assertions about resolved defaults (model inheritance,
    the engine's ``just validate`` default surfacing when nothing overrides it)
    hold exactly as they did on the single-root layout. Extra builder knobs
    (``repos``, ``main_repo``, ``queues`` …) pass straight through ``**kw``.
    """
    build_workspace(workspace, tasks=tasks, **kw)
    ns_dir = workspace / ".nightshift"
    ns_dir.mkdir(parents=True, exist_ok=True)
    (ns_dir / "manager.json").write_text(
        json.dumps({"default_model": "claude-code/claude-sonnet-4-6", "evergreen_tasks": ["00._todo"]})
    )
    return workspace / DEFAULT_TASKS_REPO


# --------------------------------------------------------------------------- #
# RunStore round-trip
# --------------------------------------------------------------------------- #


def test_runstore_round_trip(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    writer = store.start("cli")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "Fix X", "frontmatter": {"model": "m", "draft": False}}))
    writer.emit(Event(TASK_LOG, {"task": "10.x", "line": "line one\n"}))
    writer.emit(Event(TASK_LOG, {"task": "10.x", "line": "line two\n"}))
    writer.emit(Event(TASK_STATUS, {"task": "10.x", "status": "running", "phase": "validate"}))
    writer.emit(Event(TASK_RESULT, {"task": "10.x", "status": "completed", "result_line": "All 5 tests pass", "commit_sha": "abc1234"}))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()

    runs = store.list_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run["launched_by"] == "cli"
    assert run["finished_at"] is not None
    rec = run["tasks"][0]
    assert rec["task"] == "10.x"
    assert rec["title"] == "Fix X"
    assert rec["status"] == "completed"
    assert rec["result_line"] == "All 5 tests pass"
    assert rec["commit_sha"] == "abc1234"
    assert rec["frontmatter"]["model"] == "m"

    log = store.read_log(writer.run_id, "10.x")
    assert "line one" in log["text"]
    assert "line two" in log["text"]
    assert log["eof"] > 0


def test_interrupted_task_shows_aborted(tmp_path: Path) -> None:
    """A task with no terminal result must not show 'running' once the run has
    finished — it was interrupted, so it should read as 'aborted'."""
    store = RunStore(tmp_path)
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(TASK_STATUS, {"task": "10.x", "status": "running", "phase": "worker"}))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["status"] == ABORTED


def test_reconcile_stale_persists_aborted(tmp_path: Path) -> None:
    """A finished run with a still-running task gets persisted as aborted with a
    reason, and is left alone if it's still the active run."""
    store = RunStore(tmp_path)
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(TASK_STATUS, {"task": "10.x", "status": "running", "phase": "worker"}))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()
    run_id = writer.run_id

    # Treated as the active run → not touched.
    assert store.reconcile_stale({run_id}) == []
    assert store.list_runs()[0].get("aborted") is not True

    # Not active → aborted and persisted with a reason.
    assert store.reconcile_stale(set()) == [run_id]
    run = store.list_runs()[0]
    assert run["aborted"] is True
    assert run["abort_reason"] == ABORT_REASON_INTERRUPTED
    assert run["tasks"][0]["status"] == ABORTED
    # Idempotent: a second pass changes nothing.
    assert store.reconcile_stale(set()) == []


def test_reconcile_respects_live_owner(tmp_path: Path) -> None:
    """A run whose driving process is still alive is never aborted, even with a
    zero idle window — its worker may just be buffering output."""
    store = RunStore(tmp_path)
    writer = store.start("cli")  # records pid = this (live) process
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(TASK_STATUS, {"task": "10.x", "status": "running", "phase": "worker"}))
    writer.close()  # finished_at stays None; pid stays alive

    assert store.reconcile_stale(set(), stale_seconds=0) == []
    assert store.list_runs()[0]["tasks"][0]["status"] == "running"


def test_reconcile_aborts_when_runner_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the driving process gone, an unfinished run with a running task is
    aborted as 'no runner' — no idle wait needed once we know it's dead."""
    from nightshift import events as events_mod

    store = RunStore(tmp_path)
    writer = store.start("cli")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(TASK_STATUS, {"task": "10.x", "status": "running", "phase": "worker"}))
    writer.close()

    monkeypatch.setattr(events_mod, "_pid_alive", lambda pid: False)

    assert store.reconcile_stale(set()) == [writer.run_id]
    run = store.list_runs()[0]
    assert run["aborted"] is True
    assert run["abort_reason"] == ABORT_REASON_NO_RUNNER
    assert run["tasks"][0]["status"] == ABORTED
    # Idempotent.
    assert store.reconcile_stale(set()) == []


def test_reconcile_respects_orphaned_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the driver is gone, a still-alive (orphaned) worker keeps the run
    from being aborted."""
    from nightshift import events as events_mod

    store = RunStore(tmp_path)
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(WORKER_STARTED, {"task": "10.x", "pid": 4242}))
    writer.emit(Event(TASK_STATUS, {"task": "10.x", "status": "running", "phase": "worker"}))
    writer.close()

    # Owner reads dead; only the recorded worker pid is alive.
    monkeypatch.setattr(events_mod, "_pid_alive", lambda pid: pid == 4242)

    assert store.reconcile_stale(set(), stale_seconds=0) == []
    assert store.list_runs()[0]["tasks"][0]["status"] == "running"


def test_reconcile_legacy_record_uses_idleness(tmp_path: Path) -> None:
    """Records predating pid tracking fall back to log/worktree idleness: a
    fresh log is left alone, a zero window aborts."""
    store = RunStore(tmp_path)
    writer = store.start("cli")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(TASK_STATUS, {"task": "10.x", "status": "running", "phase": "worker"}))
    writer.close()

    # Strip pid fields to simulate a legacy run.json.
    meta_path = store.base / writer.run_id / "run.json"
    meta = json.loads(meta_path.read_text())
    meta.pop("pid", None)
    meta.pop("worker_pid", None)
    meta_path.write_text(json.dumps(meta))

    assert store.reconcile_stale(set()) == []  # fresh event log
    assert store.reconcile_stale(set(), stale_seconds=0) == [writer.run_id]
    assert store.list_runs()[0]["tasks"][0]["status"] == ABORTED


def test_clear_runs(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    keep = store.start("ui")
    keep.emit(Event(TASK_STARTED, {"task": "10.keep", "title": "K", "frontmatter": {}}))
    keep.close()
    gone = store.start("ui")
    gone.emit(Event(TASK_STARTED, {"task": "10.gone", "title": "G", "frontmatter": {}}))
    gone.close()

    assert store.clear_runs(keep={keep.run_id}) == 1
    remaining = [r["id"] for r in store.list_runs()]
    assert remaining == [keep.run_id]
    assert store.clear_runs() == 1
    assert store.list_runs() == []


def test_delete_task(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={"alpha": "Do alpha."})
    assert (tasks_root / "main/alpha.md").exists()

    result = delete_task(tasks_root, "alpha")
    assert result["deleted"] is True
    assert not (tasks_root / "main/alpha.md").exists()

    with pytest.raises(FileNotFoundError):
        delete_task(tasks_root, "alpha")
    # Path traversal is rejected.
    with pytest.raises(FileNotFoundError):
        delete_task(tasks_root, "../../etc/passwd")


def test_resolve_claude_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    from nightshift.engine import resolve_claude_bin, worker_env

    # Explicit override wins and expands ~.
    assert resolve_claude_bin({"claude_bin": "/custom/claude"}) == "/custom/claude"
    # Otherwise PATH lookup.
    monkeypatch.setattr(prompts.shutil, "which", lambda name: "/usr/bin/claude")
    assert resolve_claude_bin({}) == "/usr/bin/claude"

    # worker_env keeps existing PATH and appends the common bin dirs.
    monkeypatch.setenv("PATH", "/bin")
    path_parts = worker_env()["PATH"].split(os.pathsep)
    assert "/bin" in path_parts
    assert str(Path.home() / ".local/bin") in path_parts


def test_worker_env_pythonpath_points_at_worktree_src(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree's own ``src`` is prepended to PYTHONPATH so ``import nightshift``
    resolves to the branch under test, not the symlinked ``.venv``'s editable
    install (which points at the main checkout's src). See worker_env()."""
    from nightshift.engine import worker_env

    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)

    # No arg → no PYTHONPATH override (unchanged behavior for other callers).
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert "PYTHONPATH" not in worker_env()

    # Worktree given → its src is first on PYTHONPATH, ahead of any inherited
    # entries (so it shadows the editable .pth from the symlinked venv).
    monkeypatch.setenv("PYTHONPATH", f"/main/src{os.pathsep}/other")
    parts = worker_env(wt)["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(wt / "src")
    assert parts[1:] == ["/main/src", "/other"]

    # A worktree without a src dir is a no-op (PYTHONPATH untouched).
    monkeypatch.setenv("PYTHONPATH", "/main/src")
    assert worker_env(tmp_path / "no-src")["PYTHONPATH"] == "/main/src"


def test_run_task_missing_claude_errors_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_root = _seed(tmp_path, tasks={"10.x": "do x"})
    monkeypatch.setattr(runner_legacy, "setup_worktree", lambda ws, repo, task, *, queue=None: tmp_path)
    monkeypatch.setattr(runner_legacy, "teardown_worktree", lambda ws, repo, task, *, queue=None: None)
    monkeypatch.setattr(runner_legacy, "build_prompt", lambda task, *, task_file=None, validate_cmd=None, loop=False, loop_max_iterations=0, split=False, split_dir=None: "p")
    monkeypatch.setattr(runner_legacy, "write_failure_log", lambda *a, **k: None)
    monkeypatch.setattr(prompts, "resolve_claude_bin", lambda config=None: "no-such-bin-xyz")

    events: list[Event] = []
    result = engine.run_task(tmp_path, tasks_root, "10.x", emit=events.append)

    assert result.success is False
    assert "not found" in (result.result_line or "")
    statuses = [e.payload.get("status") for e in events if e.type == TASK_RESULT]
    assert statuses == ["error"]


def test_delete_run(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()
    run_id = writer.run_id

    assert len(store.list_runs()) == 1
    assert store.delete_run(run_id) is True
    assert store.list_runs() == []
    # idempotent / unknown id is a no-op
    assert store.delete_run(run_id) is False
    # path traversal is rejected
    assert store.delete_run("../../etc") is False


# --------------------------------------------------------------------------- #
# result_line extraction
# --------------------------------------------------------------------------- #


def test_extract_result_line_pytest_summary() -> None:
    out = "collecting...\n=== 1291 passed, 3 skipped in 12.3s ===\n"
    assert extract_result_line(out) == "All 1291 tests pass"


def test_extract_result_line_fallback() -> None:
    assert extract_result_line("", "boom: something broke") == "boom: something broke"
    assert extract_result_line("") == "validate passed"


# --------------------------------------------------------------------------- #
# queue listing
# --------------------------------------------------------------------------- #


def test_list_queue_skips_subdirs_and_flags_evergreen(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={
        "alpha": "Do alpha.",
        "beta": "---\ntitle: Beta thing\nmodel: claude-opus-4-8\n---\nDo beta.",
        "00._todo": "---\nautosplit: true\n---\nstuff",
        "green": "---\nevergreen: true\n---\nrecurring",
    })
    notes = tasks_root / "main/notes"
    notes.mkdir()
    (notes / "ignore.md").write_text("not a task")

    queue = list_queue(tasks_root)
    names = [q["task"] for q in queue]
    assert "alpha" in names
    assert "ignore" not in names  # subdir skipped
    by = {q["task"]: q for q in queue}
    assert by["beta"]["title"] == "Beta thing"
    assert by["green"]["evergreen"] is True
    assert by["00._todo"]["evergreen"] is True  # via config evergreen_tasks


def test_read_task_returns_brief_with_resolved_frontmatter(tmp_path: Path) -> None:
    # The detail view needs the task's brief: title, body, and frontmatter merged
    # with resolved defaults (model/draft/automerge) even when the file omits them.
    from nightshift.engine import read_task

    tasks_root = _seed(tmp_path, tasks={
        "beta": "---\ntitle: Beta thing\nmodel: claude-opus-4-8\n---\nDo beta well.\n",
        "plain": "Just a body, no frontmatter.",
        "green": "---\nevergreen: true\n---\nrecurring",
    })

    beta = read_task(tasks_root, "beta")
    assert beta["task"] == "beta"
    assert beta["title"] == "Beta thing"
    assert beta["body"] == "Do beta well."
    assert beta["frontmatter"]["model"] == "claude-opus-4-8"
    # resolved defaults are filled in for omitted fields.
    assert "draft" in beta["frontmatter"]
    assert "automerge" in beta["frontmatter"]
    assert beta["evergreen"] is False

    plain = read_task(tasks_root, "plain")
    assert plain["title"] == "plain"
    assert plain["body"] == "Just a body, no frontmatter."
    # config model default applies when the file has no model.
    assert plain["frontmatter"]["model"] == "claude-code/claude-sonnet-4-6"

    green = read_task(tasks_root, "green")
    assert green["evergreen"] is True

    # missing / traversal-shaped names raise FileNotFoundError.
    with pytest.raises(FileNotFoundError):
        read_task(tasks_root, "nope")
    with pytest.raises(FileNotFoundError):
        read_task(tasks_root, "../../etc/passwd")


def test_set_task_meta_edits_toggles_and_model(tmp_path: Path) -> None:
    # The detail pane edits the frontmatter toggles and model in place, rewriting
    # existing keys where they sit and clearing the model to inherit the default.
    from nightshift.engine import read_task, set_task_meta

    tasks_root = _seed(tmp_path, tasks={
        "alpha": "---\ntitle: Alpha\nmodel: claude-opus-4-8\ndraft: false\n---\nThe brief.\n",
    })

    brief = set_task_meta(tasks_root, "alpha", {"draft": True, "evergreen": True})
    assert brief["frontmatter"]["draft"] is True
    assert brief["evergreen"] is True
    # Untouched keys survive.
    assert brief["frontmatter"]["model"] == "claude-opus-4-8"

    # model None clears the pin so the task inherits the config default.
    cleared = set_task_meta(tasks_root, "alpha", {"model": None})
    assert "model" not in cleared["frontmatter_raw"]
    assert cleared["frontmatter"]["model"] == "claude-code/claude-sonnet-4-6"
    assert "non-editable" not in read_task(tasks_root, "alpha")["body"]


def test_set_task_meta_edits_title_and_body(tmp_path: Path) -> None:
    # "all details are editable": the pane saves a new title (frontmatter
    # headline) and brief prose alongside the toggles.
    from nightshift.engine import set_task_meta

    tasks_root = _seed(tmp_path, tasks={
        "alpha": "---\ntitle: Old title\nmodel: claude-opus-4-8\n---\nOld brief.\n",
    })

    brief = set_task_meta(
        tasks_root, "alpha", {"title": "New title", "body": "New brief prose."}
    )
    assert brief["title"] == "New title"
    assert brief["body"] == "New brief prose."
    # The title rewrites the existing key in place, not a duplicate.
    text = (tasks_root / "main/alpha.md").read_text()
    assert text.count("title:") == 1
    # The model pin is preserved through a content-only edit.
    assert brief["frontmatter"]["model"] == "claude-opus-4-8"

    # A combined save updates everything in one call.
    combined = set_task_meta(
        tasks_root, "alpha", {"title": "T2", "body": "B2", "disabled": True, "draft": True}
    )
    assert combined["title"] == "T2"
    assert combined["body"] == "B2"
    assert combined["disabled"] is True
    assert combined["frontmatter"]["draft"] is True


def test_set_task_meta_rejects_empty_title_and_bad_keys(tmp_path: Path) -> None:
    from nightshift.engine import set_task_meta

    tasks_root = _seed(tmp_path, tasks={"alpha": "---\ntitle: Alpha\n---\nbody"})
    with pytest.raises(ValueError):
        set_task_meta(tasks_root, "alpha", {"title": "   "})
    with pytest.raises(ValueError):
        set_task_meta(tasks_root, "alpha", {"bogus": "x"})
    with pytest.raises(FileNotFoundError):
        set_task_meta(tasks_root, "../../etc/passwd", {"draft": True})


def test_list_queue_respects_config_order(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={
        "alpha": "Do alpha.",
        "beta": "Do beta.",
        "gamma": "Do gamma.",
    })
    reorder_queue(tasks_root, ["gamma", "alpha", "beta"])
    queue = list_queue(tasks_root)
    assert [q["task"] for q in queue] == ["gamma", "alpha", "beta"]


# --------------------------------------------------------------------------- #
# execution order — <queue>/config.json
# --------------------------------------------------------------------------- #


def test_order_config_drives_queue_order(tmp_path: Path) -> None:
    # Numbering removed: order is driven by the queue's config.json, not the filename.
    tasks_root = _seed(tmp_path, tasks={
        "alpha": "Do alpha.",
        "beta": "Do beta.",
        "gamma": "Do gamma.",
    })
    engine.save_order(tasks_root, ["gamma", "alpha", "beta"])
    names = [q["task"] for q in list_queue(tasks_root)]
    assert names == ["gamma", "alpha", "beta"]


def test_order_unlisted_tasks_fall_back_to_filename(tmp_path: Path) -> None:
    # Listed tasks lead in configured order; unlisted ones follow lexically.
    tasks_root = _seed(tmp_path, tasks={"alpha": "a", "beta": "b", "gamma": "c", "delta": "d"})
    engine.save_order(tasks_root, ["gamma"])
    names = [q["task"] for q in list_queue(tasks_root)]
    assert names == ["gamma", "alpha", "beta", "delta"]


def test_order_stems_ignores_stale_and_missing_config(tmp_path: Path) -> None:
    # A queue with no config.json order falls back to pure filename order. The
    # builder writes a (repo-bound) config with an empty ``order``, so drop it
    # to exercise the "no order configured" path the original test covered.
    tasks_root = _seed(tmp_path)
    (tasks_root / "main/config.json").unlink()
    assert engine.order_stems(tasks_root, ["b", "a"]) == ["a", "b"]
    # Stale entries (no such stem in the input) are ignored, not surfaced.
    engine.save_order(tasks_root, ["ghost", "b"])
    assert engine.order_stems(tasks_root, ["a", "b"]) == ["b", "a"]


def test_reorder_queue_drops_unknown_and_appends_missing(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={"one": "1", "two": "2", "three": "3"})
    # Reorder with a spoofed name and an omitted real task.
    result = engine.reorder_queue(tasks_root, ["three", "one", "ghost"])
    # ghost is dropped (no file); two is appended in filename order.
    assert result == ["three", "one", "two"]
    assert engine.load_order(tasks_root) == ["three", "one", "two"]
    assert [q["task"] for q in list_queue(tasks_root)] == ["three", "one", "two"]


def test_save_queue_config_value_preserves_siblings(tmp_path: Path) -> None:
    # Persisting the per-queue validate command must keep the order (and any
    # other sibling keys) intact, and clearing it (None) removes the key.
    tasks_root = _seed(tmp_path, tasks={"one": "1", "two": "2"})
    engine.save_order(tasks_root, ["two", "one"])

    engine.save_queue_config_value(tasks_root, "validate", "just check")
    cfg = json.loads((tasks_root / "main/config.json").read_text())
    assert cfg["validate"] == "just check"
    assert cfg["order"] == ["two", "one"]  # sibling preserved

    engine.save_queue_config_value(tasks_root, "validate", None)
    cfg = json.loads((tasks_root / "main/config.json").read_text())
    assert "validate" not in cfg
    assert cfg["order"] == ["two", "one"]


def test_resolve_validate_cmd_absent_uses_default() -> None:
    # An absent validate key inherits the engine default.
    assert engine.resolve_validate_cmd({}) == ["just", "validate"]
    assert engine.resolve_validate_cmd({"model": "x"}) == ["just", "validate"]


def test_resolve_validate_cmd_present_splits() -> None:
    assert engine.resolve_validate_cmd({"validate": "just check"}) == ["just", "check"]


@pytest.mark.parametrize("empty", ["", "   ", "\t", "\n  "])
def test_resolve_validate_cmd_empty_disables_validation(empty: str) -> None:
    # A present-but-empty validate key disables validation (None) — it must NOT
    # fall back to the inherited default.
    assert engine.resolve_validate_cmd({"validate": empty}) is None


def test_format_validate_cmd() -> None:
    assert engine.format_validate_cmd(["just", "validate"]) == "just validate"
    assert engine.format_validate_cmd(None) == ""


def test_validate_cmd_from_blob_authoritative() -> None:
    argv, display = engine.validate_cmd_from_blob({"validate_cmd": "just check"})
    assert argv == ["just", "check"]
    assert display == "just check"
    assert engine.validate_cmd_from_blob({"validate_cmd": ""}) == (None, None)


def test_validate_cmd_from_blob_legacy_validate_key() -> None:
    argv, display = engine.validate_cmd_from_blob({"validate": "just lint"})
    assert argv == ["just", "lint"]
    assert display == "just lint"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("   ", ""),
        ("''", ""),
        ('""', ""),
        ("  just validate  ", "just validate"),
        ("just check", "just check"),
    ],
)
def test_normalize_validate_command(raw: str, expected: str) -> None:
    assert engine.normalize_validate_command(raw) == expected


def test_resolve_config_empty_validate_overrides_parent_default(tmp_path: Path) -> None:
    # Spec: an empty-string validate on a queue must not pick up the parent
    # config default. The playlist's "" overrides the main queue's command, and
    # resolve_validate_cmd then reads it as "validation disabled".
    from nightshift.spawn_daily import resolve_config

    tasks_root = _seed(tmp_path)
    # In the two-root model the layer a queue inherits is the content-store
    # config (``<tasks_root>/config.json``), not a sibling queue — that's the
    # "parent default" the playlist overrides (formerly ``.tasks/config.json``).
    (tasks_root / "config.json").write_text(json.dumps({"validate": "just validate"}))
    (tasks_root / "ns").mkdir(parents=True)
    (tasks_root / "ns/config.json").write_text(
        json.dumps({"validate": "", "order": []})
    )

    pl = resolve_config(tmp_path, tasks_root, "ns")
    assert pl["validate"] == ""
    assert engine.resolve_validate_cmd(pl) is None

    # The main queue still validates with the inherited parent command.
    main = resolve_config(tmp_path, tasks_root, "main")
    assert engine.resolve_validate_cmd(main) == ["just", "validate"]


def test_delete_task_removes_from_order(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={"one": "1", "two": "2"})
    engine.save_order(tasks_root, ["two", "one"])
    delete_task(tasks_root, "two")
    assert engine.load_order(tasks_root) == ["one"]


# --------------------------------------------------------------------------- #
# task creation
# --------------------------------------------------------------------------- #


def test_create_task_writes_unnumbered_file(tmp_path: Path) -> None:
    # Spec: "remove the numbering from tasks" — new tasks are named by their
    # slugified title with no NN. prefix, and appended to the execution order.
    tasks_root = _seed(tmp_path)
    created = create_task(tasks_root, "Fix the ops screen", "Make it nicer.")
    assert created["task"] == "fix-the-ops-screen"
    dest = tasks_root / "main/fix-the-ops-screen.md"
    assert dest.exists()
    text = dest.read_text()
    assert "title: Fix the ops screen" in text
    assert "Make it nicer." in text
    # The new task lands at the end of the configured order.
    assert engine.load_order(tasks_root) == ["fix-the-ops-screen"]


def test_create_task_rejects_empty_title_and_collision(tmp_path: Path) -> None:
    # Spec: numbering is gone, so there is no longer a numeric `pri` to reject;
    # the remaining guards are an empty title and a name collision.
    tasks_root = _seed(tmp_path)
    with pytest.raises(ValueError):
        create_task(tasks_root, "   ", "body")
    create_task(tasks_root, "Dup", "body")
    with pytest.raises(FileExistsError):
        create_task(tasks_root, "Dup", "body")


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #


def test_parse_duration() -> None:
    assert parse_duration("30m") == 1800
    assert parse_duration("2h") == 7200
    assert parse_duration("1h30m") == 5400
    with pytest.raises(ValueError):
        parse_duration("soon")
    with pytest.raises(ValueError):
        parse_duration("0s")


def test_settings_round_trip_and_validation(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert load_settings(tmp_path)["transport_mode"] == "auto"

    merged = save_settings(tmp_path, {"transport_mode": "repeat", "repeat_interval": "15m", "port": 9000})
    assert merged["transport_mode"] == "repeat"
    assert load_settings(tmp_path)["port"] == 9000

    assert validate_settings({"transport_mode": "repeat", "repeat_interval": "", "theme": "dark", "port": 8765})
    with pytest.raises(ValueError):
        save_settings(tmp_path, {"transport_mode": "repeat", "repeat_interval": "nope"})
    with pytest.raises(ValueError):
        save_settings(tmp_path, {"port": 70000})


# --------------------------------------------------------------------------- #
# control loop (fake worker, no claude/git)
# --------------------------------------------------------------------------- #


def _fake_worker(sleep_steps: int = 200):
    def fake(
        workspace, tasks_root, task, *,
        repo=None, emit=lambda e: None, abort_reason=None,
        backend_name=None, tasks_rel="main",
    ):
        emit(Event(TASK_STARTED, {"task": task, "title": task, "frontmatter": {}}))
        for _ in range(sleep_steps):
            reason = abort_reason() if callable(abort_reason) else None
            if reason is not None:
                emit(Event(TASK_RESULT, {"task": task, "status": reason}))
                return TaskResult(task=task, title=task, success=False, status=reason)
            time.sleep(0.005)
        emit(Event(TASK_RESULT, {"task": task, "status": "completed", "commit_sha": "deadbee"}))
        return TaskResult(task=task, title=task, success=True, commit_sha="deadbee")
    return fake


def test_controller_pause_resume() -> None:
    c = Controller()
    assert not c.paused
    c.pause()
    assert c.paused
    c.resume()
    assert not c.paused


def test_run_queue_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_legacy, "run_task", _fake_worker())
    started: list[str] = []

    def track(ev: Event) -> None:
        if ev.type == TASK_STARTED:
            started.append(ev.payload["task"])

    controller = Controller()
    out: list = []
    th = threading.Thread(
        target=lambda: out.append(
            run_queue(tmp_path, tmp_path, ["A", "B", "C", "D"], listeners=[track], controller=controller)
        )
    )
    th.start()
    time.sleep(0.05)
    controller.stop()
    th.join(timeout=5)
    assert not th.is_alive()
    summary = out[0]
    assert any(r.resolved_status() == "stopped" for r in summary.results)
    assert "D" not in started


def test_run_queue_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_legacy, "run_task", _fake_worker())
    controller = Controller()
    out: list = []
    th = threading.Thread(
        target=lambda: out.append(
            run_queue(tmp_path, tmp_path, ["A", "B"], controller=controller)
        )
    )
    th.start()
    time.sleep(0.05)
    controller.skip()
    th.join(timeout=5)
    assert not th.is_alive()
    results = {r.task: r.resolved_status() for r in out[0].results}
    assert results["A"] == "skipped"
    assert results["B"] == "completed"


def test_run_queue_pause_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_legacy, "run_task", _fake_worker(sleep_steps=10))
    order: list[str] = []

    def track(ev: Event) -> None:
        if ev.type == TASK_STARTED:
            order.append(ev.payload["task"])

    controller = Controller()
    controller.pause()
    th = threading.Thread(
        target=lambda: run_queue(tmp_path, tmp_path, ["A", "B"], listeners=[track], controller=controller)
    )
    th.start()
    time.sleep(0.15)
    assert order == []  # held at the first boundary while paused
    controller.resume()
    th.join(timeout=5)
    assert order == ["A", "B"]


# --------------------------------------------------------------------------- #
# player: transport modes
# --------------------------------------------------------------------------- #


def test_player_oneshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path)
    monkeypatch.setattr(runner_legacy, "run_task", _fake_worker(sleep_steps=2))
    monkeypatch.setattr("nightshift.server.player.build_task_list", lambda root, arg, tasks_rel="main": [arg])

    player = Player(tmp_path)
    player.play(mode="oneshot", task="10.solo")
    _wait_idle(player)

    runs = player.store.list_runs()
    assert len(runs) == 1
    assert [t["task"] for t in runs[0]["tasks"]] == ["10.solo"]


def test_player_repeat_loops_then_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path)
    monkeypatch.setattr(runner_legacy, "run_task", _fake_worker(sleep_steps=2))
    monkeypatch.setattr("nightshift.server.player.build_task_list", lambda root, arg, tasks_rel="main": ["10.x"])
    save_settings(tmp_path, {"transport_mode": "repeat", "repeat_interval": "1s"})

    player = Player(tmp_path)
    player.play(mode="repeat")
    time.sleep(1.4)
    player.stop()
    _wait_idle(player)

    assert len(player.store.list_runs()) >= 2


def _wait_idle(player: Player, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if player.state()["state"] == "idle":
            return
        time.sleep(0.02)
    raise AssertionError("player did not return to idle")


# --------------------------------------------------------------------------- #
# server smoke (FastAPI TestClient)
# --------------------------------------------------------------------------- #


def _client(root: Path):
    from fastapi.testclient import TestClient

    from nightshift.server.app import create_app

    return TestClient(create_app(root))


def test_server_queue_runs_tasks_settings(tmp_path: Path) -> None:
    _seed(tmp_path, tasks={"alpha": "Do alpha."})
    client = _client(tmp_path)

    queue = client.get("/api/queue").json()
    assert [q["task"] for q in queue] == ["alpha"]

    assert client.get("/api/runs").json() == []

    created = client.post("/api/tasks", json={"title": "New Task", "text": "body"})
    assert created.status_code == 201
    assert created.json()["task"] == "new-task"
    assert any(q["task"] == "new-task" for q in client.get("/api/queue").json())

    assert client.post("/api/tasks", json={"title": "  ", "text": ""}).status_code == 400
    assert client.post("/api/tasks", json={"title": "New Task", "text": ""}).status_code == 409

    settings = client.get("/api/settings").json()
    assert "schema" in settings and "tiers" in settings
    assert client.put("/api/settings", json={"player": {"transport_mode": "repeat", "repeat_interval": "10m"}}).status_code == 200
    assert client.put("/api/settings", json={"player": {"transport_mode": "repeat", "repeat_interval": "bad"}}).status_code == 400


def test_server_settings_exposes_queue_validate(tmp_path: Path) -> None:
    # Per-queue validate is now managed via /api/queue/config, not /api/settings.
    tasks_root = _seed(tmp_path, tasks={"alpha": "Do alpha."})
    client = _client(tmp_path)

    qcfg = client.get("/api/queue/config").json()
    # Queue config returns validate (None/string); absent means engine default.
    assert qcfg.get("validate") is None or qcfg.get("validate") == "just validate"

    resp = client.put("/api/queue/config", json={"validate": "just check"})
    assert resp.status_code == 200
    cfg = json.loads((tasks_root / "main/config.json").read_text())
    assert cfg["validate"] == "just check"
    player_path = tmp_path / ".nightshift/player.json"
    if player_path.exists():
        assert "validate" not in json.loads(player_path.read_text())
    for blank in ("  ", "''", '""'):
        resp = client.put("/api/queue/config", json={"validate": blank})
        assert resp.status_code == 200
        cfg = json.loads((tasks_root / "main/config.json").read_text())
        assert cfg["validate"] == ""


def test_server_settings_validate_follows_active_queue(tmp_path: Path) -> None:
    # Per-queue validate now uses /api/queue/config. Switching the active queue
    # routes edits to that queue's config.json, leaving the main queue untouched.
    tasks_root = _seed(tmp_path, tasks={"alpha": "a"})
    client = _client(tmp_path)
    assert client.post("/api/playlists", json={"name": "Nightshift"}).status_code == 201
    assert client.post("/api/active", json={"playlist": "nightshift"}).status_code == 200

    resp = client.put("/api/queue/config", json={"validate": "just validate-nightshift"})
    assert resp.status_code == 200
    pl_cfg = json.loads((tasks_root / "nightshift/config.json").read_text())
    assert pl_cfg["validate"] == "just validate-nightshift"
    main_cfg_path = tasks_root / "main/config.json"
    main_cfg = json.loads(main_cfg_path.read_text()) if main_cfg_path.exists() else {}
    assert "validate" not in main_cfg


def test_server_task_defaults_seeds_create_pane(tmp_path: Path) -> None:
    # The detail view is now the add surface too: it seeds its create form from
    # brief-shaped defaults (effective model/flags + curated model options) for
    # the active queue, without reading or creating any file.
    _seed(tmp_path, tasks={"alpha": "a"})
    (tmp_path / ".nightshift" / "manager.json").write_text(
        json.dumps(
            {
                "default_model": "claude-code/claude-sonnet-4-6",
                "evergreen_tasks": ["00._todo"],
                "scheduled_models_allow": ["claude-code/claude-sonnet-4-6", "claude-code/claude-opus-4-8"],
            }
        )
    )
    client = _client(tmp_path)

    resp = client.get("/api/task-defaults")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"] is None
    assert body["title"] == "" and body["body"] == ""
    assert body["frontmatter"]["model"] == "claude-code/claude-sonnet-4-6"
    assert "draft" in body["frontmatter"] and "automerge" in body["frontmatter"]
    assert body["frontmatter"]["priority"] == 3
    # The logical ``auto``/``max`` selectors always lead the dropdown (they
    # resolve worker-side), followed by the operator's scheduled allow-list.
    assert body["model_options"] == [
        "auto",
        "max",
        "claude-code/claude-sonnet-4-6",
        "claude-code/claude-opus-4-8",
    ]
    # No file was created by reading defaults.
    assert [q["task"] for q in client.get("/api/queue").json()] == ["alpha"]


def test_server_create_task_applies_detail_frontmatter(tmp_path: Path) -> None:
    # The create POST carries the same frontmatter the detail pane edits, so a
    # task created from that surface lands with its model/flags set in one call.
    _seed(tmp_path)
    client = _client(tmp_path)

    resp = client.post(
        "/api/tasks",
        json={
            "title": "Tune the ops screen",
            "text": "Make it nicer.",
            "evergreen": True,
            "draft": True,
            "automerge": False,
            "model": "claude-opus-4-8",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["task"] == "tune-the-ops-screen"

    brief = client.get("/api/tasks/tune-the-ops-screen").json()
    assert brief["body"] == "Make it nicer."
    assert brief["evergreen"] is True
    assert brief["frontmatter"]["draft"] is True
    assert brief["frontmatter"]["automerge"] is False
    assert brief["frontmatter"]["model"] == "claude-opus-4-8"

    # A "default" model leaves the pin unset so the task inherits the config model.
    plain = client.post(
        "/api/tasks", json={"title": "Plain task", "text": "x", "model": "default"}
    )
    assert plain.status_code == 201
    plain_brief = client.get("/api/tasks/plain-task").json()
    assert "model" not in plain_brief["frontmatter_raw"]
    assert plain_brief["frontmatter"]["model"] == "claude-code/claude-sonnet-4-6"


def test_server_get_task_brief(tmp_path: Path) -> None:
    _seed(tmp_path, tasks={
        "alpha": "---\ntitle: Alpha\nmodel: claude-opus-4-8\n---\nThe alpha brief.\n",
    })
    client = _client(tmp_path)

    resp = client.get("/api/tasks/alpha")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"] == "alpha"
    assert body["title"] == "Alpha"
    assert body["body"] == "The alpha brief."
    assert body["frontmatter"]["model"] == "claude-opus-4-8"

    assert client.get("/api/tasks/does-not-exist").status_code == 404


def test_server_patch_task_saves_pane_edits(tmp_path: Path) -> None:
    # The detail pane saves title, brief, toggles, and model in one PATCH.
    _seed(tmp_path, tasks={
        "alpha": "---\ntitle: Alpha\nmodel: claude-opus-4-8\n---\nOld brief.\n",
    })
    client = _client(tmp_path)

    resp = client.patch(
        "/api/tasks/alpha",
        json={
            "title": "Alpha v2",
            "body": "Reworked brief.",
            "evergreen": True,
            "draft": True,
            "model": "default",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Alpha v2"
    assert body["body"] == "Reworked brief."
    assert body["evergreen"] is True
    assert body["frontmatter"]["draft"] is True
    # "default" clears the pin so the task inherits the config model.
    assert "model" not in body["frontmatter_raw"]

    # An empty title is rejected.
    assert client.patch("/api/tasks/alpha", json={"title": "  "}).status_code == 400
    # A no-op PATCH (no fields) is rejected.
    assert client.patch("/api/tasks/alpha", json={}).status_code == 400
    # Unknown task → 404.
    assert client.patch("/api/tasks/ghost", json={"draft": True}).status_code == 404


def test_server_transport_select_and_state(tmp_path: Path) -> None:
    _seed(tmp_path, tasks={"alpha": "Do alpha."})
    client = _client(tmp_path)

    assert client.post("/api/transport", json={"action": "bogus"}).status_code == 400

    resp = client.post("/api/transport", json={"action": "select", "task": "alpha"})
    assert resp.status_code == 200
    assert resp.json()["cursor"] == "alpha"

    # idle no-ops
    assert client.post("/api/transport", json={"action": "pause"}).status_code == 200
    assert client.get("/api/state").json()["state"] == "idle"


def test_server_reorder_queue(tmp_path: Path) -> None:
    _seed(tmp_path, tasks={"alpha": "Do alpha.", "beta": "Do beta.", "gamma": "Do gamma."})
    client = _client(tmp_path)

    resp = client.put("/api/queue/order", json={"order": ["gamma", "alpha", "beta"]})
    assert resp.status_code == 200
    assert resp.json()["order"] == ["gamma", "alpha", "beta"]

    queue = client.get("/api/queue").json()
    assert [q["task"] for q in queue] == ["gamma", "alpha", "beta"]


def test_server_queue_sort_mode_round_trips(tmp_path: Path) -> None:
    # The sort toggle persists per-queue in config.json and drives both the UI
    # display order and the engine's play order (both via order_stems).
    tasks_root = _seed(tmp_path, tasks={
        "alpha": "---\npriority: 5\n---\nDo alpha.",
        "beta": "---\npriority: 0\n---\nDo beta.",
    })
    client = _client(tmp_path)

    # Defaults to manual; the config order (alpha first) is honoured.
    assert client.get("/api/queue/sort").json()["sort"] == "manual"

    resp = client.put("/api/queue/sort", json={"sort": "priority"})
    assert resp.status_code == 200 and resp.json()["sort"] == "priority"
    assert client.get("/api/queue/sort").json()["sort"] == "priority"
    cfg = json.loads((tasks_root / "main/config.json").read_text())
    assert cfg["sort"] == "priority"

    # Priority mode floats beta (P0) above alpha (P5) in the queue listing.
    assert [q["task"] for q in client.get("/api/queue").json()] == ["beta", "alpha"]

    # An unknown mode degrades to manual.
    assert client.put("/api/queue/sort", json={"sort": "bogus"}).json()["sort"] == "manual"


def test_server_task_priority_create_patch_and_validation(tmp_path: Path) -> None:
    _seed(tmp_path)
    client = _client(tmp_path)

    # Create carries the priority chosen in the detail pane.
    created = client.post(
        "/api/tasks", json={"title": "Urgent", "text": "x", "priority": 1}
    )
    assert created.status_code == 201
    assert client.get("/api/tasks/urgent").json()["frontmatter"]["priority"] == 1

    # Patch edits the priority in place.
    assert client.patch("/api/tasks/urgent", json={"priority": 4}).status_code == 200
    assert client.get("/api/tasks/urgent").json()["frontmatter"]["priority"] == 4

    # Out-of-range priority is rejected on both create and patch.
    assert client.post(
        "/api/tasks", json={"title": "Bad", "text": "x", "priority": 9}
    ).status_code == 400
    assert client.patch("/api/tasks/urgent", json={"priority": -1}).status_code == 400


def test_server_play_priorities_round_trips(tmp_path: Path) -> None:
    # The play-priority filter persists per-queue in config.json and restricts
    # the engine's play/execute set (live_ordered_queue) without hiding tasks
    # from the management listing (/api/queue stays full).
    tasks_root = _seed(tmp_path, tasks={
        "alpha": "---\npriority: 0\n---\nDo alpha.",
        "beta": "---\npriority: 3\n---\nDo beta.",
    })
    client = _client(tmp_path)

    # Defaults to no filter (play all priorities).
    assert client.get("/api/queue/play-priorities").json()["priorities"] == []

    # A non-contiguous selection is cleaned (sorted/de-duped/clamped) and saved.
    resp = client.put("/api/queue/play-priorities", json={"priorities": [3, 0, 0, 9]})
    assert resp.status_code == 200 and resp.json()["priorities"] == [0, 3]
    assert client.get("/api/queue/play-priorities").json()["priorities"] == [0, 3]
    cfg = json.loads((tasks_root / "main/config.json").read_text())
    assert cfg["play_priorities"] == [0, 3]

    # /api/queue still lists every task (filter is a play scope, not a view).
    assert {q["task"] for q in client.get("/api/queue").json()} == {"alpha", "beta"}

    # The engine's play source honours the filter.
    assert live_ordered_queue(tasks_root) == ["alpha", "beta"]
    client.put("/api/queue/play-priorities", json={"priorities": [0]})
    assert live_ordered_queue(tasks_root) == ["alpha"]

    # An empty list clears the filter again.
    assert client.put("/api/queue/play-priorities", json={"priorities": []}).json()[
        "priorities"
    ] == []


def test_server_clear_runs_and_delete_task(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={"alpha": "Do alpha.", "beta": "Do beta."})
    client = _client(tmp_path)

    # Delete a queue task file.
    assert {q["task"] for q in client.get("/api/queue").json()} == {"alpha", "beta"}
    assert client.delete("/api/tasks/alpha").status_code == 200
    assert {q["task"] for q in client.get("/api/queue").json()} == {"beta"}
    assert client.delete("/api/tasks/does-not-exist").status_code == 404

    # Seed a finished run, then clear all completed.
    store = RunStore(tasks_root)
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": "beta", "title": "B", "frontmatter": {}}))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()

    assert len(client.get("/api/runs").json()) == 1
    cleared = client.delete("/api/runs")
    assert cleared.status_code == 200 and cleared.json()["cleared"] == 1
    assert client.get("/api/runs").json() == []


def test_read_new_events_tails_complete_lines(tmp_path: Path) -> None:
    from nightshift.server.app import _read_new_events

    store = RunStore(tmp_path)
    writer = store.start("cli")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.close()

    offset, payloads = _read_new_events(store, writer.run_id, 0)
    assert offset > 0
    assert payloads[0]["type"] == TASK_STARTED
    # Nothing new on a second read from the advanced offset.
    offset2, payloads2 = _read_new_events(store, writer.run_id, offset)
    assert payloads2 == [] and offset2 == offset


def test_sse_stream_emits_initial_state(tmp_path: Path) -> None:
    import anyio

    from nightshift.server.app import sse_stream

    _seed(tmp_path)
    player = Player(tmp_path)

    async def run() -> list[str]:
        calls = {"n": 0}

        async def disconnected() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # let one loop iteration run, then stop

        chunks: list[str] = []
        async for chunk in sse_stream(player, disconnected, poll=0.01):
            chunks.append(chunk)
        return chunks

    chunks = anyio.run(run)
    assert chunks
    first = json.loads(chunks[0][len("data: "):])
    assert first["kind"] == "state"
    assert first["state"] == "idle"


def test_sse_stream_ends_on_server_shutdown(tmp_path: Path) -> None:
    """A connected client that never disconnects must still let the stream end
    when the server is shutting down (Ctrl-C), so graceful shutdown isn't hung."""
    import anyio

    from nightshift.server.app import sse_stream

    _seed(tmp_path)
    player = Player(tmp_path)

    async def run() -> tuple[list[str], bool]:
        async def never_disconnects() -> bool:
            return False

        shutting_down = {"on": False}

        def is_shutting_down() -> bool:
            return shutting_down["on"]

        chunks: list[str] = []
        # Bounded so a regression (loop never exits) fails loudly here instead
        # of hanging the whole test run.
        with anyio.move_on_after(5) as scope:
            async for chunk in sse_stream(
                player, never_disconnects, is_shutting_down, poll=0.01
            ):
                chunks.append(chunk)
                shutting_down["on"] = True  # flip after the first frame
        return chunks, scope.cancel_called

    chunks, timed_out = anyio.run(run)
    assert not timed_out, "sse_stream did not exit on server shutdown"
    assert chunks


# --------------------------------------------------------------------------- #
# Worker backends ("the shim") + instrumentation
# --------------------------------------------------------------------------- #


def test_backend_registry_and_selection() -> None:
    from nightshift import backends

    names = backends.backend_names()
    assert names == [
        "claude-code", "cursor", "gemini", "anthropic", "ollama", "ollama-cloud",
        "nightshift",
    ]

    # Known name resolves; unknown/empty falls back to the default (claude-code).
    assert backends.get_backend("cursor").name == "cursor"
    assert backends.get_backend("gemini").name == "gemini"
    assert backends.get_backend(None).name == "claude-code"
    assert backends.get_backend("nope").name == "claude-code"
    assert backends.get_backend("nightshift").name == "nightshift"
    assert backends.list_backends({})  # smoke: nightshift describes cleanly

    described = {b["name"]: b for b in backends.list_backends({})}
    assert described["claude-code"]["agentic"] is True
    assert described["gemini"]["agentic"] is True  # Gemini CLI edits files
    assert described["anthropic"]["agentic"] is False
    assert described["ollama"]["agentic"] is False
    assert described["ollama-cloud"]["agentic"] is False
    assert set(described["claude-code"]) == {"name", "description", "agentic", "available"}


def test_backend_availability_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    from nightshift import backends

    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert backends.ClaudeCodeBackend().available({}) is False
    assert backends.ClaudeCodeBackend().available({"claude_bin": "/x/claude"}) is True
    assert backends.AnthropicBackend().available({}) is False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert backends.AnthropicBackend().available({}) is True
    # Ollama is usable when a host is configured even without the CLI on PATH.
    assert backends.OllamaBackend().available({"ollama_host": "http://h"}) is True
    # Gemini needs the CLI (or an explicit bin) on the worker.
    assert backends.GeminiCLIBackend().available({}) is False
    assert backends.GeminiCLIBackend().available({"gemini_bin": "/x/gemini"}) is True


def test_gemini_argv_and_stats_parse() -> None:
    from nightshift.backends import build_gemini_argv, parse_gemini_stats

    argv = build_gemini_argv("do it", "gemini-2.5-pro", {})
    assert argv[0] == "gemini"
    assert argv[argv.index("-p") + 1] == "do it"  # prompt is the -p value
    assert "--yolo" in argv  # headless auto-approve of edit/shell tools
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "gemini-2.5-pro"
    # auto/max are worker keywords, not real model ids → no --model flag.
    assert "--model" not in build_gemini_argv("x", "auto", {})
    # cursor_model-style override.
    custom = build_gemini_argv("x", "auto", {"gemini_model": "gemini-2.5-flash"})
    assert custom[custom.index("--model") + 1] == "gemini-2.5-flash"

    # Telemetry mined from the end-of-run JSON blob's stats.models[*].
    stats = parse_gemini_stats({
        "response": "done",
        "stats": {"models": {"gemini-2.5-pro": {
            "api": {"totalRequests": 4},
            "tokens": {"prompt": 1000, "cached": 200, "candidates": 300},
        }}},
    })
    assert stats["turns"] == 4
    assert stats["input_tokens"] == 1200   # prompt + cached
    assert stats["output_tokens"] == 300   # candidates
    assert stats["cost_usd"] is None       # Gemini CLI reports no dollar cost


def test_cursor_argv_overrides() -> None:
    from nightshift.backends import build_cursor_argv

    default = build_cursor_argv("do it", "auto", {})
    assert default[0] == "cursor-agent"
    assert {"-p", "--force", "--trust"} <= set(default)
    assert default[-1] == "do it"  # prompt is the trailing positional
    assert default[default.index("--model") + 1] == "auto"

    custom = build_cursor_argv(
        "do it", "auto", {"cursor_model": "sonnet-4", "cursor_extra_args": ["--sandbox", "enabled"]}
    )
    assert custom[custom.index("--model") + 1] == "sonnet-4"
    assert "--sandbox" in custom and custom[-1] == "do it"


def test_timings_and_phase_clock_persisted(tmp_path: Path) -> None:
    """The engine's per-phase timings and the phase-start clock survive the
    round-trip through the run record."""
    store = RunStore(tmp_path)
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(TASK_STATUS, {"task": "10.x", "status": "running", "phase": "worker"}))
    writer.emit(Event(TASK_STATUS, {"task": "10.x", "status": "running", "phase": "validate"}))
    writer.emit(Event(TASK_RESULT, {
        "task": "10.x", "status": "completed", "result_line": "ok", "commit_sha": "abc1234",
        "timings": {"worker": 12.5, "validate": 3.0, "commit": 0.4, "total": 16.1},
    }))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["timings"] == {"worker": 12.5, "validate": 3.0, "commit": 0.4, "total": 16.1}
    assert rec["started_at"] is not None
    # phase_started_at advances to the latest phase transition.
    assert rec["phase_started_at"] is not None


def test_loc_persisted_in_run_record(tmp_path: Path) -> None:
    """The engine's per-task lines-of-code figure survives the round-trip through
    the run record so the Stats page can sum it across history."""
    store = RunStore(tmp_path)
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(TASK_RESULT, {
        "task": "10.x", "status": "completed", "result_line": "ok",
        "commit_sha": "abc1234", "loc": 42,
    }))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["loc"] == 42
    assert rec["commit_sha"] == "abc1234"


def test_multiple_lands_accumulate_commit_shas(tmp_path: Path) -> None:
    """A task that lands more than once (an initial land, then a later
    resolve/recovery that squashes again via append_task_result) keeps every sha
    in the history commit field as a comma-separated list rather than overwriting
    it with only the last."""
    store = RunStore(tmp_path)
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(TASK_RESULT, {
        "task": "10.x", "status": "completed", "result_line": "ok", "commit_sha": "abc1234",
    }))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()

    # A later resolve lands the task again on the same run record.
    assert store.append_task_result(
        writer.run_id, "10.x",
        status="completed", result_line="resolved", commit_sha="def5678",
    )

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["commit_sha"] == "abc1234, def5678"


def test_repeated_commit_sha_not_duplicated(tmp_path: Path) -> None:
    """Re-emitting the same result (an idempotent replay) does not grow the
    history commit field — each sha appears once."""
    store = RunStore(tmp_path)
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(TASK_RESULT, {
        "task": "10.x", "status": "completed", "commit_sha": "abc1234",
    }))
    writer.emit(Event(TASK_RESULT, {
        "task": "10.x", "status": "completed", "commit_sha": "abc1234",
    }))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["commit_sha"] == "abc1234"


def test_resultless_sha_does_not_clear_prior_lands(tmp_path: Path) -> None:
    """A later result without a sha (e.g. a status-only finish) leaves the
    accumulated commit field intact rather than wiping it."""
    store = RunStore(tmp_path)
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(TASK_RESULT, {
        "task": "10.x", "status": "completed", "commit_sha": "abc1234",
    }))
    writer.emit(Event(TASK_RESULT, {
        "task": "10.x", "status": "completed", "result_line": "noop",
    }))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["commit_sha"] == "abc1234"


def test_loc_defaults_to_none_when_absent(tmp_path: Path) -> None:
    """A result without a loc figure (legacy records, no-change runs) leaves loc
    as None rather than fabricating a count."""
    store = RunStore(tmp_path)
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": "10.x", "title": "X", "frontmatter": {}}))
    writer.emit(Event(TASK_RESULT, {
        "task": "10.x", "status": "completed", "result_line": "no changes",
    }))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["loc"] is None


def test_run_task_no_changes_completes_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that produces no commits (e.g. a non-agentic backend) finishes
    as 'completed (no changes)' rather than failing the squash step."""
    from nightshift import backends

    tasks_root = _seed(tmp_path, tasks={"10.x": "do x"})
    monkeypatch.setattr(runner_legacy, "setup_worktree", lambda ws, repo, task, *, queue=None: tmp_path)
    monkeypatch.setattr(runner_legacy, "teardown_worktree", lambda ws, repo, task, *, queue=None: None)
    monkeypatch.setattr(runner_legacy, "build_prompt", lambda task, *, task_file=None, validate_cmd=None, loop=False, loop_max_iterations=0, split=False, split_dir=None: "p")
    monkeypatch.setattr(runner_legacy, "has_commits", lambda ws, repo, task, *, queue=None: False)

    class _NoopBackend:
        name = "noop"

        def run(self, spec, emit_log, should_abort, on_worker_start=None):
            emit_log("considered; nothing to change\n")
            return backends.WorkerResult(returncode=0)

    monkeypatch.setattr(backends, "get_backend", lambda name=None: _NoopBackend())

    events: list[Event] = []
    result = engine.run_task(tmp_path, tasks_root, "10.x", emit=events.append)

    assert result.success is True
    assert "no changes" in (result.result_line or "")
    last = [e for e in events if e.type == TASK_RESULT][-1]
    assert last.payload["status"] == "completed"
    assert "timings" in last.payload and "worker" in last.payload["timings"]
    # A completed regular task leaves the queue even on the no-changes path
    # (the worker produced no branch to git-rm its own file).
    assert not (tasks_root / "main/10.x.md").exists()


def test_drop_completed_task_removes_lingering_file(tmp_path: Path) -> None:
    """A landed regular task whose worker forgot to git-rm its own file is
    dropped from the queue (file + execution-order entry) so the UI stops
    listing a completed task."""
    tasks_root = _seed(tmp_path, tasks={"alpha": "do alpha", "beta": "do beta"})
    engine.save_order(tasks_root, ["alpha", "beta"])

    assert engine.drop_completed_task(tasks_root, "alpha") is True
    assert not (tasks_root / "main/alpha.md").exists()
    assert [q["task"] for q in list_queue(tasks_root)] == ["beta"]
    assert engine.load_order(tasks_root) == ["beta"]

    # Idempotent: a file that's already gone (the worker did remove it) is a no-op.
    assert engine.drop_completed_task(tasks_root, "alpha") is False


def test_drop_completed_task_keeps_evergreen_file(tmp_path: Path) -> None:
    """An evergreen task keeps its file (it resets and re-runs), so the engine's
    completion backstop must not touch it — only regular tasks call it."""
    tasks_root = _seed(tmp_path, tasks={"green": "---\nevergreen: true\n---\nrecurring"})
    config = engine.resolve_config(tmp_path, tasks_root)
    meta = engine.split_frontmatter((tasks_root / "main/green.md").read_text())[0]
    assert engine.task_is_evergreen(meta, "green", config) is True
    # The run paths only call drop_completed_task for non-evergreen tasks; the
    # file is left in place for evergreen ones.
    assert (tasks_root / "main/green.md").exists()


def test_server_backends_endpoint(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from nightshift.server.app import create_app

    _seed(tmp_path)
    client = TestClient(create_app(tmp_path))
    resp = client.get("/api/backends")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current"] == "claude-code"
    names = [b["name"] for b in data["backends"]]
    assert names == [
        "claude-code", "cursor", "gemini", "anthropic", "ollama", "ollama-cloud",
        "nightshift",
    ]


def test_worker_backend_is_not_in_player_settings(tmp_path: Path) -> None:
    """worker_backend is removed from player settings (§7.4); it lives only
    as WorkerConfig.backend in worker.json."""
    settings = load_settings(tmp_path)
    assert "worker_backend" not in settings


# --------------------------------------------------------------------------- #
# Playlists (directory-backed alternate queues) + config layering + stop
# --------------------------------------------------------------------------- #


def test_playlists_crud_round_trip(tmp_path: Path) -> None:
    from nightshift import playlists

    tasks_root = _seed(tmp_path)
    assert playlists.list_playlists(tasks_root) == []

    created = playlists.create_playlist(tasks_root, "Morning Open")
    assert created["name"] == "morning-open"
    # A fresh playlist's config holds only an empty queue order.
    cfg = json.loads((tasks_root / "morning-open/config.json").read_text())
    assert cfg == {"order": []}

    # duplicate name → FileExistsError; empty name → ValueError.
    with pytest.raises(FileExistsError):
        playlists.create_playlist(tasks_root, "Morning Open")
    with pytest.raises(ValueError):
        playlists.create_playlist(tasks_root, "   ")

    assert playlists.exists(tasks_root, "morning-open")
    assert playlists.list_playlists(tasks_root) == [
        {"name": "morning-open", "task_count": 0, "disabled": False}
    ]

    assert playlists.delete_playlist(tasks_root, "morning-open") is True
    assert playlists.delete_playlist(tasks_root, "morning-open") is False
    # traversal-shaped names are rejected outright.
    assert playlists.delete_playlist(tasks_root, "../../etc") is False


def test_resolve_config_layers_shipped_tasks_and_playlist(tmp_path: Path) -> None:
    """Runner config resolves operator/shipped defaults <- content-store
    config.json <- per-queue config.json, so a queue inherits anything it doesn't
    override. The store config (``<tasks_root>/config.json``) is the shared layer
    that replaces the old nested ``.tasks/config.json`` parent."""
    from nightshift.spawn_daily import resolve_config

    tasks_root = _seed(tmp_path)  # operator config: model claude-sonnet-4-6
    # The content-store layer every queue inherits (formerly .tasks/config.json).
    (tasks_root / "config.json").write_text(
        json.dumps({"validate": "just validate", "automerge": True})
    )
    (tasks_root / "ns").mkdir(parents=True)
    (tasks_root / "ns/config.json").write_text(
        json.dumps({"validate": "just validate-nightshift", "order": []})
    )

    main = resolve_config(tmp_path, tasks_root, "main")
    assert main["validate"] == "just validate"
    assert main["default_model"] == "claude-code/claude-sonnet-4-6"   # from operator defaults
    assert main["automerge"] is True              # from the store layer

    pl = resolve_config(tmp_path, tasks_root, "ns")
    assert pl["validate"] == "just validate-nightshift"  # queue override
    assert pl["default_model"] == "claude-code/claude-sonnet-4-6"            # inherited from operator
    assert pl["automerge"] is True                       # inherited from the store layer


def test_queue_ops_operate_on_playlist_dir(tmp_path: Path) -> None:
    """create/list/reorder/delete work against a playlist sub-dir when tasks_rel
    points at one, leaving the main queue untouched."""
    from nightshift.engine import create_task, delete_task, list_queue, reorder_queue

    tasks_root = _seed(
        tmp_path, tasks={"main-a": "a"}, queues={"ns": {"config": {"order": []}}}
    )

    create_task(tasks_root, "Beta", "b", "ns")
    create_task(tasks_root, "Alpha", "a", "ns")
    assert (tasks_root / "ns/beta.md").exists()
    # main queue only sees its own task (sub-dirs are skipped).
    assert [q["task"] for q in list_queue(tasks_root)] == ["main-a"]

    reorder_queue(tasks_root, ["alpha", "beta"], "ns")
    assert [q["task"] for q in list_queue(tasks_root, "ns")] == ["alpha", "beta"]

    delete_task(tasks_root, "beta", "ns")
    assert [q["task"] for q in list_queue(tasks_root, "ns")] == ["alpha"]


def test_run_record_playlist_provenance_round_trip(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    writer = store.start("ui", playlist="nightshift")
    writer.emit(Event(TASK_STARTED, {"task": "alpha", "title": "A", "frontmatter": {}}))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()

    run = store.list_runs()[0]
    assert run["playlist"] == "nightshift"

    # An main-queue run carries null provenance.
    w2 = store.start("ui")
    w2.emit(Event(RUN_FINISHED, {"run_id": w2.run_id}))
    w2.close()
    main = next(r for r in store.list_runs() if r["id"] == w2.run_id)
    assert main["playlist"] is None


def test_player_set_active_switches_queue_and_store(tmp_path: Path) -> None:
    """Activating a playlist points the player at the playlist's tasks dir and
    its own runs store; switching back to main restores the default."""
    tasks_root = _seed(tmp_path, queues={"ns": {"config": {"order": []}}})

    player = Player(tmp_path)
    assert player.tasks_rel() == "main"
    assert player.store.base == tasks_root / "main/runs"

    assert player.set_active("ns")["ok"] is True
    assert player.active_playlist() == "ns"
    assert player.tasks_rel() == "ns"
    assert player.store.base == tasks_root / "ns/runs"

    assert player.set_active(None)["ok"] is True
    assert player.tasks_rel() == "main"


def test_player_set_active_focus_decoupled_from_running_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Focus is decoupled from the running queue: re-selecting the focused queue
    is a no-op that leaves the store identity untouched, and switching focus to a
    *different* queue mid-run is now allowed — it never stops the run, the
    running queue stays pinned, and the focused store follows the new queue."""
    tasks_root = _seed(tmp_path, queues={"ns": {"config": {"order": []}}})
    monkeypatch.setattr(runner_legacy, "run_task", _fake_worker(sleep_steps=2000))
    monkeypatch.setattr(
        "nightshift.server.player.build_task_list",
        lambda root, arg, tasks_rel="main": ["10.x"],
    )

    player = Player(tmp_path)
    store_before = player.store
    player.play(mode="auto")
    try:
        deadline = time.monotonic() + 5.0
        while player.state()["state"] != "playing" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert player.state()["state"] == "playing"
        assert player.running_playlist() is None  # main queue is running

        # Same (focused) queue while running: no-op, succeeds, store untouched.
        assert player.set_active(None)["ok"] is True
        assert player.store is store_before
        assert player.state()["state"] == "playing"

        # Switching focus to another queue mid-run is allowed and non-disruptive:
        # the run keeps playing, the running queue stays main, and the focused
        # store now points at the newly focused queue.
        switched = player.set_active("ns")
        assert switched["ok"] is True
        assert player.active_playlist() == "ns"
        assert player.running_playlist() is None  # run still pinned to main
        assert player.state()["state"] == "playing"
        assert player.store.base == tasks_root / "ns/runs"
    finally:
        player.stop()
        _wait_idle(player)


# --------------------------------------------------------------------------- #
# Phase 2 — per-queue runners + concurrency governor
# --------------------------------------------------------------------------- #


def test_save_config_value_round_trips_and_preserves_siblings(tmp_path: Path) -> None:
    """The root-config writer sets one key without disturbing siblings, and
    updates an existing key in place."""
    _seed(tmp_path)
    cfg = tmp_path / ".nightshift" / "manager.json"
    cfg.write_text(json.dumps(
        {"model": "m", "max_per_day": 5, "auto_resolve": True}, indent=2
    ) + "\n")

    save_config_value(tmp_path, "max_concurrent_queues", 3)
    data = json.loads(cfg.read_text())
    assert data["max_concurrent_queues"] == 3
    assert data["model"] == "m"
    assert data["max_per_day"] == 5
    assert data["auto_resolve"] is True
    assert load_config(tmp_path)["max_concurrent_queues"] == 3

    save_config_value(tmp_path, "max_concurrent_queues", 4)
    assert json.loads(cfg.read_text())["max_concurrent_queues"] == 4


def test_concurrency_gate_blocks_beyond_cap() -> None:
    """A gate with cap 1 admits one worker and blocks the next until a slot
    frees."""
    gate = ConcurrencyGate(lambda: 1)
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with gate.slot():
            held.set()
            release.wait(2.0)

    t1 = threading.Thread(target=hold, daemon=True)
    t1.start()
    assert held.wait(2.0)
    assert gate.active() == 1

    acquired = threading.Event()

    def second() -> None:
        with gate.slot():
            acquired.set()

    t2 = threading.Thread(target=second, daemon=True)
    t2.start()
    assert not acquired.wait(0.3)  # blocked by cap=1
    release.set()
    assert acquired.wait(2.0)  # proceeds once the first slot frees
    t1.join(2.0)
    t2.join(2.0)


def test_concurrency_gate_reads_limit_live() -> None:
    """The cap is read on each acquire, so raising it mid-flight admits another
    worker without a restart (a second slot that would deadlock at cap=1 is
    granted once the cap is 2)."""
    limit = {"n": 1}
    gate = ConcurrencyGate(lambda: limit["n"])
    with gate.slot():
        assert gate.active() == 1
        limit["n"] = 2  # raise the cap live
        with gate.slot():  # would block forever at cap=1; admitted at cap=2
            assert gate.active() == 2
        assert gate.active() == 1
    assert gate.active() == 0


def test_registry_runs_two_queues_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two queues' runners run independently and land into their own stores;
    histories never cross."""
    tasks_root = _seed(
        tmp_path, tasks={"10.x": "x"}, queues={"ns": {"config": {"order": []}}}
    )

    monkeypatch.setattr(runner_legacy, "run_task", _fake_worker(sleep_steps=20))
    monkeypatch.setattr(
        "nightshift.server.player.build_task_list",
        lambda root, arg, tasks_rel="main": (
            ["10.x"] if tasks_rel == "main" else ["20.y"]
        ),
    )

    reg = PlayerRegistry(tmp_path)
    reg.runner(None).play(mode="oneshot", task="10.x")
    reg.runner("ns").play(mode="oneshot", task="20.y")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and (
        reg.runner(None).running() or reg.runner("ns").running()
    ):
        time.sleep(0.02)
    assert not reg.runner(None).running()
    assert not reg.runner("ns").running()

    main_runs = reg.runner(None).store.list_runs()
    ns_runs = reg.runner("ns").store.list_runs()
    assert reg.runner(None).store.base == tasks_root / "main/runs"
    assert reg.runner("ns").store.base == tasks_root / "ns/runs"
    assert main_runs and main_runs[0]["tasks"][0]["task"] == "10.x"
    assert ns_runs and ns_runs[0]["tasks"][0]["task"] == "20.y"
    # Each queue's history holds only its own task.
    assert all(t["task"] == "10.x" for r in main_runs for t in r["tasks"])
    assert all(t["task"] == "20.y" for r in ns_runs for t in r["tasks"])


def test_disk_admission_fails_task_without_cutting_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When free disk is below the floor, the task is failed with
    ``failure_kind="disk"`` and ``run_task`` is never entered (no worktree)."""
    _seed(tmp_path, tasks={"10.x": "x"})
    monkeypatch.setattr(
        "nightshift.server.player.enough_free_disk", lambda root: False
    )
    monkeypatch.setattr(
        "nightshift.server.player.build_task_list",
        lambda root, arg, tasks_rel="main": ["10.x"],
    )
    ran = {"n": 0}

    def spy_run_task(*args: object, **kwargs: object) -> TaskResult:
        ran["n"] += 1
        return TaskResult(task="10.x", title="10.x", success=True)

    monkeypatch.setattr(runner_legacy, "run_task", spy_run_task)

    reg = PlayerRegistry(tmp_path)
    runner = reg.runner(None)
    runner.play(mode="oneshot", task="10.x")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and runner.running():
        time.sleep(0.02)
    assert not runner.running()
    assert ran["n"] == 0  # disk denial short-circuited before run_task

    runs = runner.store.list_runs()
    assert runs
    rec = runs[0]["tasks"][0]
    assert rec["status"] == "error"
    assert rec["failure_kind"] == "disk"


# --------------------------------------------------------------------------- #
# Phase 3 — queue-scoped API + multiplexed SSE
# --------------------------------------------------------------------------- #


def _make_playlist(tasks_root: Path, name: str) -> None:
    """Create an alternate queue ``<tasks_root>/<name>/`` in the content store."""
    (tasks_root / name).mkdir(parents=True, exist_ok=True)
    (tasks_root / name / "config.json").write_text(json.dumps({"order": []}))


def test_state_endpoint_exposes_per_queue_map(tmp_path: Path) -> None:
    """GET /api/state returns a ``queues`` map keyed by queue, including the
    focused queue's card, plus a flat back-compat focused state."""
    tasks_root = _seed(tmp_path, tasks={"10.x": "x"})
    _make_playlist(tasks_root, "ns")
    client = _client(tmp_path)

    body = client.get("/api/state").json()
    assert "queues" in body and "main" in body["queues"]
    assert body["queues"]["main"]["state"] == "idle"
    assert body["state"] == "idle"  # flat back-compat shape preserved
    assert body["active_playlist"] is None

    client.post("/api/active", json={"playlist": "ns"})
    body = client.get("/api/state").json()
    assert body["active_playlist"] == "ns"
    assert "ns" in body["queues"]  # focused queue always has a card


def test_transport_drives_explicit_queue_without_moving_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport call with an explicit ``queue`` runs that queue's runner and
    leaves both the focused queue and the other queue untouched."""
    tasks_root = _seed(tmp_path, tasks={"10.x": "x"})
    _make_playlist(tasks_root, "ns")
    monkeypatch.setattr(runner_legacy, "run_task", _fake_worker(sleep_steps=400))
    monkeypatch.setattr(
        "nightshift.server.player.build_task_list",
        lambda root, arg, tasks_rel="main": (
            ["10.x"] if tasks_rel == "main" else ["20.y"]
        ),
    )
    client = _client(tmp_path)

    r = client.post(
        "/api/transport",
        json={"action": "play", "mode": "oneshot", "task": "20.y", "queue": "ns"},
    )
    assert r.status_code == 200

    deadline = time.monotonic() + 3.0
    queues = {}
    while time.monotonic() < deadline:
        queues = client.get("/api/state").json()["queues"]
        if queues.get("ns", {}).get("state") == "playing":
            break
        time.sleep(0.02)
    assert queues.get("ns", {}).get("state") == "playing"
    assert queues.get("main", {}).get("state", "idle") == "idle"
    # Focus never moved (it's an explicit-queue transport, not a focus switch).
    assert client.get("/api/state").json()["active_playlist"] is None

    client.post("/api/transport", json={"action": "stop", "queue": "ns"})


def test_transport_omitted_queue_falls_back_to_focused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no explicit queue, transport targets the focused queue."""
    tasks_root = _seed(tmp_path, tasks={"10.x": "x"})
    _make_playlist(tasks_root, "ns")
    monkeypatch.setattr(runner_legacy, "run_task", _fake_worker(sleep_steps=400))
    monkeypatch.setattr(
        "nightshift.server.player.build_task_list",
        lambda root, arg, tasks_rel="main": (
            ["10.x"] if tasks_rel == "main" else ["20.y"]
        ),
    )
    client = _client(tmp_path)
    client.post("/api/active", json={"playlist": "ns"})  # focus ns

    client.post("/api/transport", json={"action": "play", "mode": "oneshot", "task": "20.y"})
    deadline = time.monotonic() + 3.0
    queues = {}
    while time.monotonic() < deadline:
        queues = client.get("/api/state").json()["queues"]
        if queues.get("ns", {}).get("state") == "playing":
            break
        time.sleep(0.02)
    assert queues.get("ns", {}).get("state") == "playing"
    assert queues.get("main", {}).get("state", "idle") == "idle"
    client.post("/api/transport", json={"action": "stop", "queue": "ns"})


def test_sse_frames_carry_queue(tmp_path: Path) -> None:
    """Every SSE frame is tagged with its queue so the UI can route it."""
    import anyio

    from nightshift.server.app import sse_stream

    _seed(tmp_path)
    player = Player(tmp_path)

    async def run() -> list[str]:
        ticks = {"n": 0}

        async def disconnected() -> bool:
            ticks["n"] += 1
            return ticks["n"] > 2

        chunks: list[str] = []
        async for chunk in sse_stream(player, disconnected, poll=0.01):
            chunks.append(chunk)
        return chunks

    chunks = anyio.run(run)
    assert chunks
    first = json.loads(chunks[0][len("data: "):])
    assert first["kind"] == "state"
    assert first["queue"] == "main"
    assert first["state"] == "idle"


def test_run_interruptible_kills_on_abort(tmp_path: Path) -> None:
    """run_interruptible terminates a long-running command promptly once
    should_abort fires, returning a non-zero (aborted) result."""
    aborted = {"v": False}

    def should_abort():
        return "stopped" if aborted["v"] else None

    # Flip the abort flag from a side thread shortly after the sleep starts.
    def flip():
        time.sleep(0.2)
        aborted["v"] = True

    t = threading.Thread(target=flip)
    t.start()
    start = time.monotonic()
    result = engine.run_interruptible(
        ["sleep", "30"], cwd=tmp_path, env=None, should_abort=should_abort,
    )
    elapsed = time.monotonic() - start
    t.join()
    assert elapsed < 10  # killed, not waited out
    assert result.returncode != 0


def test_server_playlists_and_active_api(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={"alpha": "a"})
    client = _client(tmp_path)

    assert client.get("/api/playlists").json() == []
    assert client.get("/api/active").json() == {"active_playlist": None}

    created = client.post("/api/playlists", json={"name": "Nightshift"})
    assert created.status_code == 201
    assert created.json()["name"] == "nightshift"
    # duplicate → 409; empty → 400.
    assert client.post("/api/playlists", json={"name": "Nightshift"}).status_code == 409
    assert client.post("/api/playlists", json={"name": "  "}).status_code == 400

    assert [p["name"] for p in client.get("/api/playlists").json()] == ["nightshift"]

    # Activate it: queue/state now follow the playlist (its own, empty queue).
    assert client.post("/api/active", json={"playlist": "nightshift"}).status_code == 200
    assert client.get("/api/active").json() == {"active_playlist": "nightshift"}
    assert client.get("/api/state").json()["active_playlist"] == "nightshift"
    assert client.get("/api/queue").json() == []  # playlist starts empty

    # A task created now lands in the playlist dir, not the main queue.
    assert client.post("/api/tasks", json={"title": "PL task", "text": "x"}).status_code == 201
    assert (tasks_root / "nightshift/pl-task.md").exists()
    assert not (tasks_root / "main/pl-task.md").exists()

    # Activating an unknown playlist is a 404.
    assert client.post("/api/active", json={"playlist": "nope"}).status_code == 404

    # Home: back to the main queue (sees its original task).
    assert client.post("/api/active", json={"playlist": None}).status_code == 200
    assert [q["task"] for q in client.get("/api/queue").json()] == ["alpha"]

    # Delete the playlist.
    assert client.delete("/api/playlists/nightshift").status_code == 200
    assert client.delete("/api/playlists/nightshift").status_code == 404


def test_server_crud_targets_explicit_queue_regardless_of_focus(tmp_path: Path) -> None:
    """Phase 0: CRUD routes accept an explicit ``?queue=`` so any queue can be
    read and edited while another is focused. An empty value targets main, a
    name targets that playlist, and an unknown queue is a 404."""
    tasks_root = _seed(tmp_path, tasks={"main-task": "m"})
    client = _client(tmp_path)
    assert client.post("/api/playlists", json={"name": "Nightshift"}).status_code == 201

    # Focused on main, create a task directly into the (non-focused) playlist.
    created = client.post(
        "/api/tasks", params={"queue": "nightshift"}, json={"title": "PL one", "text": "x"}
    )
    assert created.status_code == 201
    assert (tasks_root / "nightshift/pl-one.md").exists()
    assert not (tasks_root / "main/pl-one.md").exists()
    # Focus never moved.
    assert client.get("/api/active").json() == {"active_playlist": None}

    # Read each queue explicitly, independent of focus.
    assert [q["task"] for q in client.get("/api/queue", params={"queue": "nightshift"}).json()] == ["pl-one"]
    assert [q["task"] for q in client.get("/api/queue", params={"queue": ""}).json()] == ["main-task"]
    # Absent param still falls back to the focused (main) queue.
    assert [q["task"] for q in client.get("/api/queue").json()] == ["main-task"]

    # Now focus the playlist, and edit the main queue's task via an explicit "".
    assert client.post("/api/active", json={"playlist": "nightshift"}).status_code == 200
    patched = client.patch(
        "/api/tasks/main-task", params={"queue": ""}, json={"title": "Main v2"}
    )
    assert patched.status_code == 200 and patched.json()["title"] == "Main v2"
    assert client.delete("/api/tasks/pl-one", params={"queue": "nightshift"}).status_code == 200
    assert client.get("/api/queue", params={"queue": "nightshift"}).json() == []

    # An unknown explicit queue is a 404.
    assert client.get("/api/queue", params={"queue": "ghost"}).status_code == 404
    assert client.post(
        "/api/tasks", params={"queue": "ghost"}, json={"title": "x", "text": "y"}
    ).status_code == 404


def test_server_edit_guard_only_blocks_running_queue_live_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit/delete guard fires only for the live task of the *running*
    queue; the same task name in another (idle) queue stays editable, and a
    non-running task in the running queue stays editable too."""
    from nightshift.server import app as app_mod

    _seed(tmp_path, tasks={"alpha": "a", "beta": "b"})
    client = _client(tmp_path)
    assert client.post("/api/playlists", json={"name": "Other"}).status_code == 201
    client.post("/api/tasks", params={"queue": "other"}, json={"title": "alpha", "text": "x"})

    # Pretend the main queue is running task "alpha".
    real_state = app_mod.Player.state

    def fake_state(self):  # noqa: ANN001
        st = real_state(self)
        st.update({"state": "playing", "running_playlist": None, "now_playing": "alpha"})
        return st

    monkeypatch.setattr(app_mod.Player, "state", fake_state)

    # Editing/deleting the live task of the running (main) queue is refused.
    assert client.patch("/api/tasks/alpha", params={"queue": ""}, json={"draft": True}).status_code == 409
    assert client.delete("/api/tasks/alpha", params={"queue": ""}).status_code == 409
    # A different task in the running queue is still editable.
    assert client.patch("/api/tasks/beta", params={"queue": ""}, json={"draft": True}).status_code == 200
    # The same task name in another (idle) queue is editable.
    assert client.patch("/api/tasks/alpha", params={"queue": "other"}, json={"draft": True}).status_code == 200
    assert client.delete("/api/tasks/alpha", params={"queue": "other"}).status_code == 200


# --------------------------------------------------------------------------- #
# repos surface (two-root multi-repo): /api/repos, rescan, queue/repo binding,
# per-task repo override, and the repo_unavailable → paused run lifecycle.
# --------------------------------------------------------------------------- #


def test_server_repos_endpoint_known_set_bindings_and_warnings(tmp_path: Path) -> None:
    """`GET /api/repos` mirrors the manager: the workspace path + tasks-store
    name, the known-repos set (every workspace child with a `.git`, including the
    content store), each queue's bound repo + availability, and one warning per
    queue whose configured repo is currently absent."""
    _seed(
        tmp_path,
        tasks={"alpha": "a"},
        repos=("longitude", "horizon"),  # two present target repos
        # An alternate queue bound to a repo that was never created on disk.
        queues={"ghost": {"config": {"repo": "telescope"}}},
    )
    client = _client(tmp_path)

    payload = client.get("/api/repos").json()
    assert payload["workspace"] == str(tmp_path)
    assert payload["tasks_repo"] == DEFAULT_TASKS_REPO

    # Known set: every workspace child with a `.git` — the two target repos plus
    # the content-store repo — sorted, each flagged available.
    assert payload["repos"] == [
        {"name": "horizon", "available": True},
        {"name": "longitude", "available": True},
        {"name": DEFAULT_TASKS_REPO, "available": True},
    ]

    # Per-queue bindings: main → longitude (present), ghost → telescope (absent).
    by_queue = {q["queue"]: q for q in payload["queues"]}
    assert by_queue["main"] == {"queue": "main", "repo": "longitude", "available": True}
    assert by_queue["ghost"] == {"queue": "ghost", "repo": "telescope", "available": False}

    # Exactly the absent binding raises a warning.
    assert payload["warnings"] == [{"queue": "ghost", "repo": "telescope"}]


def test_server_repos_rescan_clears_warning_when_repo_appears(tmp_path: Path) -> None:
    """`POST /api/repos/rescan` recomputes the known set live from disk: cloning a
    previously-absent repo into the workspace and rescanning makes it available
    and clears its queue's warning (no restart, no DB-backed paused table)."""
    _seed(
        tmp_path,
        tasks={"alpha": "a"},
        queues={"deploy": {"config": {"repo": "satellite"}}},  # absent on disk
    )
    client = _client(tmp_path)

    before = client.get("/api/repos").json()
    assert {"queue": "deploy", "repo": "satellite"} in before["warnings"]
    assert "satellite" not in {r["name"] for r in before["repos"]}
    assert next(q for q in before["queues"] if q["queue"] == "deploy")["available"] is False

    # Clone/create the missing repo on disk, then rescan.
    make_target_repo(tmp_path, "satellite")
    after = client.post("/api/repos/rescan").json()

    assert {"name": "satellite", "available": True} in after["repos"]
    assert {"queue": "deploy", "repo": "satellite"} not in after["warnings"]
    assert next(q for q in after["queues"] if q["queue"] == "deploy")["available"] is True


def test_server_queue_repo_get_put_round_trip_and_validation(tmp_path: Path) -> None:
    """`GET/PUT /api/queue/repo` reads and persists a queue's default target repo:
    the binding round-trips per queue, a null clears it, and a malformed
    reference (the path-traversal guard) is a 400 that leaves the value intact."""
    _seed(
        tmp_path,
        tasks={"alpha": "a"},
        repos=("longitude", "horizon"),
        queues={"ns": {"config": {"repo": "horizon"}}},
    )
    client = _client(tmp_path)

    # Defaults seeded by the builder, read back per queue.
    assert client.get("/api/queue/repo").json() == {"repo": "longitude"}
    assert client.get("/api/queue/repo", params={"queue": "ns"}).json() == {"repo": "horizon"}

    # Re-point main to horizon; the value round-trips and the alternate queue is
    # untouched (each queue owns its own binding).
    assert client.put("/api/queue/repo", json={"repo": "horizon"}).status_code == 200
    assert client.get("/api/queue/repo").json() == {"repo": "horizon"}
    assert client.get("/api/queue/repo", params={"queue": "ns"}).json() == {"repo": "horizon"}

    # A malformed reference (path traversal) is rejected and never persisted.
    bad = client.put("/api/queue/repo", json={"repo": "../evil"})
    assert bad.status_code == 400
    assert client.get("/api/queue/repo").json() == {"repo": "horizon"}

    # Null clears the binding (the queue then has no default repo).
    assert client.put("/api/queue/repo", json={"repo": None}).status_code == 200
    assert client.get("/api/queue/repo").json() == {"repo": None}

    # An unknown queue is a 404 (not a silent write).
    assert client.put(
        "/api/queue/repo", params={"queue": "nope"}, json={"repo": "longitude"}
    ).status_code == 404


def test_server_task_repo_override_round_trip_and_validation(tmp_path: Path) -> None:
    """A per-task `repo` override on create/edit round-trips into the brief's
    frontmatter, clearing it (``""``/``default``) drops the key so the task
    inherits the queue default, and a malformed reference is a 400."""
    _seed(tmp_path, tasks={}, repos=("longitude", "horizon"))
    client = _client(tmp_path)

    # Create with an explicit override.
    created = client.post(
        "/api/tasks", json={"title": "Repo Task", "text": "x", "repo": "horizon"}
    )
    assert created.status_code == 201
    slug = created.json()["task"]
    assert client.get(f"/api/tasks/{slug}").json()["frontmatter_raw"]["repo"] == "horizon"

    # Edit the override to a different (valid) repo.
    assert client.patch(f"/api/tasks/{slug}", json={"repo": "longitude"}).status_code == 200
    assert client.get(f"/api/tasks/{slug}").json()["frontmatter_raw"]["repo"] == "longitude"

    # Clearing the override ("") drops the key → inherit the queue default.
    assert client.patch(f"/api/tasks/{slug}", json={"repo": ""}).status_code == 200
    assert "repo" not in client.get(f"/api/tasks/{slug}").json()["frontmatter_raw"]

    # A malformed override is a 400 on edit …
    assert client.patch(f"/api/tasks/{slug}", json={"repo": "../evil"}).status_code == 400
    # … and on create — and a rejected create must not orphan a brief.
    main_dir = tmp_path / "nightshift-tasks" / "main"
    before = {p.name for p in main_dir.glob("*.md")}
    assert client.post(
        "/api/tasks", json={"title": "Bad Repo", "text": "x", "repo": "Not A Slug"}
    ).status_code == 400
    assert {p.name for p in main_dir.glob("*.md")} == before


def test_server_absent_repo_run_pauses_in_runs_without_worktree(tmp_path: Path) -> None:
    """A queue whose configured repo is absent pauses (not fails) each task it
    runs: the run record surfaces `status: "paused"` (repo_unavailable), no run is
    aborted/failed, and no worktree is cut under `<workspace>/.worktrees/`."""
    # main is bound to a repo that does not exist on disk; no target repos created.
    _seed(tmp_path, tasks={"alpha": "Do alpha."}, main_repo="satellite", repos=())
    client = _client(tmp_path)

    # No worker is ever launched — run_task pauses before cutting a worktree — so
    # this drives the real player end-to-end with no monkeypatch.
    assert client.post("/api/transport", json={"action": "play", "mode": "auto"}).status_code == 200

    deadline = time.monotonic() + 5.0
    runs: list = []
    while time.monotonic() < deadline:
        runs = client.get("/api/runs").json()
        if runs and any(t["status"] == "paused" for t in runs[0]["tasks"]):
            break
        time.sleep(0.02)

    assert runs, "expected a run record"
    tasks = runs[0]["tasks"]
    alpha = next(t for t in tasks if t["task"] == "alpha")
    assert alpha["status"] == "paused"
    assert "satellite" in alpha["result_line"]
    # Paused, not failed/aborted: the run is clean and re-runnable.
    assert runs[0]["aborted"] is False
    assert all(t["status"] == "paused" for t in tasks)
    # No worktree was cut for the absent repo.
    assert not (tmp_path / ".worktrees").exists()
