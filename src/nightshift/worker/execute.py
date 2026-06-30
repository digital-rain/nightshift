"""Per-task execution — the worker half of the old ``engine.run_task``.

Composes the engine's existing primitives (worktree setup, backend dispatch,
validate) but **stops before landing**: a successful run leaves a committed task
branch for the manager to squash. The worker's single outward action is to
submit; it never squashes to ``main`` itself.

Outcomes:

* ``completed`` + ``landable=True``  — validated commit(s) on the branch (kept).
* ``completed`` + ``landable=False`` — worker produced no commit (nothing to land).
* ``blocked``                        — the agent emitted ``NIGHTSHIFT_BLOCKED:``
  and made no commits (an honest hold for the manager to record + a human/agent
  to resolve). No ``.BLOCKED`` file is written anywhere.
* ``error``                          — worker/launch/validation failure (branch
  kept on validation failure so the work can be resolved).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nightshift import playlists, repos
from nightshift.engine import (
    DEFAULT_VALIDATE_CMD,
    _worktree_has_commits,
    build_prompt,
    extract_blocked_reason,
    materialize_brief,
    prepare_worktree_base,
    publish_task_branch,
    run_interruptible,
    setup_worktree,
    teardown_worktree,
    validate_cmd_from_blob,
    worker_env,
)
from nightshift.manager.landing import main_advanced_sha
from nightshift.model_id import split_model
from nightshift.worker.config import WorkerConfig


# Phase callback: (phase) -> None, lets the loop mirror phase into local status.
PhaseCb = Callable[[str], None]
# Log callback: (line) -> None, streamed to the manager + local tail.
LogCb = Callable[[str], None]


@dataclass
class ExecuteOutcome:
    status: str  # completed | blocked | error
    result_line: str
    landable: bool
    resolved_model: str
    backend: str = ""
    failure_kind: str | None = None
    failure_reason: str | None = None
    # Cross-machine landing (transport B): the WIP ref the worker published its
    # validated branch to, and the branch tip SHA the manager re-verifies after
    # fetching. Both None when co-located (no rendezvous remote configured).
    branch_ref: str | None = None
    head_sha: str | None = None
    # Best-effort agent telemetry captured from the backend run (None when the
    # backend can't report it); carried to the manager for per-task rollups.
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    # Validate command the worker actually ran (None when skipped or not reached).
    validate_cmd: str | None = None
    # The worktree directory the worker used for this task (str path).
    worktree: str | None = None


def _finish_landable(
    cfg: WorkerConfig,
    repo: str,
    task: str,
    queue: str | None,
    *,
    model: str,
    backend: str,
    result_line: str,
    tele: dict[str, Any],
    on_log: LogCb,
    wip_ref_prefix: str | None = None,
    validate_cmd: str | None = None,
    worktree: str | None = None,
) -> ExecuteOutcome:
    """Finalize a validated (landable) run.

    Cross-machine (a rendezvous remote is configured): publish the task branch to
    it and report ``(branch_ref, head_sha)`` so the manager can fetch + verify +
    land. A push failure lands nothing (``status="error"``, ``publish_failed``)
    and the caller keeps the worktree for a retry. Co-located (no remote):
    publish nothing and leave the branch for the manager to squash from the
    shared workspace — today's behavior.
    """
    if not cfg.rendezvous_remote:
        return ExecuteOutcome(
            status="completed",
            result_line=result_line,
            landable=True,
            resolved_model=model,
            backend=backend,
            validate_cmd=validate_cmd,
            worktree=worktree,
            **tele,
        )
    try:
        branch_ref, head_sha = publish_task_branch(
            cfg.workspace,
            repo,
            task,
            cfg.rendezvous_remote,
            queue=queue,
            prefix=wip_ref_prefix,
        )
    except RuntimeError as exc:
        on_log(f"  publish to rendezvous remote failed: {exc}\n")
        return ExecuteOutcome(
            status="error",
            result_line="publish failed",
            landable=False,
            resolved_model=model,
            backend=backend,
            failure_kind="publish_failed",
            failure_reason=str(exc),
            validate_cmd=validate_cmd,
            worktree=worktree,
            **tele,
        )
    on_log(f"  published {branch_ref} ({head_sha[:8]}) to {cfg.rendezvous_remote}\n")
    return ExecuteOutcome(
        status="completed",
        result_line=result_line,
        landable=True,
        resolved_model=model,
        backend=backend,
        branch_ref=branch_ref,
        head_sha=head_sha,
        validate_cmd=validate_cmd,
        worktree=worktree,
        **tele,
    )


def execute_work_order(
    cfg: WorkerConfig,
    order: dict[str, Any],
    *,
    on_phase: PhaseCb,
    on_log: LogCb,
) -> ExecuteOutcome:
    """Run one work order to a landable (or failed) state. Never touches main."""
    from nightshift.backends import LAUNCH_FAILED, WorkerSpec, require_backend
    from nightshift.engine import worktree_dir

    workspace = cfg.workspace
    task = order["task"]
    repo = order["repo"]
    # The work order carries the queue *label* ("main"/<name>); the engine's
    # worktree/brief helpers take the internal queue arg (main -> None).
    queue = playlists.queue_from_tasks_rel(order.get("queue") or "main")
    config_blob = order.get("config", {})
    validate_argv, validate_display = validate_cmd_from_blob(config_blob)
    prompt_validate = validate_display or DEFAULT_VALIDATE_CMD

    # Resolve the worktree path early (deterministic from task/queue/repo) so
    # every outcome carries it — even early failures that never cut the worktree.
    wt_path = str(worktree_dir(workspace, repo, task, queue))

    model, model_error = cfg.resolve_model(config_blob.get("model"))
    if model_error:
        return ExecuteOutcome(
            status="error",
            result_line=model_error,
            landable=False,
            resolved_model=str(config_blob.get("model") or "auto"),
            failure_kind="model_unavailable",
            failure_reason=model_error,
            worktree=wt_path,
        )
    assert model is not None

    provider, bare_model = split_model(model)
    if provider is None:
        reason = f"model '{model}' is not provider-qualified (expected provider/model)"
        return ExecuteOutcome(
            status="error",
            result_line=reason,
            landable=False,
            resolved_model=model,
            failure_kind="model_unavailable",
            failure_reason=reason,
            worktree=wt_path,
        )

    # Defensive availability guard.
    if not repos.repo_available(workspace, repo):
        reason = f"repo '{repo}' is not available in the workspace"
        return ExecuteOutcome(
            status="error",
            result_line=reason,
            landable=False,
            resolved_model=model,
            backend=provider,
            failure_kind="repo_unavailable",
            failure_reason=reason,
            worktree=wt_path,
        )

    try:
        backend = require_backend(provider)
    except KeyError:
        reason = f"unknown provider '{provider}' in model '{model}'"
        return ExecuteOutcome(
            status="error",
            result_line=reason,
            landable=False,
            resolved_model=model,
            backend=provider,
            failure_kind="backend_unavailable",
            failure_reason=reason,
            worktree=wt_path,
        )
    if not backend.available(config_blob):
        reason = f"backend '{provider}' is not available on this worker"
        return ExecuteOutcome(
            status="error",
            result_line=reason,
            landable=False,
            resolved_model=model,
            backend=provider,
            failure_kind="backend_unavailable",
            failure_reason=reason,
            worktree=wt_path,
        )

    on_phase("worker")
    scratch = materialize_brief(workspace, repo, task, order["body"], queue=queue)
    base = "HEAD"
    if cfg.rendezvous_remote:
        base = prepare_worktree_base(
            workspace, repo, cfg.rendezvous_remote, order.get("base_ref")
        )
    wt_dir = setup_worktree(workspace, repo, task, queue=queue, base=base)
    preserve = False
    captured: list[str] = []

    def capture_log(line: str) -> None:
        captured.append(line)
        on_log(line)

    try:
        prompt = build_prompt(
            task,
            task_file=str(scratch),
            validate_cmd=prompt_validate,
            loop=bool(config_blob.get("loop", False)),
            loop_max_iterations=int(config_blob.get("loop_max_iterations", 0)),
        )
        env = worker_env(wt_dir)
        max_turns = config_blob.get("max_turns")
        spec = WorkerSpec(
            task=task,
            prompt=prompt,
            model=bare_model,
            max_turns=int(max_turns) if max_turns is not None else None,
            cwd=wt_dir,
            env=env,
            config=config_blob,
            timeout=cfg.model_timeout_seconds or None,
        )
        on_log(f"  running worker [{provider}] ({bare_model})...\n")
        result = backend.run(spec, capture_log, lambda: None)

        tele = {
            "turns": result.turns,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
        }

        if result.returncode == LAUNCH_FAILED:
            return ExecuteOutcome(
                status="error",
                result_line="worker executable not found",
                landable=False,
                resolved_model=model,
                backend=provider,
                failure_kind="worker_launch",
                failure_reason=result.error,
                worktree=wt_path,
                **tele,
            )

        has_commits = _worktree_has_commits(workspace, repo, task, queue=queue)

        blocked_reason = extract_blocked_reason("".join(captured))
        if blocked_reason and not has_commits:
            return ExecuteOutcome(
                status="blocked",
                result_line=f"blocked: {blocked_reason}",
                landable=False,
                resolved_model=model,
                backend=provider,
                failure_kind="blocked",
                failure_reason=blocked_reason,
                worktree=wt_path,
                **tele,
            )

        if result.returncode != 0:
            reason = (
                result.error or f"worker [{provider}] exited {result.returncode}"
            )
            return ExecuteOutcome(
                status="error",
                result_line=reason.splitlines()[0][:120],
                landable=False,
                resolved_model=model,
                backend=provider,
                failure_kind="worker_error",
                failure_reason=reason,
                worktree=wt_path,
                **tele,
            )

        if not has_commits:
            base_ref = order.get("base_ref")
            repo_root = workspace / repo
            if base_ref and main_advanced_sha(repo_root, base_ref):
                return ExecuteOutcome(
                    status="completed",
                    result_line="agent landed on main (awaiting manager adopt)",
                    landable=False,
                    resolved_model=model,
                    backend=provider,
                    worktree=wt_path,
                    **tele,
                )
            return ExecuteOutcome(
                status="completed",
                result_line="no changes produced (worker emitted output only)",
                landable=False,
                resolved_model=model,
                backend=provider,
                worktree=wt_path,
                **tele,
            )

        if validate_argv is None:
            preserve = True
            return _finish_landable(
                cfg,
                repo,
                task,
                queue,
                model=model,
                backend=provider,
                result_line="validation skipped (no validate command)",
                tele=tele,
                on_log=on_log,
                wip_ref_prefix=config_blob.get("wip_ref_prefix"),
                validate_cmd=None,
                worktree=wt_path,
            )
        on_phase("validate")
        on_log(f"  running {validate_display}...\n")
        validate = run_interruptible(
            validate_argv,
            cwd=wt_dir,
            env=env,
            should_abort=lambda: None,
        )
        if validate.returncode != 0:
            preserve = True
            tail = (validate.stdout[-1500:] + "\n" + validate.stderr[-500:]).strip()
            return ExecuteOutcome(
                status="error",
                result_line="validate failed",
                landable=False,
                resolved_model=model,
                backend=provider,
                failure_kind="validation_error",
                failure_reason=tail,
                validate_cmd=validate_display,
                worktree=wt_path,
                **tele,
            )

        preserve = True
        return _finish_landable(
            cfg,
            repo,
            task,
            queue,
            model=model,
            backend=provider,
            result_line="validated",
            tele=tele,
            on_log=on_log,
            wip_ref_prefix=config_blob.get("wip_ref_prefix"),
            validate_cmd=validate_display,
            worktree=wt_path,
        )
    finally:
        if not preserve:
            teardown_worktree(workspace, repo, task, queue=queue)
