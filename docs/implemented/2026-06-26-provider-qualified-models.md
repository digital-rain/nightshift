# Provider-Qualified Models (`<provider>/<model>`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every model reference in Nightshift a single `<provider>/<model>` string, let one worker serve multiple providers (backends) at once, and add a global per-worker model timeout.

**Architecture:** Today a worker runs exactly one `backend` and model ids are bare strings; the provider is implicit in which worker wins a task. We invert that: the **provider is encoded in the model id** (`claude-code/claude-sonnet-4-6`, `ollama-cloud/gpt-oss:120b`). The worker advertises a list of qualified ids, only for providers whose backend is actually available on its machine; at execution it parses the provider off the resolved id and dispatches to that backend. The single `backend` selector is removed; `auto`/`max` resolve to a single qualified default per worker. A worker-level `model_timeout_seconds` bounds every backend run.

**Tech Stack:** Python 3.12, dataclasses-based config + derived settings registry, `httpx`, subprocess agent CLIs, pytest. No new dependencies.

---

## Design decisions (read before starting)

- **Provider == backend name.** The provider token is exactly an existing backend name: `claude-code`, `cursor`, `antigravity`, `anthropic`, `ollama`, `ollama-cloud`. There is no separate vendor concept.
- **Slug format:** `provider/model`, split on the **first** `/` only. The model half may itself contain `/` (e.g. `ollama/hf.co/user/repo`) and `:` (e.g. `ollama-cloud/gpt-oss:120b`). Matching is case-insensitive, consistent with the existing scheduler.
- **Agnostic keywords keep working:** `auto`, `max`, `default`, `""` are *not* qualified and never carry a provider. The worker resolves them to a concrete qualified id at execution.
- **One worker, many providers:** a worker’s providers are derived from the set of qualified models it advertises. The hard single-`backend` field is removed.
- **Availability gating:** a worker only advertises a qualified model whose provider backend reports `available()` on that machine.
- **Timeout:** one global `model_timeout_seconds` in the worker’s **Models** settings. `0` (default) = no timeout (preserves today’s behavior); a positive value bounds every backend run (subprocess wall-clock kill + httpx read/connect timeout).
- **Back-compat on read:** when loading a legacy `worker.json` that still has a single `backend` plus bare model ids, unqualified ids are auto-prefixed with that `backend` so existing installs keep working. New writes are always qualified.

### Files changed (map)

- `src/nightshift/model_id.py` — **new**, pure split/qualify helpers.
- `src/nightshift/backends.py` — `known_providers()`, strict `get_backend` lookup, `WorkerSpec.timeout`, timeout enforcement in the subprocess + httpx paths.
- `src/nightshift/config/worker.py` — drop `backend`; single `auto_model`/`max_model` (qualified strings); add `model_timeout_seconds`; `providers()`/`advertised_models()`; qualified `resolve_model`; legacy normalization on load.
- `src/nightshift/worker/execute.py` — dispatch by the resolved id’s provider; pass timeout; report provider on the outcome.
- `src/nightshift/worker/loop.py` — send a provider summary instead of `cfg.backend`; record provider from the outcome.
- `src/nightshift/worker/client.py` — `backend` becomes optional on checkin/poll.
- `src/nightshift/manager/app.py` — `CheckinBody`/`PollBody` `backend` optional; dispatch records provider from the model.
- `src/nightshift/config/manager.py` — qualified `scheduled_models_allow` default; drop legacy `model`/`cursor_model`; qualified `resolve_model`.
- `src/nightshift/spawn_daily.py` — `resolve_frontmatter` reads `default_model` only.
- `src/nightshift/assets/config/worker.json`, `manager.json` — shipped templates updated to the new shape.
- `config.json` (repo root sample) — qualified `scheduled_models_allow`, drop `model`/`cursor_model`.
- `docs/configuration-reference.md`, `docs/NIGHTSHIFT.md` — documentation.
- Tests across `tests/` — updated + new.

---

## Phase A — Model-id primitives

### Task A1: `model_id` helper module

**Files:**
- Create: `src/nightshift/model_id.py`
- Test: `tests/test_model_id.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_model_id.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_model_id.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nightshift.model_id'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/nightshift/model_id.py
"""Provider-qualified model ids: ``<provider>/<model>``.

A model id is either an *agnostic keyword* (``auto`` / ``max`` / ``default`` /
``""``) which pins no provider, or a *qualified* id of the form
``provider/model`` where ``provider`` is a backend name. The model half may
itself contain ``/`` and ``:`` (e.g. ``ollama/hf.co/user/repo``,
``ollama-cloud/gpt-oss:120b``), so we always split on the **first** ``/`` only.

This module is intentionally dependency-free (no import of backends/config) so
it can be used from the scheduler, worker, and manager without cycles.
"""

from __future__ import annotations

AGNOSTIC = frozenset({"auto", "max", "default", ""})


def split_model(model_id: str | None) -> tuple[str | None, str]:
    """Return ``(provider, model)``. ``provider`` is ``None`` for agnostic ids."""
    text = (model_id or "").strip()
    if text.lower() in AGNOSTIC:
        return None, text
    provider, sep, model = text.partition("/")
    if not sep:
        return None, text  # bare/unqualified id (legacy) — no provider
    return provider.strip(), model.strip()


def is_qualified(model_id: str | None) -> bool:
    """True when ``model_id`` carries an explicit ``provider/`` prefix."""
    return split_model(model_id)[0] is not None


def provider_of(model_id: str | None) -> str | None:
    """The provider half, or ``None`` for agnostic/bare ids."""
    return split_model(model_id)[0]


def join_model(provider: str, model: str) -> str:
    """Build a qualified id; raises on an empty provider or model."""
    if not provider or not model:
        raise ValueError("provider and model must both be non-empty")
    return f"{provider}/{model}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_model_id.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nightshift/model_id.py tests/test_model_id.py
git commit -m "feat(model-id): add provider-qualified model id helpers"
```

---

## Phase B — Backends: provider lookup + timeout

### Task B1: `known_providers()` and strict backend lookup

**Files:**
- Modify: `src/nightshift/backends.py` (the registry helpers near `get_backend`, ~lines 731-760 after the ollama-cloud addition)
- Test: `tests/test_nightshift_ui.py` (extend the existing backend-registry test) and `tests/test_backends_dispatch.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backends_dispatch.py
from __future__ import annotations

import pytest

import nightshift.backends as backends_mod


def test_known_providers_matches_backend_names() -> None:
    assert backends_mod.known_providers() == set(backends_mod.backend_names())
    assert "ollama-cloud" in backends_mod.known_providers()


def test_require_backend_returns_known() -> None:
    assert backends_mod.require_backend("cursor").name == "cursor"


def test_require_backend_unknown_raises() -> None:
    with pytest.raises(KeyError):
        backends_mod.require_backend("does-not-exist")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_backends_dispatch.py -q`
Expected: FAIL with `AttributeError: module 'nightshift.backends' has no attribute 'known_providers'`

- [ ] **Step 3: Write minimal implementation**

Add after `def get_backend(...)` in `src/nightshift/backends.py`:

```python
def known_providers() -> set[str]:
    """Set of valid provider tokens (== backend names)."""
    return {b.name for b in _BACKENDS}


def require_backend(provider: str) -> Any:
    """Return the backend for ``provider`` or raise ``KeyError`` (no fallback).

    Unlike :func:`get_backend`, this never silently falls back to the default —
    an unknown provider in a qualified model id is an operator error we surface.
    """
    registry = _by_name()
    if provider not in registry:
        raise KeyError(provider)
    return registry[provider]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_backends_dispatch.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nightshift/backends.py tests/test_backends_dispatch.py
git commit -m "feat(backends): add known_providers + strict require_backend lookup"
```

### Task B2: `WorkerSpec.timeout` + enforcement in the subprocess + httpx paths

**Files:**
- Modify: `src/nightshift/backends.py` — `WorkerSpec` (~line 55), `_stream_subprocess` (~line 195), `_run_buffered` (~line 266), `_ollama_generate`, `AnthropicBackend.run`, each backend `run` call.
- Test: `tests/test_backends_dispatch.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_backends_dispatch.py
import time
from pathlib import Path

from nightshift.backends import WorkerSpec, _stream_subprocess


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_backends_dispatch.py -q`
Expected: FAIL — `WorkerSpec.__init__() got an unexpected keyword argument 'timeout'`

- [ ] **Step 3: Write minimal implementation**

In `WorkerSpec` add the field (after `config`):

```python
@dataclass
class WorkerSpec:
    """Everything a backend needs to do one task's worth of work."""

    task: str
    prompt: str
    model: str
    max_turns: int | None
    cwd: Path
    env: dict[str, str]
    config: dict[str, Any]
    # Global per-worker wall-clock bound for this run (seconds). None/0 = none.
    timeout: float | None = None
```

Change `_stream_subprocess` to accept and enforce a deadline. Add `timeout: float | None = None` to its signature and replace the abort watcher with a deadline-aware one:

```python
def _stream_subprocess(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    emit_log: EmitLog,
    should_abort: ShouldAbort,
    on_start: OnWorkerStart | None = None,
    parser: AgentStreamParser | None = None,
    timeout: float | None = None,
) -> WorkerResult:
    ...  # (Popen + on_start unchanged)
    aborted: dict[str, str | None] = {"reason": None}
    done = threading.Event()
    deadline = (time.monotonic() + timeout) if timeout and timeout > 0 else None

    def _watch_abort() -> None:
        while not done.wait(0.25):
            reason = should_abort()
            if reason is None and deadline is not None and time.monotonic() > deadline:
                reason = "timeout"
            if reason is not None:
                aborted["reason"] = reason
                engine._kill_process_group(proc)
                return

    watcher = threading.Thread(target=_watch_abort, daemon=True)
    watcher.start()
    try:
        for line in proc.stdout:
            _emit(line)
            reason = should_abort()
            if reason is None and deadline is not None and time.monotonic() > deadline:
                reason = "timeout"
            if reason is not None:
                aborted["reason"] = reason
                engine._kill_process_group(proc)
                break
        returncode = proc.wait()
    finally:
        done.set()
        watcher.join(timeout=1)
    result = WorkerResult(returncode=returncode, aborted=aborted["reason"])
    return parser.apply(result) if parser is not None else result
```

Add `import time` to the imports block at the top of `backends.py` (alongside `import threading`).

Apply the **same** deadline logic to `_run_buffered` (add `timeout` param + identical watcher/loop checks).

For each backend `run`, thread `spec.timeout` through. Examples:

```python
# ClaudeCodeBackend.run / CursorAgentBackend.run
return _stream_subprocess(
    argv, cwd=spec.cwd, env=spec.env, emit_log=emit_log,
    should_abort=should_abort, on_start=on_worker_start,
    parser=AgentStreamParser(), timeout=spec.timeout,
)
```

```python
# GeminiCLIBackend.run
rc, output, aborted, launch_err = _run_buffered(
    argv, cwd=spec.cwd, env=spec.env,
    should_abort=should_abort, on_start=on_worker_start,
    timeout=spec.timeout,
)
```

For the httpx paths, pass an `httpx.Timeout`. Add a helper near the top of `backends.py`:

```python
def _httpx_timeout(seconds: float | None) -> Any:
    """An httpx timeout: a finite per-op bound, or no timeout when unset/<=0."""
    return httpx.Timeout(seconds) if seconds and seconds > 0 else None
```

In `_ollama_generate`, add a `timeout: float | None = None` parameter and use `timeout=_httpx_timeout(timeout)` in `httpx.stream(...)`. In `OllamaBackend.run`/`OllamaCloudBackend.run` pass `timeout=spec.timeout` to `_ollama_generate`. In `AnthropicBackend.run` change `timeout=None` in the `httpx.stream` call to `timeout=_httpx_timeout(spec.timeout)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_backends_dispatch.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nightshift/backends.py tests/test_backends_dispatch.py
git commit -m "feat(backends): add per-run timeout to subprocess and httpx backends"
```

---

## Phase C — Worker config: providers, qualified auto/max, timeout

### Task C1: Rework `WorkerConfig`

**Files:**
- Modify: `src/nightshift/config/worker.py` (whole file: defaults, fields, `resolve_model`, load/save, new helpers)
- Test: `tests/test_nightshift_worker.py` (rewrite the config/model-resolution tests), `tests/test_config_model.py`

- [ ] **Step 1: Write the failing tests**

Replace the model-resolution tests in `tests/test_nightshift_worker.py` (the `_cfg`, `test_model_resolution_auto_max_explicit`, `test_model_resolution_no_vendor_mismatch`, `test_model_aliases_remap_explicit_ids`, and the ollama-cloud `test_ollama_cloud_model_defaults`) with:

```python
def _cfg(**kw) -> WorkerConfig:
    base = dict(workspace=Path("/tmp"), worker_id="w", manager_url="http://x")
    base.update(kw)
    return WorkerConfig(**base)


def test_resolve_auto_max_default_qualified() -> None:
    cfg = _cfg()
    assert cfg.resolve_model("auto") == ("claude-code/claude-sonnet-4-6", None)
    assert cfg.resolve_model("max") == ("claude-code/claude-opus-4-8", None)
    assert cfg.resolve_model(None) == ("claude-code/claude-sonnet-4-6", None)


def test_resolve_auto_max_honor_overrides() -> None:
    cfg = _cfg(auto_model="ollama-cloud/gpt-oss:120b", max_model="ollama-cloud/deepseek-v3.1:671b")
    assert cfg.resolve_model("auto") == ("ollama-cloud/gpt-oss:120b", None)
    assert cfg.resolve_model("max") == ("ollama-cloud/deepseek-v3.1:671b", None)


def test_resolve_explicit_qualified_passthrough() -> None:
    cfg = _cfg()
    assert cfg.resolve_model("cursor/gpt-5") == ("cursor/gpt-5", None)


def test_resolve_explicit_alias_remap() -> None:
    cfg = _cfg(model_aliases={"cursor/gpt-5": "cursor/gpt-5.1"})
    assert cfg.resolve_model("cursor/gpt-5") == ("cursor/gpt-5.1", None)


def test_providers_derived_from_models() -> None:
    cfg = _cfg(models=["claude-code/claude-opus-4-8", "ollama-cloud/gpt-oss:120b"])
    assert cfg.providers() == {"claude-code", "ollama-cloud"}


def test_advertised_models_filters_unavailable(monkeypatch) -> None:
    # claude-code unavailable (no CLI), ollama-cloud available (key set).
    monkeypatch.setattr("shutil.which", lambda _n: None)
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    cfg = _cfg(models=["claude-code/claude-opus-4-8", "ollama-cloud/gpt-oss:120b"])
    assert cfg.advertised_models() == ["ollama-cloud/gpt-oss:120b"]


def test_legacy_backend_qualifies_bare_models(tmp_path: Path, monkeypatch) -> None:
    # A legacy worker.json with a single backend + bare ids is auto-qualified.
    _seed(tmp_path, {})
    (tmp_path / ".nightshift").mkdir(exist_ok=True)
    (tmp_path / ".nightshift" / "worker.json").write_text(
        '{"backend": "ollama", "models": ["llama3.1", "llama3.1:70b"]}'
    )
    cfg = load_worker_config(tmp_path)
    assert cfg.models == ["ollama/llama3.1", "ollama/llama3.1:70b"]


def test_model_timeout_default_zero() -> None:
    assert _cfg().model_timeout_seconds == 0.0
```

Update `test_worker_config_from_env` to stop asserting `cfg.backend` and instead assert the new behavior:

```python
def test_worker_config_from_env(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, {})
    monkeypatch.setenv("NIGHTSHIFT_MANAGER_URL", "http://mgr:8800/")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_ID", "worker-x")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_QUEUES", "main,alpha")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_PRIORITIES", "0,1")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_MODELS", "claude-code/claude-opus-4-8, cursor/gpt-5")
    cfg = load_worker_config(tmp_path)
    assert cfg.worker_id == "worker-x"
    assert cfg.manager_url == "http://mgr:8800"
    assert cfg.queues == ["main", "alpha"]
    assert cfg.priorities == [0, 1]
    assert cfg.models == ["claude-code/claude-opus-4-8", "cursor/gpt-5"]
    assert cfg.providers() == {"claude-code", "cursor"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightshift_worker.py -q`
Expected: FAIL — `WorkerConfig.__init__() got an unexpected keyword argument` / `AttributeError: 'WorkerConfig' object has no attribute 'providers'`

- [ ] **Step 3: Write minimal implementation**

Rewrite the top of `src/nightshift/config/worker.py`:

```python
import os
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift import backends as backends_mod
from nightshift.config.io import load_dotenv, load_json, save_json, worker_json_path
from nightshift.config.meta import meta
from nightshift.model_id import is_qualified, join_model, provider_of, split_model


DEFAULT_AUTO_MODEL = "claude-code/claude-sonnet-4-6"
DEFAULT_MAX_MODEL = "claude-code/claude-opus-4-8"
```

Replace the `backend`, `auto_model`, `max_model` fields and add `model_timeout_seconds`. Remove the `backend` field entirely; the `models` field stays but its label/desc updates to mention the format:

```python
    # (remove the `backend` field)

    models: list[str] = field(default_factory=list, metadata=meta(
        category="Models", label="Available",
        desc="Qualified model ids this worker advertises, as provider/model "
             "(e.g. claude-code/claude-opus-4-8, ollama-cloud/gpt-oss:120b).",
        apply="restart", env="NIGHTSHIFT_WORKER_MODELS", type="string_list"))
    model_aliases: dict[str, str] = field(default_factory=dict, metadata=meta(
        category="Models", label="Model aliases",
        desc="Remap {requested: actual} (both provider/model) applied at execution.",
        apply="restart", type="str_map"))
    auto_model: str = field(default=DEFAULT_AUTO_MODEL, metadata=meta(
        category="Models", label="Auto model",
        desc="Qualified model 'auto' resolves to (provider/model).",
        apply="restart"))
    max_model: str = field(default=DEFAULT_MAX_MODEL, metadata=meta(
        category="Models", label="Max model",
        desc="Qualified model 'max' resolves to (provider/model).",
        apply="restart"))
    model_timeout_seconds: float = field(default=0.0, metadata=meta(
        category="Models", label="Model timeout seconds",
        desc="Global wall-clock bound for any backend run. 0 = no timeout.",
        apply="restart", env="NIGHTSHIFT_MODEL_TIMEOUT_SECONDS"))
```

Replace `resolve_model`, `_auto_model`, `_max_model` and add `providers`/`advertised_models`:

```python
    def resolve_model(self, requested: str | None) -> tuple[str | None, str | None]:
        """Resolve a work-order model to a qualified provider/model id.

        ``auto``/``max``/unset resolve to this worker's configured defaults;
        an explicit id passes through ``model_aliases`` (identity unless mapped).
        """
        key = (requested or "auto").strip().lower()
        if key in ("", "auto", "default"):
            return self.auto_model, None
        if key == "max":
            return self.max_model, None
        return self.model_aliases.get(requested, requested), None

    def providers(self) -> set[str]:
        """Distinct provider tokens across advertised models (+ auto/max)."""
        out: set[str] = set()
        for m in [*self.models, self.auto_model, self.max_model]:
            p = provider_of(m)
            if p:
                out.add(p)
        return out

    def advertised_models(self, config: dict[str, Any] | None = None) -> list[str]:
        """Advertised models whose provider backend is available on this host."""
        out: list[str] = []
        for m in self.models:
            provider = provider_of(m)
            if provider is None:
                continue
            try:
                backend = backends_mod.require_backend(provider)
            except KeyError:
                continue
            if backend.available(config or {}):
                out.append(m)
        return out
```

Delete the `_auto_model`/`_max_model` methods and the `DEFAULT_AUTO_MODELS`/`DEFAULT_MAX_MODELS` dicts.

Add a legacy normalizer and use it in `load_worker_config`:

```python
def _qualify(models: list[str], legacy_backend: str | None) -> list[str]:
    """Prefix bare ids with a legacy single-backend, leaving qualified ids."""
    if not legacy_backend:
        return models
    return [m if is_qualified(m) else join_model(legacy_backend, m) for m in models]
```

In `load_worker_config`, drop all `backend` resolution, normalize models, and read the new fields:

```python
def load_worker_config(workspace: Path) -> WorkerConfig:
    workspace = workspace.resolve()
    load_dotenv(workspace)
    local = load_json(worker_json_path(workspace))

    legacy_backend = local.get("backend")  # back-compat only

    worker_id = (
        os.environ.get("NIGHTSHIFT_WORKER_ID")
        or local.get("worker_id")
        or f"{socket.gethostname()}-{os.getpid()}"
    )
    manager_url = (
        os.environ.get("NIGHTSHIFT_MANAGER_URL")
        or local.get("manager_url")
        or "http://localhost:8800"
    ).rstrip("/")

    queues_raw = _csv_list(os.environ.get("NIGHTSHIFT_WORKER_QUEUES"))
    if queues_raw is None and isinstance(local.get("queues"), list):
        queues_raw = [str(q) for q in local["queues"]]
    priorities_raw = _int_csv(os.environ.get("NIGHTSHIFT_WORKER_PRIORITIES"))
    if priorities_raw is None and isinstance(local.get("priorities"), list):
        priorities_raw = [int(p) for p in local["priorities"]]

    models_raw = _csv_list(os.environ.get("NIGHTSHIFT_WORKER_MODELS"))
    if models_raw is None and isinstance(local.get("models"), list):
        models_raw = [str(m) for m in local["models"]]
    models = _qualify(models_raw or [], legacy_backend)

    mcps_raw = _csv_list(os.environ.get("NIGHTSHIFT_WORKER_MCPS"))
    if mcps_raw is None and isinstance(local.get("mcps"), list):
        mcps_raw = [str(m) for m in local["mcps"]]

    model_aliases = (
        dict(local.get("model_aliases", {}))
        if isinstance(local.get("model_aliases"), dict) else {}
    )

    def _legacy_single(value: Any, default: str) -> str:
        # Accept either a new qualified string or a legacy {backend: model} map.
        if isinstance(value, str) and value.strip():
            return value if is_qualified(value) else _qualify([value], legacy_backend)[0]
        if isinstance(value, dict) and legacy_backend and value.get(legacy_backend):
            return join_model(legacy_backend, str(value[legacy_backend]))
        return default

    return WorkerConfig(
        workspace=workspace,
        worker_id=worker_id,
        manager_url=manager_url,
        shared_secret=os.environ.get("NIGHTSHIFT_SHARED_SECRET") or None,
        rendezvous_remote=(
            os.environ.get("NIGHTSHIFT_RENDEZVOUS_REMOTE")
            or local.get("rendezvous_remote")
            or None
        ),
        queues=queues_raw if queues_raw else None,
        priorities=priorities_raw if priorities_raw else None,
        models=models,
        mcps=mcps_raw or [],
        model_aliases=model_aliases,
        auto_model=_legacy_single(local.get("auto_model"), DEFAULT_AUTO_MODEL),
        max_model=_legacy_single(local.get("max_model"), DEFAULT_MAX_MODEL),
        model_timeout_seconds=float(
            os.environ.get("NIGHTSHIFT_MODEL_TIMEOUT_SECONDS")
            or local.get("model_timeout_seconds")
            or 0.0
        ),
        ui_host=os.environ.get("NIGHTSHIFT_WORKER_UI_HOST") or local.get("ui_host") or "0.0.0.0",
        ui_port=int(os.environ.get("NIGHTSHIFT_WORKER_UI_PORT") or local.get("ui_port") or 8810),
        raw=local,
    )
```

Update `save_worker_config` to drop `backend` and write the new fields:

```python
    data: dict[str, Any] = {
        "worker_id": config.worker_id,
        "manager_url": config.manager_url,
        "rendezvous_remote": config.rendezvous_remote,
        "queues": config.queues,
        "priorities": config.priorities,
        "models": config.models,
        "mcps": config.mcps,
        "model_aliases": config.model_aliases,
        "auto_model": config.auto_model,
        "max_model": config.max_model,
        "model_timeout_seconds": config.model_timeout_seconds,
        "ui_host": config.ui_host,
        "ui_port": config.ui_port,
    }
```

> **Import-cycle note:** `config/worker.py` importing `nightshift.backends` is safe — `backends` imports `nightshift.engine` (not config). Verify with `.venv/bin/python -c "import nightshift.config.worker"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_nightshift_worker.py tests/test_config_model.py -q`
Expected: PASS (config + resolution tests)

- [ ] **Step 5: Commit**

```bash
git add src/nightshift/config/worker.py tests/test_nightshift_worker.py
git commit -m "feat(worker-config): qualified models, multi-provider, drop single backend, add timeout"
```

---

## Phase D — Execution: dispatch by provider

### Task D1: `execute_work_order` dispatches by the resolved provider

**Files:**
- Modify: `src/nightshift/worker/execute.py` (the `ExecuteOutcome` dataclass ~line 50, and the body ~lines 199-252)
- Test: `tests/test_nightshift_worker.py` (the full-handshake test + a new dispatch-by-provider test)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_nightshift_worker.py
def test_execute_dispatches_by_model_provider(tmp_path: Path, monkeypatch) -> None:
    """A qualified model routes to that provider's backend; outcome.backend == provider."""
    workspace = build_workspace(tmp_path, tasks={"main": {"00.demo": "Do a thing."}})
    seen: dict[str, Any] = {}

    class _FakeBackend:
        name = "ollama-cloud"
        agentic = False

        def available(self, config=None) -> bool:
            return True

        def run(self, spec, emit_log, should_abort, on_worker_start=None):
            seen["model"] = spec.model
            seen["timeout"] = spec.timeout
            emit_log("ok\n")
            return WorkerResult(returncode=0, turns=1)

    monkeypatch.setattr(backends_mod, "require_backend", lambda p: _FakeBackend())
    cfg = WorkerConfig(
        workspace=workspace, worker_id="w", manager_url="http://x",
        models=["ollama-cloud/gpt-oss:120b"], model_timeout_seconds=42.0,
    )
    order = {
        "task": "00.demo", "repo": "longitude", "queue": "main",
        "body": "Do a thing.", "base_ref": "HEAD",
        "config": {"model": "ollama-cloud/gpt-oss:120b", "validate": ""},
    }
    outcome = execute_work_order(cfg, order, on_phase=lambda _p: None, on_log=lambda _l: None)
    assert seen["model"] == "gpt-oss:120b"      # bare model handed to the backend
    assert seen["timeout"] == 42.0
    assert outcome.backend == "ollama-cloud"     # provider recorded on the outcome
    assert outcome.resolved_model == "ollama-cloud/gpt-oss:120b"


def test_execute_unknown_provider_errors(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, tasks={"main": {"00.demo": "x"}})
    cfg = WorkerConfig(workspace=workspace, worker_id="w", manager_url="http://x",
                       models=["bogus/x"])
    order = {"task": "00.demo", "repo": "longitude", "queue": "main", "body": "x",
             "base_ref": "HEAD", "config": {"model": "bogus/x"}}
    outcome = execute_work_order(cfg, order, on_phase=lambda _p: None, on_log=lambda _l: None)
    assert outcome.status == "error"
    assert outcome.failure_kind in {"backend_unavailable", "model_unavailable"}
```

Update the existing `test_full_handshake_*` test setup: it monkeypatches `backends_mod.get_backend`; change it to monkeypatch `backends_mod.require_backend` instead, and give the work order a qualified model (e.g. `claude-code/claude-sonnet-4-6`). Assert the landed run’s `backend == "claude-code"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightshift_worker.py -k execute_dispatches -q`
Expected: FAIL — `AttributeError: 'ExecuteOutcome' object has no attribute 'backend'`

- [ ] **Step 3: Write minimal implementation**

Add `backend` to `ExecuteOutcome` (after `resolved_model`):

```python
    resolved_model: str
    backend: str = ""
```

Rewrite the backend-selection block in `execute_work_order`. Replace:

```python
    model, model_error = cfg.resolve_model(config_blob.get("model"))
    ...
    backend = get_backend(cfg.backend)
    if not backend.available(config_blob):
        reason = f"backend '{cfg.backend}' is not available on this worker"
        ...
```

with:

```python
    model, model_error = cfg.resolve_model(config_blob.get("model"))
    if model_error:
        return ExecuteOutcome(
            status="error", result_line=model_error, landable=False,
            resolved_model=str(config_blob.get("model") or "auto"),
            failure_kind="model_unavailable", failure_reason=model_error,
            worktree=wt_path,
        )
    assert model is not None
    provider, bare_model = split_model(model)
    if provider is None:
        reason = f"model '{model}' is not provider-qualified (expected provider/model)"
        return ExecuteOutcome(
            status="error", result_line=reason, landable=False,
            resolved_model=model, backend="",
            failure_kind="model_unavailable", failure_reason=reason,
            worktree=wt_path,
        )
    # ... repo availability guard (unchanged) ...
    try:
        backend = require_backend(provider)
    except KeyError:
        reason = f"unknown provider '{provider}' in model '{model}'"
        return ExecuteOutcome(
            status="error", result_line=reason, landable=False,
            resolved_model=model, backend=provider,
            failure_kind="backend_unavailable", failure_reason=reason,
            worktree=wt_path,
        )
    if not backend.available(config_blob):
        reason = f"backend '{provider}' is not available on this worker"
        return ExecuteOutcome(
            status="error", result_line=reason, landable=False,
            resolved_model=model, backend=provider,
            failure_kind="backend_unavailable", failure_reason=reason,
            worktree=wt_path,
        )
```

Build the spec with the **bare** model + timeout, and run:

```python
        spec = WorkerSpec(
            task=task,
            prompt=prompt,
            model=bare_model,
            max_turns=int(max_turns) if max_turns is not None else None,
            cwd=wt_dir,
            env=env,
            config=config_blob,
            timeout=cfg.model_timeout_seconds or None,
        )
        on_log(f"  running worker [{provider}] ({bare_model})...\n")
        result = backend.run(spec, capture_log, lambda: None)
```

Set `backend=provider` and `resolved_model=model` on **every** `ExecuteOutcome` returned after this point (the launch-failed, blocked, error, no-commit, validate-skipped, validate-failed, and landable returns). For the `_finish_landable` returns, add a `backend=provider` argument by extending `_finish_landable` with a `backend: str` param threaded into both `ExecuteOutcome(...)` it builds.

Update imports at the top of `execute.py`:

```python
from nightshift.model_id import split_model
# inside the function's local import line:
from nightshift.backends import LAUNCH_FAILED, WorkerSpec, require_backend
```

(Drop `get_backend` from that local import.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_nightshift_worker.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nightshift/worker/execute.py tests/test_nightshift_worker.py
git commit -m "feat(execute): dispatch by model provider, thread timeout, record provider"
```

---

## Phase E — Worker↔manager wire + manager config

### Task E1: Worker loop + client send a provider summary, record outcome provider

**Files:**
- Modify: `src/nightshift/worker/loop.py` (checkin ~44, run_once poll ~76, `_process` begin ~102-112, `_submit` ~162-227)
- Modify: `src/nightshift/worker/client.py` (`checkin`/`poll` `backend` → optional)
- Test: `tests/test_nightshift_worker.py` (handshake assertions)

- [ ] **Step 1: Write the failing test**

Extend the handshake test to assert the worker advertises filtered qualified models and the run records the provider:

```python
def test_worker_advertises_filtered_models(tmp_path, monkeypatch) -> None:
    # Built in the handshake harness; assert the checkin payload carried only
    # available qualified models and no single backend.
    ...
    assert sent_checkin["models"] == ["claude-code/claude-sonnet-4-6"]
    assert sent_checkin.get("backend") in (None, "claude-code")  # summary only
```

(Adapt to the existing handshake harness’ fake client which records calls.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightshift_worker.py -k advertises_filtered -q`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `loop.py` `checkin` and `run_once`, replace `backend=self.cfg.backend, ... models=self.cfg.models` with:

```python
        resp = self.client.checkin(
            self.cfg.worker_id,
            backend=",".join(sorted(self.cfg.providers())) or None,
            queues=self.cfg.queues,
            priorities=self.cfg.priorities,
            models=self.cfg.advertised_models(),
            mcps=self.cfg.mcps,
            meta={"pid": _safe_pid()},
        )
```

```python
        work = self.client.poll(
            self.cfg.worker_id,
            backend=",".join(sorted(self.cfg.providers())) or None,
            queues=self.cfg.queues,
            priorities=self.cfg.priorities,
            models=self.cfg.advertised_models(),
            mcps=self.cfg.mcps,
        )
```

In `_process`, set the `begin(...)` backend from the order’s model provider (best-effort, may be empty for `auto`):

```python
        from nightshift.model_id import provider_of  # local: avoid top churn
        order_model = str(order.get("config", {}).get("model", "auto"))
        self.local.begin(
            run_id=run_id, task=task, queue=queue, title=title,
            model=order_model, backend=provider_of(order_model) or "",
            repo=repo, branch=branch, worktree=wt,
        )
```

> Per the no-inline-imports rule, hoist `from nightshift.model_id import provider_of` to the top of `loop.py` instead of importing inside `_process`.

In `_submit`, replace both `backend=self.cfg.backend` occurrences with `backend=outcome.backend`.

In `client.py`, change the `backend: str` parameters of `checkin` and `poll` to `backend: str | None = None`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_nightshift_worker.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nightshift/worker/loop.py src/nightshift/worker/client.py tests/test_nightshift_worker.py
git commit -m "feat(worker): advertise qualified provider models, record per-run provider"
```

### Task E2: Manager bodies + dispatch tolerate optional backend, record provider

**Files:**
- Modify: `src/nightshift/manager/app.py` — `CheckinBody.backend`/`PollBody.backend` → optional (~92, ~105); dispatch `create_run` backend (~796-799)
- Test: `tests/test_nightshift_manager.py`

- [ ] **Step 1: Write the failing test**

```python
def test_checkin_without_backend(client) -> None:
    # client = TestClient over create_app; posting checkin with no backend works.
    r = client.post("/api/worker/checkin", json={
        "worker_id": "w1", "queues": None, "priorities": None,
        "models": ["claude-code/claude-opus-4-8"], "mcps": [],
    })
    assert r.status_code == 200
```

(Use the existing manager test harness/fixtures in `tests/test_nightshift_manager.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightshift_manager.py -k without_backend -q`
Expected: FAIL — 422 (validation: backend required)

- [ ] **Step 3: Write minimal implementation**

```python
class CheckinBody(BaseModel):
    worker_id: str
    backend: str | None = None
    ...

class PollBody(BaseModel):
    worker_id: str
    backend: str | None = None
    ...
```

In `worker_poll`’s dispatch, set the run’s provider from the chosen model rather than the worker’s (now-optional) backend. Replace `backend=body.backend, model=order["config"]["model"]` with:

```python
            backend=provider_of(order["config"]["model"]),
            model=order["config"]["model"],
```

Add `from nightshift.model_id import provider_of` to the imports at the top of `manager/app.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_nightshift_manager.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nightshift/manager/app.py tests/test_nightshift_manager.py
git commit -m "feat(manager): optional worker backend on the wire, record provider per run"
```

### Task E3: Manager config cleanup (qualified defaults, drop legacy model fields)

**Files:**
- Modify: `src/nightshift/config/manager.py` — `scheduled_models_allow` default (~66), drop `model`/`cursor_model` (~76-83, ~309-310, ~405-408), qualified `resolve_model` desc (~174)
- Modify: `src/nightshift/spawn_daily.py` — `resolve_frontmatter` (~283-289)
- Test: `tests/test_nightshift_config.py`, `tests/test_spawn_daily.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_nightshift_config.py
def test_scheduled_models_allow_default_is_qualified() -> None:
    from nightshift.config.manager import OperatorConfig
    assert OperatorConfig().scheduled_models_allow == (
        "claude-code/claude-sonnet-4-6", "claude-code/claude-opus-4-8",
    )

def test_resolve_frontmatter_uses_default_model_only() -> None:
    from nightshift.spawn_daily import resolve_frontmatter
    out = resolve_frontmatter({}, {"default_model": "auto"})
    assert out["model"] == "auto"
    out2 = resolve_frontmatter({"model": "cursor/gpt-5"}, {"default_model": "auto"})
    assert out2["model"] == "cursor/gpt-5"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightshift_config.py -k scheduled_models_allow_default_is_qualified -q`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `config/manager.py`:

```python
    scheduled_models_allow: tuple[str, ...] = field(
        default=("claude-code/claude-sonnet-4-6", "claude-code/claude-opus-4-8"),
        metadata=meta(
            category="Scheduling", label="Scheduled models allow",
            desc="Filter: only auto-schedule tasks pinned to these provider/model ids.",
            apply="next-task", type="string_list"))
```

Remove the `model` and `cursor_model` fields from `OperatorConfig`, the `model=data.get("model")`/`cursor_model=data.get("cursor_model")` lines in `load_manager_settings`, the `_as_tuple(... or data.get("scheduled_models"))` legacy alias default (update default tuple), and the `if settings.operator.model is not None:` / `cursor_model` blocks in `save_manager_settings`.

In `spawn_daily.py`:

```python
def resolve_frontmatter(meta: dict, config: dict) -> dict:
    raw_turns = meta.get("turns", config.get("max_turns"))
    return {
        "model": meta.get("model", config.get("default_model", "auto")),
        "max_turns": int(raw_turns) if raw_turns is not None else None,
        "automerge": bool(meta.get("automerge", config.get("automerge", False))),
        ...  # keep the remaining keys exactly as-is
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_nightshift_config.py tests/test_spawn_daily.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nightshift/config/manager.py src/nightshift/spawn_daily.py tests/
git commit -m "feat(manager-config): qualify scheduled models, drop legacy model/cursor_model"
```

---

## Phase F — Templates, samples, docs

### Task F1: Update shipped templates + sample config

**Files:**
- Modify: `src/nightshift/assets/config/worker.json` (drop `backend`; qualified `models`/`auto_model`/`max_model`; add `model_timeout_seconds`)
- Modify: `src/nightshift/assets/config/manager.json` (qualified `scheduled_models_allow`; drop `model`/`cursor_model`)
- Modify: `config.json` (repo root sample — qualified `scheduled_models_allow`, drop `model`/`cursor_model`)
- Test: `tests/test_nightshift_config.py` (template round-trip)

- [ ] **Step 1: Write the failing test**

```python
def test_shipped_worker_template_is_qualified() -> None:
    import json
    from nightshift._paths import asset
    data = json.loads(asset("config", "worker.json").read_text())
    assert "backend" not in data
    assert all("/" in m for m in data.get("models", []))
    assert "/" in data["auto_model"] and "/" in data["max_model"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightshift_config.py -k shipped_worker_template -q`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

`src/nightshift/assets/config/worker.json`:

```json
{
  "worker_id": "",
  "manager_url": "http://localhost:8800",
  "queues": null,
  "priorities": null,
  "models": ["claude-code/claude-sonnet-4-6", "claude-code/claude-opus-4-8"],
  "mcps": [],
  "model_aliases": {},
  "auto_model": "claude-code/claude-sonnet-4-6",
  "max_model": "claude-code/claude-opus-4-8",
  "model_timeout_seconds": 0,
  "ui_host": "0.0.0.0",
  "ui_port": 8810
}
```

Update `src/nightshift/assets/config/manager.json` `scheduled_models_allow` to `["claude-code/claude-sonnet-4-6", "claude-code/claude-opus-4-8"]` and remove any `model`/`cursor_model` keys. Apply the same `scheduled_models_allow` change and key removal to the repo-root `config.json` sample.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_nightshift_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nightshift/assets/config/worker.json src/nightshift/assets/config/manager.json config.json tests/test_nightshift_config.py
git commit -m "chore(templates): qualified model ids in shipped configs + sample"
```

### Task F2: Documentation

**Files:**
- Modify: `docs/configuration-reference.md` (worker keys table, backends section, `.env`, auto/max defaults table, new `model_timeout_seconds`, routing description)
- Modify: `docs/NIGHTSHIFT.md` (the `model` frontmatter row → `provider/model`)

- [ ] **Step 1: Update `docs/configuration-reference.md`**

- Replace the `backend` / `NIGHTSHIFT_WORKER_BACKEND` row with a note that `backend` is removed; providers are derived from advertised `models`.
- Change the `models` row to describe `provider/model` ids and add a `model_timeout_seconds` (env `NIGHTSHIFT_MODEL_TIMEOUT_SECONDS`) row under Models.
- Replace the per-backend `auto`/`max` defaults table with the single defaults (`auto` = `claude-code/claude-sonnet-4-6`, `max` = `claude-code/claude-opus-4-8`) and note these are single qualified ids.
- Update the Backends section to clarify a worker can serve multiple providers concurrently; the provider is chosen per task from the model id.
- Update `scheduled_models_allow` to show qualified ids.

- [ ] **Step 2: Update `docs/NIGHTSHIFT.md`**

Change the `model` frontmatter row to: explicit ids are now `provider/model` (e.g. `cursor/gpt-5`); `auto`/`max` unchanged; an explicit id routes to any worker advertising that exact qualified id.

- [ ] **Step 3: Commit**

```bash
git add docs/configuration-reference.md docs/NIGHTSHIFT.md
git commit -m "docs: provider/model id format, multi-provider workers, model timeout"
```

---

## Phase G — Full sweep + green build

### Task G1: Update remaining tests that assume bare ids / single backend

**Files:**
- Modify: `tests/test_nightshift_ui.py` (backend-registry test already lists names; check model-dropdown/settings tests that assert bare ids or `worker.backend`)
- Modify: any test asserting `("worker", "backend")` in the settings registry (`tests/test_settings_api.py`) — `backend` is gone; assert `("worker", "model_timeout_seconds")` typed `float` and `("worker", "auto_model")` typed `string` instead of `str_map`.
- Modify: `tests/test_nightshift_worker.py` (`test_worker_config_advertises_capabilities` model strings → qualified)

- [ ] **Step 1: Run the full suite to find breakage**

Run: `.venv/bin/python -m pytest -q`
Expected: failures enumerated — fix each to the new contract (qualified ids; no `worker.backend`).

- [ ] **Step 2: Fix each failing assertion** to use qualified ids and the new field set. Do not weaken assertions — update them to the new expected values.

- [ ] **Step 3: Run the full suite to verify green**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all)

- [ ] **Step 4: Lint**

Run: `.venv/bin/ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: update suite for provider-qualified models + multi-provider worker"
```

### Task G2: Manual smoke (optional, per `control-ui` skill)

- [ ] Launch the manager (`just manager`) + a worker (`just worker`) with a `worker.json` advertising two providers (e.g. `claude-code/...` and `ollama-cloud/...`), confirm the Workers page shows both providers and a task pinned to `ollama-cloud/gpt-oss:120b` routes to that worker and runs via the cloud backend.

---

## Self-Review

**Spec coverage:**
- "Combine provider into model as `<provider>/<model>` everywhere" → A1 (primitives), C1 (worker models/auto/max/aliases), D1 (execution), E1/E2 (wire + run records), E3 (scheduled_models_allow, frontmatter), F1 (templates/sample), F2 (docs). ✔
- "Posted to the manager on registration" → E1 (`advertised_models()` in checkin/poll) + E2 (bodies accept it). ✔
- "In the task model selection" → frontmatter `model:` is already free-form; E3 makes `default_model`/`scheduled_models_allow` qualified; the UI dropdown is fed by `models_for_queue` which now returns qualified ids automatically (no code change needed — it unions advertised models). ✔
- "In all settings that use models, remove the separate model field where it makes no sense" → C1 removes `backend` + collapses auto/max maps to single qualified strings; E3 removes manager `model`/`cursor_model`. ✔
- "Worker no longer tied to a single backend; can provide multiple providers/models; update the backend to handle this" → C1 (`providers()`/`advertised_models()`), B1 (`require_backend`), D1 (dispatch by provider). ✔
- "Global timeout in the Models setting" → C1 (`model_timeout_seconds`), B2 (enforcement), D1 (plumb). ✔

**Placeholder scan:** No `TBD`/`handle edge cases`/`similar to`. Every code step shows code. ✔

**Type consistency:** `split_model` returns `(provider, model)` and is used the same way in A1/D1/E1/E2. `resolve_model` returns `(qualified_id, None)` everywhere. `ExecuteOutcome.backend` added in D1 and read in E1. `require_backend` defined B1, used D1. `WorkerSpec.timeout` defined B2, set D1, read by backends B2. ✔

**Known follow-ups (out of scope, called out so they aren’t forgotten):**
- The conflict-resolve path (`OperatorConfig.resolve_model`/`resolve_backend`, `manager/app.py:582`, `manager/resolve_job.py`, `engine.resolve_task`) still uses a separate `resolve_backend`. Left as-is: `resolve_backend` remains an optional explicit override; when unset, derive the provider from a qualified `resolve_model`. If the team wants full removal, add a Task D2 mirroring D1 in the resolve path.
- Postgres `workers.backend` / `runs.backend` columns are unchanged (they now store a provider summary / per-run provider string respectively) — no migration required.
