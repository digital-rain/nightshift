from __future__ import annotations

import time
from pathlib import Path

import pytest

import nightshift.backends as backends_mod
from nightshift.backends import WorkerSpec, _stream_subprocess
from nightshift.resolve_runner import BackendSelectionError, select_run_backend


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


def test_select_run_backend_unknown_provider_raises() -> None:
    # Declared-fallbacks-only (S5): an unknown provider used to fall through
    # silently to `fallback_backend` — now it raises, naming the model and
    # where a known provider can be declared.
    with pytest.raises(BackendSelectionError, match="bogus"):
        select_run_backend("bogus/x", "cursor")


def test_select_run_backend_agnostic_without_fallback_raises() -> None:
    with pytest.raises(BackendSelectionError, match="resolve_model") as exc:
        select_run_backend("auto", None)
    assert "resolve_model" in str(exc.value)


def test_select_run_backend_agnostic_unknown_fallback_raises() -> None:
    with pytest.raises(BackendSelectionError, match="bogus-backend") as exc:
        select_run_backend("auto", "bogus-backend")
    assert "resolve_backend" in str(exc.value)
    assert "resolve_model" in str(exc.value)


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
        "claude-code", "cursor", "antigravity", "anthropic", "ollama", "ollama-cloud",
        "nightshift",
    ]

    # Known name resolves; None/empty falls back to the declared default
    # (claude-code). An unknown name is never silently substituted (S5:
    # declared fallbacks only) — it raises KeyError, mirroring require_backend.
    assert backends_mod.get_backend("cursor").name == "cursor"
    assert backends_mod.get_backend("antigravity").name == "antigravity"
    assert backends_mod.get_backend(None).name == "claude-code"
    with pytest.raises(KeyError):
        backends_mod.get_backend("nope")
    assert backends_mod.get_backend("nightshift").name == "nightshift"
    assert backends_mod.list_backends({})  # smoke: nightshift describes cleanly

    described = {b["name"]: b for b in backends_mod.list_backends({})}
    assert described["claude-code"]["agentic"] is True
    assert described["antigravity"]["agentic"] is True  # agy edits files
    assert described["anthropic"]["agentic"] is False
    assert described["ollama"]["agentic"] is False
    assert described["ollama-cloud"]["agentic"] is False
    assert set(described["claude-code"]) == {"name", "description", "agentic", "available"}


def test_backend_availability_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backends_mod.shutil, "which", lambda name: None)
    # Antigravity also probes EXTRA_BIN_DIRS on disk (agy lives in ~/.local/bin).
    monkeypatch.setattr(backends_mod.prompts, "EXTRA_BIN_DIRS", ())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert backends_mod.ClaudeCodeBackend().available({}) is False
    assert backends_mod.ClaudeCodeBackend().available({"claude_bin": "/x/claude"}) is True
    assert backends_mod.AnthropicBackend().available({}) is False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert backends_mod.AnthropicBackend().available({}) is True
    # Ollama is usable when a host is configured even without the CLI on PATH.
    assert backends_mod.OllamaBackend().available({"ollama_host": "http://h"}) is True
    # Antigravity needs the agy CLI (or an explicit bin) on the worker.
    assert backends_mod.AntigravityBackend().available({}) is False
    assert backends_mod.AntigravityBackend().available({"antigravity_bin": "/x/agy"}) is True


def test_antigravity_argv() -> None:
    argv = backends_mod.build_antigravity_argv("do it", "gemini-3.1-pro-high", {})
    assert argv[0] == "agy"
    assert argv[argv.index("-p") + 1] == "do it"  # prompt is the -p value
    assert "--dangerously-skip-permissions" in argv  # headless auto-approve
    assert argv[argv.index("--model") + 1] == "gemini-3.1-pro-high"
    # auto/max are worker keywords, not real model ids → no --model flag.
    assert "--model" not in backends_mod.build_antigravity_argv("x", "auto", {})
    # antigravity_model-style override.
    custom = backends_mod.build_antigravity_argv(
        "x", "auto", {"antigravity_model": "gemini-3.5-flash-low"},
    )
    assert custom[custom.index("--model") + 1] == "gemini-3.5-flash-low"
    # Session resume maps to --conversation.
    resumed = backends_mod.build_antigravity_argv(
        "x", "gemini-3.5-flash-low", {"resume_session_id": "abc-123"},
    )
    assert resumed[resumed.index("--conversation") + 1] == "abc-123"


def test_usage_cache_tokens_distinguishes_unreported_from_zero() -> None:
    # Missing fields entirely -> None, None (vendor doesn't report cache activity).
    assert backends_mod._usage_cache_tokens(None) == (None, None)
    assert backends_mod._usage_cache_tokens({"input_tokens": 10}) == (None, None)
    # Explicit 0 is a real, reported zero.
    assert backends_mod._usage_cache_tokens({
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 5,
    }) == (0, 5)


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


# --------------------------------------------------------------------------- #
# claude-code billing: the declared mode decides what the CLI subprocess sees
# --------------------------------------------------------------------------- #

import json  # noqa: E402
import os  # noqa: E402

from _fake_claude import install_fake_claude  # noqa: E402
from nightshift import billing as billing_mod  # noqa: E402
from nightshift.agent.transport import TransportError  # noqa: E402
from nightshift.prompts import build_claude_argv, build_claude_text_argv  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_auth_cache():
    billing_mod.clear_auth_status_cache()
    yield
    billing_mod.clear_auth_status_cache()


def _claude_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "FAKE_CLAUDE_ENV_DUMP": str(tmp_path / "env.json"),
        "FAKE_CLAUDE_ARGV_DUMP": str(tmp_path / "argv.json"),
    }
    env.update(extra)
    return env


def _dumped_env(tmp_path: Path) -> dict[str, str]:
    return json.loads((tmp_path / "env.json").read_text())


def _run_claude(tmp_path: Path, *, auth: str, mode: str, env: dict[str, str]):
    fake = install_fake_claude(tmp_path / "bin", auth=auth)
    logs: list[str] = []
    spec = WorkerSpec(
        task="t", prompt="do it", model="claude-sonnet-4-6", max_turns=None,
        cwd=tmp_path, env=env,
        config={"claude_bin": str(fake), "claude_billing": mode},
    )
    result = backends_mod.ClaudeCodeBackend().run(spec, logs.append, lambda: None)
    return result, "".join(logs)


def test_claude_run_subscription_scrubs_the_key(tmp_path: Path) -> None:
    env = _claude_env(
        tmp_path, ANTHROPIC_API_KEY="sk-leak", ANTHROPIC_BASE_URL="http://proxy",
        CLAUDE_CODE_USE_BEDROCK="1", KEEP_ME="yes",
    )
    result, log = _run_claude(tmp_path, auth="logged_in", mode="subscription", env=env)
    assert result.returncode == 0
    assert result.billing == "subscription"
    seen = _dumped_env(tmp_path)
    for key in billing_mod.CLI_AUTH_ENV_KEYS:
        assert key not in seen
    assert seen["KEEP_ME"] == "yes"
    # Telemetry still flows (the CLI reports a notional cost either way).
    assert result.cost_usd == 0.0015 and result.turns == 1
    assert "[claude-code] billing: subscription" in log


def test_claude_run_api_passes_the_key_through(tmp_path: Path) -> None:
    env = _claude_env(tmp_path, ANTHROPIC_API_KEY="sk-real")
    result, log = _run_claude(tmp_path, auth="logged_out", mode="api", env=env)
    assert result.returncode == 0
    assert result.billing == "api"
    assert _dumped_env(tmp_path)["ANTHROPIC_API_KEY"] == "sk-real"
    assert "[claude-code] billing: api" in log


def test_claude_run_api_without_key_is_a_typed_error(tmp_path: Path) -> None:
    result, _ = _run_claude(tmp_path, auth="logged_in", mode="api", env=_claude_env(tmp_path))
    assert result.returncode == 2
    assert result.error and "claude_billing=api" in result.error
    assert not (tmp_path / "env.json").exists(), "the CLI must not have been spawned"


def test_claude_run_auto_prefers_the_login(tmp_path: Path) -> None:
    env = _claude_env(tmp_path, ANTHROPIC_API_KEY="sk-leak")
    result, log = _run_claude(tmp_path, auth="logged_in", mode="auto", env=env)
    assert result.billing == "subscription"
    assert "ANTHROPIC_API_KEY" not in _dumped_env(tmp_path)
    assert "billing: subscription (claude.ai login (max))" in log


def test_claude_run_auto_falls_back_to_the_key_loudly(tmp_path: Path) -> None:
    env = _claude_env(tmp_path, ANTHROPIC_API_KEY="sk-real")
    result, log = _run_claude(tmp_path, auth="logged_out", mode="auto", env=env)
    assert result.billing == "api"
    assert _dumped_env(tmp_path)["ANTHROPIC_API_KEY"] == "sk-real"
    assert "claude.ai login not detected" in log
    assert "billing: api (auto: not logged in" in log


def test_claude_run_auto_with_nothing_errors_naming_the_setting(tmp_path: Path) -> None:
    result, _ = _run_claude(tmp_path, auth="logged_out", mode="auto", env=_claude_env(tmp_path))
    assert result.returncode == 2
    assert result.error and "claude_billing=auto" in result.error


def test_claude_run_invalid_mode_errors_naming_the_setting(tmp_path: Path) -> None:
    result, _ = _run_claude(tmp_path, auth="logged_in", mode="maybe", env=_claude_env(tmp_path))
    assert result.returncode == 2
    assert result.error and "claude_billing='maybe'" in result.error


def test_claude_complete_text_honours_the_mode(tmp_path: Path) -> None:
    fake = install_fake_claude(tmp_path / "bin", auth="logged_in", result_text="rewritten")
    backend = backends_mod.ClaudeCodeBackend()
    env = _claude_env(tmp_path, ANTHROPIC_API_KEY="sk-leak")

    text, usage = backend.complete_text(
        "sys", "user", model="claude-sonnet-4-6", env=env, timeout=30,
        config={"claude_bin": str(fake), "claude_billing": "subscription"},
    )
    assert text == "rewritten" and usage["input_tokens"] == 10
    assert "ANTHROPIC_API_KEY" not in _dumped_env(tmp_path)

    backend.complete_text(
        "sys", "user", model="claude-sonnet-4-6", env=env, timeout=30,
        config={"claude_bin": str(fake), "claude_billing": "api"},
    )
    assert _dumped_env(tmp_path)["ANTHROPIC_API_KEY"] == "sk-leak"

    with pytest.raises(TransportError, match="claude_billing=api"):
        backend.complete_text(
            "sys", "user", model="claude-sonnet-4-6", env=_claude_env(tmp_path),
            timeout=30, config={"claude_bin": str(fake), "claude_billing": "api"},
        )


def test_claude_argv_never_uses_bare() -> None:
    # --bare reads only ANTHROPIC_API_KEY (OAuth is never consulted), which
    # would defeat subscription billing outright.
    assert "--bare" not in build_claude_argv("p", "m", None, resume="s")
    assert "--bare" not in build_claude_text_argv("sys", "user", "m")


def test_anthropic_backend_stamps_api_billing(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200
        text = ""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b""

        def iter_lines(self):
            yield 'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}'
            yield 'data: {"type":"content_block_delta","delta":{"text":"hi"}}'
            yield 'data: {"type":"message_delta","usage":{"output_tokens":2}}'

    monkeypatch.setattr(backends_mod.httpx, "stream", lambda *a, **k: _Resp())
    spec = WorkerSpec(
        task="t", prompt="hi", model="claude-sonnet-4-6", max_turns=None,
        cwd=Path("/tmp"), env={"ANTHROPIC_API_KEY": "k"}, config={},
    )
    result = backends_mod.AnthropicBackend().run(spec, lambda _l: None, lambda: None)
    assert result.returncode == 0
    assert result.billing == "api"
