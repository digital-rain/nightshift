"""Run a nightshift worker: ``python -m nightshift.worker``.

Starts the poll loop (background thread) and the minimal worker UI (foreground
uvicorn). The only required setting is ``NIGHTSHIFT_MANAGER_URL`` (in ``.env`` or
``config.json.local``); backend + routing are worker-owned.
"""

from __future__ import annotations

import argparse
import threading
from pathlib import Path

import uvicorn

from nightshift.worker.client import ManagerClient
from nightshift.worker.config import load_worker_config
from nightshift.worker.local_store import LocalStore
from nightshift.worker.loop import WorkerLoop
from nightshift.worker.ui_app import create_worker_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--ui-host", default=None)
    parser.add_argument("--ui-port", type=int, default=None)
    parser.add_argument(
        "--no-ui", action="store_true", help="run only the poll loop (no worker UI)"
    )
    args = parser.parse_args(argv)

    cfg = load_worker_config(args.root.resolve())
    local = LocalStore(cfg.root)
    client = ManagerClient(cfg.manager_url, shared_secret=cfg.shared_secret)
    loop = WorkerLoop(cfg, client, local)

    print(
        f"[nightshift-worker] id={cfg.worker_id} backend={cfg.backend} "
        f"manager={cfg.manager_url} queues={cfg.queues} priorities={cfg.priorities}"
    )

    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    if args.no_ui:
        try:
            loop_thread.join()
        except KeyboardInterrupt:
            loop.stop()
        return 0

    app = create_worker_app(cfg, local)
    try:
        uvicorn.run(
            app,
            host=args.ui_host or cfg.ui_host,
            port=args.ui_port or cfg.ui_port,
            log_level="warning",
        )
    finally:
        loop.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
