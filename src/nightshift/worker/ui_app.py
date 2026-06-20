"""Minimal worker UI server — Now + History (local) screens.

Exposes a tiny read-only status API over the worker's :class:`LocalStore` and
serves the shared-branding ``ui-worker/`` SPA. This is intentionally small: the
manager owns the full operator console and durable cross-worker history.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from nightshift._paths import UI_DIR as SHARED_UI_DIR
from nightshift._paths import WORKER_UI_DIR as UI_DIR
from nightshift.worker.config import WorkerConfig
from nightshift.worker.local_store import LocalStore


def create_worker_app(cfg: WorkerConfig, local: LocalStore) -> FastAPI:
    app = FastAPI(title="Nightshift Worker")

    @app.get("/api/info")
    def info() -> JSONResponse:
        return JSONResponse(
            {
                "worker_id": cfg.worker_id,
                "backend": cfg.backend,
                "queues": cfg.queues,
                "priorities": cfg.priorities,
                "models": cfg.models,
                "mcps": cfg.mcps,
                "manager_url": cfg.manager_url,
                "brand_tag": "Nightshift Worker",
                "refresh_ms": cfg.refresh_ms,
            }
        )

    @app.get("/api/now")
    def now() -> JSONResponse:
        return JSONResponse(local.now() or {})

    @app.get("/api/history")
    def history(limit: int = 200) -> JSONResponse:
        return JSONResponse(local.history(limit=limit))

    @app.get("/api/stats")
    def stats() -> JSONResponse:
        return JSONResponse(local.stats())

    # Shared branding (style.css, logo.png) is reused from the operator UI dir,
    # mounted at /shared so the worker SPA can reference it without duplication.
    if SHARED_UI_DIR.exists():
        app.mount("/shared", StaticFiles(directory=str(SHARED_UI_DIR)), name="shared")
    if UI_DIR.exists():
        app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui-worker")

    return app
