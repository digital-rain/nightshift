"""Phase 1 tests — the deterministic SEARCH/REPLACE applier (spec §5.3, §10).

Pure and network-free. These pin the invariants the latency lever rests on:
exactly-once literal matching, atomic multi-block application, and the
create-vs-clobber distinction.
"""

from __future__ import annotations

import pytest

from nightshift.agent.apply import ApplyError, Block, apply_edits, parse_blocks


def _block(search: str, replace: str) -> Block:
    return Block(search=search, replace=replace)


def test_unique_match_success() -> None:
    out = apply_edits("alpha\nbeta\ngamma\n", [_block("beta", "BETA")])
    assert out == "alpha\nBETA\ngamma\n"


def test_zero_match_raises_and_keeps_original() -> None:
    original = "alpha\nbeta\n"
    with pytest.raises(ApplyError) as exc:
        apply_edits(original, [_block("delta", "DELTA")])
    assert exc.value.kind == "zero_match"
    assert exc.value.block_index == 0


def test_multi_match_refuses_to_guess() -> None:
    with pytest.raises(ApplyError) as exc:
        apply_edits("x\nx\n", [_block("x", "y")])
    assert exc.value.kind == "multi_match"


def test_empty_search_creates_into_empty_content() -> None:
    out = apply_edits("", [_block("", "fresh file body\n")])
    assert out == "fresh file body\n"


def test_empty_search_against_nonempty_is_create_conflict() -> None:
    with pytest.raises(ApplyError) as exc:
        apply_edits("existing\n", [_block("", "new")])
    assert exc.value.kind == "create_conflict"
    assert exc.value.block_index == 0


def test_multi_block_atomicity_second_block_fails() -> None:
    # Block 1 would succeed, block 2 has no match: the whole batch must abort and
    # the caller keeps the original (apply_edits returns nothing).
    original = "one\ntwo\n"
    blocks = [_block("one", "ONE"), _block("missing", "X")]
    with pytest.raises(ApplyError) as exc:
        apply_edits(original, blocks)
    assert exc.value.kind == "zero_match"
    assert exc.value.block_index == 1


def test_sequential_apply_block_two_matches_block_one_output() -> None:
    # Block 1 introduces "INSERTED"; block 2 must be able to match it.
    original = "header\nbody\n"
    blocks = [
        _block("body", "INSERTED"),
        _block("INSERTED", "FINAL"),
    ]
    assert apply_edits(original, blocks) == "header\nFINAL\n"


def test_parse_roundtrip_well_formed() -> None:
    text = (
        "I'll change beta.\n"
        "<<<<<<< SEARCH\n"
        "beta\n"
        "=======\n"
        "BETA\n"
        ">>>>>>> REPLACE\n"
    )
    blocks = parse_blocks(text)
    assert blocks == [Block(search="beta", replace="BETA")]


def test_parse_no_markers_is_empty() -> None:
    assert parse_blocks("just some prose, no edits here") == []


def test_parse_malformed_missing_divider() -> None:
    text = "<<<<<<< SEARCH\nbeta\n>>>>>>> REPLACE\n"
    with pytest.raises(ApplyError) as exc:
        parse_blocks(text)
    assert exc.value.kind == "malformed"


def test_parse_malformed_unterminated_replace() -> None:
    text = "<<<<<<< SEARCH\nbeta\n=======\nBETA\n"
    with pytest.raises(ApplyError) as exc:
        parse_blocks(text)
    assert exc.value.kind == "malformed"


def test_parse_then_apply_end_to_end() -> None:
    text = (
        "<<<<<<< SEARCH\n"
        "old line\n"
        "=======\n"
        "new line\n"
        ">>>>>>> REPLACE\n"
    )
    out = apply_edits("prefix\nold line\nsuffix\n", parse_blocks(text))
    assert out == "prefix\nnew line\nsuffix\n"
