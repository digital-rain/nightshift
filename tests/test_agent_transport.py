"""Phase 3 tests — the vendor adapter (spec §5, §6.2, deviations 1-3, §10).

Network-free: ``httpx.post`` is monkeypatched to a fake that records the request
and returns a canned vendor response.
"""

from __future__ import annotations

from typing import Any

import pytest

import nightshift.agent.transport as transport
from nightshift.agent.transport import (
    TransportError,
    complete,
    split_vendor,
)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = "error body"

    def json(self) -> dict[str, Any]:
        return self._payload


def _capture_post(captured: dict[str, Any], payload: dict[str, Any]):
    def fake_post(url, *, json=None, headers=None, timeout=None):
        captured.update(url=url, headers=headers, body=json)
        return _FakeResponse(payload)

    return fake_post


# --------------------------------------------------------------------------- #
# split_vendor
# --------------------------------------------------------------------------- #


def test_split_vendor() -> None:
    assert split_vendor("anthropic/claude-sonnet-4-6") == ("anthropic", "claude-sonnet-4-6")
    assert split_vendor("ollama-cloud/qwen3-coder:480b") == (
        "ollama-cloud",
        "qwen3-coder:480b",
    )


def test_unsupported_vendor_raises() -> None:
    with pytest.raises(TransportError):
        complete([], [], {}, model="openai/gpt-4", env={})


# --------------------------------------------------------------------------- #
# Anthropic path
# --------------------------------------------------------------------------- #

_ANTHROPIC_REPLY = {
    "content": [
        {"type": "text", "text": "thinking out loud"},
        {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a"}},
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3},
}


def test_anthropic_url_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(transport.httpx, "post", _capture_post(captured, _ANTHROPIC_REPLY))
    out = complete(
        [{"role": "user", "content": "hi"}],
        [{"name": "read_file", "description": "", "input_schema": {}}],
        {"max_tokens": 1234},
        model="anthropic/claude-sonnet-4-6",
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )
    assert captured["url"] == transport.ANTHROPIC_URL
    assert captured["headers"]["x-api-key"] == "sk-test"
    assert captured["headers"]["anthropic-version"] == transport.ANTHROPIC_VERSION
    assert captured["body"]["max_tokens"] == 1234
    assert captured["body"]["stream"] is False
    # response parsed into text + tool_calls + verbatim usage
    assert out.text == "thinking out loud"
    assert out.tool_calls[0].name == "read_file"
    assert out.usage["cache_read_input_tokens"] == 3
    assert out.stop_reason == "tool_use"


def test_anthropic_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(TransportError):
        complete([], [], {}, model="anthropic/claude-sonnet-4-6", env={})


def test_anthropic_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url, *, json=None, headers=None, timeout=None):
        return _FakeResponse({}, status_code=500)

    monkeypatch.setattr(transport.httpx, "post", fake_post)
    with pytest.raises(TransportError):
        complete([], [], {}, model="anthropic/x", env={"ANTHROPIC_API_KEY": "k"})


def test_thinking_adaptive_on_current_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(transport.httpx, "post", _capture_post(captured, _ANTHROPIC_REPLY))
    out = complete(
        [], [], {"effort": "high"},
        model="anthropic/claude-opus-4-8", env={"ANTHROPIC_API_KEY": "k"},
    )
    assert captured["body"]["thinking"] == {"type": "adaptive"}
    assert captured["body"]["output_config"] == {"effort": "high"}
    assert out.honoured["thinking"] == "adaptive"


def test_thinking_legacy_on_old_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(transport.httpx, "post", _capture_post(captured, _ANTHROPIC_REPLY))
    out = complete(
        [], [], {"effort": "high", "thinking_budget": 5000},
        model="anthropic/claude-3-5-sonnet", env={"ANTHROPIC_API_KEY": "k"},
    )
    assert captured["body"]["thinking"] == {"type": "enabled", "budget_tokens": 5000}
    assert "output_config" not in captured["body"]
    assert out.honoured["thinking"] == "legacy"


def test_thinking_off_omits_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(transport.httpx, "post", _capture_post(captured, _ANTHROPIC_REPLY))
    complete([], [], {}, model="anthropic/claude-opus-4-8", env={"ANTHROPIC_API_KEY": "k"})
    assert "thinking" not in captured["body"]
    assert "output_config" not in captured["body"]


def test_temperature_lands_in_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(transport.httpx, "post", _capture_post(captured, _ANTHROPIC_REPLY))
    complete(
        [], [], {"temperature": 0.2},
        model="anthropic/claude-opus-4-8", env={"ANTHROPIC_API_KEY": "k"},
    )
    assert captured["body"]["temperature"] == 0.2


# --------------------------------------------------------------------------- #
# Ollama path (cloud + local)
# --------------------------------------------------------------------------- #

_OLLAMA_REPLY = {
    "message": {
        "content": "done",
        "tool_calls": [
            {"function": {"name": "grep", "arguments": {"pattern": "x"}}},
        ],
    },
    "prompt_eval_count": 12,
    "eval_count": 8,
    "done_reason": "stop",
}


def test_ollama_cloud_bearer_and_host(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(transport.httpx, "post", _capture_post(captured, _OLLAMA_REPLY))
    out = complete(
        [{"role": "user", "content": "hi"}],
        [{"name": "grep", "description": "", "input_schema": {"type": "object"}}],
        {},
        model="ollama-cloud/qwen3-coder:480b",
        env={"OLLAMA_API_KEY": "oll-key"},
    )
    assert captured["url"] == f"{transport.OLLAMA_CLOUD_HOST}/api/chat"
    assert captured["headers"]["Authorization"] == "Bearer oll-key"
    assert captured["body"]["stream"] is False
    # tool def translated to {type:function, function:{...}}
    assert captured["body"]["tools"][0]["type"] == "function"
    assert captured["body"]["tools"][0]["function"]["name"] == "grep"
    # tool_calls + usage normalized into the Anthropic shape
    assert out.tool_calls[0].name == "grep"
    assert out.stop_reason == "tool_use"
    assert (out.usage["input_tokens"], out.usage["output_tokens"]) == (12, 8)


def test_ollama_local_host_no_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr(transport.httpx, "post", _capture_post(captured, _OLLAMA_REPLY))
    complete([], [], {}, model="ollama/llama3", env={})
    assert captured["url"] == f"{transport.OLLAMA_LOCAL_HOST}/api/chat"
    assert "Authorization" not in captured["headers"]


def test_ollama_cache_thinking_are_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(transport.httpx, "post", _capture_post(captured, _OLLAMA_REPLY))
    out = complete(
        [], [], {"enable_cache": True, "effort": "high"},
        model="ollama/llama3", env={},
    )
    assert "cache_control" not in str(captured["body"])
    assert "thinking" not in captured["body"]
    assert out.honoured["cache"] is False
    assert out.honoured["thinking"] == "unsupported"


def test_ollama_cloud_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with pytest.raises(TransportError):
        complete([], [], {}, model="ollama-cloud/x", env={})
