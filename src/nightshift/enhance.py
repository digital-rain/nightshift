"""Brief enhancement — the manager-side enhance-on-create pass.

One synchronous, tool-less completion (:func:`nightshift.agent.transport
.complete`) that rewrites an operator's raw brief into a self-contained spec a
worker can implement without conversation context. The system prompt is the
shipped asset ``assets/prompts/enhance-brief.md`` (distilled from the original
embedded ``task`` skill's "writing the brief" rules).

Callers run this off the event loop (``asyncio.to_thread``) — the transport is
blocking httpx. Failures surface as :class:`EnhanceError` so the API layer can
refuse task creation (the operator's draft survives client-side) instead of
silently queueing the raw text as if it had been enhanced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nightshift._paths import PROMPTS_DIR
from nightshift.agent.transport import TransportError, complete


ENHANCE_PROMPT_PATH = PROMPTS_DIR / "enhance-brief.md"

# One-shot rewrite of prose: generous but bounded — a hung vendor call must
# not pin the create request forever.
ENHANCE_TIMEOUT_SECONDS = 120.0


class EnhanceError(Exception):
    """The enhancement pass failed (transport error or an empty rewrite)."""


@dataclass(frozen=True)
class EnhanceResult:
    """The rewritten brief plus the call's telemetry (vendor-shaped usage)."""

    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)


def enhance_brief(
    title: str,
    text: str,
    *,
    model: str,
    env: dict[str, str],
    timeout: float = ENHANCE_TIMEOUT_SECONDS,
) -> EnhanceResult:
    """Rewrite ``text`` (the operator's raw brief) for worker execution.

    ``model`` is the bare ``<vendor>/<upstream>`` id (the transport's shape,
    e.g. ``anthropic/claude-sonnet-4-6``); ``env`` supplies the vendor API
    keys. Raises :class:`EnhanceError` on any transport failure or when the
    model returns an empty rewrite.
    """
    system = ENHANCE_PROMPT_PATH.read_text()
    user = f"Title: {title.strip()}\n\nOriginal brief:\n\n{text.strip()}"
    try:
        completion = complete(
            [{"role": "user", "content": user}],
            tools=[],
            knobs={"max_tokens": 8192},
            model=model,
            system=system,
            env=env,
            timeout=timeout,
        )
    except TransportError as exc:
        raise EnhanceError(str(exc)) from exc
    enhanced = (completion.text or "").strip()
    if not enhanced:
        raise EnhanceError(f"model {model} returned an empty rewrite")
    return EnhanceResult(text=enhanced, model=model, usage=completion.usage or {})
