"""Tests for ``nightshift.prompts`` (prompt building, claude argv, result-line
extraction, worker env) and the ``AgentStreamParser`` telemetry capture in
``nightshift.backends``.

Relocated in Phase 9 from the legacy ``test_run_local.py`` and
``test_nightshift_ui.py`` suites to the real module homes; behavior unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from _workspace import build_workspace
from nightshift import prompts
from nightshift._paths import PROMPTS_DIR
from nightshift.backends import AgentStreamParser
from nightshift.prompts import (
    build_claude_argv,
    build_prompt,
    extract_result_line,
    resolve_claude_bin,
    worker_env,
)
from nightshift.queue_config import DEFAULT_VALIDATE_CMD
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.spawn_daily import resolve_config
from nightshift.task_files import materialize_brief


REPO = "longitude"


def _full(
    tmp_path: Path,
    *,
    tasks: dict[str, str] | None = None,
    queues: dict[str, dict[str, object]] | None = None,
) -> tuple[Path, Path]:
    workspace = build_workspace(tmp_path, tasks=tasks, queues=queues)
    return workspace, workspace / DEFAULT_TASKS_REPO


# --------------------------------------------------------------------------- #
# build_prompt — a pure formatter over the materialised brief path
# --------------------------------------------------------------------------- #


def test_build_prompt_matches_ci_format(tmp_path: Path) -> None:
    workspace, _tasks_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    scratch = materialize_brief(workspace, REPO, "10.hello", "Do something.", queue=None)
    prompt = build_prompt("10.hello", task_file=str(scratch), validate_cmd=DEFAULT_VALIDATE_CMD)
    assert prompt.startswith(f"Your task file is: {scratch}\n")
    assert "The TASK variable is: 10.hello" in prompt
    # The shipped local worker charter is appended verbatim after the injected vars.
    assert "You are the nightshift worker running **locally**." in prompt


def test_build_prompt_injects_default_validate(tmp_path: Path) -> None:
    """With no queue/operator override, the worker is told the default
    (``just validate``) — the same command the runner resolves and injects."""
    workspace, tasks_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    # Clear the seeded operator ``validate`` so the queue inherits the default.
    ns_dir = workspace / ".nightshift"
    ns_dir.mkdir(parents=True, exist_ok=True)
    (ns_dir / "manager.json").write_text(json.dumps({"model": "auto"}) + "\n")
    config = resolve_config(workspace, tasks_root, "main")
    validate_cmd = str(config.get("validate") or DEFAULT_VALIDATE_CMD)
    prompt = build_prompt("10.hello", task_file="/scratch.md", validate_cmd=validate_cmd)
    assert "The VALIDATE command is: just validate" in prompt


def test_build_prompt_injects_playlist_validate_override(tmp_path: Path) -> None:
    """A queue's ``validate`` override is resolved by the runner and injected as
    ``$VALIDATE`` so the worker self-validates with the queue's command, not just
    the engine gate."""
    workspace, tasks_root = _full(
        tmp_path,
        queues={
            "nightshift": {
                "config": {"validate": "just validate-nightshift", "order": [], "repo": REPO}
            }
        },
    )
    config = resolve_config(workspace, tasks_root, "nightshift")
    validate_cmd = str(config.get("validate") or DEFAULT_VALIDATE_CMD)
    prompt = build_prompt("10.hello", task_file="/scratch.md", validate_cmd=validate_cmd)
    assert "The VALIDATE command is: just validate-nightshift" in prompt
    assert "The VALIDATE command is: just validate\n" not in prompt


def test_build_prompt_injects_task_file_path_for_main_queue(tmp_path: Path) -> None:
    """``$TASK_FILE`` carries the brief's *delivered* path — a run-scratch file
    OUTSIDE the target repo (so the brief never enters the repo the agent lands
    in), never ``.tasks/<task>.md`` inside it."""
    workspace, _tasks_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    scratch = materialize_brief(workspace, REPO, "10.hello", "Do something.", queue=None)
    prompt = build_prompt("10.hello", task_file=str(scratch), validate_cmd=DEFAULT_VALIDATE_CMD)
    assert f"The TASK_FILE variable is: {scratch}" in prompt
    assert f"Your task file is: {scratch}" in prompt
    # The brief is delivered via the worktree-sibling scratch, not the repo tree.
    assert str(scratch).startswith(str(workspace / ".worktrees"))
    assert ".tasks/10.hello.md" not in prompt


def test_build_prompt_injects_playlist_task_file_path(tmp_path: Path) -> None:
    """An alternate-queue task's scratch is namespaced by queue and still lives
    OUTSIDE the target repo — not at ``.tasks/<playlist>/<task>.md``."""
    workspace, _tasks_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    scratch = materialize_brief(workspace, REPO, "10.hello", "Do something.", queue="nightshift")
    prompt = build_prompt("10.hello", task_file=str(scratch), validate_cmd="just validate-nightshift")
    assert f"The TASK_FILE variable is: {scratch}" in prompt
    assert "task-local-nightshift-10.hello" in str(scratch)
    assert ".tasks/nightshift/10.hello.md" not in prompt


def test_local_prompt_delivers_task_file_as_readonly_scratch() -> None:
    """The shipped local worker prompt delivers ``$TASK_FILE`` as a read-only
    scratch copy that the worker must not modify/move/commit — the brief lives in
    the separate content store and the runner retires it. It must never hardcode
    ``.tasks/$TASK.md`` (which would point at a path that no longer exists)."""
    prompt_body = (PROMPTS_DIR / "nightshift-local.md").read_text()
    assert "$TASK_FILE" in prompt_body
    assert "read-only scratch copy" in prompt_body
    assert "does not live in this repo" in prompt_body
    assert 'git rm ".tasks/$TASK.md"' not in prompt_body


def test_build_prompt_split_injects_variables(tmp_path: Path) -> None:
    """When split=True, $SPLIT and $SPLIT_DIR are injected into the prompt."""
    workspace, _tasks_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    scratch = materialize_brief(workspace, REPO, "10.hello", "Do something.")
    prompt = build_prompt(
        "10.hello",
        task_file=str(scratch),
        validate_cmd=DEFAULT_VALIDATE_CMD,
        split=True,
        split_dir="/tmp/split-dir",
    )
    assert "The SPLIT variable is: true" in prompt
    assert "The SPLIT_DIR variable is: /tmp/split-dir" in prompt


def test_build_prompt_no_split_omits_variables(tmp_path: Path) -> None:
    """When split is False (default), the injected SPLIT/SPLIT_DIR header lines
    are absent (the prompt body may still reference $SPLIT_DIR in docs)."""
    workspace, _tasks_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    scratch = materialize_brief(workspace, REPO, "10.hello", "Do something.")
    prompt = build_prompt(
        "10.hello",
        task_file=str(scratch),
        validate_cmd=DEFAULT_VALIDATE_CMD,
    )
    assert "The SPLIT variable is:" not in prompt
    assert "The SPLIT_DIR variable is:" not in prompt


# --------------------------------------------------------------------------- #
# build_claude_argv + stream telemetry
# --------------------------------------------------------------------------- #


def test_build_claude_argv_with_model() -> None:
    argv = build_claude_argv("do stuff", "claude-opus-4-6", None)
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "do stuff"
    assert argv[argv.index("--model") + 1] == "claude-opus-4-6"
    assert "--max-turns" not in argv
    assert "--dangerously-skip-permissions" in argv


def test_build_claude_argv_with_max_turns() -> None:
    argv = build_claude_argv("do stuff", "claude-sonnet-4-6", 30)
    assert "--max-turns" in argv
    assert argv[argv.index("--max-turns") + 1] == "30"


def test_build_claude_argv_allowed_tools() -> None:
    argv = build_claude_argv("x", "claude-sonnet-4-6", None)
    idx = argv.index("--allowedTools")
    assert argv[idx + 1] == "Bash,Edit,MultiEdit,Write,Read,Glob,Grep,LS"


def test_build_claude_argv_requests_stream_json_for_telemetry() -> None:
    argv = build_claude_argv("x", "claude-opus-4-6", None)
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv  # claude requires --verbose with -p + stream-json


def test_agent_stream_parser_captures_turns_tokens_cost() -> None:
    parser = AgentStreamParser()
    lines = [
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Working"}]}}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{}}]}}',
        '{"type":"result","subtype":"success","num_turns":9,'
        '"usage":{"input_tokens":1000,"cache_read_input_tokens":200,"output_tokens":300},'
        '"total_cost_usd":0.1234}',
    ]
    emitted = "".join(parser.feed(line) for line in lines)
    assert "Working" in emitted
    assert "[tool: Bash]" in emitted
    assert parser.turns == 9
    assert parser.input_tokens == 1200  # input + cache_read folded in
    assert parser.output_tokens == 300
    assert round(parser.cost_usd, 4) == 0.1234


def test_agent_stream_parser_passes_through_non_json() -> None:
    parser = AgentStreamParser()
    # An older CLI that ignores --output-format still streams readable text.
    assert parser.feed("plain log line\n") == "plain log line\n"
    assert parser.turns is None and parser.input_tokens is None


def test_agent_stream_parser_captures_cache_splits_and_model_usage() -> None:
    """Cache splits fold into input_tokens (existing behavior) and also land
    separately on cache_read/cache_creation; the raw usage blob plus Claude
    Code's modelUsage ride along in usage_payload for the run's usage jsonb."""
    parser = AgentStreamParser()
    lines = [
        '{"type":"result","subtype":"success","num_turns":9,'
        '"usage":{"input_tokens":1000,"cache_read_input_tokens":200,'
        '"cache_creation_input_tokens":50,"output_tokens":300},'
        '"modelUsage":{"claude-opus-4-8":{"inputTokens":1000,"outputTokens":300}},'
        '"total_cost_usd":0.1234}',
    ]
    for line in lines:
        parser.feed(line)
    assert parser.cache_read_input_tokens == 200
    assert parser.cache_creation_input_tokens == 50
    assert parser.usage_payload["input_tokens"] == 1000
    assert parser.usage_payload["cache_read_input_tokens"] == 200
    assert "claude-opus-4-8" in parser.usage_payload["modelUsage"]


def test_agent_stream_parser_apply_sets_cache_fields_and_usage_on_result() -> None:
    from nightshift.backends import WorkerResult

    parser = AgentStreamParser()
    parser.feed(
        '{"type":"result","num_turns":2,'
        '"usage":{"input_tokens":500,"cache_read_input_tokens":100,"output_tokens":50}}'
    )
    result = parser.apply(WorkerResult(returncode=0))
    assert result.cache_read_input_tokens == 100
    assert result.cache_creation_input_tokens is None  # not reported
    assert result.usage["input_tokens"] == 500


# --------------------------------------------------------------------------- #
# result_line extraction + worker env (relocated from test_nightshift_ui.py)
# --------------------------------------------------------------------------- #


def test_extract_result_line_pytest_summary() -> None:
    out = "collecting...\n=== 1291 passed, 3 skipped in 12.3s ===\n"
    assert extract_result_line(out) == "All 1291 tests pass"


def test_extract_result_line_fallback() -> None:
    assert extract_result_line("", "boom: something broke") == "boom: something broke"
    assert extract_result_line("") == "validate passed"


def test_resolve_claude_bin(monkeypatch: pytest.MonkeyPatch) -> None:
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
