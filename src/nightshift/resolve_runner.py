"""The conflict-resolve driver — diagnose a failed land, then re-squash or
drive an agent to resolve it.

Extracted from the legacy single-process runner in Phase 9: this is the one
piece of that world still in production. The manager spawns
``manager/resolve_job.py`` as a separate OS process, and that job calls
:func:`resolve_task` in the task's *preserved* worktree (rebase onto main →
agent resolves conflicts → re-validate → squash to local main).
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nightshift import backends, playlists
from nightshift.events import (
    TASK_LOG,
    TASK_RESULT,
    TASK_STARTED,
    TASK_STATUS,
    WORKER_STARTED,
    Event,
    Listener,
)
from nightshift.git import GitRunner
from nightshift.git.refs import branch_exists
from nightshift.git.squash import compute_code_loc, squash_to_main
from nightshift.git.worktrees import (
    abort_rebase,
    ensure_worktree_for_branch,
    rebase_in_progress,
    rebase_onto_main,
    teardown_worktree,
    worktree_branch,
)
from nightshift.model_id import split_model
from nightshift.preflight import run_interruptible
from nightshift.prompts import build_resolve_prompt, worker_env
from nightshift.queue_config import DEFAULT_VALIDATE_CMD, resolve_validate_cmd
from nightshift.spawn_daily import (
    resolve_config,
    resolve_frontmatter,
    split_frontmatter,
)
from nightshift.task_files import (
    drop_completed_task,
    materialize_brief,
    task_is_evergreen,
)


def _noop(_event: Event) -> None:
    return None


def select_run_backend(model: str, fallback_backend: str | None) -> tuple[Any, str]:
    """Pick the backend for a (possibly qualified) model id.

    A ``provider/model`` id dispatches to that provider's backend and the bare
    model is what reaches the CLI. Agnostic keywords (``auto``/``max``) and bare
    or unrecognized ids fall back to ``fallback_backend`` (the default backend
    when ``None``) with the id passed through unchanged.

    References :mod:`nightshift.backends` through the module object so test
    monkeypatching of ``backends.require_backend``/``get_backend`` holds.
    """
    provider, bare = split_model(model)
    if provider is not None:
        try:
            return backends.require_backend(provider), bare
        except KeyError:
            pass
    return backends.get_backend(fallback_backend), model


@dataclass
class TaskResult:
    task: str
    title: str
    success: bool
    commit_sha: str | None = None
    # Code lines churned by the landed commit (None when nothing landed).
    loc: int | None = None
    error: str | None = None
    status: str = ""
    result_line: str = ""
    # Classified failure category when ``success`` is False (see events.py).
    failure_kind: str | None = None

    def resolved_status(self) -> str:
        if self.status:
            return self.status
        return "completed" if self.success else "error"


DEFAULT_MAX_RESOLVE_ATTEMPTS = 2


def resolve_task(
    workspace: Path,
    repo: str,
    tasks_root: Path,
    task: str,
    title: str,
    *,
    emit: Listener = _noop,
    config: dict | None = None,
    backend_name: str | None = None,
    abort_reason: object = None,
    queue: str | None = None,
) -> TaskResult:
    """Resolve a task whose validated work failed to land on the target repo's
    ``main`` (``repo_root = workspace / repo``).

    Diagnoses first, then acts:

    * If a plain re-squash now works (a transient blocker has cleared) it lands
      immediately — the cheap recovery path.
    * If ``main`` is still dirty (``recoverable``) it reports that: an agent can't
      touch the operator's unrelated local edits.
    * Otherwise it's a content conflict: an agent rebases the branch onto ``main``,
      resolves the conflicts, re-validates, and squashes — bounded by
      ``max_resolve_attempts``.

    The brief is read from ``tasks_root`` (the content store); rebase/squash run
    in ``repo_root``. Emits ``TASK_STARTED``/``TASK_STATUS``/``TASK_RESULT`` so the
    caller can drive it as a tracked job (live log + ``resolve`` phase). The
    branch is preserved on failure so the operator can still resolve it by hand.
    """
    tasks_rel = playlists.tasks_rel(queue)
    config = config or resolve_config(workspace, tasks_root, tasks_rel)
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)

    task_file = tasks_root / tasks_rel / f"{task}.md"
    meta: dict = {}
    body = ""
    if task_file.exists():
        meta, body = split_frontmatter(task_file.read_text())
    emit(Event(TASK_STARTED, {
        "task": task, "title": title, "repo": repo,
        "frontmatter": {**meta}, "body": body.strip(),
    }))

    if not branch_exists(repo_root, branch):
        error = (
            "nothing to resolve: the task branch no longer exists. "
            "Re-run the task instead."
        )
        emit(Event(TASK_RESULT, {
            "task": task, "status": "error", "error": error, "repo": repo,
            "result_line": "branch gone — re-run the task",
            "failure_kind": "merge_rejected",
        }))
        return TaskResult(
            task=task, title=title, success=False, error=error,
            failure_kind="merge_rejected",
        )

    # 1. Cheap path: re-attempt the squash. Lands transient blockers that cleared.
    emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "commit"}))
    sha, detail, recoverable = squash_to_main(
        workspace, repo, task, title, queue=queue
    )
    if sha is not None:
        loc = compute_code_loc(repo_root, sha)
        teardown_worktree(workspace, repo, task, queue=queue)
        # Backstop queue removal for a landed regular task (see drop_completed_task).
        if not task_is_evergreen(meta, task, config):
            drop_completed_task(tasks_root, task, tasks_rel, queue=queue)
        result_line = f"resolved: landed ({sha})"
        emit(Event(TASK_RESULT, {
            "task": task, "status": "completed", "repo": repo,
            "result_line": result_line, "commit_sha": sha, "loc": loc,
        }))
        return TaskResult(
            task=task, title=title, success=True, commit_sha=sha, loc=loc,
            result_line=result_line,
        )

    if recoverable:
        # Transient blocker (e.g. main has uncommitted edits): not an agent's job.
        emit(Event(TASK_RESULT, {
            "task": task, "status": "error", "error": detail, "repo": repo,
            "result_line": "blocked — clear main, then resolve",
            "recoverable": True, "failure_kind": "merge_rejected",
        }))
        return TaskResult(
            task=task, title=title, success=False, error=detail,
            failure_kind="merge_rejected",
        )

    # 2. Content conflict (or generic merge failure): hand it to the agent.
    return _agent_resolve(
        workspace, repo, tasks_root, task, title,
        conflict_detail=detail, emit=emit, config=config,
        backend_name=backend_name, abort_reason=abort_reason, queue=queue,
    )


def _agent_resolve(
    workspace: Path,
    repo: str,
    tasks_root: Path,
    task: str,
    title: str,
    *,
    conflict_detail: str,
    emit: Listener,
    config: dict,
    backend_name: str | None,
    abort_reason: object = None,
    queue: str | None = None,
) -> TaskResult:
    """Rebase the task branch onto the target repo's ``main`` and drive an agent
    to resolve the conflicts / validation failures, then squash. Bounded by
    config ``max_resolve_attempts``."""
    tasks_rel = playlists.tasks_rel(queue)
    repo_root = workspace / repo

    def _emit_log(line: str) -> None:
        emit(Event(TASK_LOG, {"task": task, "line": line}))

    def _on_worker_start(pid: int) -> None:
        emit(Event(WORKER_STARTED, {"task": task, "pid": pid}))

    def _should_abort() -> str | None:
        return abort_reason() if callable(abort_reason) else None

    worktree_dir = ensure_worktree_for_branch(workspace, repo, task, queue=queue)
    if worktree_dir is None:
        error = "could not prepare the task worktree for resolution"
        emit(Event(TASK_RESULT, {
            "task": task, "status": "error", "error": error, "repo": repo,
            "result_line": "resolve setup failed", "failure_kind": "merge_conflict",
        }))
        return TaskResult(
            task=task, title=title, success=False, error=error,
            failure_kind="merge_conflict",
        )

    max_attempts = int(config.get("max_resolve_attempts", DEFAULT_MAX_RESOLVE_ATTEMPTS))
    validate_cmd = resolve_validate_cmd(config)
    env = worker_env(worktree_dir)
    task_file = tasks_root / tasks_rel / f"{task}.md"
    meta: dict = {}
    body = ""
    if task_file.exists():
        meta, body = split_frontmatter(task_file.read_text())
    # Deliver the brief via a run-scratch file outside the worktree (the brief
    # never enters the target repo).
    scratch = materialize_brief(workspace, repo, task, body, queue=queue)
    resolved = resolve_frontmatter(meta, config)
    backend, model = select_run_backend(
        config.get("resolve_model") or resolved["model"],
        config.get("resolve_backend") or backend_name or config.get("worker_backend"),
    )

    last_error = conflict_detail or "merge conflict"
    for attempt in range(1, max_attempts + 1):
        emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "resolve"}))
        _emit_log(f"  resolve attempt {attempt}/{max_attempts}: rebasing onto main...\n")
        outcome, rebase_detail = rebase_onto_main(worktree_dir)
        if outcome == "error":
            abort_rebase(worktree_dir)
            last_error = f"rebase onto main failed:\n{rebase_detail}"
            continue

        if outcome == "conflict":
            _emit_log("  conflicts detected — running resolver agent...\n")
            context = (
                f"{conflict_detail}\n\n"
                "A `git rebase main` is in progress in this worktree and has "
                f"paused on conflicts:\n{rebase_detail}"
            )
            spec = backends.WorkerSpec(
                task=task,
                prompt=build_resolve_prompt(task, task_file=str(scratch), context=context),
                model=model,
                max_turns=resolved["max_turns"],
                cwd=worktree_dir,
                env=env,
                config=config,
            )
            worker = backend.run(
                spec, _emit_log, _should_abort, on_worker_start=_on_worker_start
            )
            if worker.aborted is not None:
                if rebase_in_progress(worktree_dir):
                    abort_rebase(worktree_dir)
                emit(Event(TASK_RESULT, {
                    "task": task, "status": worker.aborted, "repo": repo,
                }))
                return TaskResult(
                    task=task, title=title, success=False, status=worker.aborted,
                )
            if worker.returncode == backends.LAUNCH_FAILED:
                error = (
                    f"{worker.error}. Add the worker binary to PATH or set its "
                    "'*_bin' in config.json."
                )
                if rebase_in_progress(worktree_dir):
                    abort_rebase(worktree_dir)
                emit(Event(TASK_RESULT, {
                    "task": task, "status": "error", "error": error, "repo": repo,
                    "result_line": "worker executable not found",
                    "failure_kind": "worker_launch",
                }))
                return TaskResult(
                    task=task, title=title, success=False, error=error,
                    failure_kind="worker_launch",
                )
            if rebase_in_progress(worktree_dir):
                abort_rebase(worktree_dir)
                last_error = "resolver did not finish the rebase (conflicts remain)"
                continue

        # Rebase complete (clean or resolved) — validate (unless the queue opted
        # out with an empty validate command), then squash.
        if validate_cmd is None:
            _emit_log("  validation disabled for this queue — skipping.\n")
        else:
            emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "validate"}))
            _emit_log(f"  running {' '.join(validate_cmd)}...\n")
            validate_result = subprocess.run(
                validate_cmd, cwd=worktree_dir, capture_output=True, text=True, env=env,
            )
            if validate_result.returncode != 0:
                _emit_log("  validate failed — attempting auto-repair...\n")
                validate_result = attempt_repair(
                    worktree_dir, validate_result, validate_cmd=validate_cmd, env=env,
                )
            if validate_result.returncode != 0:
                _emit_log(f"\n── validate failed (exit {validate_result.returncode}) ──\n")
                if validate_result.stdout:
                    _emit_log(validate_result.stdout[-3000:])
                if validate_result.stderr:
                    _emit_log(validate_result.stderr[-1500:])
                _emit_log("\n── end validate output ──\n")
                last_error = (
                    "validate failed after resolution:\n"
                    f"{validate_result.stdout[-1500:]}\n{validate_result.stderr[-1500:]}"
                )
                continue

        emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "commit"}))
        sha, squash_detail, _recoverable = squash_to_main(
            workspace, repo, task, title, queue=queue,
        )
        if sha is not None:
            loc = compute_code_loc(repo_root, sha)
            teardown_worktree(workspace, repo, task, queue=queue)
            # Backstop queue removal for a landed regular task (see drop_completed_task).
            if not task_is_evergreen(meta, task, config):
                drop_completed_task(tasks_root, task, tasks_rel, queue=queue)
            result_line = f"resolved: landed ({sha})"
            emit(Event(TASK_RESULT, {
                "task": task, "status": "completed", "repo": repo,
                "result_line": result_line, "commit_sha": sha, "loc": loc,
            }))
            return TaskResult(
                task=task, title=title, success=True, commit_sha=sha, loc=loc,
                result_line=result_line,
            )
        last_error = squash_detail or "squash-merge still failed after resolution"

    error = f"auto-resolve failed after {max_attempts} attempt(s):\n{last_error}"
    write_failure_log(workspace, repo, worktree_dir, task, error)
    emit(Event(TASK_RESULT, {
        "task": task, "status": "error", "error": error, "repo": repo,
        "result_line": "auto-resolve failed — manual resolution needed",
        "recoverable": False, "failure_kind": "merge_conflict",
    }))
    return TaskResult(
        task=task, title=title, success=False, error=error,
        failure_kind="merge_conflict",
    )


def write_failure_log(
    workspace: Path,
    repo: str,
    worktree_dir: Path,
    task: str,
    error: str,
    *,
    validate_stdout: str = "",
    validate_stderr: str = "",
) -> Path:
    """Write a terse failure log so repeated failures are diagnosable. Logs live
    under the workspace-level worktree area for the repo
    (``<workspace>/.worktrees/<repo>/failures/<task>.log``), never in the target
    repo."""
    log_dir = workspace / ".worktrees" / repo / "failures"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task}.log"

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"{timestamp}  task={task}",
        f"error: {error.splitlines()[0][:200]}",
    ]

    if validate_stderr:
        last_lines = [l for l in validate_stderr.strip().splitlines() if l.strip()][-3:]
        lines.append("stderr: " + " | ".join(last_lines)[:300])
    elif validate_stdout:
        last_lines = [l for l in validate_stdout.strip().splitlines() if l.strip()][-3:]
        lines.append("stdout: " + " | ".join(last_lines)[:300])

    log_path.write_text("\n".join(lines) + "\n")
    print(f"  failure log: {log_path}")
    return log_path


def attempt_repair(
    worktree_dir: Path,
    failed_result: subprocess.CompletedProcess[str],
    *,
    validate_cmd: list[str] | None = None,
    env: dict[str, str] | None = None,
    should_abort=None,
) -> subprocess.CompletedProcess[str]:
    """Run deterministic auto-fixes and retry validate once (interruptibly).

    Ruff is run over the whole worktree (``.``); it honors the target repo's own
    ``pyproject.toml`` / ``ruff.toml`` (selected rules, ``exclude`` globs), so the
    repair pass stays correct without the resolver knowing the repo's layout.
    """
    subprocess.run(
        [".venv/bin/ruff", "check", "--fix", "--unsafe-fixes", "."],
        cwd=worktree_dir,
        capture_output=True,
    )
    subprocess.run(
        [".venv/bin/ruff", "format", "."],
        cwd=worktree_dir,
        capture_output=True,
    )
    git = GitRunner(worktree_dir)
    dirty = git.run("status", "--porcelain")
    if dirty.stdout.strip():
        # Best-effort commit of whatever ruff fixed: the validate retry below
        # is the real gate, whether or not the autofix commit landed.
        git.run("add", "-A")
        git.run("commit", "-m", "autofix: ruff check --fix + format")
        print("  applied ruff auto-fixes, retrying validate...")
    else:
        print("  no auto-fixable issues; retrying validate...")

    return run_interruptible(
        validate_cmd or shlex.split(DEFAULT_VALIDATE_CMD),
        cwd=worktree_dir,
        env=env,
        should_abort=should_abort,
    )
