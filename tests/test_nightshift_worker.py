"""Worker tests (Phase 2): config + model resolution, and a full worker<->manager
handshake that actually lands a commit on main via the manager (co-located).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

import nightshift.backends as backends_mod
from _workspace import build_workspace
from nightshift.backends import WorkerResult, WorkerSpec
from nightshift.manager.app import create_app
from nightshift.manager.store_sqlite import SqliteStore
from nightshift.worker.config import WorkerConfig, load_worker_config
from nightshift.worker.execute import execute_work_order
from nightshift.worker.local_store import LocalStore
from nightshift.worker.loop import WorkerLoop


def _seed(tmp_path: Path, tasks: dict[str, str]) -> Path:
    """Build a two-root workspace seeded with ``tasks`` in the default queue.

    Returns the workspace path. The content store is
    ``<workspace>/nightshift-tasks`` and the default target repo (``longitude``)
    is bound to the ``main`` queue, so a dispatched task lands there.
    """
    return build_workspace(tmp_path, tasks=tasks)


# --------------------------------------------------------------------------- #
# config + model resolution
# --------------------------------------------------------------------------- #


def _cfg(**kw) -> WorkerConfig:
    base: dict[str, Any] = dict(workspace=Path("/tmp"), worker_id="w", manager_url="http://x")
    base.update(kw)
    return WorkerConfig(**base)


def test_worker_config_from_env(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, {})
    monkeypatch.setenv("NIGHTSHIFT_MANAGER_URL", "http://mgr:8800/")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_ID", "worker-x")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_QUEUES", "main,alpha")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_PRIORITIES", "0,1")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_MODELS", "claude-code/claude-opus-4-8, cursor/gpt-5")
    cfg = load_worker_config(tmp_path)
    assert cfg.worker_id == "worker-x"
    assert cfg.manager_url == "http://mgr:8800"  # trailing slash stripped
    assert cfg.queues == ["main", "alpha"]
    assert cfg.priorities == [0, 1]
    assert cfg.models == ["claude-code/claude-opus-4-8", "cursor/gpt-5"]
    assert cfg.providers() == {"claude-code", "cursor"}


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
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _n: None)
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    cfg = _cfg(models=["claude-code/claude-opus-4-8", "ollama-cloud/gpt-oss:120b"])
    assert cfg.advertised_models() == ["ollama-cloud/gpt-oss:120b"]


def test_legacy_backend_qualifies_bare_models(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, {})
    (tmp_path / ".nightshift").mkdir(exist_ok=True)
    (tmp_path / ".nightshift" / "worker.json").write_text(
        '{"backend": "ollama", "models": ["llama3.1", "llama3.1:70b"]}'
    )
    cfg = load_worker_config(tmp_path)
    assert cfg.models == ["ollama/llama3.1", "ollama/llama3.1:70b"]


def test_model_timeout_default_zero() -> None:
    assert _cfg().model_timeout_seconds == 0.0


def test_worker_config_advertises_capabilities(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NIGHTSHIFT_MANAGER_URL", "http://mgr")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_MODELS", "claude-code/claude-opus-4-8, cursor/gpt-5.5")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_MCPS", "slack,github")
    cfg = load_worker_config(tmp_path)
    assert cfg.models == ["claude-code/claude-opus-4-8", "cursor/gpt-5.5"]
    assert cfg.mcps == ["slack", "github"]


# --------------------------------------------------------------------------- #
# ollama-cloud backend
# --------------------------------------------------------------------------- #


class _FakeOllamaResponse:
    """Minimal stand-in for an ``httpx.stream`` context manager."""

    def __init__(self, lines: list[str], status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self._lines = lines

    def __enter__(self) -> _FakeOllamaResponse:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def iter_lines(self):
        yield from self._lines

    def read(self) -> bytes:
        return b""


def test_ollama_cloud_registered() -> None:
    assert "ollama-cloud" in backends_mod.backend_names()
    backend = backends_mod.get_backend("ollama-cloud")
    assert isinstance(backend, backends_mod.OllamaCloudBackend)
    assert backend.agentic is False


def test_ollama_cloud_availability(monkeypatch) -> None:
    backend = backends_mod.OllamaCloudBackend()
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert backend.available({}) is False
    # Either the env var or a config override makes it available.
    assert backend.available({"ollama_cloud_api_key": "k"}) is True
    monkeypatch.setenv("OLLAMA_API_KEY", "env-key")
    assert backend.available({}) is True


def test_ollama_cloud_model_defaults() -> None:
    cfg = _cfg(auto_model="ollama-cloud/gpt-oss:120b", max_model="ollama-cloud/deepseek-v3.1:671b")
    assert cfg.resolve_model("auto") == ("ollama-cloud/gpt-oss:120b", None)
    assert cfg.resolve_model("max") == ("ollama-cloud/deepseek-v3.1:671b", None)
    # explicit ids pass through unchanged.
    assert cfg.resolve_model("ollama-cloud/qwen3-coder:480b") == ("ollama-cloud/qwen3-coder:480b", None)


def test_ollama_cloud_missing_key_errors(monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    spec = WorkerSpec(
        task="t", prompt="hi", model="gpt-oss:120b", max_turns=None,
        cwd=Path("/tmp"), env={}, config={},
    )
    result = backends_mod.OllamaCloudBackend().run(spec, lambda _l: None, lambda: None)
    assert result.returncode == 2
    assert result.error is not None and "OLLAMA_API_KEY" in result.error


def test_ollama_cloud_sends_bearer_to_cloud(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    lines = [
        json.dumps({"response": "Hello"}),
        json.dumps({
            "response": " world", "done": True,
            "prompt_eval_count": 5, "eval_count": 7,
        }),
    ]

    def fake_stream(method, url, *, json=None, headers=None, timeout=None):
        captured.update(method=method, url=url, headers=headers, body=json)
        return _FakeOllamaResponse(lines)

    monkeypatch.setattr(backends_mod.httpx, "stream", fake_stream)
    monkeypatch.setenv("OLLAMA_API_KEY", "test-key-123")
    spec = WorkerSpec(
        task="t", prompt="why is the sky blue?", model="gpt-oss:120b",
        max_turns=None, cwd=Path("/tmp"), env={}, config={},
    )
    logs: list[str] = []
    result = backends_mod.OllamaCloudBackend().run(spec, logs.append, lambda: None)

    assert result.returncode == 0
    assert result.turns == 1
    assert (result.input_tokens, result.output_tokens) == (5, 7)
    assert captured["url"] == "https://ollama.com/api/generate"
    assert captured["headers"] == {"Authorization": "Bearer test-key-123"}
    assert captured["body"]["model"] == "gpt-oss:120b"
    output = "".join(logs)
    assert output.startswith("  [ollama-cloud] gpt-oss:120b @ https://ollama.com")
    assert "Hello world" in output


def test_ollama_cloud_config_overrides_host_and_key(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    lines = [json.dumps({"response": "ok", "done": True})]

    def fake_stream(method, url, *, json=None, headers=None, timeout=None):
        captured.update(url=url, headers=headers, body=json)
        return _FakeOllamaResponse(lines)

    monkeypatch.setattr(backends_mod.httpx, "stream", fake_stream)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    spec = WorkerSpec(
        task="t", prompt="hi", model="auto", max_turns=None,
        cwd=Path("/tmp"), env={},
        config={
            "ollama_cloud_host": "https://proxy.internal/",
            "ollama_cloud_model": "qwen3-coder:480b",
            "ollama_cloud_api_key": "cfg-key",
        },
    )
    result = backends_mod.OllamaCloudBackend().run(spec, lambda _l: None, lambda: None)
    assert result.returncode == 0
    assert captured["url"] == "https://proxy.internal/api/generate"
    assert captured["headers"] == {"Authorization": "Bearer cfg-key"}
    assert captured["body"]["model"] == "qwen3-coder:480b"


# --------------------------------------------------------------------------- #
# Full handshake: poll -> execute (fake backend commits) -> submit -> land
# --------------------------------------------------------------------------- #


class _CommittingBackend:
    """A fake agentic backend that writes a file and commits it in the worktree,
    so the worker produces a landable branch the manager can squash."""

    name = "claude-code"
    agentic = True

    def available(self, config=None) -> bool:
        return True

    def run(self, spec, emit_log, should_abort, on_worker_start=None) -> WorkerResult:
        emit_log(f"fake backend working on {spec.task}\n")
        new_file = spec.cwd / "GENERATED.txt"
        new_file.write_text(f"done by {spec.task}\n")
        subprocess.run(["git", "add", "-A"], cwd=spec.cwd, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"work for {spec.task}"],
            cwd=spec.cwd, check=True, capture_output=True,
        )
        return WorkerResult(returncode=0)


class _LoopClient:
    """Adapts the manager TestClient to the ManagerClient surface the loop uses."""

    def __init__(self, client: TestClient) -> None:
        self._c = client

    def checkin(
        self, worker_id, *, backend, queues, priorities,
        models=None, mcps=None, meta=None,
    ) -> dict[str, Any]:
        return self._c.post(
            "/api/worker/checkin",
            json={"worker_id": worker_id, "backend": backend, "queues": queues,
                  "priorities": priorities, "models": models, "mcps": mcps,
                  "meta": meta},
        ).json()

    def poll(self, worker_id, *, backend, queues, priorities, models=None, mcps=None, exclude_queues=None):
        return self._c.post(
            "/api/worker/poll",
            json={"worker_id": worker_id, "backend": backend, "queues": queues,
                  "priorities": priorities, "models": models, "mcps": mcps,
                  "exclude_queues": exclude_queues},
        ).json()

    def heartbeat(self, worker_id, *, lease_id=None, phase=None) -> None:
        self._c.post(
            "/api/worker/heartbeat",
            json={"worker_id": worker_id, "lease_id": lease_id, "phase": phase},
        )

    def post_events(self, run_id, events) -> None:
        self._c.post(f"/api/worker/runs/{run_id}/events", json={"events": events})

    def submit(self, run_id, payload) -> dict[str, Any]:
        return self._c.post(f"/api/worker/runs/{run_id}/submit", json=payload).json()


def test_worker_lands_a_task_via_manager(tmp_path: Path, monkeypatch) -> None:
    workspace = _seed(tmp_path, {"10.do": "---\nmodel: auto\n---\nDo the thing."})
    repo_root = workspace / "longitude"  # the target repo bound to the main queue
    monkeypatch.setattr(backends_mod, "require_backend", lambda name: _CommittingBackend())

    with TestClient(create_app(workspace, store=SqliteStore())) as tc:
        cfg = WorkerConfig(
            workspace=workspace, worker_id="w1",
            manager_url="http://test",
        )
        local = LocalStore(workspace)
        loop = WorkerLoop(cfg, _LoopClient(tc), local)
        loop.checkin()

        did = loop.run_once()
        assert did is True
        # Phase 7: the submit queues the land on the manager's repo executor
        # and returns; drain before asserting the landed state.
        tc.portal.call(tc.app.state.drain_git_jobs)

        # The task landed on the TARGET repo's main (the generated file is now
        # committed there, not in the workspace or the content store).
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=repo_root, capture_output=True, text=True
        ).stdout
        assert "task: " in log
        assert (repo_root / "GENERATED.txt").exists()

        # The run is recorded completed with a commit + backend/model captured.
        runs = tc.get("/api/runs").json()
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"
        assert runs[0]["commit_sha"]
        assert runs[0]["backend"] == "claude-code"
        # The run carries the target repo it landed against.
        assert runs[0]["repo"] == "longitude"

        # Local worker history reflects the landed run.
        assert local.history()[0]["status"] == "completed"

        # A second poll finds nothing left to do.
        assert loop.run_once() is False


# --------------------------------------------------------------------------- #
# Dispatch by provider
# --------------------------------------------------------------------------- #


def test_execute_dispatches_by_model_provider(tmp_path: Path, monkeypatch) -> None:
    """A qualified model routes to that provider's backend; outcome.backend == provider."""
    workspace = build_workspace(tmp_path, tasks={"00.demo": "Do a thing."})
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
    assert outcome.model == "ollama-cloud/gpt-oss:120b"


def test_execute_passes_through_cache_splits_and_usage_payload(
    tmp_path: Path, monkeypatch
) -> None:
    """The tele dict execute_work_order builds carries the WorkerResult's
    cache-split + raw usage fields through to the Outcome (token-usage-
    granularity plan, wire plumbing)."""
    workspace = build_workspace(tmp_path, tasks={"00.demo": "Do a thing."})
    usage_payload = {"input_tokens": 1000, "cache_read_input_tokens": 300}

    class _FakeBackend:
        name = "nightshift"
        agentic = True

        def available(self, config=None) -> bool:
            return True

        def run(self, spec, emit_log, should_abort, on_worker_start=None):
            return WorkerResult(
                returncode=0, turns=2, input_tokens=1000, output_tokens=50,
                cache_read_input_tokens=300, cache_creation_input_tokens=0,
                usage=usage_payload,
            )

    monkeypatch.setattr(backends_mod, "require_backend", lambda p: _FakeBackend())
    cfg = WorkerConfig(
        workspace=workspace, worker_id="w", manager_url="http://x",
        models=["nightshift/anthropic/claude-sonnet-4-6"],
    )
    order = {
        "task": "00.demo", "repo": "longitude", "queue": "main",
        "body": "Do a thing.", "base_ref": "HEAD",
        "config": {"model": "nightshift/anthropic/claude-sonnet-4-6", "validate": ""},
    }
    outcome = execute_work_order(cfg, order, on_phase=lambda _p: None, on_log=lambda _l: None)
    assert outcome.cache_read_input_tokens == 300
    assert outcome.cache_creation_input_tokens == 0
    assert outcome.usage == usage_payload


# --------------------------------------------------------------------------- #
# Worker-local two-failure backoff
# --------------------------------------------------------------------------- #


def test_worker_backs_off_a_queue_after_two_consecutive_failures(tmp_path: Path) -> None:
    root = _seed(tmp_path, {})
    cfg = load_worker_config(root)
    loop = WorkerLoop(cfg, None, LocalStore(root))
    loop._note_submit_outcome("main", "error")
    assert loop._backoff_queues == set()
    loop._note_submit_outcome("main", "error")
    assert loop._backoff_queues == {"main"}


def test_success_resets_the_backoff_counter(tmp_path: Path) -> None:
    root = _seed(tmp_path, {})
    cfg = load_worker_config(root)
    loop = WorkerLoop(cfg, None, LocalStore(root))
    loop._note_submit_outcome("main", "error")
    loop._note_submit_outcome("main", "completed")
    loop._note_submit_outcome("main", "error")
    assert loop._backoff_queues == set()


def test_backoff_clears_when_queue_absent_from_pauses(tmp_path: Path) -> None:
    root = _seed(tmp_path, {})
    cfg = load_worker_config(root)
    loop = WorkerLoop(cfg, None, LocalStore(root))
    loop._backoff_queues = {"main"}
    loop._sync_backoff_with_manager({})
    assert loop._backoff_queues == set()
