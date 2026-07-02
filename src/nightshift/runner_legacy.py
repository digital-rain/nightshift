"""The pre-split single-process runner — ``Controller`` / ``run_task`` /
``run_queue`` plus the recover/resolve drivers and their legacy result shapes.

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration.
Explicitly legacy: retired in Phase 9 (the manager/worker split is the
production path); consumed by :mod:`nightshift.run_local` and
:mod:`nightshift.server` until then.
"""

from __future__ import annotations

import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nightshift import playlists, repos
from nightshift.events import (
    RUN_FINISHED,
    RUN_STARTED,
    TASK_LOG,
    TASK_RESULT,
    TASK_STARTED,
    TASK_STATUS,
    WORKER_STARTED,
    Event,
    Listener,
)
from nightshift.git import GitError, GitRunner
from nightshift.git.refs import branch_exists
from nightshift.git.squash import compute_code_loc, squash_failure_kind, squash_to_main
from nightshift.git.worktrees import (
    abort_rebase,
    ensure_worktree_for_branch,
    has_commits,
    rebase_in_progress,
    rebase_onto_main,
    setup_worktree,
    teardown_worktree,
    worktree_branch,
)
from nightshift.lifecycle import FailureKind
from nightshift.model_id import split_model
from nightshift.preflight import run_interruptible
from nightshift.prompts import (
    build_prompt,
    build_resolve_prompt,
    extract_result_line,
    worker_env,
)
from nightshift.queue_config import DEFAULT_VALIDATE_CMD, resolve_validate_cmd
from nightshift.spawn_daily import (
    is_completed,
    is_disabled,
    is_quarantined,
    load_config,
    load_queue_config,
    resolve_config,
    resolve_frontmatter,
    split_frontmatter,
)
from nightshift.task_files import (
    drop_completed_task,
    harvest_split_output,
    live_ordered_queue,
    materialize_brief,
    resolve_title,
    split_output_dir,
    task_is_evergreen,
)


def _noop(_event: Event) -> None:
    return None


def select_run_backend(model: str, fallback_backend: str | None) -> tuple[Any, str]:
    """Pick the backend for a (possibly qualified) model in the legacy run path.

    A ``provider/model`` id dispatches to that provider's backend and the bare
    model is what reaches the CLI. Agnostic keywords (``auto``/``max``) and bare
    or unrecognized ids fall back to ``fallback_backend`` (the default backend
    when ``None``) with the id passed through unchanged.

    Imported lazily to avoid the backends<->engine import cycle (backends reuses
    engine's claude argv/bin helpers).
    """
    from nightshift.backends import get_backend, require_backend

    provider, bare = split_model(model)
    if provider is not None:
        try:
            return require_backend(provider), bare
        except KeyError:
            pass
    return get_backend(fallback_backend), model


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


@dataclass
class RunSummary:
    results: list[TaskResult] = field(default_factory=list)

    @property
    def landed(self) -> list[TaskResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[TaskResult]:
        return [r for r in self.results if not r.success and r.resolved_status() == "error"]


DEFAULT_MAX_RESOLVE_ATTEMPTS = 2


def recover_task(
    workspace: Path, repo: str, task: str, title: str, *, queue: str | None = None
) -> TaskResult:
    """Re-attempt the squash-merge for a task whose validate passed but whose
    merge to the target repo's ``main`` failed (typically a dirty tree at the
    time).

    The worktree branch is preserved on such failures precisely so this cheap
    recovery is possible without re-running the worker. On success the branch
    and worktree are torn down; on failure they are left in place so the user
    can fix the blocker (e.g. commit their work) and retry again.
    """
    branch = worktree_branch(task, queue)
    repo_root = workspace / repo
    if not branch_exists(repo_root, branch):
        return TaskResult(
            task=task, title=title, success=False,
            error=(
                "nothing to recover: the task branch no longer exists. "
                "Re-run the task instead."
            ),
        )

    # Autostash is an operator/global default in ``<workspace>/config.json``;
    # recovery doesn't carry a tasks_root so it reads the host config directly.
    try:
        host_config = load_config(workspace)
    except (FileNotFoundError, ValueError):
        host_config = {}
    autostash = bool(host_config.get("autostash_operator_work", True))
    sha, detail, _ = squash_to_main(
        workspace, repo, task, title, queue=queue, autostash=autostash
    )
    if sha is None:
        return TaskResult(
            task=task, title=title, success=False,
            error=detail or "squash-merge to main failed",
        )

    teardown_worktree(workspace, repo, task, queue=queue)
    return TaskResult(
        task=task, title=title, success=True, commit_sha=sha,
        result_line=f"recovered: landed ({sha})",
    )


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
      immediately — this is the cheap legacy :func:`recover_task` path.
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
    autostash = bool(config.get("autostash_operator_work", True))
    sha, detail, recoverable = squash_to_main(
        workspace, repo, task, title, queue=queue, autostash=autostash
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
    from nightshift.backends import LAUNCH_FAILED, WorkerSpec

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
            spec = WorkerSpec(
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
            if worker.returncode == LAUNCH_FAILED:
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
            autostash=bool(config.get("autostash_operator_work", True)),
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
    repair pass stays correct without the engine knowing the repo's layout.
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


class Controller:
    """Thread-safe transport control for a play-through.

    The run loop consults this between tasks (pause/stop) and a running worker
    consults :meth:`abort_reason` to terminate early on skip/stop.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._skip = threading.Event()
        self._not_paused = threading.Event()
        self._not_paused.set()

    def pause(self) -> None:
        self._not_paused.clear()

    def resume(self) -> None:
        self._not_paused.set()

    @property
    def paused(self) -> bool:
        return not self._not_paused.is_set()

    def stop(self) -> None:
        self._stop.set()
        self._skip.set()
        self._not_paused.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def skip(self) -> None:
        self._skip.set()

    def begin_task(self) -> None:
        """Clear the per-task skip flag before starting a task."""
        self._skip.clear()

    def abort_reason(self) -> str | None:
        """Why the current worker should abort, if at all."""
        if self._stop.is_set():
            return "stopped"
        if self._skip.is_set():
            return "skipped"
        return None

    def wait_while_paused(self) -> None:
        while not self._not_paused.wait(timeout=0.2):
            if self._stop.is_set():
                return


def run_task(
    workspace: Path,
    tasks_root: Path,
    task: str,
    *,
    repo: str | None = None,
    emit: Listener = _noop,
    abort_reason: object = None,
    backend_name: str | None = None,
    tasks_rel: str = "main",
) -> TaskResult:
    """Run a single task end-to-end across the two roots. Always cleans up the
    worktree when done.

    The brief is read from ``tasks_root`` (the content store); the target repo is
    resolved per task (``repo`` override → queue ``config.json`` ``repo``) and all
    git ops run in ``repo_root = workspace / repo``. ``abort_reason`` is an
    optional zero-arg callable returning ``None`` to continue or a status string
    (``"skipped"`` / ``"stopped"``) to terminate the worker early.
    ``backend_name`` selects the worker shim (claude / cursor / anthropic /
    ollama); when ``None`` it falls back to ``config`` then the default.
    ``tasks_rel`` selects the queue dir (``main`` or an alternate queue) and whose
    config (incl. the ``validate`` command and default ``repo``) applies.

    Two non-error early exits exist: a malformed/absent ``repo`` reference is an
    **authoring** error (``RepoConfigError`` → ``TASK_RESULT`` ``error``), while a
    well-formed but currently-absent repo **pauses** the task
    (``TASK_RESULT`` ``"paused"`` + reason ``repo_unavailable``) without cutting a
    worktree. The final ``task_result`` event carries a ``timings`` dict of
    per-phase seconds (``worker`` / ``validate`` / ``commit`` / ``total``).
    """
    tasks_dir = tasks_root / tasks_rel
    task_file = tasks_dir / f"{task}.md"
    config = resolve_config(workspace, tasks_root, tasks_rel)
    queue = playlists.queue_from_tasks_rel(tasks_rel)

    if not task_file.exists():
        result = TaskResult(task=task, title=task, success=False, error="task file not found")
        emit(Event(TASK_RESULT, {"task": task, "status": "error", "error": result.error}))
        return result

    text = task_file.read_text()
    meta, body = split_frontmatter(text)
    resolved = resolve_frontmatter(meta, config)
    title = resolve_title(task, meta)

    # Re-check the disabled flag at launch, not just when the queue was built.
    # The queue scan that produced this task list (build_task_list /
    # live_ordered_queue) filters disabled tasks, but a task can be disabled
    # through the UI after the list is built or between drain iterations, and a
    # single named task bypasses that scan entirely. Reading the flag here — the
    # one point every launch funnels through — guarantees a disabled task is
    # never handed to a worker. Skipped, not failed: a disabled task is a
    # deliberate operator choice, not an error.
    if is_disabled(meta):
        emit(Event(TASK_RESULT, {
            "task": task,
            "status": "skipped",
            "result_line": "skipped: task is disabled",
        }))
        return TaskResult(
            task=task, title=title, success=False, status="skipped",
            result_line="skipped: task is disabled",
        )

    if is_quarantined(meta):
        emit(Event(TASK_RESULT, {
            "task": task,
            "status": "skipped",
            "result_line": "skipped: task is quarantined",
        }))
        return TaskResult(
            task=task, title=title, success=False, status="skipped",
            result_line="skipped: task is quarantined",
        )

    if is_completed(meta):
        emit(Event(TASK_RESULT, {
            "task": task,
            "status": "skipped",
            "result_line": "skipped: task is completed",
        }))
        return TaskResult(
            task=task, title=title, success=False, status="skipped",
            result_line="skipped: task is completed",
        )

    # Resolve the target repo (task frontmatter override → queue default). A
    # malformed/absent reference is an authoring error (never dispatched).
    if not repo:
        try:
            repo = repos.resolve_repo(
                meta.get("repo"),
                load_queue_config(tasks_root, tasks_rel).get("repo"),
            )
        except repos.RepoConfigError as err:
            emit(Event(TASK_RESULT, {
                "task": task, "status": "error", "error": str(err),
                "result_line": "repo configuration error",
                "failure_kind": "repo_config",
            }))
            return TaskResult(
                task=task, title=title, success=False, error=str(err),
                result_line="repo configuration error", failure_kind="repo_config",
            )

    # A well-formed reference to an absent/`.git`-less repo pauses (not fails) the
    # task until the repo appears — no worktree is cut, no run is recorded.
    if not repos.repo_available(workspace, repo):
        result_line = f"paused: repo '{repo}' is not available"
        emit(Event(TASK_RESULT, {
            "task": task, "status": "paused", "repo": repo,
            "reason": "repo_unavailable", "result_line": result_line,
        }))
        return TaskResult(
            task=task, title=title, success=False, status="paused",
            result_line=result_line,
        )

    frontmatter = {**meta}
    frontmatter.setdefault("model", resolved["model"])
    # Carry the brief prose into the run record so History can show the original
    # brief after the task file is removed (completed tasks leave the queue).
    emit(Event(TASK_STARTED, {
        "task": task, "title": title, "repo": repo,
        "frontmatter": frontmatter, "body": body.strip(),
    }))
    emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "worker"}))

    def _should_abort() -> str | None:
        return abort_reason() if callable(abort_reason) else None

    repo_root = workspace / repo
    t_task_start = time.monotonic()
    # Deliver the brief via a run-scratch file outside the worktree (the brief
    # never enters the target repo), then cut the worktree from the target repo.
    scratch = materialize_brief(workspace, repo, task, body, queue=queue)
    is_split = bool(meta.get("split", False))
    sdir: str | None = None
    if is_split:
        sdir_path = split_output_dir(workspace, repo, task, queue=queue)
        sdir_path.mkdir(parents=True, exist_ok=True)
        sdir = str(sdir_path)
    try:
        worktree_dir = setup_worktree(workspace, repo, task, queue=queue)
    except GitError as err:
        # A failed `worktree add` is task-fatal but must surface as a typed
        # failure (worktree_failed), never a raw traceback.
        error = f"could not create the task worktree: {err}"
        emit(Event(TASK_RESULT, {
            "task": task, "status": "error", "error": error, "repo": repo,
            "result_line": "worktree setup failed",
            "failure_kind": FailureKind.WORKTREE_FAILED,
        }))
        return TaskResult(
            task=task, title=title, success=False, error=error,
            result_line="worktree setup failed",
            failure_kind=FailureKind.WORKTREE_FAILED,
        )
    preserve_worktree = False

    # Imported here, not at module top, to avoid a backends<->engine import
    # cycle: backends reuses engine's claude argv/bin helpers.
    from nightshift.backends import LAUNCH_FAILED, WorkerSpec

    try:
        prompt = build_prompt(
            task,
            task_file=str(scratch),
            validate_cmd=str(config.get("validate") or DEFAULT_VALIDATE_CMD),
            loop=bool(meta.get("loop", False)),
            loop_max_iterations=int(meta.get("loop_max_iterations", 0)),
            split=is_split,
            split_dir=sdir,
        )
        env = worker_env(worktree_dir)
        validate_cmd = resolve_validate_cmd(config)
        backend, run_model = select_run_backend(
            resolved["model"], backend_name or config.get("worker_backend")
        )
        spec = WorkerSpec(
            task=task,
            prompt=prompt,
            model=run_model,
            max_turns=resolved["max_turns"],
            cwd=worktree_dir,
            env=env,
            config=config,
        )
        timings: dict[str, float] = {}

        def _with_total() -> dict[str, float]:
            timings["total"] = round(time.monotonic() - t_task_start, 1)
            return dict(timings)

        def _emit_log(line: str) -> None:
            emit(Event(TASK_LOG, {"task": task, "line": line}))

        def _on_worker_start(pid: int) -> None:
            # Record the live worker pid so stale-run reconciliation can tell a
            # busy (even orphaned) worker from an abandoned run.
            emit(Event(WORKER_STARTED, {"task": task, "pid": pid}))

        emit(Event(TASK_LOG, {
            "task": task,
            "line": f"  running worker [{backend.name}] ({run_model})...\n",
        }))
        t_worker = time.monotonic()
        worker = backend.run(spec, _emit_log, _should_abort, on_worker_start=_on_worker_start)
        timings["worker"] = round(time.monotonic() - t_worker, 1)

        if worker.aborted is not None:
            emit(Event(TASK_RESULT, {
                "task": task, "status": worker.aborted, "repo": repo,
                "timings": _with_total(),
            }))
            return TaskResult(task=task, title=title, success=False, status=worker.aborted)

        if worker.returncode == LAUNCH_FAILED:
            error = (
                f"{worker.error}. Add the worker binary to PATH or set its "
                "'*_bin' in config.json."
            )
            write_failure_log(workspace, repo, worktree_dir, task, error)
            emit(Event(TASK_RESULT, {
                "task": task, "status": "error", "error": error, "repo": repo,
                "result_line": "worker executable not found",
                "failure_kind": "worker_launch", "timings": _with_total(),
            }))
            return TaskResult(
                task=task, title=title, success=False, error=error,
                result_line="worker executable not found",
                failure_kind="worker_launch",
            )

        if worker.returncode != 0:
            error = worker.error or f"worker [{backend.name}] exited with code {worker.returncode}"
            write_failure_log(workspace, repo, worktree_dir, task, error)
            line = error.splitlines()[0][:120]
            emit(Event(TASK_RESULT, {
                "task": task, "status": "error", "error": error, "repo": repo,
                "result_line": line, "failure_kind": "worker_error",
                "timings": _with_total(),
            }))
            return TaskResult(
                task=task, title=title, success=False, error=error,
                result_line=line, failure_kind="worker_error",
            )

        # Split (decomposition) runs: harvest subtask briefs and enqueue them,
        # then retire the parent. No repo commits are expected.
        if is_split:
            created = harvest_split_output(
                workspace, tasks_root, repo, task, meta,
                queue=queue, tasks_rel=tasks_rel,
            )
            if created:
                result_line = (
                    f"decomposed into {len(created)} subtask(s): "
                    + ", ".join(created)
                )
            else:
                result_line = "decomposition run produced no subtasks"
            emit(Event(TASK_RESULT, {
                "task": task, "status": "completed", "repo": repo,
                "result_line": result_line, "timings": _with_total(),
                "subtasks": created,
            }))
            return TaskResult(
                task=task, title=title, success=bool(created),
                result_line=result_line,
            )

        # No commits → nothing to validate or squash. Finish cleanly instead of
        # tripping the squash step (e.g. non-agentic completion backends).
        if not has_commits(workspace, repo, task, queue=queue):
            result_line = "no changes produced (worker emitted output only)"
            # A no-changes completion never removed the brief (no branch to land),
            # so drop it here for regular tasks — a completed task must leave the
            # queue. Evergreen tasks keep their file and re-run.
            if not task_is_evergreen(meta, task, config):
                drop_completed_task(tasks_root, task, tasks_rel, queue=queue)
            emit(Event(TASK_RESULT, {
                "task": task, "status": "completed", "repo": repo,
                "result_line": result_line, "timings": _with_total(),
            }))
            return TaskResult(task=task, title=title, success=True, result_line=result_line)

        # Honour a stop/skip requested while the worker was running, before we
        # sink time into validate.
        if _should_abort() is not None:
            reason = _should_abort()
            emit(Event(TASK_RESULT, {
                "task": task, "status": reason, "repo": repo, "timings": _with_total(),
            }))
            return TaskResult(task=task, title=title, success=False, status=reason)

        # A queue may opt out of validation by setting an empty validate command;
        # the worker's work then lands without a validate gate.
        if validate_cmd is None:
            emit(Event(TASK_LOG, {
                "task": task, "line": "  validation disabled for this queue — skipping.\n",
            }))
            result_line = "validation skipped (no validate command)"
        else:
            emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "validate"}))
            emit(Event(TASK_LOG, {"task": task, "line": f"  running {' '.join(validate_cmd)}...\n"}))
            t_validate = time.monotonic()
            validate_result = run_interruptible(
                validate_cmd, cwd=worktree_dir, env=env, should_abort=_should_abort,
            )

            if validate_result.returncode != 0 and _should_abort() is None:
                emit(Event(TASK_LOG, {"task": task, "line": "  validate failed — attempting auto-repair...\n"}))
                validate_result = attempt_repair(
                    worktree_dir, validate_result,
                    validate_cmd=validate_cmd, env=env, should_abort=_should_abort,
                )
            timings["validate"] = round(time.monotonic() - t_validate, 1)

            # A stop during validate (the process was killed) ends the task now —
            # nothing is committed to main.
            if _should_abort() is not None:
                reason = _should_abort()
                emit(Event(TASK_RESULT, {
                    "task": task, "status": reason, "repo": repo,
                    "timings": _with_total(),
                }))
                return TaskResult(task=task, title=title, success=False, status=reason)

            if validate_result.returncode != 0:
                error = f"just validate failed:\n{validate_result.stdout[-2000:]}\n{validate_result.stderr[-2000:]}"
                emit(Event(TASK_LOG, {"task": task, "line": f"\n── validate failed (exit {validate_result.returncode}) ──\n"}))
                if validate_result.stdout:
                    emit(Event(TASK_LOG, {"task": task, "line": validate_result.stdout[-3000:]}))
                if validate_result.stderr:
                    emit(Event(TASK_LOG, {"task": task, "line": validate_result.stderr[-1500:]}))
                emit(Event(TASK_LOG, {"task": task, "line": "\n── end validate output ──\n"}))
                write_failure_log(
                    workspace, repo, worktree_dir, task, error,
                    validate_stdout=validate_result.stdout,
                    validate_stderr=validate_result.stderr,
                )
                result_line = extract_result_line(validate_result.stdout, validate_result.stderr)
                emit(Event(TASK_RESULT, {
                    "task": task, "status": "error", "error": error, "repo": repo,
                    "result_line": result_line, "failure_kind": "validation_error",
                    "timings": _with_total(),
                }))
                return TaskResult(
                    task=task, title=title, success=False, error=error,
                    result_line=result_line, failure_kind="validation_error",
                )

            result_line = extract_result_line(validate_result.stdout, validate_result.stderr)

        # Last chance to bail before mutating main — a stop here leaves the
        # validated work on the task branch (recoverable) rather than landing it.
        if _should_abort() is not None:
            reason = _should_abort()
            preserve_worktree = True
            emit(Event(TASK_RESULT, {
                "task": task, "status": reason, "repo": repo, "timings": _with_total(),
            }))
            return TaskResult(task=task, title=title, success=False, status=reason)

        emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "commit"}))
        t_commit = time.monotonic()
        sha, squash_error, recoverable = squash_to_main(
            workspace, repo, task, title, queue=queue,
            autostash=bool(config.get("autostash_operator_work", True)),
        )
        timings["commit"] = round(time.monotonic() - t_commit, 1)
        # A successful land that couldn't reapply set-aside operator work surfaces
        # squash_error as a warning (the commit still landed; stash is preserved).
        if sha is not None and squash_error:
            emit(Event(TASK_LOG, {"task": task, "line": f"  warning: {squash_error}\n"}))
        if sha is None:
            error = squash_error or "squash-merge to main failed"
            write_failure_log(workspace, repo, worktree_dir, task, error)
            # Keep the worktree branch either way so the validated work is never
            # lost: a transient blocker can be re-squashed once cleared, and a
            # content conflict can be resolved by hand against the branch.
            preserve_worktree = True
            failure_kind = squash_failure_kind(recoverable, error)

            # Gated auto-resolve: on a content conflict, hand straight to the
            # resolver agent instead of parking for a human. Off by default;
            # enabled per-repo (config ``auto_resolve``) or per-task
            # (frontmatter ``autoresolve``). A transient blocker (dirty main) is
            # never auto-resolved — an agent can't touch the operator's tree.
            auto_resolve = bool(meta.get("autoresolve", config.get("auto_resolve", False)))
            if auto_resolve and failure_kind == "merge_conflict":
                emit(Event(TASK_LOG, {
                    "task": task,
                    "line": "  auto-resolve enabled — launching resolver agent...\n",
                }))
                result = _agent_resolve(
                    workspace, repo, tasks_root, task, title,
                    conflict_detail=error, emit=emit, config=config,
                    backend_name=backend_name, abort_reason=abort_reason, queue=queue,
                )
                preserve_worktree = not result.success
                return result

            result_line = (
                "squash-merge failed — recoverable"
                if recoverable
                else "squash-merge conflict — manual resolution needed"
            )
            emit(Event(TASK_RESULT, {
                "task": task, "status": "error", "error": error, "repo": repo,
                "result_line": result_line,
                "recoverable": recoverable, "failure_kind": failure_kind,
                "timings": _with_total(),
            }))
            return TaskResult(
                task=task, title=title, success=False, error=error,
                failure_kind=failure_kind,
            )

        # Code lines churned by the squash commit this task landed on the target
        # repo's ``main`` (added + removed), excluding build files, docs, comments,
        # and build/output dirs — summed on the Stats page. The landed commit is
        # the one metric the Stats backfill can also reconstruct from a record's
        # ``commit_sha`` after the task branch is torn down, so live capture and
        # backfill report the *same* figure for every task (a branch-history sum
        # would diverge from any later backfill and make the total inconsistent).
        loc = compute_code_loc(repo_root, sha)
        # Backstop the worker's queue removal: a regular task that lands must
        # leave the queue. If the worker's branch didn't ``git rm`` its brief, the
        # squash kept it on ``main`` and the UI would keep listing a completed
        # task — drop it from the content store here. Evergreen tasks keep theirs.
        if not task_is_evergreen(meta, task, config):
            drop_completed_task(tasks_root, task, tasks_rel, queue=queue)
        emit(Event(TASK_RESULT, {
            "task": task,
            "status": "completed",
            "repo": repo,
            "result_line": result_line,
            "commit_sha": sha,
            "loc": loc,
            "timings": _with_total(),
        }))
        return TaskResult(
            task=task, title=title, success=True, commit_sha=sha, loc=loc,
            result_line=result_line,
        )

    finally:
        if not preserve_worktree:
            teardown_worktree(workspace, repo, task, queue=queue)


def run_queue(
    workspace: Path,
    tasks_root: Path,
    tasks: list[str],
    *,
    listeners: list[Listener] | None = None,
    controller: Controller | None = None,
    run_id: str | None = None,
    backend_name: str | None = None,
    tasks_rel: str = "main",
    follow_queue: bool = False,
    task_slot: Callable[[], AbstractContextManager[object]] | None = None,
    admit_task: Callable[[], str | None] | None = None,
) -> RunSummary:
    """Run tasks from a queue, emitting events to ``listeners``.

    Briefs are read from ``tasks_root`` (the content store) and each task resolves
    its own target repo inside :func:`run_task` (the run is paused, not failed, if
    that repo is currently absent). If a :class:`Controller` is supplied the loop
    honours pause/stop/skip; with no controller it runs straight through (the CLI
    path). ``backend_name`` selects the worker shim for every task in the run.
    ``tasks_rel`` selects the queue dir (``main`` or an alternate queue).

    ``task_slot`` (server concurrency governor) is an optional context-manager
    factory held for the duration of *each* ``run_task`` (worker→validate→land),
    so a shared gate can cap simultaneous workers across queues. ``admit_task``
    (server disk admission) is an optional check run before each task; when it
    returns a message the task is failed (``failure_kind="disk"``) without
    cutting a worktree and the run stops rather than thrashing. Both default to
    ``None`` (the CLI path is unchanged — sequential, ungoverned).

    With ``follow_queue`` (queue/"all"/repeat runs) the loop drains the *live*
    queue: tasks added to the queue dir mid-run are folded in (in configured
    order) and executed in this same run, rather than waiting for the next cycle.
    An ``attempted`` set bounds each task to one attempt per run (so evergreen and
    failed tasks don't loop; completed regular tasks remove their own file and
    drop out of the scan). With ``follow_queue`` off the loop drains exactly the
    passed ``tasks`` (oneshot semantics, unchanged).

    There is no pre-run target-repo snapshot: briefs live in ``tasks_root`` and
    are delivered to the worker via a run-scratch file, so the target repo only
    ever receives the implementation squash.
    """
    emit: Listener = _noop
    if listeners:
        active = listeners

        def emit(event: Event) -> None:
            for listener in active:
                listener(event)

    emit(Event(RUN_STARTED, {"run_id": run_id, "tasks": list(tasks)}))
    summary = RunSummary()
    attempted: set[str] = set()
    # Seed with the passed list (carries autosplit-spawned subtasks + any
    # start_task slice); live additions are folded in when following the queue.
    order: list[str] = list(tasks)
    try:
        while True:
            if controller is not None and controller.stopped:
                break
            if follow_queue:
                # Re-read the live queue every iteration so mid-run edits — a
                # changed priority, a flipped sort mode, a dragged row, or a
                # freshly-added task — take effect at the next task boundary.
                # New stems are folded in; the not-yet-attempted tail is then
                # re-sorted by this fresh ordering so "Up Next" always reflects
                # the current on-disk state, never a list captured at play time.
                # The running task is already in ``attempted`` (added below
                # before its run_task call), so it's never reshuffled.
                live = live_ordered_queue(tasks_root, tasks_rel)
                for stem in live:
                    if stem not in order and stem not in attempted:
                        order.append(stem)
                live_rank = {stem: i for i, stem in enumerate(live)}
                pending = sorted(
                    (t for t in order if t not in attempted),
                    key=lambda s: live_rank.get(s, len(live_rank)),
                )
            else:
                pending = [t for t in order if t not in attempted]
            if not pending:
                break
            task = pending[0]
            attempted.add(task)

            if controller is not None:
                if controller.stopped:
                    break
                controller.wait_while_paused()
                if controller.stopped:
                    break
                controller.begin_task()

            # Disk admission: refuse to start a task when the tree is too full —
            # fail it cleanly (no worktree cut) and stop rather than thrash.
            if admit_task is not None:
                denial = admit_task()
                if denial is not None:
                    emit(Event(TASK_STARTED, {"task": task, "title": task}))
                    emit(Event(TASK_RESULT, {
                        "task": task, "status": "error", "error": denial,
                        "result_line": "insufficient disk — run paused",
                        "failure_kind": "disk",
                    }))
                    break

            def _run() -> TaskResult:
                return run_task(
                    workspace,
                    tasks_root,
                    task,
                    emit=emit,
                    abort_reason=(
                        controller.abort_reason if controller is not None else None
                    ),
                    backend_name=backend_name,
                    tasks_rel=tasks_rel,
                )

            # Hold a concurrency slot for the whole task when a gate is supplied,
            # so simultaneous workers across queues stay capped.
            if task_slot is not None:
                with task_slot():
                    result = _run()
            else:
                result = _run()
            summary.results.append(result)
            if controller is not None and controller.stopped:
                break
    finally:
        emit(Event(RUN_FINISHED, {"run_id": run_id}))
    return summary
