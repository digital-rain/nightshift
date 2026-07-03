"""Manager configuration — loads from ``.nightshift/manager.json``.

Maintains the same flat ``ManagerConfig`` API for existing callers. The
authoritative model, defaults, and metadata live in
``nightshift.config.manager``; this module re-exports what's needed and
provides the backward-compatible loader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.config.manager import (
    Cadences,
    ManagerSettings,
    OperatorConfig,
    load_manager_settings,
    save_manager_settings,
)
from nightshift.lifecycle import LandingMode


LANDING_MODES = tuple(mode.value for mode in LandingMode)


@dataclass(frozen=True)
class ManagerConfig:
    """Resolved manager settings — backward-compatible flat dataclass.

    Legacy callers access ``cfg.host``, ``cfg.landing_mode``,
    ``cfg.default_model``, ``cfg.tasks_repo``, etc. directly. These are
    loaded from ``.nightshift/manager.json`` + env overrides.
    """

    host: str = "0.0.0.0"
    port: int = 8800
    landing_mode: LandingMode = LandingMode.NONE
    default_model: str = "auto"
    shared_secret: str | None = None
    dsn: str | None = None
    tasks_repo: str = "nightshift-tasks"
    rendezvous_remote: str | None = "origin"
    wip_ref_prefix: str = "nightshift-wip"
    # Consecutive no-progress runs of a task before it is quarantined (held in
    # the queue but skipped by every worker). 0 disables the guard.
    quarantine_threshold: int = 2
    # Base of the exponential retry backoff (seconds). Smoke/tests dial this
    # down so the failure round-trip runs in seconds, not minutes.
    retry_backoff_seconds: float = 60.0
    # Optimistic-concurrency landing knobs (see manager/landing.py).
    max_push_retries: int = 3
    validate_on_integrate: bool = False
    # Conflict-resolution: auto-escalation + out-of-process resolve concurrency.
    auto_resolve: bool = True
    max_concurrent_resolves: int = 1
    cadences: Cadences = field(default_factory=Cadences)
    raw: dict[str, Any] = field(default_factory=dict)


def load_manager_config(workspace: Path) -> ManagerConfig:
    """Resolve manager config from ``.nightshift/manager.json`` + env.

    Reads from the new unified file path. Environment overrides
    (``NIGHTSHIFT_*``) always win. Secrets resolve purely from env/.env.
    """
    settings = load_manager_settings(workspace)
    return ManagerConfig(
        host=settings.host,
        port=settings.port,
        landing_mode=settings.operator.landing_mode,
        default_model=settings.operator.default_model,
        shared_secret=settings.shared_secret,
        dsn=settings.dsn,
        tasks_repo=settings.operator.tasks_repo,
        rendezvous_remote=settings.operator.rendezvous_remote,
        wip_ref_prefix=settings.operator.wip_ref_prefix,
        quarantine_threshold=settings.operator.quarantine_threshold,
        retry_backoff_seconds=settings.operator.retry_backoff_seconds,
        max_push_retries=settings.operator.max_push_retries,
        validate_on_integrate=settings.operator.validate_on_integrate,
        auto_resolve=settings.operator.auto_resolve,
        max_concurrent_resolves=settings.operator.max_concurrent_resolves,
        cadences=settings.cadences,
        raw=settings.raw,
    )


__all__ = [
    "Cadences",
    "LANDING_MODES",
    "ManagerConfig",
    "ManagerSettings",
    "OperatorConfig",
    "load_manager_config",
    "load_manager_settings",
    "save_manager_settings",
]
