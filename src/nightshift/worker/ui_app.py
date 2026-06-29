"""Minimal worker UI server — Now + History (local) screens + settings.

Exposes a status API over the worker's :class:`LocalStore`, settings
GET/PUT over ``worker.json``, and serves the ``ui-worker/`` SPA.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from nightshift._paths import UI_DIR as SHARED_UI_DIR
from nightshift._paths import WORKER_UI_DIR as UI_DIR
from nightshift.config.validate import build_get_response, validate_delta, write_delta
from nightshift.repos import known_repos
from nightshift.worker.config import WorkerConfig
from nightshift.worker.local_store import LocalStore


def _workspace_tier(workspace: Path) -> dict:
    """Build a read-only 'Workspace' tier for the worker /api/settings response."""
    env_var = "NIGHTSHIFT_WORKSPACE"
    env_val = os.environ.get(env_var)
    return {
        "surface": "workspace",
        "categories": [
            {
                "name": "Workspace",
                "fields": [
                    {
                        "key": "location",
                        "label": "Location",
                        "desc": (
                            "The workspace directory this worker operates in. "
                            f"Set via --workspace flag or {env_var} env var; "
                            "resolved at launch."
                        ),
                        "type": "readonly",
                        "apply": "restart",
                        "store": "—",
                        "default": None,
                        "secret": False,
                        "stored": str(workspace),
                        "effective": str(workspace),
                        "env": env_var,
                        "env_shadowed": bool(env_val),
                    }
                ],
            }
        ],
    }


def create_worker_app(cfg: WorkerConfig, local: LocalStore) -> FastAPI:
    app = FastAPI(title="Nightshift Worker")

    @app.get("/api/info")
    def info() -> JSONResponse:
        return JSONResponse(
            {
                "worker_id": cfg.worker_id,
                "backend": ",".join(sorted(cfg.providers())) or None,
                "queues": cfg.queues,
                "priorities": cfg.priorities,
                "models": cfg.models,
                "mcps": cfg.mcps,
                "manager_url": cfg.manager_url,
                "worker_url": cfg.worker_url,
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

    @app.get("/api/scan-queues")
    def scan_queues() -> JSONResponse:
        repos = known_repos(cfg.workspace)
        return JSONResponse({"queues": repos})

    _WORKER_SURFACES = ["worker"]

    @app.get("/api/settings")
    def get_settings() -> JSONResponse:
        response = build_get_response(cfg.workspace, _WORKER_SURFACES)
        response["tiers"].insert(0, _workspace_tier(cfg.workspace))
        return JSONResponse(response)

    @app.put("/api/settings")
    def put_settings(body: dict[str, Any]) -> JSONResponse:
        allowed = set(_WORKER_SURFACES)
        resolved, errors = validate_delta(body, allowed)
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=400)

        applied_live, restart_required = write_delta(cfg.workspace, resolved)

        return JSONResponse({
            "ok": True,
            "applied_live": applied_live,
            "restart_required": restart_required,
            **build_get_response(cfg.workspace, _WORKER_SURFACES),
        })

    # Shared branding (style.css, logo.png) is reused from the operator UI dir,
    # mounted at /shared so the worker SPA can reference it without duplication.
    if SHARED_UI_DIR.exists():
        app.mount("/shared", StaticFiles(directory=str(SHARED_UI_DIR)), name="shared")
    if UI_DIR.exists():
        app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui-worker")

    return app
