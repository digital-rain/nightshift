from __future__ import annotations

import pytest

from nightshift.model_id import is_qualified, join_model, provider_of, split_model


def test_split_qualified_basic() -> None:
    assert split_model("claude-code/claude-sonnet-4-6") == ("claude-code", "claude-sonnet-4-6")


def test_split_keeps_colons_and_extra_slashes_in_model() -> None:
    assert split_model("ollama-cloud/gpt-oss:120b") == ("ollama-cloud", "gpt-oss:120b")
    assert split_model("ollama/hf.co/user/repo") == ("ollama", "hf.co/user/repo")


def test_split_agnostic_has_no_provider() -> None:
    for kw in ("auto", "max", "default", "", "  ", None):
        assert split_model(kw) == (None, (kw or "").strip())


def test_is_qualified() -> None:
    assert is_qualified("cursor/gpt-5") is True
    assert is_qualified("auto") is False
    assert is_qualified("claude-opus-4-8") is False  # bare, no provider


def test_provider_of() -> None:
    assert provider_of("anthropic/claude-opus-4-8") == "anthropic"
    assert provider_of("auto") is None


def test_join_model() -> None:
    assert join_model("ollama", "llama3.1") == "ollama/llama3.1"


def test_join_rejects_empty() -> None:
    with pytest.raises(ValueError):
        join_model("", "llama3.1")
