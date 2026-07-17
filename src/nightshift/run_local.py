"""Local one-shot runner — drain a queue through the production manager/worker
split, in a single process invocation.

.. deprecated::
    Prefer a long-running co-located manager (``just manager``) plus a worker
    (``just worker``). This CLI exists for the "run my queue once, right now"
    workflow: it stands up an **ephemeral** in-process manager (real HTTP on a
    loopback port, in-memory store) and one worker loop, runs the queue to
    completion, prints a summary, and exits. Landing policy, retry/quarantine
    ladders, and git authority are the manager's — identical to production.

Ported in Phase 9 from the legacy single-process runner. Features that world
had and this one deliberately drops (the run is ephemeral; the manager path
owns the durable equivalents): Slack listeners, the JSONL run-record sink
(``RunStore``), and direct in-process ``run_task`` orchestration. Failure
bookkeeping now follows production semantics — a failing task gets
``failed: true`` written to its brief's frontmatter by the manager, where the
legacy runner only reported it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, TextIO

import httpx
import uvicorn

from nightshift import playlists, repos
from nightshift.config.manager import load_manager_config
from nightshift.lifecycle import Outcome, RunStatus
from nightshift.manager.app import create_app
from nightshift.manager.store_sqlite import SqliteStore
from nightshift.preflight import acquire_lock, check_preconditions
from nightshift.spawn_daily import load_queue_config
from nightshift.task_files import build_task_list, live_ordered_queue
from nightshift.worker.client import ManagerClient
from nightshift.worker.config import load_worker_config
from nightshift.worker.local_store import LocalStore
from nightshift.worker.loop import WorkerLoop


# Run logs stay local and gitignored under the queue's ``logs/`` in the
# content store (the store's ``.gitignore`` ignores ``*/logs/``), so they are
# never committed and never written into a target repo.
LOG_SUBDIR = "logs"

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

# How long to keep waiting for queued lands / resolve children after the
# worker loop drains. Local git lands are seconds; an agent-driven resolve can
# take minutes.
_SETTLE_TIMEOUT_SECONDS = 900.0


class _Tee:
    """A text stream that fans writes out to several underlying streams.

    Used to mirror the run's stdout/stderr to both the terminal and a log
    file, like ``tee``. Reports ``isatty()`` as ``False`` since the combined
    sink is not a terminal.
    """

    def __init__(self, *streams: TextIO) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()

    def isatty(self) -> bool:
        return False


def open_run_log(tasks_root: Path, tasks_rel: str) -> IO[str]:
    """Create ``<tasks_root>/<queue>/logs/`` and open a timestamped run log.

    The log lives under the content store's gitignored ``logs/`` dir so the run
    transcript is local-only and never committed."""
    log_dir = tasks_root / tasks_rel / LOG_SUBDIR
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return (log_dir / f"nightshift-local-{stamp}.log").open("w", encoding="utf-8")


class _EchoLocalStore(LocalStore):
    """The worker's local store, echoing run progress to stdout like the
    legacy CLI did (task banner, streamed log lines, colored result)."""

    def begin(self, **kw: Any) -> None:
        print(f"[{kw.get('task')}]")
        super().begin(**kw)

    def log(self, line: str) -> None:
        sys.stdout.write(line)
        super().log(line)

    def finish(self, record: dict[str, Any]) -> None:
        status = record.get("status")
        if record.get("landed"):
            print(f"  {GREEN}landed{RESET} ({record.get('commit_sha')})\n")
        elif status == "completed":
            print(f"  completed: {record.get('result_line') or 'no changes'}\n")
        elif status in ("error", "blocked"):
            print(f"  {RED}{status}{RESET}: {record.get('result_line') or ''}\n")
        else:
            print(f"  {status}\n")
        super().finish(record)


class _OneShotLoop(WorkerLoop):
    """A :class:`WorkerLoop` bounded to one attempt per task per run.

    Parity with the legacy runner's ``attempted`` set: when the manager
    re-offers a task this run already attempted (an evergreen land keeps its
    brief and is immediately dispatchable again; the failed-task retry path
    re-admits once its backoff elapses — offers a long-running worker would
    accept), the offer is declined with a neutral ``aborted`` submit — no
    policy counters move, no frontmatter is touched — and the task is blocked
    in the ephemeral store (the same shape as :func:`_hold_unselected`, dying
    with the store) so the drain continues with the rest of the queue instead
    of stranding it.
    """

    def __init__(self, *args: Any, store: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._store = store
        self.attempted: set[tuple[str, str]] = set()

    def _decline(self, order: dict[str, Any]) -> None:
        self._submit(order, Outcome(
            status=RunStatus.ABORTED,
            result_line="run_local: one attempt per task per run",
        ))
        queue = playlists.queue_from_tasks_rel(order.get("queue") or "main")
        asyncio.run(self._store.set_task_state(
            queue, order["task"], "blocked",
            blocked_reason="run_local: already attempted",
        ))

    def _process(self, order: dict[str, Any]) -> dict[str, Any]:
        key = (order.get("queue") or "main", order["task"])
        if key in self.attempted:
            self._decline(order)
            return {}
        self.attempted.add(key)
        # A workflow chain (``next_order``) is the *same* task advancing its
        # cursor, not a re-offer: follow it inline before the attempted guard
        # would otherwise decline the next step. Each chained step re-marks the
        # key (a no-op) and the drain re-polls once the chain ends.
        result = super()._process(order)
        while (chained := result.get("next_order")) is not None:
            result = super()._process(chained)
        return {}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_manager(workspace: Path, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    """Create the manager app pinned to ``port`` and serve it on a daemon
    thread. The port is passed through the config env override so the
    auto-resolve escalation (a subprocess that phones home over HTTP) reaches
    *this* ephemeral manager, not the configured production port."""
    saved = os.environ.get("NIGHTSHIFT_MANAGER_PORT")
    os.environ["NIGHTSHIFT_MANAGER_PORT"] = str(port)
    try:
        # The store is explicitly ephemeral (never the configured DSN): a
        # run_local invocation must not attach to a real manager's database.
        store = SqliteStore()
        asyncio.run(store.init())
        app = create_app(workspace, store=store)
    finally:
        if saved is None:
            os.environ.pop("NIGHTSHIFT_MANAGER_PORT", None)
        else:
            os.environ["NIGHTSHIFT_MANAGER_PORT"] = saved

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not server.started:
        if not thread.is_alive():
            sys.exit("error: the in-process manager failed to start")
        if time.monotonic() > deadline:
            sys.exit("error: timed out waiting for the in-process manager")
        time.sleep(0.02)
    return server, thread


def _hold_unselected(
    store: Any, tasks_root: Path, tasks_rel: str, queue: str | None, wanted: set[str]
) -> None:
    """Single-task mode: block every other currently-queued task in the
    ephemeral store so the scheduler only dispatches the selection. The holds
    are ``retry_eligible=False`` (never re-admitted) and die with the store."""

    async def _apply() -> None:
        for stem in live_ordered_queue(tasks_root, tasks_rel):
            if stem not in wanted:
                await store.set_task_state(
                    queue, stem, "blocked",
                    blocked_reason="run_local: not selected",
                )

    asyncio.run(_apply())


def _settle_runs(base_url: str, secret: str | None) -> list[dict[str, Any]]:
    """Wait for queued lands / resolve children to finish, then return the
    final run rows from the manager's wire surface."""
    headers = {"X-Nightshift-Secret": secret} if secret else {}
    deadline = time.monotonic() + _SETTLE_TIMEOUT_SECONDS
    runs: list[dict[str, Any]] = []
    while True:
        resp = httpx.get(f"{base_url}/api/runs", headers=headers, timeout=30.0)
        resp.raise_for_status()
        runs = resp.json()
        if not any(r.get("status") == "running" for r in runs):
            return runs
        if time.monotonic() > deadline:
            print("warning: timed out waiting for pending lands/resolves to settle")
            return runs
        time.sleep(0.2)


def print_summary(runs: list[dict[str, Any]]) -> None:
    """Print a human-readable summary of all run rows."""
    landed = [r for r in runs if r.get("status") == "completed" and r.get("commit_sha")]
    completed = [r for r in runs if r.get("status") == "completed" and not r.get("commit_sha")]
    failed = [r for r in runs if r.get("status") in ("error", "blocked")]

    print("\n" + "=" * 60)
    print("Nightshift local — run complete")
    print("=" * 60)

    if landed:
        print(f"\n{GREEN}Landed ({len(landed)}):{RESET}")
        for r in landed:
            print(f"  {r.get('commit_sha')}  {r.get('title') or r.get('task')}")

    if completed:
        print(f"\nCompleted without landing ({len(completed)}):")
        for r in completed:
            print(f"  {r.get('task')}: {r.get('result_line') or ''}")

    if failed:
        print(f"\n{RED}Failed ({len(failed)}):{RESET}")
        for r in failed:
            print(f"  {r.get('task')}: {r.get('result_line') or r.get('status')}")
        print("\n  Failure logs: <workspace>/.worktrees/<repo>/failures/")

    if not runs:
        print("\n  No tasks to run.")

    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drain a nightshift queue once through an ephemeral "
        "in-process manager + worker, then exit."
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--task",
        default="all",
        help="Single task name or 'all' to run the full queue",
    )
    parser.add_argument(
        "--queue",
        default="main",
        help="Queue label to drain (default: the main queue)",
    )
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()

    # Briefs + queue config live in the content store ``<workspace>/<tasks_repo>``;
    # git ops run per task in ``<workspace>/<repo>`` (resolved by the manager).
    mgr_cfg = load_manager_config(workspace)  # also loads <workspace>/.env
    tasks_root = workspace / mgr_cfg.tasks_repo
    queue_internal = None if args.queue == "main" else args.queue
    tasks_rel = playlists.tasks_rel(queue_internal)

    # Mirror everything from here on to a timestamped log file (tee). Set this
    # up before check_preconditions so the pre-flight validate, lock messages,
    # and any sys.exit failure reason are captured too.
    log_file = open_run_log(tasks_root, tasks_rel)
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _Tee(orig_stdout, log_file)  # type: ignore[assignment]
    sys.stderr = _Tee(orig_stderr, log_file)  # type: ignore[assignment]
    print(f"Logging this run to {log_file.name}")

    try:
        # Pre-flight (disk + tracked-code WIP + `just validate`) runs against
        # the queue's default target repo when that repo is present. Tasks
        # whose own repo is absent are held (repo_unavailable) by the manager
        # rather than failed, so only a present repo is pre-checked.
        try:
            queue_repo: str | None = repos.resolve_repo(
                None, load_queue_config(tasks_root, tasks_rel).get("repo")
            )
        except repos.RepoConfigError:
            queue_repo = None
        if queue_repo and repos.repo_available(workspace, queue_repo):
            check_preconditions(workspace, queue_repo)

        lock_fd = acquire_lock(workspace)

        # Same startup scan as the legacy CLI: spawns due autosplit dailies
        # (committing the dispatch) and resolves a single task name — including
        # an autosplit source name — to the concrete task stems to run.
        print(f"Building task list (task={args.task})...")
        tasks = build_task_list(tasks_root, args.task, tasks_rel)
        if not tasks:
            print("No tasks to run.")
            os.close(lock_fd)
            return 0
        print(f"Running {len(tasks)} task(s): {', '.join(tasks)}\n")

        port = _free_port()
        server, server_thread = _start_manager(workspace, port)
        manager_url = f"http://127.0.0.1:{port}"
        try:
            store = server.config.app.state.store
            if args.task != "all":
                _hold_unselected(
                    store, tasks_root, tasks_rel, queue_internal, set(tasks)
                )

            wcfg = load_worker_config(workspace)
            wcfg.manager_url = manager_url
            wcfg.worker_id = f"local-{os.getpid()}"
            wcfg.queues = [args.queue]

            client = ManagerClient(manager_url, shared_secret=wcfg.shared_secret)
            try:
                loop = _OneShotLoop(
                    wcfg, client, _EchoLocalStore(workspace), store=store
                )
                loop.checkin()
                while loop.run_once():
                    pass
            finally:
                client.close()

            runs = _settle_runs(manager_url, wcfg.shared_secret)
        finally:
            server.should_exit = True
            server_thread.join(timeout=30.0)

        print_summary(runs)
        os.close(lock_fd)
        return 1 if any(r.get("status") in ("error", "blocked") for r in runs) else 0
    finally:
        sys.stdout, sys.stderr = orig_stdout, orig_stderr
        log_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
