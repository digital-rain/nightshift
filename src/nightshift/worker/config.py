"""Worker configuration + model resolution (worker-owned).

A worker resolves its identity, backend, routing constraints, and manager
location from (in precedence order, later wins):

1. built-in defaults,
2. ``tools/nightshift/config.json.local`` (gitignored; the worker's own knobs),
3. process environment / ``.env`` (``NIGHTSHIFT_*``).

The worker does **not** read the centralized ``config.json``; per-task config
(forbidden paths, caps, base_ref, validate) arrives in the manager's work order.
The only field every worker must set is ``NIGHTSHIFT_MANAGER_URL``.

Model resolution is worker-owned: the manager sends ``auto`` | ``max`` | an
explicit model id, and the worker maps ``auto``/``max`` to its own most
cost-effective / most capable model. An explicit id passes through the
worker-owned ``model_aliases`` remap (identity by default). There is no
vendor-mismatch failure: capability-based routing only ever hands this worker a
model it advertised.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


LOCAL_CONFIG_REL = "config.json.local"

# Per-backend defaults for the worker-interpreted keywords. ``auto`` favours a
# cost-effective capable model; ``max`` favours the most capable config. These
# are overridable in config.json.local (``auto_model`` / ``max_model`` maps).
DEFAULT_AUTO_MODELS = {
    "claude-code": "claude-sonnet-4-6",
    "cursor": "auto",                 # Cursor's own auto picker
    "gemini": "gemini-2.5-flash",     # cost-effective Gemini
    "anthropic": "claude-sonnet-4-6",
    "ollama": "llama3.1",
}
DEFAULT_MAX_MODELS = {
    "claude-code": "claude-opus-4-8",
    "cursor": "claude-opus-4-8-high",
    "gemini": "gemini-2.5-pro",       # most capable Gemini
    "anthropic": "claude-opus-4-8",
    "ollama": "llama3.1:70b",
}


@dataclass
class WorkerConfig:
    root: Path
    worker_id: str
    backend: str
    manager_url: str
    shared_secret: str | None = None
    queues: list[str] | None = None
    priorities: list[int] | None = None
    # Advertised capabilities sent on every checkin + poll. ``models`` are the
    # request-facing ids this worker accepts (a task pinning one of these routes
    # here); ``mcps`` are the MCP connectors wired into this worker's harness.
    models: list[str] = field(default_factory=list)
    mcps: list[str] = field(default_factory=list)
    # Worker-owned remap applied at execution: a requested model id is translated
    # to the id the harness actually runs. Default is identity (pass-through);
    # use it to absorb upgrades, sunsets, and cross-vendor naming.
    model_aliases: dict[str, str] = field(default_factory=dict)
    auto_models: dict[str, str] = field(default_factory=dict)
    max_models: dict[str, str] = field(default_factory=dict)
    ui_host: str = "0.0.0.0"
    ui_port: int = 8810
    refresh_ms: int = 3000  # UI poll cadence; set from the manager at checkin
    raw: dict = field(default_factory=dict)

    def resolve_model(self, requested: str | None) -> tuple[str | None, str | None]:
        """Resolve a work-order model to the concrete id this worker runs.

        Routing already guaranteed this worker advertised the requested model, so
        there is no vendor-mismatch failure here. ``auto`` / ``max`` map to this
        worker's per-backend keyword model; any explicit id passes through the
        worker-owned ``model_aliases`` remap (identity by default), which absorbs
        upgrades, sunsets, and cross-vendor naming. Always returns ``(model,
        None)``.
        """
        key = (requested or "auto").strip().lower()
        if key in ("", "auto", "default"):
            return self._auto_model(), None
        if key == "max":
            return self._max_model(), None
        return self.model_aliases.get(requested, requested), None

    def _auto_model(self) -> str:
        return self.auto_models.get(self.backend) or DEFAULT_AUTO_MODELS.get(
            self.backend, "auto"
        )

    def _max_model(self) -> str:
        return self.max_models.get(self.backend) or DEFAULT_MAX_MODELS.get(
            self.backend, self._auto_model()
        )


def _load_dotenv(root: Path) -> None:
    """Load ``.env`` into ``os.environ`` (without overriding existing vars)."""
    path = root / ".env"
    if not path.exists():
        return
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _load_local(root: Path) -> dict:
    path = root / LOCAL_CONFIG_REL
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


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


def load_worker_config(root: Path) -> WorkerConfig:
    root = root.resolve()
    _load_dotenv(root)
    local = _load_local(root)

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

    queues = _csv_list(os.environ.get("NIGHTSHIFT_WORKER_QUEUES"))
    if queues is None and isinstance(local.get("queues"), list):
        queues = [str(q) for q in local["queues"]]
    priorities = _int_csv(os.environ.get("NIGHTSHIFT_WORKER_PRIORITIES"))
    if priorities is None and isinstance(local.get("priorities"), list):
        priorities = [int(p) for p in local["priorities"]]

    models = _csv_list(os.environ.get("NIGHTSHIFT_WORKER_MODELS"))
    if models is None and isinstance(local.get("models"), list):
        models = [str(m) for m in local["models"]]
    mcps = _csv_list(os.environ.get("NIGHTSHIFT_WORKER_MCPS"))
    if mcps is None and isinstance(local.get("mcps"), list):
        mcps = [str(m) for m in local["mcps"]]
    model_aliases = (
        dict(local.get("model_aliases", {}))
        if isinstance(local.get("model_aliases"), dict)
        else {}
    )

    return WorkerConfig(
        root=root,
        worker_id=worker_id,
        backend=backend,
        manager_url=manager_url,
        shared_secret=os.environ.get("NIGHTSHIFT_SHARED_SECRET") or local.get("shared_secret"),
        queues=queues,
        priorities=priorities,
        models=models or [],
        mcps=mcps or [],
        model_aliases=model_aliases,
        auto_models=dict(local.get("auto_model", {})) if isinstance(local.get("auto_model"), dict) else {},
        max_models=dict(local.get("max_model", {})) if isinstance(local.get("max_model"), dict) else {},
        ui_host=os.environ.get("NIGHTSHIFT_WORKER_UI_HOST") or local.get("ui_host") or "0.0.0.0",
        ui_port=int(os.environ.get("NIGHTSHIFT_WORKER_UI_PORT") or local.get("ui_port") or 8810),
        raw=local,
    )
