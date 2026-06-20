"""Local nightshift runner — run task workers via Claude Code CLI.

.. deprecated::
    This in-process single-run path is **deprecated** in favour of the
    manager/worker split: run a co-located manager (``just nightshift-manager``)
    plus one worker (``just nightshift-worker``) for the same single-VM
    behaviour with durable Postgres-backed state, multi-client UI convergence,
    and the manager-owned landing policy. ``just run-tasks-local`` is kept as a
    thin compat shim for the legacy flow; new work should target the manager.
    The engine orchestration core this wraps is still shared with the worker,
    so the module stays for now (and the regression tests below pin its public
    surface), but it is no longer the recommended entry point.

Runs one task at a time, sequentially. After each agent finishes:
- If validate passes: squash-commit to local main, delete worktree.
- If validate fails: delete worktree anyway (main stays clean).

A lockfile prevents concurrent instances from running.

This module is a thin CLI front-end over :mod:`nightshift.engine`. The
orchestration core lives in the engine so the server reuses identical code;
here we just wire stdout/log output and a run-record sink to the engine's
event stream. The names re-exported below are part of the public surface
imported by the regression tests.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TextIO

from nightshift.engine import (
    FAILURE_LOG_DIR,
    MIN_FREE_PCT,
    SYMLINK_TARGETS,
    Controller,
    RunSummary,
    TaskResult,
    _attempt_repair,
    _commit_dispatch,
    _find_autosplit_tasks,
    _write_failure_log,
    acquire_lock,
    build_claude_argv,
    build_prompt,
    build_task_list,
    check_preconditions,
    enough_free_disk,
    extract_result_line,
    list_queue,
    recover_task,
    resolve_task,
    resolve_title,
    run_queue,
    run_task,
    setup_worktree,
    squash_to_main,
    teardown_worktree,
)
from nightshift.events import (
    TASK_LOG,
    TASK_RESULT,
    TASK_STARTED,
    Event,
    RunStore,
)
from nightshift.slack import listener_for_queue


try:
    from dotenv import load_dotenv as _dotenv_load
except ImportError:
    _dotenv_load = None


__all__ = [
    "FAILURE_LOG_DIR",
    "MIN_FREE_PCT",
    "SYMLINK_TARGETS",
    "Controller",
    "RunSummary",
    "TaskResult",
    "_Tee",
    "_attempt_repair",
    "_commit_dispatch",
    "_find_autosplit_tasks",
    "_write_failure_log",
    "acquire_lock",
    "build_claude_argv",
    "build_prompt",
    "build_task_list",
    "check_preconditions",
    "enough_free_disk",
    "extract_result_line",
    "list_queue",
    "load_dotenv",
    "open_run_log",
    "recover_task",
    "resolve_task",
    "resolve_title",
    "run_queue",
    "run_task",
    "setup_worktree",
    "squash_to_main",
    "teardown_worktree",
]


def load_dotenv(root: Path) -> None:
    """Load .env into os.environ without overwriting existing vars."""
    env_file = root / ".env"
    if not env_file.exists():
        return
    if _dotenv_load is not None:
        _dotenv_load(env_file, override=False)
    else:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key not in os.environ:
                os.environ[key] = value


LOG_DIR = ".tasks/logs"


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


def open_run_log(root: Path) -> IO[str]:
    """Create .tasks/logs/ and open a timestamped run log for writing."""
    log_dir = root / LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return (log_dir / f"nightshift-local-{stamp}.log").open(
        "w", encoding="utf-8"
    )


GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


def make_stdout_listener() -> object:
    """Return a listener that prints engine events like the legacy runner."""

    def listener(event: Event) -> None:
        if event.type == TASK_STARTED:
            print(f"[{event.payload.get('task')}]")
        elif event.type == TASK_LOG:
            sys.stdout.write(event.payload.get("line", ""))
        elif event.type == TASK_RESULT:
            status = event.payload.get("status")
            if status == "completed":
                print(f"  {GREEN}landed{RESET} ({event.payload.get('commit_sha')})\n")
            elif status == "error":
                print(f"  {RED}failed{RESET}: {event.payload.get('error')}\n")
            else:
                print(f"  {status}\n")

    return listener


def print_summary(summary: RunSummary) -> None:
    """Print a human-readable summary of all task results."""
    print("\n" + "=" * 60)
    print("Nightshift local — run complete")
    print("=" * 60)

    if summary.landed:
        print(f"\n{GREEN}Landed ({len(summary.landed)}):{RESET}")
        for r in summary.landed:
            print(f"  {r.commit_sha}  {r.title}")

    if summary.failed:
        print(f"\n{RED}Failed ({len(summary.failed)}):{RESET}")
        for r in summary.failed:
            print(f"  {r.task}: {r.error}")
        print(f"\n  Failure logs: {FAILURE_LOG_DIR}/")

    if not summary.results:
        print("\n  No tasks to run.")

    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run nightshift tasks locally via Claude Code CLI."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--task",
        default="all",
        help="Single task name or 'all' to run the full queue",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    load_dotenv(root)

    # Mirror everything from here on to a timestamped log file (tee). Set this
    # up before check_preconditions so the pre-flight validate, lock messages,
    # and any sys.exit failure reason are captured too.
    log_file = open_run_log(root)
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _Tee(orig_stdout, log_file)  # type: ignore[assignment]
    sys.stderr = _Tee(orig_stderr, log_file)  # type: ignore[assignment]
    print(f"Logging this run to {log_file.name}")

    try:
        check_preconditions(root)

        lock_fd = acquire_lock(root)

        print(f"Building task list (task={args.task})...")
        tasks = build_task_list(root, args.task)

        if not tasks:
            print("No tasks to run.")
            os.close(lock_fd)
            return 0

        print(f"Running {len(tasks)} task(s) sequentially: {', '.join(tasks)}\n")

        store = RunStore(root)
        writer = store.start(launched_by="cli")
        slack_listener = listener_for_queue(root, tasks_rel=".tasks", queue=None)
        try:
            summary = run_queue(
                root,
                tasks,
                listeners=[make_stdout_listener(), writer.emit, slack_listener],
                run_id=writer.run_id,
                # `all` drains the live queue (picks up mid-run additions); a
                # single named task stays oneshot.
                follow_queue=(args.task == "all"),
            )
        finally:
            writer.close()

        print_summary(summary)
        os.close(lock_fd)
        return 1 if summary.failed else 0
    finally:
        sys.stdout, sys.stderr = orig_stdout, orig_stderr
        log_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
