"""Tests for the manager-side brief enhancement pass (``nightshift.enhance``).

Network-free: :func:`nightshift.enhance.complete` (the transport seam) is
monkeypatched, mirroring the fake-vendor style of ``test_agent_transport.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

import nightshift.enhance as enhance_mod
from nightshift.agent.transport import Completion, TransportError
from nightshift.enhance import (
    ENHANCE_PROMPT_PATH,
    EnhanceError,
    enhance_brief,
)


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
