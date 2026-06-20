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
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    cfg = load_manager_config(root)
    app = create_app(root)
    uvicorn.run(
        app,
        host=args.host or cfg.host,
        port=args.port or cfg.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
