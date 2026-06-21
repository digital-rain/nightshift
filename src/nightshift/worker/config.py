"""Worker configuration — loads from ``.nightshift/worker.json``.

Re-exports from the unified config package. Existing callers import
``WorkerConfig`` and ``load_worker_config`` from here unchanged.
"""

from __future__ import annotations

from nightshift.config.worker import (
    DEFAULT_AUTO_MODELS,
    DEFAULT_MAX_MODELS,
    WorkerConfig,
    load_worker_config,
    save_worker_config,
)


__all__ = [
    "DEFAULT_AUTO_MODELS",
    "DEFAULT_MAX_MODELS",
    "WorkerConfig",
    "load_worker_config",
    "save_worker_config",
]
