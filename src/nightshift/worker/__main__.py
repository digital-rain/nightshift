"""Run a nightshift worker: ``python -m nightshift.worker``.

Starts the poll loop (background thread) and the minimal worker UI (foreground
uvicorn). The only required setting is ``NIGHTSHIFT_MANAGER_URL`` (in ``.env`` or
``.nightshift/worker.json``); backend + routing are worker-owned.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

import uvicorn

from nightshift.restart import re_exec
from nightshift.worker.client import ManagerClient
from nightshift.worker.config import load_worker_config
from nightshift.worker.local_store import LocalStore
from nightshift.worker.loop import WorkerLoop
from nightshift.worker.ui_app import create_worker_app


def main(argv: list[str] | None = None) -> int:
    # Capture original CLI args so an operator-requested restart re-execs this
    # same process with identical flags.
    original_args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--ui-host", default=None)
    parser.add_argument("--ui-port", type=int, default=None)
    parser.add_argument(
        "--no-ui", action="store_true", help="run only the poll loop (no worker UI)"
    )
    args = parser.parse_args(argv)

    # ``expanduser`` first so a ``~``-prefixed value (e.g. from an ``.env`` /
    # ``NIGHTSHIFT_WORKSPACE``, which shells don't tilde-expand) resolves against
    # ``$HOME`` rather than being treated as a relative dir joined to the cwd.
    workspace = args.workspace.expanduser().resolve()
    # The workspace must exist as a directory at startup. Per-task repo
    # availability is the manager's concern (it pauses unavailable repos), not a
    # worker startup failure.
    if not workspace.is_dir():
        parser.error(f"workspace is not an existing directory: {workspace}")

    cfg = load_worker_config(workspace)
    local = LocalStore(cfg.workspace)
    client = ManagerClient(cfg.manager_url, shared_secret=cfg.shared_secret)
    loop = WorkerLoop(cfg, client, local)

    providers = ",".join(sorted(cfg.providers())) or "?"
    url_tag = f" worker_url={cfg.worker_url}" if cfg.worker_url else ""
    print(
        f"[nightshift-worker] id={cfg.worker_id} providers={providers} "
        f"manager={cfg.manager_url} queues={cfg.queues} priorities={cfg.priorities}"
        f"{url_tag}"
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
    ui_host = args.ui_host or cfg.ui_host
    ui_port = args.ui_port or cfg.ui_port
    print(f"[nightshift-worker] UI running on http://{ui_host}:{ui_port}")
    # Build the Server explicitly (rather than uvicorn.run) so the worker UI can
    # trigger a graceful shutdown from an operator "Restart!" click; the drain
    # poller in ui_app sets ``server.should_exit`` once the worker is idle.
    config = uvicorn.Config(
        app,
        host=ui_host,
        port=ui_port,
        log_level="warning",
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    app.state.restart_requested = False
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl-C: uvicorn re-raises after shutting down; swallow it quietly.
        # ``finally`` stops the loop; return before the re-exec check so a
        # Ctrl-C never re-execs (only an operator "Restart!" does).
        return 0
    finally:
        loop.stop()
    # A clean server exit with the restart flag set means the operator pressed
    # "Restart!" in the worker UI. Join the loop thread briefly, then re-exec.
    if getattr(app.state, "restart_requested", False):
        loop_thread.join(timeout=5)
        print("[nightshift-worker] restarting (operator requested)…")
        re_exec("nightshift.worker", original_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
