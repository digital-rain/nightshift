"""Which credential a ``claude`` CLI run spends — subscription or API key.

Nightshift loads ``<workspace>/.env`` into every entrypoint's environment, and
the ``claude-code`` backend hands that environment to the CLI unchanged, so an
``ANTHROPIC_API_KEY`` meant for the ``anthropic/`` API backend silently billed
every agentic run to the API instead of the operator's claude.ai subscription.

The fix is not to rely on the CLI's own credential precedence — it is
undocumented, version-dependent, and invisible in a run record. Instead the
declared ``claude_billing`` setting picks the credential and this module
*removes* the other one from the environment the CLI is spawned with, so the
choice is structural and the run record can state it:

``subscription``
    Scrub :data:`CLI_AUTH_ENV_KEYS` unconditionally; the CLI uses its own
    ``claude login``. If it is not logged in the run fails honestly.
``api``
    Pass the environment through untouched; the key is billed. A missing key
    is a configuration error, not a silent fallback.
``auto`` (the default)
    Probe ``claude auth status`` against an already-scrubbed environment: a
    claude.ai login means subscription, anything else falls back to the API key
    with one loud log line, and no key at all is a configuration error.

Deliberately dependency-free: nothing here imports from ``nightshift``, so both
config models and the backend can rely on it without a layering cycle.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


CLAUDE_BILLING_MODES: tuple[str, ...] = ("auto", "subscription", "api")
DEFAULT_CLAUDE_BILLING = "auto"
BILLING_API = "api"
BILLING_SUBSCRIPTION = "subscription"

#: Every variable that can point the CLI at an API credential or an alternate
#: provider. Removing all five is what makes "subscription" mean subscription.
CLI_AUTH_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


class BillingConfigError(ValueError):
    """An invalid or unsatisfiable ``claude_billing`` declaration.

    The message always names ``claude_billing`` so an operator reading a run
    log knows which setting to change.
    """


def scrub_cli_auth(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``env`` with exactly :data:`CLI_AUTH_ENV_KEYS` removed."""
    return {k: v for k, v in env.items() if k not in CLI_AUTH_ENV_KEYS}


def _normalize_mode(value: Any) -> str:
    """Normalise a declared mode; missing/empty is the default, junk raises."""
    if value is None:
        return DEFAULT_CLAUDE_BILLING
    text = str(value).strip().lower()
    if not text:
        return DEFAULT_CLAUDE_BILLING
    if text not in CLAUDE_BILLING_MODES:
        raise BillingConfigError(
            f"claude_billing={value!r} is not a valid mode "
            f"(expected one of {', '.join(CLAUDE_BILLING_MODES)})"
        )
    return text


def billing_setting(config: Mapping[str, Any] | None) -> str:
    """The ``claude_billing`` mode declared by *config*.

    A missing key, ``None``, or an empty string resolves to
    :data:`DEFAULT_CLAUDE_BILLING`; anything else that is not a declared mode
    raises :class:`BillingConfigError`. Values are trimmed and lower-cased.
    """
    if config is None:
        return DEFAULT_CLAUDE_BILLING
    return _normalize_mode(config.get("claude_billing"))


# Per-process probe cache keyed on the binary path: `claude auth status` is a
# process spawn, and the auth state cannot change under a running worker
# without operator action. A failed probe is never cached, so a transient
# failure (a busy box, a half-installed CLI) retries on the next run.
_AUTH_STATUS_CACHE: dict[str, dict[str, Any]] = {}


def clear_auth_status_cache() -> None:
    """Forget every cached ``claude auth status`` answer."""
    _AUTH_STATUS_CACHE.clear()


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Terminate a probe and its whole group, escalating SIGTERM to SIGKILL.

    Mirrors ``preflight.kill_process_group``; kept local so this module stays
    free of ``nightshift`` imports.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def claude_auth_status(
    claude_bin: str,
    env: Mapping[str, str],
    *,
    timeout: float = 15.0,
    use_cache: bool = True,
) -> dict[str, Any] | None:
    """Run ``claude auth status`` and return its parsed JSON, or ``None``.

    Callers pass an already-scrubbed environment — the probe must see the same
    credentials the run will, or it answers a question nobody asked. ``None``
    means "cannot say a claude.ai login exists": the binary is missing, the
    call could not be spawned or timed out, or the output was not a JSON
    object. The probe is spawned in its own session so a hang can be killed as
    a group.
    """
    if use_cache:
        cached = _AUTH_STATUS_CACHE.get(claude_bin)
        if cached is not None:
            return cached

    try:
        proc = subprocess.Popen(
            [claude_bin, "auth", "status"],
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError, ValueError):
        return None

    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return None

    # The exit code is not the signal: the real CLI (v2.1.183) prints the
    # logged-out answer ``{"loggedIn": false, ...}`` and exits 1. A JSON object
    # on stdout is an answer, whatever the code, and every answer is cached;
    # only a spawn failure, a timeout, or non-JSON output is "cannot say".
    try:
        status = json.loads(out or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(status, dict):
        return None

    _AUTH_STATUS_CACHE[claude_bin] = status
    return status


@dataclass(frozen=True)
class BillingDecision:
    """Which credential a run spends, why, and the env that enforces it."""

    mode: str
    reason: str
    env: dict[str, str]


_AUTO_FALLBACK_LOG = (
    "[claude-code] billing: API key (claude_billing=auto, claude.ai login not "
    "detected) — set claude_billing=subscription to force the subscription, or "
    "run `claude login`"
)


_AUTO_PROBE_FAILED_LOG = (
    "[claude-code] billing: API key (claude_billing=auto, `claude auth status` "
    "could not be read — missing binary, timeout, or unparseable output) — set "
    "claude_billing=subscription to force the subscription"
)


def has_api_key(env: Mapping[str, str]) -> bool:
    """True when ``env`` carries a non-empty ``ANTHROPIC_API_KEY``."""
    return bool((env.get("ANTHROPIC_API_KEY") or "").strip())


def is_claude_login(status: Mapping[str, Any] | None) -> bool:
    """True when a ``claude auth status`` answer reports a claude.ai login."""
    return (
        bool(status)
        and status.get("loggedIn") is True
        and status.get("authMethod") == "claude.ai"
    )


def describe_claude_billing(
    setting: str, *, claude_bin: str, env: Mapping[str, str] | None = None
) -> str:
    """One line for a startup banner: which account this process's claude-code
    spawns will bill, or the configuration error that stops them. Resolves
    (and caches) the same decision the backend makes per spawn, so a
    misdeclaration is visible before the first run rather than inside it."""
    try:
        decision = decide_claude_billing(
            setting, env=os.environ if env is None else env, claude_bin=claude_bin,
        )
    except BillingConfigError as exc:
        return f"ERROR — {exc}"
    return f"{decision.mode} ({decision.reason})"


def decide_claude_billing(
    setting: str,
    *,
    env: Mapping[str, str],
    claude_bin: str,
    log: Callable[[str], None] | None = None,
) -> BillingDecision:
    """Resolve *setting* against the environment into a :class:`BillingDecision`.

    ``subscription`` scrubs without probing; ``api`` keeps the key and errors
    when there is none; ``auto`` probes ``claude auth status`` with a scrubbed
    environment and falls back to the key — loudly, through ``log`` — when no
    claude.ai login is visible. An unsatisfiable declaration raises
    :class:`BillingConfigError` rather than quietly spending the other budget.
    """
    mode = _normalize_mode(setting)

    if mode == BILLING_SUBSCRIPTION:
        return BillingDecision(
            mode=BILLING_SUBSCRIPTION,
            reason="claude_billing=subscription",
            env=scrub_cli_auth(env),
        )

    if mode == BILLING_API:
        if not has_api_key(env):
            raise BillingConfigError(
                "claude_billing=api but ANTHROPIC_API_KEY is not set "
                "(add it to .env or set claude_billing to subscription/auto)"
            )
        return BillingDecision(
            mode=BILLING_API, reason="claude_billing=api", env=dict(env)
        )

    scrubbed = scrub_cli_auth(env)
    status = claude_auth_status(claude_bin, scrubbed)
    if is_claude_login(status):
        plan = status.get("subscriptionType")
        reason = f"claude.ai login ({plan})" if plan else "claude.ai login"
        return BillingDecision(
            mode=BILLING_SUBSCRIPTION, reason=reason, env=scrubbed
        )

    if has_api_key(env):
        # A probe that could not be read is reported as such, so a logged-in
        # box that hit a timeout is not told to log in again.
        probe_failed = status is None
        if log is not None:
            log(_AUTO_PROBE_FAILED_LOG if probe_failed else _AUTO_FALLBACK_LOG)
        return BillingDecision(
            mode=BILLING_API,
            reason=(
                "auto: auth probe unreadable, ANTHROPIC_API_KEY present"
                if probe_failed else
                "auto: not logged in, ANTHROPIC_API_KEY present"
            ),
            env=dict(env),
        )

    raise BillingConfigError(
        "claude_billing=auto found neither a claude.ai login nor an "
        "ANTHROPIC_API_KEY (run `claude login` for the subscription, or add "
        "ANTHROPIC_API_KEY to .env and set claude_billing=api)"
    )
