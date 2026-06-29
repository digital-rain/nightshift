"""Nightshift configuration — typed dataclasses as the single source of truth.

This package owns the config dataclasses (shape, defaults, editor metadata),
file I/O, and .env handling. Manager, worker, and player share one mechanism.
"""

from nightshift.config.manager import Cadences, ManagerSettings, OperatorConfig
from nightshift.config.meta import FieldMeta, assert_complete, meta
from nightshift.config.player import PlayerConfig
from nightshift.config.registry import FieldSpec, build_registry, emit_json_schema
from nightshift.config.worker import NightshiftBackendConfig, WorkerConfig


assert_complete(
    ManagerSettings, Cadences, OperatorConfig, WorkerConfig,
    NightshiftBackendConfig, PlayerConfig,
)

__all__ = [
    "Cadences",
    "FieldMeta",
    "FieldSpec",
    "ManagerSettings",
    "NightshiftBackendConfig",
    "OperatorConfig",
    "PlayerConfig",
    "WorkerConfig",
    "assert_complete",
    "build_registry",
    "emit_json_schema",
    "meta",
]
