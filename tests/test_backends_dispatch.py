from __future__ import annotations

import time
from pathlib import Path

import pytest

import nightshift.backends as backends_mod
from nightshift.backends import WorkerSpec, _stream_subprocess
from nightshift.resolve_runner import select_run_backend


def test_known_providers_matches_backend_names() -> None:
    assert backends_mod.known_providers() == set(backends_mod.backend_names())
    assert "ollama-cloud" in backends_mod.known_providers()


def test_require_backend_returns_known() -> None:
    assert backends_mod.require_backend("cursor").name == "cursor"


def test_require_backend_unknown_raises() -> None:
    with pytest.raises(KeyError):
        backends_mod.require_backend("does-not-exist")


def test_spec_has_timeout_field() -> None:
    spec = WorkerSpec(
        task="t", prompt="p", model="m", max_turns=None,
        cwd=Path("/tmp"), env={}, config={}, timeout=12.5,
    )
    assert spec.timeout == 12.5


def test_select_run_backend_uses_qualified_provider() -> None:
    backend, model = select_run_backend("ollama-cloud/gpt-oss:120b", None)
    assert backend.name == "ollama-cloud"
    assert model == "gpt-oss:120b"


def test_select_run_backend_keeps_colons_and_slashes_in_model() -> None:
    backend, model = select_run_backend("ollama/hf.co/user/repo", "cursor")
    assert backend.name == "ollama"
    assert model == "hf.co/user/repo"


def test_select_run_backend_falls_back_for_agnostic() -> None:
    backend, model = select_run_backend("auto", "cursor")
    assert backend.name == "cursor"
    assert model == "auto"  # keyword passed through to the fallback backend


def test_select_run_backend_unknown_provider_falls_back() -> None:
    backend, model = select_run_backend("bogus/x", "cursor")
    assert backend.name == "cursor"
    assert model == "bogus/x"  # unrecognized prefix is left untouched


def test_stream_subprocess_kills_on_timeout(tmp_path: Path) -> None:
    logs: list[str] = []
    start = time.monotonic()
    result = _stream_subprocess(
        ["sleep", "30"],
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
        emit_log=logs.append, should_abort=lambda: None,
        timeout=1.0,
    )
    assert time.monotonic() - start < 10  # killed early, not after 30s
    assert result.aborted == "timeout" or (result.error and "tim" in result.error.lower())


# --------------------------------------------------------------------------- #
# Backend registry, availability gating, per-backend argv
# (relocated from test_nightshift_ui.py)
# --------------------------------------------------------------------------- #


def test_backend_registry_and_selection() -> None:
    names = backends_mod.backend_names()
    assert names == [
        "claude-code", "cursor", "gemini", "anthropic", "ollama", "ollama-cloud",
        "nightshift",
    ]

    # Known name resolves; unknown/empty falls back to the default (claude-code).
    assert backends_mod.get_backend("cursor").name == "cursor"
    assert backends_mod.get_backend("gemini").name == "gemini"
    assert backends_mod.get_backend(None).name == "claude-code"
    assert backends_mod.get_backend("nope").name == "claude-code"
    assert backends_mod.get_backend("nightshift").name == "nightshift"
    assert backends_mod.list_backends({})  # smoke: nightshift describes cleanly

    described = {b["name"]: b for b in backends_mod.list_backends({})}
    assert described["claude-code"]["agentic"] is True
    assert described["gemini"]["agentic"] is True  # Gemini CLI edits files
    assert described["anthropic"]["agentic"] is False
    assert described["ollama"]["agentic"] is False
    assert described["ollama-cloud"]["agentic"] is False
    assert set(described["claude-code"]) == {"name", "description", "agentic", "available"}


def test_backend_availability_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backends_mod.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert backends_mod.ClaudeCodeBackend().available({}) is False
    assert backends_mod.ClaudeCodeBackend().available({"claude_bin": "/x/claude"}) is True
    assert backends_mod.AnthropicBackend().available({}) is False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert backends_mod.AnthropicBackend().available({}) is True
    # Ollama is usable when a host is configured even without the CLI on PATH.
    assert backends_mod.OllamaBackend().available({"ollama_host": "http://h"}) is True
    # Gemini needs the CLI (or an explicit bin) on the worker.
    assert backends_mod.GeminiCLIBackend().available({}) is False
    assert backends_mod.GeminiCLIBackend().available({"gemini_bin": "/x/gemini"}) is True


def test_gemini_argv_and_stats_parse() -> None:
    argv = backends_mod.build_gemini_argv("do it", "gemini-2.5-pro", {})
    assert argv[0] == "gemini"
    assert argv[argv.index("-p") + 1] == "do it"  # prompt is the -p value
    assert "--yolo" in argv  # headless auto-approve of edit/shell tools
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "gemini-2.5-pro"
    # auto/max are worker keywords, not real model ids → no --model flag.
    assert "--model" not in backends_mod.build_gemini_argv("x", "auto", {})
    # cursor_model-style override.
    custom = backends_mod.build_gemini_argv("x", "auto", {"gemini_model": "gemini-2.5-flash"})
    assert custom[custom.index("--model") + 1] == "gemini-2.5-flash"

    # Telemetry mined from the end-of-run JSON blob's stats.models[*].
    stats = backends_mod.parse_gemini_stats({
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
    default = backends_mod.build_cursor_argv("do it", "auto", {})
    assert default[0] == "cursor-agent"
    assert {"-p", "--force", "--trust"} <= set(default)
    assert default[-1] == "do it"  # prompt is the trailing positional
    assert default[default.index("--model") + 1] == "auto"

    custom = backends_mod.build_cursor_argv(
        "do it", "auto", {"cursor_model": "sonnet-4", "cursor_extra_args": ["--sandbox", "enabled"]}
    )
    assert custom[custom.index("--model") + 1] == "sonnet-4"
    assert "--sandbox" in custom and custom[-1] == "do it"
