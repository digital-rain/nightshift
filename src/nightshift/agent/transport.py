"""Vendor adapter — one non-streaming completion call (spec §5, §6.2).

The loop never speaks HTTP directly; it calls :func:`complete`, which picks the
upstream API from the **vendor** token (the first segment of the bare model half
after ``select_run_backend`` strips ``nightshift/``) and returns a uniform
:class:`Completion`.

Vendors (spec invariant 4):

* ``anthropic`` — non-streaming ``POST /v1/messages`` (reuses the
  ``AnthropicBackend`` header block + ``anthropic-version``). Carries the
  thinking knob per *deviation #1*: adaptive+effort on current models, legacy
  ``budget_tokens`` only on older ids. ``usage`` is returned verbatim so its
  cache splits fold through :func:`nightshift.backends._usage_tokens`.
* ``ollama-cloud`` / ``ollama`` — ``POST {host}/api/chat`` with ``stream:false``
  (``/api/generate`` has no tools; *deviation #2*). Cloud adds a bearer + the
  ``ollama.com`` host; local hits ``localhost:11434``. Cache/thinking knobs are
  no-ops here, recorded in ``honoured``.

Non-streaming throughout (*deviation #3*): ``tool_use`` accumulation is far
simpler than SSE and ``emit_log`` gets the full assistant text per turn.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from nightshift.backends import _httpx_timeout


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OLLAMA_CLOUD_HOST = "https://ollama.com"
OLLAMA_LOCAL_HOST = "http://localhost:11434"

SUPPORTED_VENDORS = ("anthropic", "ollama-cloud", "ollama")

# Model-id prefixes that still require the *legacy* fixed-budget thinking form.
# Current models (Opus 4.6+/Sonnet 4.6/Opus 4.7/4.8) reject budget_tokens and
# want thinking:{type:"adaptive"} + output_config:{effort}. See deviation #1.
_LEGACY_THINKING_PREFIXES = (
    "claude-3-5",
    "claude-3-7",
    "claude-sonnet-3",
    "claude-opus-3",
)


class TransportError(Exception):
    """An upstream HTTP/transport failure (>=400 or unreachable).

    The loop turns this into an honest ``LoopResult(error=...)`` with no partial
    state (spec §5.5) — we never pretend a failed call produced an edit.
    """


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation the model asked for, normalized across vendors."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Completion:
    """One turn's result, vendor-independent.

    ``honoured`` records which knobs the vendor actually applied (e.g. Ollama
    drops ``cache``/``thinking``) so the loop and tests can assert intent without
    inspecting the wire body.
    """

    text: str
    tool_calls: list[ToolCall]
    usage: dict[str, Any]
    stop_reason: str | None
    honoured: dict[str, Any] = field(default_factory=dict)


def split_vendor(model: str) -> tuple[str, str]:
    """Split the bare half ``<vendor>/<upstream-model>`` on the first ``/``.

    ``model`` is what reaches the backend after ``select_run_backend`` strips the
    ``nightshift/`` provider — e.g. ``anthropic/claude-sonnet-4-6`` →
    ``("anthropic", "claude-sonnet-4-6")``.
    """
    vendor, _, upstream = model.partition("/")
    return vendor.strip(), upstream.strip()


def _uses_legacy_thinking(model: str) -> bool:
    return any(model.startswith(p) for p in _LEGACY_THINKING_PREFIXES)


def complete(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    knobs: dict[str, Any],
    *,
    model: str,
    system: Any = None,
    env: dict[str, str],
    timeout: float | None = None,
    should_abort: Any = None,
) -> Completion:
    """Drive one completion. ``model`` is the bare ``<vendor>/<upstream>`` half.

    Raises :class:`TransportError` on any upstream failure.
    """
    vendor, upstream = split_vendor(model)
    if vendor == "anthropic":
        return _complete_anthropic(
            messages, tools, knobs, model=upstream, system=system, env=env, timeout=timeout
        )
    if vendor in ("ollama-cloud", "ollama"):
        return _complete_ollama(
            messages, tools, knobs, vendor=vendor, model=upstream, system=system,
            env=env, timeout=timeout,
        )
    raise TransportError(
        f"unsupported vendor {vendor!r} (expected one of {SUPPORTED_VENDORS})"
    )


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #


def _thinking_body(model: str, knobs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map the ``effort``/``thinking_budget`` knob to request fields + honoured.

    Returns ``(body_fragment, honoured_fragment)``. Deviation #1: current models
    take ``thinking:{type:"adaptive"}`` + ``output_config:{effort}``; only older
    ids accept ``thinking:{type:"enabled", budget_tokens:N}``.
    """
    effort = knobs.get("effort")
    if effort in (None, "", "off"):
        return {}, {"thinking": "off"}
    if _uses_legacy_thinking(model):
        budget = int(knobs.get("thinking_budget") or 8192)
        return (
            {"thinking": {"type": "enabled", "budget_tokens": budget}},
            {"thinking": "legacy", "budget_tokens": budget},
        )
    return (
        {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        },
        {"thinking": "adaptive", "effort": effort},
    )


def _complete_anthropic(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    knobs: dict[str, Any],
    *,
    model: str,
    system: Any,
    env: dict[str, str],
    timeout: float | None,
) -> Completion:
    key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise TransportError("ANTHROPIC_API_KEY is not set")
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": int(knobs.get("max_tokens", 4096)),
        "messages": messages,
        "tools": tools,
        "stream": False,
    }
    if system is not None:
        body["system"] = system
    if knobs.get("temperature") is not None:
        body["temperature"] = float(knobs["temperature"])
    think_body, think_honoured = _thinking_body(model, knobs)
    body.update(think_body)
    honoured: dict[str, Any] = {"vendor": "anthropic", **think_honoured}
    honoured["cache"] = bool(knobs.get("enable_cache", True))

    try:
        resp = httpx.post(
            ANTHROPIC_URL, json=body, headers=headers, timeout=_httpx_timeout(timeout)
        )
    except httpx.HTTPError as exc:
        raise TransportError(f"anthropic request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise TransportError(f"anthropic HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in data.get("content", []):
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    input=block.get("input", {}) or {},
                )
            )
    return Completion(
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=data.get("usage", {}) or {},
        stop_reason=data.get("stop_reason"),
        honoured=honoured,
    )


# --------------------------------------------------------------------------- #
# Ollama (cloud + local) — /api/chat with tools
# --------------------------------------------------------------------------- #


def _complete_ollama(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    knobs: dict[str, Any],
    *,
    vendor: str,
    model: str,
    system: Any,
    env: dict[str, str],
    timeout: float | None,
) -> Completion:
    if vendor == "ollama-cloud":
        host = OLLAMA_CLOUD_HOST
        key = env.get("OLLAMA_API_KEY") or os.environ.get("OLLAMA_API_KEY")
        if not key:
            raise TransportError("OLLAMA_API_KEY is not set")
        headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    else:
        host = env.get("OLLAMA_HOST") or os.environ.get("OLLAMA_HOST") or OLLAMA_LOCAL_HOST
        headers = {"content-type": "application/json"}

    # /api/chat takes the system turn as a message, not a top-level field.
    chat_messages = list(messages)
    if system is not None:
        sys_text = system if isinstance(system, str) else _system_to_text(system)
        chat_messages = [{"role": "system", "content": sys_text}, *messages]

    body: dict[str, Any] = {
        "model": model,
        "messages": chat_messages,
        "tools": [_ollama_tool(t) for t in tools],
        "stream": False,
    }
    options: dict[str, Any] = {}
    if knobs.get("temperature") is not None:
        options["temperature"] = float(knobs["temperature"])
    if knobs.get("max_tokens") is not None:
        options["num_predict"] = int(knobs["max_tokens"])
    if options:
        body["options"] = options
    # cache/thinking are Anthropic-only — record them as not honoured here.
    honoured = {"vendor": vendor, "cache": False, "thinking": "unsupported"}

    try:
        resp = httpx.post(
            f"{host}/api/chat", json=body, headers=headers, timeout=_httpx_timeout(timeout)
        )
    except httpx.HTTPError as exc:
        raise TransportError(f"ollama request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise TransportError(f"ollama HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()

    message = data.get("message", {}) or {}
    tool_calls: list[ToolCall] = []
    for index, call in enumerate(message.get("tool_calls", []) or []):
        fn = call.get("function", {}) or {}
        tool_calls.append(
            ToolCall(
                id=call.get("id") or f"call_{index}",
                name=fn.get("name", ""),
                input=fn.get("arguments", {}) or {},
            )
        )
    # Map Ollama's counts into the Anthropic-shaped usage so _usage_tokens folds
    # them uniformly (input_tokens / output_tokens; no cache splits to add).
    usage = {
        "input_tokens": data.get("prompt_eval_count"),
        "output_tokens": data.get("eval_count"),
    }
    stop_reason = "tool_use" if tool_calls else (data.get("done_reason") or "stop")
    return Completion(
        text=message.get("content", "") or "",
        tool_calls=tool_calls,
        usage=usage,
        stop_reason=stop_reason,
        honoured=honoured,
    )


def _ollama_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Anthropic-shaped tool def → Ollama's ``{type:function, function:{...}}``."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _system_to_text(system: Any) -> str:
    """Flatten an Anthropic system value (str or list of text blocks) to text."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            b.get("text", "") for b in system if isinstance(b, dict)
        )
    return str(system)
