"""Tests for the manager-side brief enhancement pass (``nightshift.enhance``).

Network- and subprocess-free: both seams the pass dispatches to are
monkeypatched — :func:`nightshift.enhance.complete` (the API transport,
mirroring the fake-vendor style of ``test_agent_transport.py``) and
``subprocess.run`` (the ``claude-code`` CLI print-mode path).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import nightshift.enhance as enhance_mod
from nightshift.agent.transport import Completion, TransportError
from nightshift.enhance import (
    ENHANCE_PROMPT_PATH,
    EnhanceError,
    enhance_brief,
)


def _unreachable(*args: Any, **kwargs: Any) -> Any:
    """A seam this call must never touch."""
    raise AssertionError(f"unexpected call: {args} {kwargs}")


def _fake_complete(captured: dict[str, Any], reply: Completion):
    def fake(messages, tools, knobs, *, model, system, env, timeout):
        captured.update(
            messages=messages, tools=tools, knobs=knobs,
            model=model, system=system, env=env, timeout=timeout,
        )
        return reply

    return fake


def test_prompt_asset_ships_with_the_package() -> None:
    text = ENHANCE_PROMPT_PATH.read_text()
    assert "brief" in text.lower()


def test_enhance_brief_shapes_the_call_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    reply = Completion(
        text="  A rewritten, self-contained brief.  ",
        tool_calls=[],
        usage={"input_tokens": 120, "output_tokens": 45},
        stop_reason="end_turn",
    )
    monkeypatch.setattr(enhance_mod, "complete", _fake_complete(captured, reply))

    result = enhance_brief(
        " Fix the ops screen ",
        "make it nicer\n",
        model="anthropic/claude-sonnet-4-6",
        env={"ANTHROPIC_API_KEY": "k"},
    )

    # One tool-less user turn; the shipped prompt is the system message.
    assert captured["tools"] == []
    assert captured["system"] == ENHANCE_PROMPT_PATH.read_text()
    assert captured["model"] == "anthropic/claude-sonnet-4-6"
    (msg,) = captured["messages"]
    assert msg["role"] == "user"
    assert "Fix the ops screen" in msg["content"]
    assert "make it nicer" in msg["content"]
    # The rewrite comes back stripped, with the vendor usage attached.
    assert result.text == "A rewritten, self-contained brief."
    assert result.model == "anthropic/claude-sonnet-4-6"
    assert result.usage == {"input_tokens": 120, "output_tokens": 45}


def test_enhance_brief_empty_rewrite_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = Completion(text="   ", tool_calls=[], usage={}, stop_reason="end_turn")
    monkeypatch.setattr(enhance_mod, "complete", _fake_complete({}, reply))
    with pytest.raises(EnhanceError, match="empty rewrite"):
        enhance_brief("T", "body", model="anthropic/claude-sonnet-4-6", env={})


def test_enhance_brief_wraps_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Completion:
        raise TransportError("vendor 500")

    monkeypatch.setattr(enhance_mod, "complete", boom)
    with pytest.raises(EnhanceError, match="vendor 500"):
        enhance_brief("T", "body", model="anthropic/claude-sonnet-4-6", env={})


# --------------------------------------------------------------------------- #
# claude-code provider — the CLI print-mode path
# --------------------------------------------------------------------------- #


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run(captured: dict[str, Any], proc: _Proc):
    def fake(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return proc

    return fake


_CLI_OK = json.dumps({
    "type": "result",
    "is_error": False,
    "result": "  A rewritten, self-contained brief.  ",
    "usage": {"input_tokens": 120, "output_tokens": 45},
})


def test_claude_code_model_runs_the_cli_tool_less(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        enhance_mod.subprocess, "run", _fake_run(captured, _Proc(stdout=_CLI_OK))
    )
    # The transport must not be reached for a CLI provider.
    monkeypatch.setattr(enhance_mod, "complete", _unreachable)

    result = enhance_brief(
        " Fix the ops screen ",
        "make it nicer\n",
        model="claude-code/claude-sonnet-4-6",
        env={"PATH": "/usr/bin"},
    )

    argv = captured["argv"]
    # The bare model reaches the CLI (the provider half is ours, not its).
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    assert argv[argv.index("--system-prompt") + 1] == ENHANCE_PROMPT_PATH.read_text()
    assert "Fix the ops screen" in argv[argv.index("-p") + 1]
    assert "make it nicer" in argv[argv.index("-p") + 1]
    # Tool-less and non-agentic: this pass rewrites prose, it never edits a repo.
    assert argv[argv.index("--tools") + 1] == ""
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    # A scratch cwd, so no unrelated project CLAUDE.md joins the rewrite.
    assert captured["cwd"] != str(Path.cwd())
    assert captured["timeout"] == enhance_mod.ENHANCE_TIMEOUT_SECONDS

    assert result.text == "A rewritten, self-contained brief."
    assert result.model == "claude-code/claude-sonnet-4-6"
    assert result.usage == {"input_tokens": 120, "output_tokens": 45}


def test_claude_code_cli_error_payload_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"is_error": True, "result": "credit balance too low"})
    monkeypatch.setattr(
        enhance_mod.subprocess, "run", _fake_run({}, _Proc(stdout=payload))
    )
    with pytest.raises(EnhanceError, match="credit balance too low"):
        enhance_brief("T", "body", model="claude-code/claude-sonnet-4-6", env={})


def test_claude_code_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        enhance_mod.subprocess,
        "run",
        _fake_run({}, _Proc(returncode=1, stderr="unknown option --tools")),
    )
    with pytest.raises(EnhanceError, match="unknown option"):
        enhance_brief("T", "body", model="claude-code/claude-sonnet-4-6", env={})


def test_claude_code_unparseable_output_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        enhance_mod.subprocess, "run", _fake_run({}, _Proc(stdout="not json"))
    )
    with pytest.raises(EnhanceError, match="unparseable"):
        enhance_brief("T", "body", model="claude-code/claude-sonnet-4-6", env={})


def test_claude_code_empty_result_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"is_error": False, "result": "   "})
    monkeypatch.setattr(
        enhance_mod.subprocess, "run", _fake_run({}, _Proc(stdout=payload))
    )
    with pytest.raises(EnhanceError, match="empty rewrite"):
        enhance_brief("T", "body", model="claude-code/claude-sonnet-4-6", env={})


def test_claude_code_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=120.0)

    monkeypatch.setattr(enhance_mod.subprocess, "run", boom)
    with pytest.raises(EnhanceError, match="timed out"):
        enhance_brief("T", "body", model="claude-code/claude-sonnet-4-6", env={})


def test_claude_code_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(enhance_mod.subprocess, "run", boom)
    with pytest.raises(EnhanceError, match="not found"):
        enhance_brief("T", "body", model="claude-code/claude-sonnet-4-6", env={})


@pytest.mark.parametrize("model", ["cursor/gpt-5", "antigravity/gemini-3", "auto", ""])
def test_unsupported_provider_names_what_works(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """Neither seam is touched — the failure names the supported providers."""
    monkeypatch.setattr(enhance_mod, "complete", _unreachable)
    monkeypatch.setattr(enhance_mod.subprocess, "run", _unreachable)
    with pytest.raises(EnhanceError, match="claude-code"):
        enhance_brief("T", "body", model=model, env={})
