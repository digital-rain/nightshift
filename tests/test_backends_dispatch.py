from __future__ import annotations

import time
from pathlib import Path

import pytest

import nightshift.backends as backends_mod
from nightshift.backends import WorkerSpec, _stream_subprocess


def test_known_providers_matches_backend_names() -> None:
    assert backends_mod.known_providers() == set(backends_mod.backend_names())
    assert "ollama-cloud" in backends_mod.known_providers()


def test_require_backend_returns_known() -> None:
    assert backends_mod.require_backend("cursor").name == "cursor"


def test_require_backend_unknown_raises() -> None:
    with pytest.raises(KeyError):
        backends_mod.require_backend("does-not-exist")


def test_spec_has_timeout_field() -> None:
    spec = WorkerSpec(
        task="t", prompt="p", model="m", max_turns=None,
        cwd=Path("/tmp"), env={}, config={}, timeout=12.5,
    )
    assert spec.timeout == 12.5


def test_stream_subprocess_kills_on_timeout(tmp_path: Path) -> None:
    logs: list[str] = []
    start = time.monotonic()
    result = _stream_subprocess(
        ["sleep", "30"],
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
        emit_log=logs.append, should_abort=lambda: None,
        timeout=1.0,
    )
    assert time.monotonic() - start < 10  # killed early, not after 30s
    assert result.aborted == "timeout" or (result.error and "tim" in result.error.lower())
