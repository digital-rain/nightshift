"""Tests for the owned price table (:mod:`nightshift.price`).

The tuning KPI (cost per landed change) depends on ``cost_of`` turning an
Anthropic-shaped usage blob into dollars, with cache tokens billed at their
distinct multipliers and an honest ``None`` for unpriced models.
"""

from __future__ import annotations

from nightshift import price


def test_normalize_strips_provider_prefixes_and_date_suffix() -> None:
    assert price.normalize_model("claude-opus-4-8") == "claude-opus-4-8"
    assert price.normalize_model("anthropic/claude-sonnet-4-6") == "claude-sonnet-4-6"
    # nightshift harness ids carry a second vendor segment.
    assert price.normalize_model("nightshift/anthropic/claude-opus-4-8") == "claude-opus-4-8"
    # A trailing 8-digit date stamp is dropped; other numeric tails are kept.
    assert price.normalize_model("claude-opus-4-8-20260514") == "claude-opus-4-8"
    assert price.normalize_model("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_normalize_strips_effort_and_cloud_tags() -> None:
    # Antigravity effort markers.
    assert price.normalize_model("antigravity/gemini-3.1-pro-high") == "gemini-3.1-pro"
    assert price.normalize_model("gemini-3.5-flash-medium") == "gemini-3.5-flash"
    assert price.normalize_model("gemini-3.5-flash-lite") == "gemini-3.5-flash-lite"
    # Cursor effort + Fast: strip effort, keep -fast as a distinct SKU.
    assert price.normalize_model("cursor/cursor-grok-4.5-high") == "cursor-grok-4.5"
    assert (
        price.normalize_model("cursor/cursor-grok-4.5-high-fast")
        == "cursor-grok-4.5-fast"
    )
    # Ollama Cloud tag variants.
    assert price.normalize_model("ollama-cloud/kimi-k2.6:cloud") == "kimi-k2.6"
    assert price.normalize_model("ollama-cloud/gpt-oss:120b") == "gpt-oss:120b"


def test_cost_of_uncached_input_and_output() -> None:
    # sonnet-4-6: $3/M in, $15/M out. 1M in + 1M out = 3 + 15 = 18.
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert price.cost_of("claude-sonnet-4-6", usage) == 18.0


def test_cost_of_applies_cache_multipliers() -> None:
    # Current opus-4-8 is $5/$25: cache read 0.1x, cache write 1.25x of input.
    #   uncached 1M * 5            = 5
    #   cache_read 1M * 5 * 0.1    = 0.5
    #   cache_creation 1M * 5*1.25 = 6.25
    #   output 0                   = 0
    usage = {
        "input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "output_tokens": 0,
    }
    got = price.cost_of("claude-opus-4-8", usage)
    assert got is not None
    assert abs(got - (5.0 + 0.5 + 6.25)) < 1e-9


def test_cost_of_cursor_and_antigravity_and_ollama_cloud() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    # Cursor first-party.
    assert price.cost_of("cursor/composer-2.5", usage) == 0.5 + 2.5
    assert price.cost_of("cursor/cursor-grok-4.5-high", usage) == 2.0 + 6.0
    assert price.cost_of("cursor/cursor-grok-4.5-high-fast", usage) == 4.0 + 18.0
    assert price.cost_of("cursor/gpt-5.6-luna", usage) == 1.0 + 6.0
    # Antigravity Gemini.
    assert price.cost_of("antigravity/gemini-3.1-pro-high", usage) == 2.0 + 12.0
    assert price.cost_of("antigravity/gemini-3.5-flash-medium", usage) == 1.5 + 9.0
    # Ollama Cloud vendor proxies.
    assert abs(price.cost_of("ollama-cloud/kimi-k2.6", usage) - (0.95 + 4.0)) < 1e-9
    assert abs(price.cost_of("ollama-cloud/deepseek-v4-flash", usage) - 0.42) < 1e-9
    assert abs(price.cost_of("ollama-cloud/gpt-oss:20b", usage) - (0.03 + 0.14)) < 1e-9


def test_cost_of_unknown_model_is_none_not_zero() -> None:
    usage = {"input_tokens": 500, "output_tokens": 500}
    assert price.cost_of("totally-unknown-model", usage) is None
    assert price.cost_of("ollama/llama3", usage) is None  # local, unpriced
    assert price.cost_of(None, usage) is None


def test_cost_of_missing_usage_is_none() -> None:
    assert price.cost_of("claude-sonnet-4-6", None) is None
    assert price.cost_of("claude-sonnet-4-6", {}) == 0.0  # priced model, no tokens


def test_has_rate() -> None:
    assert price.has_rate("anthropic/claude-opus-4-8")
    assert price.has_rate("cursor/cursor-grok-4.5-high")
    assert price.has_rate("antigravity/gemini-3.6-flash-low")
    assert price.has_rate("ollama-cloud/kimi-k2.7-code:cloud")
    assert not price.has_rate("some-unknown-model")
