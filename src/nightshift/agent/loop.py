"""The agentic tool loop + cache strategy (spec §5, invariant 7).

:func:`run_loop` drives a model through turns of: send messages+tools → if the
model asked for tools, dispatch them in the sandbox and feed the results back →
repeat until it stops asking (success), or an abort/timeout/turn-limit/transport
failure ends it.

The render order is fixed — ``tools → system(charter) → messages(brief, then
turns)`` — and the system block (tools + charter) is **byte-stable** across the
run, which is what makes the Anthropic prompt cache pay off (spec §1.3, the
latency/cost lever). Cache breakpoints are placed only for the ``anthropic``
vendor; Ollama skips placement entirely.

The charter is loaded from a static asset with **no per-run interpolation** — a
timestamp or task id in it would bust the cache prefix every run (invariant 7a).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nightshift._paths import asset
from nightshift.agent.tools import ToolRegistry
from nightshift.agent.transport import Completion, TransportError, split_vendor


# Defaults — version-controlled module constants (spec invariant 5). The Phase 7
# config dataclass defaults must agree with these.
DEFAULT_MAX_TURNS = 50
DEFAULT_MAX_TOKENS = 4096
DEFAULT_CACHE_BREAKPOINTS = 2
MAX_CACHE_BREAKPOINTS = 4

# Anthropic completion signal that the model wants tools dispatched.
_STOP_TOOL_USE = "tool_use"

TransportComplete = Callable[..., Completion]


@dataclass
class LoopResult:
    """Outcome of a full loop run.

    ``error`` set (with ``returncode != 0`` upstream) means an honest transport
    failure — no edits are claimed (spec §5.5). ``aborted`` set means the
    controller asked us to stop. ``input_tokens``/``output_tokens`` are summed
    across turns (cache splits folded in by the caller via ``_usage_tokens``).
    """

    turns: int = 0
    text: str = ""
    error: str | None = None
    aborted: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    honoured: dict[str, Any] = field(default_factory=dict)


def load_charter() -> str:
    """Load the byte-stable system charter (no interpolation — invariant 7a)."""
    return asset("prompts", "agent-charter.md").read_text(encoding="utf-8")


def _sum_usage(acc: dict[str, Any], turn_usage: dict[str, Any]) -> None:
    """Add one turn's usage into the accumulator, keeping the Anthropic shape so
    ``_usage_tokens`` can fold the cache splits downstream."""
    for k in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        v = turn_usage.get(k)
        if v is not None:
            acc[k] = (acc.get(k) or 0) + int(v)


def _apply_cache_breakpoints(
    system_blocks: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    *,
    enabled: bool,
    ttl: str,
) -> None:
    """Place Anthropic ``cache_control`` markers in place (spec invariant 7).

    Two breakpoints by default: (1) a **stable prefix** marker on the system
    block — caches ``tools + charter``, both byte-stable; (2) a **rolling**
    marker on the last content block of the latest turn, so the lookback stays in
    range as the conversation grows. No-op when ``enabled`` is false.
    """
    if not enabled:
        return
    control = {"type": "ephemeral"}
    if ttl == "1h":
        control["ttl"] = "1h"
    if system_blocks:
        system_blocks[-1]["cache_control"] = control
    # Rolling breakpoint on the most recent message's last content block.
    if messages:
        last = messages[-1]
        content = last.get("content")
        if isinstance(content, list) and content:
            content[-1] = {**content[-1], "cache_control": control}


def run_loop(
    *,
    transport_complete: TransportComplete,
    registry: ToolRegistry,
    charter: str,
    brief: str,
    model: str,
    knobs: dict[str, Any] | None = None,
    max_turns: int | None = None,
    timeout: float | None = None,
    should_abort: Callable[[], str | None] | None = None,
    emit_log: Callable[[str], None] | None = None,
) -> LoopResult:
    """Run the tool loop to completion. See module docstring for the contract.

    ``transport_complete`` is injected (the real one is
    :func:`nightshift.agent.transport.complete`; tests pass a fake) so the loop
    is network-free under test.
    """
    knobs = knobs or {}
    emit = emit_log or (lambda _s: None)
    abort = should_abort or (lambda: None)
    limit = max_turns if max_turns is not None else DEFAULT_MAX_TURNS
    vendor, _ = split_vendor(model)
    is_anthropic = vendor == "anthropic"
    cache_enabled = is_anthropic and bool(knobs.get("enable_cache", True))
    cache_ttl = str(knobs.get("cache_ttl", "5m"))

    tools = registry.specs()
    system_blocks = [{"type": "text", "text": charter}]
    messages: list[dict[str, Any]] = [{"role": "user", "content": brief}]
    result = LoopResult()
    deadline = (time.monotonic() + timeout) if timeout and timeout > 0 else None

    while result.turns < limit:
        reason = abort()
        if reason is not None:
            result.aborted = reason
            return result
        if deadline is not None and time.monotonic() >= deadline:
            result.aborted = "timeout"
            return result

        _apply_cache_breakpoints(
            system_blocks, messages, enabled=cache_enabled, ttl=cache_ttl
        )
        remaining = (
            max(0.0, deadline - time.monotonic()) if deadline is not None else timeout
        )
        try:
            completion = transport_complete(
                messages,
                tools,
                knobs,
                model=model,
                system=system_blocks,
                timeout=remaining,
                should_abort=abort,
            )
        except TransportError as exc:
            result.error = str(exc)
            return result

        result.turns += 1
        _sum_usage(result.usage, completion.usage)
        result.honoured = completion.honoured
        if completion.text:
            result.text = completion.text
            emit(completion.text + "\n")

        if not completion.tool_calls and completion.stop_reason != _STOP_TOOL_USE:
            return result  # model is done — success

        # Append the assistant turn, then a user turn of tool_result blocks.
        assistant_content: list[dict[str, Any]] = []
        if completion.text:
            assistant_content.append({"type": "text", "text": completion.text})
        for call in completion.tool_calls:
            assistant_content.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.input,
                }
            )
        messages.append({"role": "assistant", "content": assistant_content})

        tool_results: list[dict[str, Any]] = []
        for call in completion.tool_calls:
            outcome = registry.dispatch(call.name, call.input)
            emit(f"  [{call.name}] {'error' if outcome.is_error else 'ok'}\n")
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": outcome.content,
            }
            if outcome.is_error:
                block["is_error"] = True
            tool_results.append(block)
        messages.append({"role": "user", "content": tool_results})

    result.error = f"reached max_turns ({limit}) without completing"
    return result
