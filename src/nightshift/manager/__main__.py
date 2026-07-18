"""Run the nightshift manager: ``python -m nightshift.manager``.

Serves the worker + operator API and the operator UI. Reads host/port from
``.nightshift/manager.json`` with env overrides; state goes to Postgres when
``NIGHTSHIFT_PG_DSN`` is set (via .env or the process environment), else an
in-memory store.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from nightshift.config.manager import load_manager_config
from nightshift.manager.app import create_app
from nightshift.restart import re_exec


def main(argv: list[str] | None = None) -> int:
    # Capture the original CLI args so an operator-requested restart can re-exec
    # this same process with identical flags (host/port/workspace all survive).
    original_args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    # ``expanduser`` first so a ``~``-prefixed value (e.g. from an ``.env`` /
    # ``NIGHTSHIFT_WORKSPACE``, which shells don't tilde-expand) resolves against
    # ``$HOME`` rather than being treated as a relative dir joined to the cwd.
    workspace = args.workspace.expanduser().resolve()
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
    app.state.restart_requested = False
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl-C: uvicorn re-raises after shutting down. Swallow it so the exit
        # is quiet rather than dumping a traceback.
        print("\nNightshift manager stopped.")
        return 0
    # A clean return from server.run() with the restart flag set means the
    # operator pressed "Restart manager" in the UI (the endpoint set
    # should_exit). Re-exec so fresh code + settings are picked up; the Ctrl-C
    # path above returns first and never reaches here.
    if getattr(app.state, "restart_requested", False):
        print("[nightshift-manager] restarting (operator requested)…")
        re_exec("nightshift.manager", original_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
