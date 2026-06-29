"""Phase 5 tests — the agentic tool loop + cache strategy (spec §5, §1.3, §10).

Network-free: a fake ``transport_complete`` drives the loop against a real
temp-dir tool registry. The cache test asserts the §1.3 lever — a byte-stable
system prefix produces a real cache *read* on turn 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nightshift.agent.loop import LoopResult, load_charter, run_loop
from nightshift.agent.tools import build_registry
from nightshift.agent.transport import Completion, ToolCall, TransportError


def _registry(tmp_path: Path):
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    return build_registry(tmp_path)


def test_loop_terminates_on_no_tool_calls(tmp_path: Path) -> None:
    def fake(messages, tools, knobs, **kw):
        return Completion("all done", [], {"input_tokens": 4, "output_tokens": 2}, "end_turn")

    res = run_loop(
        transport_complete=fake,
        registry=_registry(tmp_path),
        charter="CHARTER",
        brief="do it",
        model="anthropic/claude-opus-4-8",
    )
    assert res.error is None and res.aborted is None
    assert res.turns == 1
    assert res.text == "all done"


def test_loop_dispatches_tool_then_finishes(tmp_path: Path) -> None:
    logs: list[str] = []
    calls = iter(
        [
            Completion(
                "let me read",
                [ToolCall("t1", "read_file", {"path": "f.txt"})],
                {"input_tokens": 10, "output_tokens": 3},
                "tool_use",
            ),
            Completion("done reading", [], {"input_tokens": 8, "output_tokens": 2}, "end_turn"),
        ]
    )

    def fake(messages, tools, knobs, **kw):
        return next(calls)

    res = run_loop(
        transport_complete=fake,
        registry=_registry(tmp_path),
        charter="CHARTER",
        brief="read f.txt",
        model="anthropic/claude-opus-4-8",
        emit_log=logs.append,
    )
    assert res.turns == 2
    # telemetry summed across turns
    assert res.usage["input_tokens"] == 18
    assert res.usage["output_tokens"] == 5
    # both assistant texts surfaced
    joined = "".join(logs)
    assert "let me read" in joined and "done reading" in joined
    assert "[read_file] ok" in joined


def test_max_turns_honoured(tmp_path: Path) -> None:
    def always_tool(messages, tools, knobs, **kw):
        return Completion("", [ToolCall("t", "read_file", {"path": "f.txt"})], {}, "tool_use")

    res = run_loop(
        transport_complete=always_tool,
        registry=_registry(tmp_path),
        charter="C",
        brief="b",
        model="anthropic/x",
        max_turns=3,
    )
    assert res.turns == 3
    assert res.error is not None and "max_turns" in res.error


def test_should_abort_stops_loop(tmp_path: Path) -> None:
    def fake(messages, tools, knobs, **kw):
        return Completion("", [ToolCall("t", "read_file", {"path": "f.txt"})], {}, "tool_use")

    res = run_loop(
        transport_complete=fake,
        registry=_registry(tmp_path),
        charter="C",
        brief="b",
        model="anthropic/x",
        should_abort=lambda: "stopped",
    )
    assert res.aborted == "stopped"
    assert res.turns == 0


def test_transport_error_is_honest_failure(tmp_path: Path) -> None:
    def fake(messages, tools, knobs, **kw):
        raise TransportError("boom")

    res = run_loop(
        transport_complete=fake,
        registry=_registry(tmp_path),
        charter="C",
        brief="b",
        model="anthropic/x",
    )
    assert isinstance(res, LoopResult)
    assert res.error == "boom"
    assert res.turns == 0


# --------------------------------------------------------------------------- #
# Cache strategy — the §1.3 lever
# --------------------------------------------------------------------------- #


class _CacheAwareTransport:
    """Records every request; emits a cache *read* split on turn N only when the
    system prefix is byte-identical to turn N-1 (what a real cache does)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._prev_system: Any = None
        self._turn = 0

    def __call__(self, messages, tools, knobs, *, system=None, **kw):
        self._turn += 1
        # The system blocks may carry cache_control; compare the *text* prefix,
        # which is what stays byte-stable across turns.
        prefix = tuple(b.get("text") for b in (system or []))
        self.requests.append({"system": system, "messages": list(messages)})
        if self._turn == 1:
            usage = {"input_tokens": 100, "output_tokens": 5, "cache_creation_input_tokens": 90}
            self._prev_system = prefix
            return Completion(
                "step 1", [ToolCall("t1", "read_file", {"path": "f.txt"})], usage, "tool_use"
            )
        # turn 2+: real cache read only if the prefix matched the previous turn
        hit = prefix == self._prev_system
        usage = {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_input_tokens": 90 if hit else 0,
        }
        self._prev_system = prefix
        return Completion("step 2 done", [], usage, "end_turn")


def test_cache_breakpoint_and_real_hit(tmp_path: Path) -> None:
    from nightshift.backends import _usage_tokens

    fake = _CacheAwareTransport()
    res = run_loop(
        transport_complete=fake,
        registry=_registry(tmp_path),
        charter="STABLE CHARTER TEXT",
        brief="do the thing",
        model="anthropic/claude-opus-4-8",
        knobs={"enable_cache": True},
    )
    # (a) request 1 carries a stable cache breakpoint on the system block
    sys0 = fake.requests[0]["system"]
    assert sys0[-1].get("cache_control", {}).get("type") == "ephemeral"
    # (b) turn 2 reported a real cache read (>0)
    assert res.usage["cache_read_input_tokens"] == 90
    # (c) _usage_tokens folds the splits into input_tokens
    inp, out = _usage_tokens(res.usage)
    # input = sum(input_tokens) + cache_creation + cache_read = 110 + 90 + 90
    assert inp == 110 + 90 + 90
    assert out == 9


def test_cache_disabled_places_no_breakpoint(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake(messages, tools, knobs, *, system=None, **kw):
        seen["system"] = system
        return Completion("done", [], {}, "end_turn")

    run_loop(
        transport_complete=fake,
        registry=_registry(tmp_path),
        charter="C",
        brief="b",
        model="anthropic/x",
        knobs={"enable_cache": False},
    )
    assert "cache_control" not in seen["system"][-1]


def test_ollama_vendor_skips_cache_placement(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake(messages, tools, knobs, *, system=None, **kw):
        seen["system"] = system
        return Completion("done", [], {}, "stop")

    run_loop(
        transport_complete=fake,
        registry=_registry(tmp_path),
        charter="C",
        brief="b",
        model="ollama/llama3",
        knobs={"enable_cache": True},  # ignored for non-anthropic vendors
    )
    assert "cache_control" not in seen["system"][-1]


# --------------------------------------------------------------------------- #
# Charter regression guard (invariant 7a)
# --------------------------------------------------------------------------- #


def test_charter_is_byte_stable() -> None:
    # The shipped charter must contain no per-run interpolation tokens that would
    # bust the cache prefix every run.
    text = load_charter()
    assert text == load_charter()  # deterministic
    for token in ("{", "%s", "{{", "TIMESTAMP", "task_id"):
        assert token not in text, f"charter contains interpolation marker {token!r}"
