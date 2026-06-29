"""Deterministic SEARCH/REPLACE applier — the latency lever (spec §5.3).

The harness asks the model for edits as *aider-style* SEARCH/REPLACE blocks and
applies them with literal string matching — **no** apply-model round-trip and
**no** fuzzy matching (``difflib``, whitespace normalization, etc.). That makes
edits cheap and, more importantly, *deterministic and regression-testable*
(spec invariant 1 & 2).

A block looks like::

    <<<<<<< SEARCH
    old text
    =======
    new text
    >>>>>>> REPLACE

Matching rules (spec §5.3):

* The SEARCH text must occur in the file **exactly once** (literal
  :meth:`str.find`). Zero occurrences → ``zero_match``; more than one →
  ``multi_match``. We refuse to guess which one the model meant.
* An **empty** SEARCH means "create this content"; against already-non-empty
  content that is a ``create_conflict`` — the *caller* (``tools.py``) decides
  file creation, the applier never silently clobbers.
* Blocks apply **sequentially** against an in-memory working copy, so a later
  block may match text an earlier block introduced.
* Any failure aborts the whole batch and returns nothing — application is
  **atomic** (all blocks or none); the caller still holds the original.

This module is pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass


SEARCH_MARKER = "<<<<<<< SEARCH"
DIVIDER_MARKER = "======="
REPLACE_MARKER = ">>>>>>> REPLACE"


@dataclass(frozen=True)
class Block:
    """One parsed SEARCH/REPLACE edit.

    ``search`` empty means "create" (see module docstring). Both halves preserve
    the model's text verbatim, sans the single trailing newline that separates a
    half from its following marker line.
    """

    search: str
    replace: str


class ApplyError(Exception):
    """A parse or application failure.

    ``kind`` is a stable machine-readable tag — ``malformed``, ``zero_match``,
    ``multi_match``, ``create_conflict`` — so the tool layer and tests can branch
    on the failure mode rather than the message. ``block_index`` is the
    zero-based index of the offending block (``-1`` for whole-text parse errors).
    """

    def __init__(self, kind: str, block_index: int, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.block_index = block_index
        self.message = message


def parse_blocks(text: str) -> list[Block]:
    """Parse aider SEARCH/REPLACE fences out of ``text``.

    Raises :class:`ApplyError` (``kind="malformed"``) on any structural problem:
    a marker out of order, a missing divider/closer, or trailing junk before a
    closer. Text with no markers at all parses to an empty list (caller decides
    whether that is an error in its context).
    """
    lines = text.splitlines()
    blocks: list[Block] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() != SEARCH_MARKER:
            # Free text outside any block is ignored (the model often narrates
            # around its edits); only the fenced regions are authoritative.
            i += 1
            continue

        search_lines: list[str] = []
        i += 1
        while i < n and lines[i].strip() != DIVIDER_MARKER:
            if lines[i].strip() == SEARCH_MARKER or lines[i].strip() == REPLACE_MARKER:
                raise ApplyError(
                    "malformed",
                    len(blocks),
                    f"expected '{DIVIDER_MARKER}' before '{lines[i].strip()}'",
                )
            search_lines.append(lines[i])
            i += 1
        if i >= n:
            raise ApplyError(
                "malformed",
                len(blocks),
                f"unterminated SEARCH block: missing '{DIVIDER_MARKER}'",
            )

        replace_lines: list[str] = []
        i += 1  # consume the divider
        while i < n and lines[i].strip() != REPLACE_MARKER:
            if lines[i].strip() == SEARCH_MARKER or lines[i].strip() == DIVIDER_MARKER:
                raise ApplyError(
                    "malformed",
                    len(blocks),
                    f"expected '{REPLACE_MARKER}' before '{lines[i].strip()}'",
                )
            replace_lines.append(lines[i])
            i += 1
        if i >= n:
            raise ApplyError(
                "malformed",
                len(blocks),
                f"unterminated REPLACE block: missing '{REPLACE_MARKER}'",
            )
        i += 1  # consume the closer

        blocks.append(
            Block(
                search="\n".join(search_lines),
                replace="\n".join(replace_lines),
            )
        )
    return blocks


def apply_edits(original: str, blocks: list[Block]) -> str:
    """Apply ``blocks`` to ``original`` and return the new text.

    Pure and atomic: each block is applied to an in-memory working copy in order;
    any failure raises :class:`ApplyError` and nothing is returned, so the caller
    keeps ``original`` intact. See module docstring for the matching rules.
    """
    working = original
    for index, block in enumerate(blocks):
        if block.search == "":
            # Empty SEARCH = create/append. Only legal against empty content;
            # clobbering real content is the caller's decision, not ours.
            if working != "":
                raise ApplyError(
                    "create_conflict",
                    index,
                    "empty SEARCH against non-empty content",
                )
            working = block.replace
            continue

        first = working.find(block.search)
        if first == -1:
            raise ApplyError(
                "zero_match",
                index,
                "SEARCH text not found",
            )
        second = working.find(block.search, first + 1)
        if second != -1:
            raise ApplyError(
                "multi_match",
                index,
                "SEARCH text matched more than once",
            )
        working = working[:first] + block.replace + working[first + len(block.search):]
    return working
