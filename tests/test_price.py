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


def test_cost_of_uncached_input_and_output() -> None:
    # sonnet-4-6: $3/M in, $15/M out. 1M in + 1M out = 3 + 15 = 18.
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert price.cost_of("claude-sonnet-4-6", usage) == 18.0


def test_cost_of_applies_cache_multipliers() -> None:
    # opus (15/75): cache read at 0.1x, cache write at 1.25x of the input rate.
    #   uncached 1M * 15           = 15
    #   cache_read 1M * 15 * 0.1   = 1.5
    #   cache_creation 1M * 15*1.25= 18.75
    #   output 0                   = 0
    usage = {
        "input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "output_tokens": 0,
    }
    got = price.cost_of("claude-opus-4-8", usage)
    assert got is not None
    assert abs(got - (15.0 + 1.5 + 18.75)) < 1e-9


def test_cost_of_unknown_model_is_none_not_zero() -> None:
    usage = {"input_tokens": 500, "output_tokens": 500}
    assert price.cost_of("gpt-5", usage) is None
    assert price.cost_of("ollama/llama3", usage) is None
    assert price.cost_of(None, usage) is None


def test_cost_of_missing_usage_is_none() -> None:
    assert price.cost_of("claude-sonnet-4-6", None) is None
    assert price.cost_of("claude-sonnet-4-6", {}) == 0.0  # priced model, no tokens


def test_has_rate() -> None:
    assert price.has_rate("anthropic/claude-opus-4-8")
    assert not price.has_rate("some-unknown-model")
