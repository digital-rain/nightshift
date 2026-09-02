"""Brief enhancement — the manager-side enhance-on-create pass.

One synchronous, tool-less completion that rewrites an operator's raw brief
into a self-contained spec a worker can implement without conversation context.
The system prompt is the shipped asset ``assets/prompts/enhance-brief.md``
(distilled from the original embedded ``task`` skill's "writing the brief"
rules).

``enhance_brief_model`` is a provider-qualified id like every other model
setting, and dispatch follows the same provider/backend registry every other
model consumer uses: :func:`nightshift.backends.require_backend` picks the
backend, and its ``complete_text`` seam runs the one-shot rewrite (the API
vendors via the harness transport, ``claude-code`` via the CLI's print mode).
A provider without that seam — the other agentic CLIs, the in-process harness
— fails with a message naming the providers that work, rather than a
vendor-level "unsupported" from deeper in the stack.

Callers run this off the event loop (``asyncio.to_thread``) — every path
blocks. Failures surface as :class:`EnhanceError` so the API layer can refuse
task creation (the operator's draft survives client-side) instead of silently
queueing the raw text as if it had been enhanced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nightshift import backends
from nightshift._paths import PROMPTS_DIR
from nightshift.agent.transport import TransportError
from nightshift.model_id import split_model


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


def _supported() -> str:
    return ", ".join(sorted(backends.text_capable_providers()))


def enhance_brief(
    title: str,
    text: str,
    *,
    model: str,
    env: dict[str, str],
    timeout: float = ENHANCE_TIMEOUT_SECONDS,
) -> EnhanceResult:
    """Rewrite ``text`` (the operator's raw brief) for worker execution.

    ``model`` is a provider-qualified id (e.g. ``claude-code/claude-sonnet-4-6``
    or ``anthropic/claude-sonnet-4-6``); ``env`` supplies the vendor API keys
    and the child process environment. Raises :class:`EnhanceError` on any
    completion failure, a provider without one-shot text support, or an empty
    rewrite.
    """
    provider, bare = split_model(model)
    if provider is None:
        raise EnhanceError(
            f"enhance model {model!r} must be a qualified provider/model id "
            f"(supported providers: {_supported()})"
        )
    try:
        backend = backends.require_backend(provider)
    except KeyError:
        raise EnhanceError(
            f"unknown provider {provider!r} in enhance model {model!r} "
            f"(supported providers: {_supported()})"
        ) from None
    complete_text = getattr(backend, "complete_text", None)
    if complete_text is None:
        raise EnhanceError(
            f"provider {provider!r} has no one-shot text completion, so it "
            f"cannot enhance a brief (supported providers: {_supported()})"
        )
    system = ENHANCE_PROMPT_PATH.read_text()
    user = f"Title: {title.strip()}\n\nOriginal brief:\n\n{text.strip()}"
    try:
        enhanced, usage = complete_text(
            system, user, model=bare, env=env, timeout=timeout
        )
    except TransportError as exc:
        raise EnhanceError(str(exc)) from exc
    enhanced = enhanced.strip()
    if not enhanced:
        raise EnhanceError(f"model {model} returned an empty rewrite")
    return EnhanceResult(text=enhanced, model=model, usage=usage or {})
