"""Owned, version-controlled model price table → per-run ``cost_usd``.

The tuning KPI is **cost per landed change**, which is uncomputable when a
backend leaves ``cost_usd`` unset (the in-house harness and the single-shot
Anthropic backend both did). This module turns an Anthropic-shaped ``usage``
blob into a dollar cost from a static rate sheet, so every run that reports
token usage can report a cost.

Honesty over coverage: an unknown model returns ``None`` (not a guessed
zero), so a missing price is visibly missing rather than silently understated.
Ollama/Antigravity stay ``None`` because there is no meaningful public per-token
price to apply.

Rates are **USD per million tokens**. Cache economics follow Anthropic's
published multipliers: a cache *write* (creation) bills at 1.25x the base
input rate, a cache *read* at 0.1x. Because ``usage`` folds cache tokens into
neither the uncached-input figure nor each other, ``cost_of`` reconstructs the
three input components explicitly (see :func:`_input_components`).

The table is deliberately small and keyed on a normalized model name (vendor
prefixes stripped, date suffixes tolerated). It is a rate *sheet*, not a
catalog: add a row when a model starts being used. Update it as vendor pricing
changes — that is the point of owning it in version control.
"""

from __future__ import annotations

from typing import Any

from nightshift.model_id import split_model


# USD per million tokens: (input, output). Cache multipliers are applied to the
# input rate (see module docstring). Keyed by normalized model name.
_RATES: dict[str, tuple[float, float]] = {
    # Anthropic Claude — public API list prices.
    "claude-opus-4-8": (15.0, 75.0),
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
}

_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.1
_PER_MILLION = 1_000_000.0


def normalize_model(model: str | None) -> str:
    """Reduce a (possibly provider-qualified) model id to a rate-table key.

    Strips a leading ``provider/`` (``nightshift`` runs carry a second vendor
    segment — ``nightshift/anthropic/claude-…`` — so we peel until the tail has
    no more slashes), lowercases, and drops a trailing ``-YYYYMMDD`` date stamp
    that some vendor ids append (``claude-opus-4-8-20260514`` → ``claude-opus-4-8``).
    """
    text = (model or "").strip()
    while "/" in text:
        _, text = split_model(text)
    text = text.lower()
    parts = text.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        text = parts[0]
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
