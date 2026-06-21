"""Worker configuration model — .nightshift/worker.json.

A worker resolves its identity, backend, routing constraints, and manager
location from: built-in defaults → .nightshift/worker.json → environment.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.config.io import load_dotenv, load_json, save_json, worker_json_path
from nightshift.config.meta import meta


DEFAULT_AUTO_MODELS: dict[str, str] = {
    "claude-code": "claude-sonnet-4-6",
    "cursor": "auto",
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-sonnet-4-6",
    "ollama": "llama3.1",
}
DEFAULT_MAX_MODELS: dict[str, str] = {
    "claude-code": "claude-opus-4-8",
    "cursor": "claude-opus-4-8-high",
    "gemini": "gemini-2.5-pro",
    "anthropic": "claude-opus-4-8",
    "ollama": "llama3.1:70b",
}


@dataclass
class WorkerConfig:
    """Resolved worker settings from .nightshift/worker.json + env."""

    workspace: Path = field(metadata=meta(
        category="Identity & connection", label="Workspace",
        desc="Workspace directory (launch input).",
        apply="restart", editable=False))
    worker_id: str = field(default="", metadata=meta(
        category="Identity & connection", label="Worker ID",
        desc="Stable identity; must be unique per worker.",
        apply="restart", env="NIGHTSHIFT_WORKER_ID"))
    backend: str = field(default="claude-code", metadata=meta(
        category="Identity & connection", label="Backend",
        desc="Which backend this worker runs.",
        apply="restart", env="NIGHTSHIFT_WORKER_BACKEND"))
    manager_url: str = field(default="http://localhost:8800", metadata=meta(
        category="Identity & connection", label="Manager URL",
        desc="Manager location (required).",
        apply="restart", env="NIGHTSHIFT_MANAGER_URL"))
    shared_secret: str | None = field(default=None, metadata=meta(
        category="Identity & connection", label="Shared secret",
        desc="Must match the manager's secret if one is set.",
        apply="restart", secret=True, env="NIGHTSHIFT_SHARED_SECRET"))
    rendezvous_remote: str | None = field(default=None, metadata=meta(
        category="Identity & connection", label="Rendezvous remote",
        desc="Git remote for cross-machine landing; null = co-located.",
        apply="restart", env="NIGHTSHIFT_RENDEZVOUS_REMOTE"))

    queues: list[str] | None = field(default=None, metadata=meta(
        category="Routing", label="Queues",
        desc="Queue labels this worker serves (null/empty = any).",
        apply="restart", env="NIGHTSHIFT_WORKER_QUEUES", type="string_list"))
    priorities: list[int] | None = field(default=None, metadata=meta(
        category="Routing", label="Priorities",
        desc="0–5 priority levels this worker accepts (null/empty = any).",
        apply="restart", env="NIGHTSHIFT_WORKER_PRIORITIES", type="int_list"))
    models: list[str] = field(default_factory=list, metadata=meta(
        category="Routing", label="Models",
        desc="Request-facing model ids this worker advertises.",
        apply="restart", env="NIGHTSHIFT_WORKER_MODELS", type="string_list"))
    mcps: list[str] = field(default_factory=list, metadata=meta(
        category="Routing", label="MCPs",
        desc="MCP connectors wired into this worker's harness.",
        apply="restart", env="NIGHTSHIFT_WORKER_MCPS", type="string_list"))

    model_aliases: dict[str, str] = field(default_factory=dict, metadata=meta(
        category="Models", label="Model aliases",
        desc="Remap {requested: actual} applied at execution.",
        apply="restart", type="str_map"))
    auto_model: dict[str, str] = field(default_factory=dict, metadata=meta(
        category="Models", label="Auto model",
        desc="Overrides the model 'auto' resolves to, per backend.",
        apply="restart", type="str_map"))
    max_model: dict[str, str] = field(default_factory=dict, metadata=meta(
        category="Models", label="Max model",
        desc="Overrides the model 'max' resolves to, per backend.",
        apply="restart", type="str_map"))

    ui_host: str = field(default="0.0.0.0", metadata=meta(
        category="UI & Network", label="UI host",
        desc="Worker UI bind address.",
        apply="restart", env="NIGHTSHIFT_WORKER_UI_HOST"))
    ui_port: int = field(default=8810, metadata=meta(
        category="UI & Network", label="UI port",
        desc="Worker UI bind port.",
        apply="restart", env="NIGHTSHIFT_WORKER_UI_PORT"))

    refresh_ms: int = field(default=3000, metadata=meta(
        category="UI & Network", label="Refresh ms",
        desc="UI poll cadence; set from the manager at checkin.",
        apply="restart", editable=False))
    raw: dict[str, Any] = field(default_factory=dict, metadata=meta(
        category="Internal", label="Raw", desc="Raw dict.",
        apply="restart", editable=False))

    def resolve_model(self, requested: str | None) -> tuple[str | None, str | None]:
        """Resolve a work-order model to the concrete id this worker runs."""
        key = (requested or "auto").strip().lower()
        if key in ("", "auto", "default"):
            return self._auto_model(), None
        if key == "max":
            return self._max_model(), None
        return self.model_aliases.get(requested, requested), None

    def _auto_model(self) -> str:
        return self.auto_model.get(self.backend) or DEFAULT_AUTO_MODELS.get(
            self.backend, "auto"
        )

    def _max_model(self) -> str:
        return self.max_model.get(self.backend) or DEFAULT_MAX_MODELS.get(
            self.backend, self._auto_model()
        )


def _csv_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


def _int_csv(value: str | None) -> list[int] | None:
    items = _csv_list(value)
    if items is None:
        return None
    out: list[int] = []
    for v in items:
        try:
            out.append(int(v))
        except ValueError:
            continue
    return out or None


def load_worker_config(workspace: Path) -> WorkerConfig:
    """Resolve worker config from ``.nightshift/worker.json`` + env."""
    workspace = workspace.resolve()
    load_dotenv(workspace)
    local = load_json(worker_json_path(workspace))

    backend = (
        os.environ.get("NIGHTSHIFT_WORKER_BACKEND")
        or local.get("backend")
        or "claude-code"
    )
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
    mcps_raw = _csv_list(os.environ.get("NIGHTSHIFT_WORKER_MCPS"))
    if mcps_raw is None and isinstance(local.get("mcps"), list):
        mcps_raw = [str(m) for m in local["mcps"]]

    model_aliases = (
        dict(local.get("model_aliases", {}))
        if isinstance(local.get("model_aliases"), dict)
        else {}
    )

    return WorkerConfig(
        workspace=workspace,
        worker_id=worker_id,
        backend=backend,
        manager_url=manager_url,
        shared_secret=os.environ.get("NIGHTSHIFT_SHARED_SECRET") or None,
        rendezvous_remote=(
            os.environ.get("NIGHTSHIFT_RENDEZVOUS_REMOTE")
            or local.get("rendezvous_remote")
            or None
        ),
        queues=queues_raw if queues_raw else None,
        priorities=priorities_raw if priorities_raw else None,
        models=models_raw or [],
        mcps=mcps_raw or [],
        model_aliases=model_aliases,
        auto_model=(
            dict(local.get("auto_model", {}))
            if isinstance(local.get("auto_model"), dict) else {}
        ),
        max_model=(
            dict(local.get("max_model", {}))
            if isinstance(local.get("max_model"), dict) else {}
        ),
        ui_host=os.environ.get("NIGHTSHIFT_WORKER_UI_HOST") or local.get("ui_host") or "0.0.0.0",
        ui_port=int(os.environ.get("NIGHTSHIFT_WORKER_UI_PORT") or local.get("ui_port") or 8810),
        raw=local,
    )


def save_worker_config(workspace: Path, config: WorkerConfig) -> None:
    """Persist a WorkerConfig to ``.nightshift/worker.json``.

    Secrets (shared_secret) are never written to the JSON file.
    """
    data: dict[str, Any] = {
        "worker_id": config.worker_id,
        "backend": config.backend,
        "manager_url": config.manager_url,
        "rendezvous_remote": config.rendezvous_remote,
        "queues": config.queues,
        "priorities": config.priorities,
        "models": config.models,
        "mcps": config.mcps,
        "model_aliases": config.model_aliases,
        "auto_model": config.auto_model,
        "max_model": config.max_model,
        "ui_host": config.ui_host,
        "ui_port": config.ui_port,
    }
    save_json(worker_json_path(workspace), data)
