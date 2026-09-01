"""Brief enhancement — the manager-side enhance-on-create pass.

One synchronous, tool-less completion that rewrites an operator's raw brief
into a self-contained spec a worker can implement without conversation context.
The system prompt is the shipped asset ``assets/prompts/enhance-brief.md``
(distilled from the original embedded ``task`` skill's "writing the brief"
rules).

``enhance_brief_model`` is a provider-qualified id like every other model
setting, so the pass dispatches on its provider:

* the API vendors (``anthropic`` / ``ollama-cloud`` / ``ollama``) go through
  :func:`nightshift.agent.transport.complete` — the same non-streaming adapter
  the harness loop uses;
* ``claude-code`` goes through the Claude Code CLI in print mode
  (``claude -p --output-format json``), which is how an operator with no
  ``ANTHROPIC_API_KEY`` (CLI subscription auth) gets an enhancement at all.
  The call is deliberately *not* the agentic worker invocation: no tools, no
  worktree, a scratch cwd — this rewrites prose, it never touches a repo.

The other agentic CLIs (cursor / antigravity) have no comparable one-shot text
mode wired up, so they fail with a message naming what is supported rather than
a vendor-level "unsupported" from deeper in the stack.

Callers run this off the event loop (``asyncio.to_thread``) — both paths block.
Failures surface as :class:`EnhanceError` so the API layer can refuse task
creation (the operator's draft survives client-side) instead of silently
queueing the raw text as if it had been enhanced.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

from nightshift._paths import PROMPTS_DIR
from nightshift.agent.transport import SUPPORTED_VENDORS, TransportError, complete
from nightshift.model_id import split_model
from nightshift.prompts import resolve_claude_bin


ENHANCE_PROMPT_PATH = PROMPTS_DIR / "enhance-brief.md"

# One-shot rewrite of prose: generous but bounded — a hung vendor call must
# not pin the create request forever.
ENHANCE_TIMEOUT_SECONDS = 120.0

# Providers this pass can drive: the transport's API vendors plus the Claude
# Code CLI's print mode.
CLI_PROVIDER = "claude-code"
SUPPORTED_PROVIDERS = (*SUPPORTED_VENDORS, CLI_PROVIDER)


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

    ``model`` is a provider-qualified id (e.g. ``anthropic/claude-sonnet-4-6``
    or ``claude-code/claude-sonnet-4-6``); ``env`` supplies the vendor API keys
    and the child process environment. Raises :class:`EnhanceError` on any
    transport/CLI failure, an unsupported provider, or an empty rewrite.
    """
    system = ENHANCE_PROMPT_PATH.read_text()
    user = f"Title: {title.strip()}\n\nOriginal brief:\n\n{text.strip()}"
    provider, bare = split_model(model)
    if provider in SUPPORTED_VENDORS:
        enhanced, usage = _complete_via_transport(
            system, user, model=model, env=env, timeout=timeout
        )
    elif provider == CLI_PROVIDER:
        enhanced, usage = _complete_via_claude_cli(
            system, user, model=bare, env=env, timeout=timeout
        )
    else:
        raise EnhanceError(
            f"model {model!r} names no provider this pass can drive "
            f"(expected one of {SUPPORTED_PROVIDERS} as the provider/ prefix)"
        )
    enhanced = enhanced.strip()
    if not enhanced:
        raise EnhanceError(f"model {model} returned an empty rewrite")
    return EnhanceResult(text=enhanced, model=model, usage=usage)


def _complete_via_transport(
    system: str,
    user: str,
    *,
    model: str,
    env: dict[str, str],
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    """One tool-less API completion through the harness transport."""
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
    return completion.text or "", completion.usage or {}


def build_enhance_claude_argv(system: str, user: str, model: str) -> list[str]:
    """The ``claude`` print-mode argv for a tool-less one-shot rewrite.

    Deliberately unlike :func:`nightshift.prompts.build_claude_argv` (the
    agentic worker invocation): ``--tools ""`` disables every built-in tool and
    there is no ``--dangerously-skip-permissions``, so the pass can only emit
    text. ``--output-format json`` returns a single object carrying the reply
    (``result``) and Anthropic-shaped ``usage`` for the telemetry row.
    """
    return [
        resolve_claude_bin(),
        "-p", user,
        "--model", model,
        "--system-prompt", system,
        "--tools", "",
        "--output-format", "json",
    ]


def _complete_via_claude_cli(
    system: str,
    user: str,
    *,
    model: str,
    env: dict[str, str],
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    """One tool-less rewrite through the Claude Code CLI's print mode.

    Runs in a throwaway cwd so the CLI picks up no project ``CLAUDE.md`` from
    whatever directory the manager happens to be serving — the rewrite sees the
    brief and nothing else.
    """
    argv = build_enhance_claude_argv(system, user, model)
    try:
        with tempfile.TemporaryDirectory(prefix="nightshift-enhance-") as cwd:
            proc = subprocess.run(
                argv, cwd=cwd, env=env or None, capture_output=True,
                text=True, timeout=timeout,
            )
    except FileNotFoundError as exc:
        raise EnhanceError(f"claude CLI not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise EnhanceError(f"claude CLI timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise EnhanceError(f"claude CLI failed to launch: {exc}") from exc
    if proc.returncode != 0:
        raise EnhanceError(
            f"claude CLI exited {proc.returncode}: {_tail(proc.stderr or proc.stdout)}"
        )
    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        raise EnhanceError(
            f"claude CLI returned unparseable output: {_tail(proc.stdout)}"
        ) from None
    if not isinstance(payload, dict):
        raise EnhanceError("claude CLI returned an unexpected JSON shape")
    if payload.get("is_error"):
        raise EnhanceError(f"claude CLI reported an error: {_tail(payload.get('result'))}")
    usage = payload.get("usage")
    return str(payload.get("result") or ""), usage if isinstance(usage, dict) else {}


def _tail(output: Any, limit: int = 300) -> str:
    """The last, most diagnostic slice of a CLI output blob (bounded)."""
    text = str(output or "").strip()
    if not text:
        return "(no output)"
    return text[-limit:]
