"""Run the nightshift manager: ``python -m nightshift.manager``.

Serves the worker + operator API and the operator UI. Reads host/port from the
manager config (``config.json`` ``manager`` block) with env overrides; state
goes to Postgres when ``NIGHTSHIFT_PG_DSN`` (or the ``manager.dsn`` block) is
set, else an in-memory store.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from nightshift.manager.app import create_app
from nightshift.manager.config import load_manager_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    workspace = args.workspace.resolve()
    cfg = load_manager_config(workspace)
    app = create_app(workspace)
    # Build the Server explicitly (rather than uvicorn.run) so the SSE endpoint
    # can poll `server.should_exit` and end its stream on Ctrl-C; the graceful
    # timeout is a backstop so any lingering long-lived connection (an open
    # /api/events browser tab) can't hang the process indefinitely.
    config = uvicorn.Config(
        app,
        host=args.host or cfg.host,
        port=args.port or cfg.port,
        log_level="info",
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl-C: uvicorn re-raises after shutting down. Swallow it so the exit
        # is quiet rather than dumping a traceback.
        print("\nNightshift manager stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
