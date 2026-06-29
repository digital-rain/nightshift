"""Worker configuration model — .nightshift/worker.json.

A worker resolves its identity, routing constraints, and manager
location from: built-in defaults → .nightshift/worker.json → environment.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift import backends as backends_mod
from nightshift.config.io import load_dotenv, load_json, save_json, worker_json_path
from nightshift.config.meta import meta
from nightshift.model_id import is_qualified, join_model, provider_of


DEFAULT_AUTO_MODEL = "claude-code/claude-sonnet-4-6"
DEFAULT_MAX_MODEL = "claude-code/claude-opus-4-8"


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
    mcps: list[str] = field(default_factory=list, metadata=meta(
        category="Routing", label="MCPs",
        desc="MCP connectors wired into this worker's harness.",
        apply="restart", env="NIGHTSHIFT_WORKER_MCPS", type="string_list"))

    models: list[str] = field(default_factory=list, metadata=meta(
        category="Models", label="Available",
        desc="Qualified model ids this worker advertises, as provider/model "
             "(e.g. claude-code/claude-opus-4-8, ollama-cloud/gpt-oss:120b).",
        apply="restart", env="NIGHTSHIFT_WORKER_MODELS", type="string_list",
        validate="model_id_list"))
    model_aliases: dict[str, str] = field(default_factory=dict, metadata=meta(
        category="Models", label="Model aliases",
        desc="Remap {requested: actual} (both provider/model) applied at execution.",
        apply="restart", type="str_map", validate="model_id_map"))
    auto_model: str = field(default=DEFAULT_AUTO_MODEL, metadata=meta(
        category="Models", label="Auto model",
        desc="Qualified model 'auto' resolves to (provider/model).",
        apply="restart", validate="model_id"))
    max_model: str = field(default=DEFAULT_MAX_MODEL, metadata=meta(
        category="Models", label="Max model",
        desc="Qualified model 'max' resolves to (provider/model).",
        apply="restart", validate="model_id"))
    model_timeout_seconds: float = field(default=0.0, metadata=meta(
        category="Models", label="Model timeout seconds",
        desc="Global wall-clock bound for any backend run. 0 = no timeout.",
        apply="restart", env="NIGHTSHIFT_MODEL_TIMEOUT_SECONDS"))

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


def _qualify(models: list[str], legacy_backend: str | None) -> list[str]:
    """Prefix bare ids with a legacy single-backend, leaving qualified ids."""
    if not legacy_backend:
        return models
    return [m if is_qualified(m) else join_model(legacy_backend, m) for m in models]


def load_worker_config(workspace: Path) -> WorkerConfig:
    """Resolve worker config from ``.nightshift/worker.json`` + env."""
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
        if isinstance(local.get("model_aliases"), dict)
        else {}
    )

    def _legacy_single(value: Any, default: str) -> str:
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


def save_worker_config(workspace: Path, config: WorkerConfig) -> None:
    """Persist a WorkerConfig to ``.nightshift/worker.json``.

    Secrets (shared_secret) are never written to the JSON file.
    """
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
    save_json(worker_json_path(workspace), data)
