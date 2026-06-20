"""Manager configuration — centralized, config-driven, no hardcoded cadences.

The manager owns ``tools/nightshift/config.json``. Cadences (poll / heartbeat /
lease TTL / UI refresh) and the landing policy live under a ``manager`` block
there, overridable by environment variables, honoring invariant 13 (refresh /
polling cadence is config-driven, never hardcoded).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.spawn_daily import load_config


# Landing destinations the manager applies *after* the always-on local
# fast-forward of canonical main (see landing.py).
LANDING_MODES = ("none", "push", "pr")


@dataclass(frozen=True)
class Cadences:
    """Timing knobs shared by manager + workers (seconds, except refresh_ms)."""

    poll_seconds: float = 5.0          # worker idle poll interval
    heartbeat_seconds: float = 10.0    # worker -> manager heartbeat interval
    lease_ttl_seconds: float = 120.0   # lease lifetime before reclaim
    worker_stale_seconds: float = 45.0 # mark a worker offline after this silence
    refresh_ms: int = 20000            # UI safety-poll fallback (SSE is primary)


@dataclass(frozen=True)
class ManagerConfig:
    """Resolved manager settings."""

    host: str = "0.0.0.0"
    port: int = 8800
    landing_mode: str = "none"
    default_model: str = "auto"
    shared_secret: str | None = None
    dsn: str | None = None
    cadences: Cadences = field(default_factory=Cadences)
    raw: dict[str, Any] = field(default_factory=dict)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_manager_config(root: Path) -> ManagerConfig:
    """Resolve manager config from ``config.json`` ``manager`` block + env.

    Environment overrides (operator/deploy-time): ``NIGHTSHIFT_MANAGER_HOST``,
    ``NIGHTSHIFT_MANAGER_PORT``, ``NIGHTSHIFT_LANDING_MODE``,
    ``NIGHTSHIFT_SHARED_SECRET``, ``NIGHTSHIFT_DEFAULT_MODEL``,
    ``NIGHTSHIFT_PG_DSN``.

    The store DSN is Nightshift's own (``NIGHTSHIFT_PG_DSN`` env > ``manager.dsn``
    block); it deliberately does **not** fall back to longitude's ``LONG_PG_DSN``,
    so Nightshift never silently rides on the longitude database. Point it at the
    same DSN explicitly if you want them to share one. When unset, the manager
    uses the in-memory store.
    """
    try:
        cfg = load_config(root)
    except (FileNotFoundError, ValueError):
        cfg = {}
    block = cfg.get("manager", {}) if isinstance(cfg, dict) else {}
    cad = block.get("cadences", {}) if isinstance(block, dict) else {}

    cadences = Cadences(
        poll_seconds=_as_float(cad.get("poll_seconds"), 5.0),
        heartbeat_seconds=_as_float(cad.get("heartbeat_seconds"), 10.0),
        lease_ttl_seconds=_as_float(cad.get("lease_ttl_seconds"), 120.0),
        worker_stale_seconds=_as_float(cad.get("worker_stale_seconds"), 45.0),
        refresh_ms=_as_int(cad.get("refresh_ms"), 20000),
    )

    landing_mode = os.environ.get("NIGHTSHIFT_LANDING_MODE") or block.get("landing_mode") or "none"
    if landing_mode not in LANDING_MODES:
        landing_mode = "none"

    default_model = (
        os.environ.get("NIGHTSHIFT_DEFAULT_MODEL")
        or cfg.get("default_model")
        or "auto"
    )

    dsn = os.environ.get("NIGHTSHIFT_PG_DSN") or block.get("dsn") or None

    return ManagerConfig(
        host=os.environ.get("NIGHTSHIFT_MANAGER_HOST") or block.get("host") or "0.0.0.0",
        port=_as_int(os.environ.get("NIGHTSHIFT_MANAGER_PORT") or block.get("port"), 8800),
        landing_mode=landing_mode,
        default_model=default_model,
        shared_secret=os.environ.get("NIGHTSHIFT_SHARED_SECRET") or block.get("shared_secret"),
        dsn=dsn,
        cadences=cadences,
        raw=cfg if isinstance(cfg, dict) else {},
    )
