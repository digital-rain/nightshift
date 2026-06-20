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
    resolve_validate_cmd,
    run_interruptible,
    setup_worktree,
    teardown_worktree,
    worker_env,
)
from nightshift.worker.config import WorkerConfig


# Phase callback: (phase) -> None, lets the loop mirror phase into local status.
PhaseCb = Callable[[str], None]
# Log callback: (line) -> None, streamed to the manager + local tail.
LogCb = Callable[[str], None]


@dataclass
class ExecuteOutcome:
    status: str                 # completed | blocked | error
    result_line: str
    landable: bool
    resolved_model: str
    failure_kind: str | None = None
    failure_reason: str | None = None
    # Best-effort agent telemetry captured from the backend run (None when the
    # backend can't report it); carried to the manager for per-task rollups.
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


def execute_work_order(
    cfg: WorkerConfig,
    order: dict[str, Any],
    *,
    on_phase: PhaseCb,
    on_log: LogCb,
) -> ExecuteOutcome:
    """Run one work order to a landable (or failed) state. Never touches main."""
    from nightshift.backends import LAUNCH_FAILED, WorkerSpec, get_backend

    workspace = cfg.workspace
    task = order["task"]
    repo = order["repo"]
    # The work order carries the queue *label* ("main"/<name>); the engine's
    # worktree/brief helpers take the internal queue arg (main -> None).
    queue = playlists.queue_from_tasks_rel(order.get("queue") or "main")
    config_blob = order.get("config", {})

    # Worker-owned model resolution. A vendor mismatch fails the task with a
    # clear reason (surfaced to the operator via the manager).
    model, model_error = cfg.resolve_model(config_blob.get("model"))
    if model_error:
        return ExecuteOutcome(
            status="error",
            result_line=model_error,
            landable=False,
            resolved_model=str(config_blob.get("model") or "auto"),
            failure_kind="model_unavailable",
            failure_reason=model_error,
        )
    assert model is not None

    # Defensive availability guard. The manager pauses tasks whose repo is absent
    # before dispatch (repo availability is its concern), so reaching here means
    # the workspace changed under us. Fail the order without cutting a worktree.
    if not repos.repo_available(workspace, repo):
        reason = f"repo '{repo}' is not available in the workspace"
        return ExecuteOutcome(
            status="error", result_line=reason, landable=False,
            resolved_model=model, failure_kind="repo_unavailable",
            failure_reason=reason,
        )

    backend = get_backend(cfg.backend)
    if not backend.available(config_blob):
        reason = f"backend '{cfg.backend}' is not available on this worker"
        return ExecuteOutcome(
            status="error", result_line=reason, landable=False,
            resolved_model=model, failure_kind="backend_unavailable",
            failure_reason=reason,
        )

    on_phase("worker")
    # Deliver the brief via a run-scratch file OUTSIDE the target repo (so it
    # never enters the worktree's tracked tree), then cut the worktree from the
    # target repo. base_ref for landing/diff comes from order["base_ref"] (the
    # manager's canonical_head); the worker never recomputes it.
    scratch = materialize_brief(workspace, repo, task, order["body"], queue=queue)
    wt_dir = setup_worktree(workspace, repo, task, queue=queue)
    preserve = False
    captured: list[str] = []

    def capture_log(line: str) -> None:
        captured.append(line)
        on_log(line)

    try:
        prompt = build_prompt(
            task,
            task_file=str(scratch),
            validate_cmd=str(config_blob.get("validate") or DEFAULT_VALIDATE_CMD),
        )
        env = worker_env()
        max_turns = config_blob.get("max_turns")
        spec = WorkerSpec(
            task=task,
            prompt=prompt,
            model=model,
            max_turns=int(max_turns) if max_turns is not None else None,
            cwd=wt_dir,
            env=env,
            config=config_blob,
        )
        on_log(f"  running worker [{cfg.backend}] ({model})...\n")
        result = backend.run(spec, capture_log, lambda: None)

        # Best-effort telemetry the agent reported; attached to every outcome
        # below (a failed/validation-failed run still consumed turns + tokens).
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
                landable=False, resolved_model=model,
                failure_kind="worker_launch", failure_reason=result.error,
                **tele,
            )

        has_commits = _worktree_has_commits(workspace, repo, task, queue=queue)

        # Honest block (file-free sentinel): the agent emitted a final
        # NIGHTSHIFT_BLOCKED line and made no commits. This is an explicit intent
        # signal, so it takes precedence over a generic non-zero exit; surface a
        # `blocked` status + reason for the manager to record. Nothing to land.
        blocked_reason = extract_blocked_reason("".join(captured))
        if blocked_reason and not has_commits:
            return ExecuteOutcome(
                status="blocked",
                result_line=f"blocked: {blocked_reason}",
                landable=False, resolved_model=model,
                failure_kind="blocked", failure_reason=blocked_reason,
                **tele,
            )

        if result.returncode != 0:
            reason = result.error or f"worker [{cfg.backend}] exited {result.returncode}"
            return ExecuteOutcome(
                status="error", result_line=reason.splitlines()[0][:120],
                landable=False, resolved_model=model,
                failure_kind="worker_error", failure_reason=reason,
                **tele,
            )

        # No commits → nothing to land; finish cleanly.
        if not has_commits:
            return ExecuteOutcome(
                status="completed",
                result_line="no changes produced (worker emitted output only)",
                landable=False, resolved_model=model, **tele,
            )

        # Validate in the worktree (the manager re-checks nothing; the worker's
        # gate is the trust boundary for local/push landing).
        validate_cmd = resolve_validate_cmd(config_blob)
        if validate_cmd is None:
            preserve = True
            return ExecuteOutcome(
                status="completed",
                result_line="validation skipped (no validate command)",
                landable=True, resolved_model=model, **tele,
            )
        on_phase("validate")
        on_log(f"  running {' '.join(validate_cmd)}...\n")
        validate = run_interruptible(validate_cmd, cwd=wt_dir, env=env, should_abort=lambda: None)
        if validate.returncode != 0:
            preserve = True  # keep the branch so the work can be resolved
            tail = (validate.stdout[-1500:] + "\n" + validate.stderr[-500:]).strip()
            return ExecuteOutcome(
                status="error",
                result_line="validate failed",
                landable=False, resolved_model=model,
                failure_kind="validation_error", failure_reason=tail,
                **tele,
            )

        preserve = True  # landable branch — the manager squashes it
        return ExecuteOutcome(
            status="completed", result_line="validated", landable=True,
            resolved_model=model, **tele,
        )
    finally:
        if not preserve:
            teardown_worktree(workspace, repo, task, queue=queue)
