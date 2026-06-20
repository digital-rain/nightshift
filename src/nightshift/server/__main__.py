"""Launch the Nightshift UI server.

    python -m nightshift.server [--workspace .] [--port 8799] [--host 127.0.0.1]

The ``--workspace`` parents every git repo Nightshift may touch; briefs + queue
config live in its ``<workspace>/<tasks_repo>`` content store (resolved inside
:func:`create_app`). Port resolution order: --port flag, then NIGHTSHIFT_UI_PORT,
then the player settings file, then the default (8799). Avoids 8765, which the
Cursor IDE binds.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from nightshift.server.app import create_app
from nightshift.server.settings import DEFAULTS, load_settings


def resolve_port(workspace: Path, cli_port: int | None) -> int:
    if cli_port is not None:
        return cli_port
    env_port = os.environ.get("NIGHTSHIFT_UI_PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
    value = load_settings(workspace).get("port", DEFAULTS["port"])
    return int(value) if str(value).isdigit() else int(DEFAULTS["port"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Nightshift UI server.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    workspace = args.workspace.resolve()
    port = resolve_port(workspace, args.port)
    app = create_app(workspace)
    print(f"Nightshift UI → http://{args.host}:{port}  (workspace: {workspace})")
    # Build the Server explicitly (rather than uvicorn.run) so the SSE endpoint
    # can poll `server.should_exit` and end its stream on Ctrl-C; the graceful
    # timeout is a backstop so any lingering long-lived connection can't hang
    # the process indefinitely.
    config = uvicorn.Config(
        app,
        host=args.host,
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl-C: uvicorn re-raises after shutting down. Swallow it so the exit
        # is quiet rather than dumping a traceback.
        print("\nNightshift UI stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
