"""Phases 6-8 tests — backend registration, real edit-flow via a fake transport,
honest failure, config round-trip + UI surfacing, and the toggle routing.

(Kept in its own file rather than folded into test_nightshift_worker.py so the
new provider's surface stays cohesive; the manager squash-land path is already
covered there generically via require_backend monkeypatching.)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import nightshift.agent.transport as transport
import nightshift.backends as backends_mod
from nightshift.agent.transport import Completion, ToolCall, TransportError
from nightshift.backends import WorkerSpec
from nightshift.config.registry import build_registry as build_field_registry
from nightshift.config.validate import validate_delta
from nightshift.config.worker import (
    NightshiftBackendConfig,
    WorkerConfig,
    _load_nightshift,
    load_worker_config,
    save_worker_config,
)
from nightshift.resolve_runner import select_run_backend


def _spec(tmp_path: Path, model: str, config: dict[str, Any] | None = None) -> WorkerSpec:
    return WorkerSpec(
        task="t",
        prompt="make an edit",
        model=model,
        max_turns=None,
        cwd=tmp_path,
        env={"ANTHROPIC_API_KEY": "sk-test"},
        config=config or {},
    )


# --------------------------------------------------------------------------- #
# Phase 6 — registration
# --------------------------------------------------------------------------- #


def test_nightshift_registered() -> None:
    assert "nightshift" in backends_mod.backend_names()
    backend = backends_mod.require_backend("nightshift")
    assert isinstance(backend, backends_mod.NightshiftAgentBackend)
    assert backend.agentic is True


def test_select_run_backend_strips_provider() -> None:
    backend, bare = select_run_backend("nightshift/anthropic/claude-sonnet-4-6", None)
    assert isinstance(backend, backends_mod.NightshiftAgentBackend)
    assert bare == "anthropic/claude-sonnet-4-6"


def test_availability_per_vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = backends_mod.NightshiftAgentBackend()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(backends_mod.shutil, "which", lambda _n: None)
    assert backend.available({}) is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert backend.available({}) is True


def test_malformed_model_is_error(tmp_path: Path) -> None:
    backend = backends_mod.NightshiftAgentBackend()
    res = backend.run(_spec(tmp_path, "anthropic"), lambda _s: None, lambda: None)
    assert res.returncode == 2
    assert res.error is not None and "vendor" in res.error


# --------------------------------------------------------------------------- #
# Phase 6 — real edit flows through the backend (fake transport)
# --------------------------------------------------------------------------- #


def test_backend_applies_edit_via_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "code.txt").write_text("old line\n", encoding="utf-8")

    edits = "<<<<<<< SEARCH\nold line\n=======\nnew line\n>>>>>>> REPLACE\n"
    calls = iter(
        [
            Completion(
                "editing",
                [ToolCall("c1", "edit_file", {"path": "code.txt", "edits": edits})],
                {"input_tokens": 20, "output_tokens": 6},
                "tool_use",
            ),
            Completion("done", [], {"input_tokens": 5, "output_tokens": 2}, "end_turn"),
        ]
    )

    def fake_complete(messages, tools, knobs, **kw):
        return next(calls)

    monkeypatch.setattr(transport, "complete", fake_complete)
    backend = backends_mod.NightshiftAgentBackend()
    res = backend.run(
        _spec(tmp_path, "anthropic/claude-sonnet-4-6"),
        lambda _s: None,
        lambda: None,
    )
    assert res.returncode == 0
    assert res.turns == 2
    # the real write flowed through to disk (what git.squash.squash_to_main commits)
    assert (tmp_path / "code.txt").read_text() == "new line\n"
    # usage folded via _usage_tokens (no cache splits here → plain sums)
    assert (res.input_tokens, res.output_tokens) == (25, 8)


def test_backend_transport_error_is_honest_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(messages, tools, knobs, **kw):
        raise TransportError("upstream 500")

    monkeypatch.setattr(transport, "complete", boom)
    backend = backends_mod.NightshiftAgentBackend()
    res = backend.run(
        _spec(tmp_path, "anthropic/claude-sonnet-4-6"),
        lambda _s: None,
        lambda: None,
    )
    assert res.returncode == 1
    assert res.error == "upstream 500"
    # nothing was written
    assert not any(tmp_path.iterdir())


def test_backend_reads_knobs_from_spec_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_complete(messages, tools, knobs, **kw):
        seen["knobs"] = knobs
        return Completion("done", [], {}, "end_turn")

    monkeypatch.setattr(transport, "complete", fake_complete)
    backend = backends_mod.NightshiftAgentBackend()
    backend.run(
        _spec(
            tmp_path,
            "anthropic/claude-sonnet-4-6",
            config={"nightshift": {"max_tokens": 8000, "effort": "high"}},
        ),
        lambda _s: None,
        lambda: None,
    )
    assert seen["knobs"]["max_tokens"] == 8000
    assert seen["knobs"]["effort"] == "high"


# --------------------------------------------------------------------------- #
# Phase 7 — config dataclass, round-trip, UI registry surfacing
# --------------------------------------------------------------------------- #


def test_nightshift_fields_in_registry() -> None:
    specs = {
        s.key: s
        for s in build_field_registry()
        if s.surface == "worker" and s.key.startswith("nightshift.")
    }
    assert specs["nightshift.enabled"].type == "bool"
    assert set(specs["nightshift.vendor"].options) == {"anthropic", "ollama-cloud", "ollama"}
    assert specs["nightshift.context_policy"].options == ["spans", "whole_file"]
    # grouped under the harness category
    assert specs["nightshift.enabled"].category == "Nightshift harness"


def test_config_round_trips_through_disk(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / ".nightshift").mkdir()
    cfg = WorkerConfig(
        workspace=workspace,
        nightshift=NightshiftBackendConfig(
            enabled=True, vendor="ollama-cloud", model="qwen3-coder:480b", max_tokens=8000
        ),
    )
    save_worker_config(workspace, cfg)
    on_disk = json.loads((workspace / ".nightshift" / "worker.json").read_text())
    assert on_disk["nightshift"]["enabled"] is True
    assert on_disk["nightshift"]["vendor"] == "ollama-cloud"

    reloaded = load_worker_config(workspace)
    assert reloaded.nightshift.enabled is True
    assert reloaded.nightshift.vendor == "ollama-cloud"
    assert reloaded.nightshift.max_tokens == 8000


def test_load_nightshift_defaults_when_absent() -> None:
    ns = _load_nightshift(None)
    assert ns.enabled is False
    assert ns.vendor == "anthropic"
    assert ns == NightshiftBackendConfig()


def test_validate_delta_accepts_enabled_toggle() -> None:
    resolved, errors = validate_delta(
        {"worker": {"nightshift.enabled": True}}, {"worker"}
    )
    assert errors == {}
    assert resolved["worker"]["nightshift.enabled"] is True


def test_validate_delta_rejects_bad_vendor_enum() -> None:
    resolved, errors = validate_delta(
        {"worker": {"nightshift.vendor": "openai"}}, {"worker"}
    )
    assert "worker.nightshift.vendor" in errors


# --------------------------------------------------------------------------- #
# Phase 8 — the toggle's routing rewrite
# --------------------------------------------------------------------------- #


def _routing_cfg(enabled: bool, monkeypatch: pytest.MonkeyPatch) -> WorkerConfig:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")  # makes the harness vendor available
    return WorkerConfig(
        workspace=Path("/tmp"),
        auto_model="cursor/gpt-5",
        max_model="claude-code/claude-opus-4-8",
        nightshift=NightshiftBackendConfig(
            enabled=enabled, vendor="anthropic", model="claude-sonnet-4-6"
        ),
    )


def test_routing_off_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _routing_cfg(False, monkeypatch)
    assert cfg.resolve_model("auto") == ("cursor/gpt-5", None)
    assert cfg.resolve_model("max") == ("claude-code/claude-opus-4-8", None)
    assert cfg.resolve_model("anthropic/claude-sonnet-4-6") == (
        "anthropic/claude-sonnet-4-6",
        None,
    )


def test_routing_on_rewrites_cli_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _routing_cfg(True, monkeypatch)
    assert cfg.resolve_model("auto") == ("nightshift/anthropic/claude-sonnet-4-6", None)
    assert cfg.resolve_model("max") == ("nightshift/anthropic/claude-sonnet-4-6", None)


def test_routing_on_leaves_noncli_and_nightshift_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _routing_cfg(True, monkeypatch)
    # an explicit non-agentic-CLI id passes through
    assert cfg.resolve_model("anthropic/claude-opus-4-8") == (
        "anthropic/claude-opus-4-8",
        None,
    )
    # an already-nightshift id passes through
    assert cfg.resolve_model("nightshift/ollama/llama3") == ("nightshift/ollama/llama3", None)


def test_routing_falls_back_when_vendor_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No vendor creds → harness unavailable → original id preserved (no break).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(backends_mod.shutil, "which", lambda _n: None)
    cfg = WorkerConfig(
        workspace=Path("/tmp"),
        auto_model="cursor/gpt-5",
        nightshift=NightshiftBackendConfig(enabled=True, vendor="anthropic"),
    )
    assert cfg.resolve_model("auto") == ("cursor/gpt-5", None)
