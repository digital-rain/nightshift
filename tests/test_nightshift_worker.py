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
from nightshift.manager.store import MemoryStore
from nightshift.worker.config import WorkerConfig, load_worker_config
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


def test_worker_config_from_env(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, {})
    monkeypatch.setenv("NIGHTSHIFT_MANAGER_URL", "http://mgr:8800/")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_BACKEND", "ollama")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_ID", "worker-x")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_QUEUES", "main,alpha")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_PRIORITIES", "0,1")
    cfg = load_worker_config(tmp_path)
    assert cfg.worker_id == "worker-x"
    assert cfg.backend == "ollama"
    assert cfg.manager_url == "http://mgr:8800"  # trailing slash stripped
    assert cfg.queues == ["main", "alpha"]
    assert cfg.priorities == [0, 1]


def _cfg(backend: str) -> WorkerConfig:
    return WorkerConfig(
        workspace=Path("/tmp"), worker_id="w", backend=backend, manager_url="http://x"
    )


def test_model_resolution_auto_max_explicit() -> None:
    claude = _cfg("claude-code")
    assert claude.resolve_model("auto") == ("claude-sonnet-4-6", None)
    assert claude.resolve_model("max") == ("claude-opus-4-8", None)
    assert claude.resolve_model(None) == ("claude-sonnet-4-6", None)
    # explicit, matching vendor → passes through.
    assert claude.resolve_model("claude-opus-4-5") == ("claude-opus-4-5", None)


def test_model_resolution_no_vendor_mismatch() -> None:
    # Capability routing only ever hands a worker a model it advertised, so an
    # explicit id always passes through (no vendor-mismatch failure any more).
    ollama = _cfg("ollama")
    model, err = ollama.resolve_model("claude-opus-4-8")
    assert err is None
    assert model == "claude-opus-4-8"


def test_model_aliases_remap_explicit_ids() -> None:
    cfg = WorkerConfig(
        workspace=Path("/tmp"), worker_id="w", backend="gemini", manager_url="http://x",
        model_aliases={"gemini-3-pro": "gemini-3-pro-002"},
    )
    # A mapped id resolves to its target; an unmapped id passes through; auto/max
    # still resolve to the worker's keyword models.
    assert cfg.resolve_model("gemini-3-pro") == ("gemini-3-pro-002", None)
    assert cfg.resolve_model("gemini-2.5-flash") == ("gemini-2.5-flash", None)
    assert cfg.resolve_model("auto")[0] == "gemini-2.5-flash"


def test_worker_config_advertises_capabilities(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NIGHTSHIFT_MANAGER_URL", "http://mgr")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_MODELS", "claude-opus-4-8, gpt-5.5")
    monkeypatch.setenv("NIGHTSHIFT_WORKER_MCPS", "slack,github")
    cfg = load_worker_config(tmp_path)
    assert cfg.models == ["claude-opus-4-8", "gpt-5.5"]
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
    cfg = _cfg("ollama-cloud")
    assert cfg.resolve_model("auto") == ("gpt-oss:120b", None)
    assert cfg.resolve_model("max") == ("deepseek-v3.1:671b", None)
    # explicit ids pass through unchanged.
    assert cfg.resolve_model("qwen3-coder:480b") == ("qwen3-coder:480b", None)


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

    def poll(self, worker_id, *, backend, queues, priorities, models=None, mcps=None):
        return self._c.post(
            "/api/worker/poll",
            json={"worker_id": worker_id, "backend": backend, "queues": queues,
                  "priorities": priorities, "models": models, "mcps": mcps},
        ).json().get("work")

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
    tasks_root = workspace / "nightshift-tasks"
    repo_root = workspace / "longitude"  # the target repo bound to the main queue
    monkeypatch.setattr(backends_mod, "get_backend", lambda name: _CommittingBackend())

    with TestClient(create_app(workspace, store=MemoryStore())) as tc:
        cfg = WorkerConfig(
            workspace=workspace, worker_id="w1", backend="claude-code",
            manager_url="http://test",
        )
        local = LocalStore(workspace)
        loop = WorkerLoop(cfg, _LoopClient(tc), local)
        loop.checkin()

        did = loop.run_once()
        assert did is True

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

        # The task file left the content store's queue (completed tasks leave it).
        assert not (tasks_root / "main/10.do.md").exists()

        # A second poll finds nothing left to do.
        assert loop.run_once() is False
