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

from nightshift.engine import WIP_REF_PREFIX, normalize_wip_prefix
from nightshift.repos import DEFAULT_TASKS_REPO
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
    # Name of the content-store repo (a workspace child) holding briefs + queue
    # config; ``tasks_root = workspace / tasks_repo``.
    tasks_repo: str = DEFAULT_TASKS_REPO
    # Git remote name (resolved inside each repo_root) used to fetch a worker's
    # cross-machine task branch and to keep local ``main`` synced to ``origin/main``
    # in PR mode. Default ``origin``; set null to disable (cross-machine submits
    # then fail closed).
    rendezvous_remote: str | None = "origin"
    # WIP namespace a cross-machine worker publishes its validated branch under
    # (``refs/heads/<wip_ref_prefix>/<queue>/<task>``). Operator-configurable so
    # worker push credentials can be scoped to any namespace; read at launch and
    # delivered to workers in the work order. Default :data:`WIP_REF_PREFIX`.
    wip_ref_prefix: str = WIP_REF_PREFIX
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


def load_manager_config(workspace: Path) -> ManagerConfig:
    """Resolve manager config from ``<workspace>/config.json`` ``manager`` block + env.

    Environment overrides (operator/deploy-time): ``NIGHTSHIFT_MANAGER_HOST``,
    ``NIGHTSHIFT_MANAGER_PORT``, ``NIGHTSHIFT_LANDING_MODE``,
    ``NIGHTSHIFT_SHARED_SECRET``, ``NIGHTSHIFT_DEFAULT_MODEL``,
    ``NIGHTSHIFT_PG_DSN``, ``NIGHTSHIFT_TASKS_REPO``,
    ``NIGHTSHIFT_WIP_REF_PREFIX``.

    The store DSN is Nightshift's own (``NIGHTSHIFT_PG_DSN`` env > ``manager.dsn``
    block); it deliberately does **not** fall back to longitude's ``LONG_PG_DSN``,
    so Nightshift never silently rides on the longitude database. Point it at the
    same DSN explicitly if you want them to share one. When unset, the manager
    uses the in-memory store.

    ``tasks_repo`` (the content-store repo name) is a top-level operator key in
    ``<workspace>/config.json`` (env ``NIGHTSHIFT_TASKS_REPO`` wins), defaulting
    to :data:`nightshift.repos.DEFAULT_TASKS_REPO`.
    """
    try:
        cfg = load_config(workspace)
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

    tasks_repo = (
        os.environ.get("NIGHTSHIFT_TASKS_REPO")
        or (cfg.get("tasks_repo") if isinstance(cfg, dict) else None)
        or DEFAULT_TASKS_REPO
    )

    # Env wins over the top-level config key; an unsafe value falls back to the
    # default so a bad operator entry never crashes the manager (the Settings
    # PUT validates strictly and surfaces a 400 at edit time instead).
    raw_wip_prefix = (
        os.environ.get("NIGHTSHIFT_WIP_REF_PREFIX")
        or (cfg.get("wip_ref_prefix") if isinstance(cfg, dict) else None)
        or WIP_REF_PREFIX
    )
    try:
        wip_ref_prefix = normalize_wip_prefix(raw_wip_prefix)
    except ValueError:
        wip_ref_prefix = WIP_REF_PREFIX

    # Env wins; an explicit ``manager.rendezvous_remote: null`` disables it; an
    # absent key defaults to ``origin``.
    env_rendezvous = os.environ.get("NIGHTSHIFT_RENDEZVOUS_REMOTE")
    if env_rendezvous:
        rendezvous_remote: str | None = env_rendezvous
    elif "rendezvous_remote" in block:
        block_rendezvous = block.get("rendezvous_remote")
        rendezvous_remote = str(block_rendezvous) if block_rendezvous else None
    else:
        rendezvous_remote = "origin"

    return ManagerConfig(
        host=os.environ.get("NIGHTSHIFT_MANAGER_HOST") or block.get("host") or "0.0.0.0",
        port=_as_int(os.environ.get("NIGHTSHIFT_MANAGER_PORT") or block.get("port"), 8800),
        landing_mode=landing_mode,
        default_model=default_model,
        shared_secret=os.environ.get("NIGHTSHIFT_SHARED_SECRET") or block.get("shared_secret"),
        dsn=dsn,
        tasks_repo=str(tasks_repo),
        rendezvous_remote=rendezvous_remote,
        wip_ref_prefix=wip_ref_prefix,
        cadences=cadences,
        raw=cfg if isinstance(cfg, dict) else {},
    )
