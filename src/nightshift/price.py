"""Owned, version-controlled model price table → per-run ``cost_usd``.

The tuning KPI is **cost per landed change**, which is uncomputable when a
backend leaves ``cost_usd`` unset (the in-house harness and the single-shot
Anthropic backend both did). This module turns an Anthropic-shaped ``usage``
blob into a dollar cost from a static rate sheet, so every run that reports
token usage can report a cost.

Honesty over coverage: an unknown model returns ``None`` (not a guessed
zero), so a missing price is visibly missing rather than silently understated.
Local Ollama (self-hosted weights) stays ``None`` — there is no public
per-token bill. Ollama Cloud is subscription/GPU-time billed; entries there
are **vendor API list-price proxies** so analytics can still compare models.

Rates are **USD per million tokens**. Cache economics follow Anthropic's
published multipliers: a cache *write* (creation) bills at 1.25x the base
input rate, a cache *read* at 0.1x. Because ``usage`` folds cache tokens into
neither the uncached-input figure nor each other, ``cost_of`` reconstructs the
three input components explicitly (see :func:`_input_components`).

The table is deliberately keyed on a normalized model name (vendor prefixes
stripped, date / effort / ``:cloud`` suffixes tolerated). It is a rate
*sheet*, not a catalog: add a row when a model starts being used. Update it
as vendor pricing changes — that is the point of owning it in version control.
"""

from __future__ import annotations

from typing import Any

from nightshift.model_id import split_model


# USD per million tokens: (input, output). Cache multipliers are applied to the
# input rate (see module docstring). Keyed by normalized model name.
#
# Sources (spot-checked 2026-07-24):
#   Anthropic public API list / Cursor models-and-pricing mirror
#   Cursor first-party (Composer 2.5, Grok 4.5) + Auto Cost
#   Google Gemini Developer API (Antigravity consumes these)
#   Ollama Cloud: vendor API proxies (Moonshot / DeepSeek / OpenRouter gpt-oss)
_RATES: dict[str, tuple[float, float]] = {
    # ── Anthropic Claude (public API list) ──────────────────────────────
    # Current Opus generation is $5/$25 (since Opus 4.5). Legacy Opus 4 / 4.1
    # remain at the original $15/$75.
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-8-fast": (10.0, 50.0),  # Fast mode = 2× standard
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-7-fast": (30.0, 150.0),  # research-preview fast (3× of old)
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-5": (2.0, 10.0),  # intro through 2026-08-31; then 3/15
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-fable-5": (10.0, 50.0),

    # ── Cursor first-party ──────────────────────────────────────────────
    # Composer 2.5 Standard vs Fast (Fast is the interactive default).
    "composer-2.5": (0.5, 2.5),
    "composer-2.5-fast": (3.0, 15.0),
    "cursor-composer-2.5": (0.5, 2.5),
    "cursor-composer-2.5-fast": (3.0, 15.0),
    "composer-1": (1.25, 10.0),
    # Grok 4.5 (joint Cursor + SpaceXAI); Fast is 2×.
    "cursor-grok-4.5": (2.0, 6.0),
    "cursor-grok-4.5-fast": (4.0, 18.0),
    "grok-4.5": (2.0, 6.0),
    "grok-4.5-fast": (4.0, 18.0),
    # Auto Cost flat rate (regardless of routed model).
    "auto-cost": (1.25, 6.0),

    # ── OpenAI via Cursor (Other Models pool @ API list) ────────────────
    "gpt-5.6-luna": (1.0, 6.0),
    "gpt-5.6-luna-fast": (2.0, 12.0),
    "gpt-5.6-terra": (2.5, 15.0),
    "gpt-5.6-sol": (5.0, 30.0),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.4-nano": (0.2, 1.25),
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-codex": (1.25, 10.0),
    "gpt-5.3-codex": (1.75, 14.0),

    # ── Google Gemini (Antigravity / Cursor) ───────────────────────────
    # Antigravity model ids append effort (-high/-medium/-low); normalize
    # strips those before lookup. Rates are ≤200k-prompt tier where tiered.
    "gemini-3.6-flash": (1.5, 7.5),
    "gemini-3.5-flash": (1.5, 9.0),
    "gemini-3.5-flash-lite": (0.3, 2.5),
    "gemini-3.1-pro": (2.0, 12.0),
    "gemini-3-flash": (0.5, 3.0),
    "gemini-3-pro": (2.0, 12.0),
    "gemini-2.5-flash": (0.3, 2.5),

    # ── Ollama Cloud — vendor API list-price proxies ────────────────────
    # Ollama Cloud itself bills by GPU-time subscription, not tokens. These
    # rates approximate the underlying vendor's public API so cost analytics
    # can still rank models. Prefer the Moonshot / DeepSeek / OpenRouter
    # figures over inventing Ollama-native token rates.
    "kimi-k2.7-code": (0.95, 4.0),  # Moonshot
    "kimi-k2.6": (0.95, 4.0),
    "kimi-k2.5": (0.60, 3.0),
    "deepseek-v4-pro": (0.435, 0.87),
    "deepseek-v4-flash": (0.14, 0.28),  # cheap workhorse
    "gpt-oss:120b": (0.04, 0.18),  # OpenRouter-class hosted proxy
    "gpt-oss:20b": (0.03, 0.14),  # cheaper / lighter
    "gpt-oss": (0.04, 0.18),  # bare tag → 120b-class default
}

_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.1
_PER_MILLION = 1_000_000.0

# Trailing effort markers used by Antigravity / Cursor model ids. Stripped so
# ``gemini-3.1-pro-high`` and ``cursor-grok-4.5-high`` hit the base rate row.
# ``-fast`` is *kept* — Fast variants are distinct priced SKUs.
_EFFORT_SUFFIXES = ("-high", "-medium", "-low")


def normalize_model(model: str | None) -> str:
    """Reduce a (possibly provider-qualified) model id to a rate-table key.

    Strips a leading ``provider/`` (``nightshift`` runs carry a second vendor
    segment — ``nightshift/anthropic/claude-…`` — so we peel until the tail has
    no more slashes), lowercases, drops an Ollama ``:cloud``/``-cloud`` tag, a
    trailing ``-YYYYMMDD`` date stamp, and a trailing effort marker
    (``-high``/``-medium``/``-low``). Fast variants keep their ``-fast`` tail
    so they resolve to a separate rate row.
    """
    text = (model or "").strip()
    while "/" in text:
        _, text = split_model(text)
    text = text.lower()
    if text.endswith(":cloud"):
        text = text[: -len(":cloud")]
    elif text.endswith("-cloud"):
        text = text[: -len("-cloud")]
    parts = text.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        text = parts[0]
    # ``…-high-fast`` → strip effort, leave ``…-fast``. Bare ``…-high`` → base.
    for effort in _EFFORT_SUFFIXES:
        fast_tail = f"{effort}-fast"
        if text.endswith(fast_tail):
            text = text[: -len(fast_tail)] + "-fast"
            break
        if text.endswith(effort):
            text = text[: -len(effort)]
            break
    return text


def _input_components(usage: dict[str, Any]) -> tuple[int, int, int]:
    """Return ``(uncached_input, cache_read, cache_creation)`` token counts.

    Anthropic's ``input_tokens`` is the *uncached* input (cache reads/writes are
    reported separately), so the three are additive and can be priced at their
    distinct rates. Missing fields read as 0.
    """
    uncached = int(usage.get("input_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    return uncached, cache_read, cache_creation


def cost_of(model: str | None, usage: dict[str, Any] | None) -> float | None:
    """Compute ``cost_usd`` for one run from ``model`` + an Anthropic-shaped
    ``usage`` blob. ``None`` when the model isn't in the table or ``usage`` is
    absent — an honest "unknown", never a guessed zero."""
    if not isinstance(usage, dict):
        return None
    rate = _RATES.get(normalize_model(model))
    if rate is None:
        return None
    in_rate, out_rate = rate
    uncached, cache_read, cache_creation = _input_components(usage)
    output = int(usage.get("output_tokens", 0) or 0)
    input_cost = (
        uncached * in_rate
        + cache_read * in_rate * _CACHE_READ_MULT
        + cache_creation * in_rate * _CACHE_WRITE_MULT
    )
    output_cost = output * out_rate
    return (input_cost + output_cost) / _PER_MILLION


def has_rate(model: str | None) -> bool:
    """True when the table can price ``model`` (for tests / diagnostics)."""
    return normalize_model(model) in _RATES
