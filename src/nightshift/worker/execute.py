"""Per-task execution — the worker half of the old ``engine.run_task``.

Composes the engine's existing primitives (worktree setup, backend dispatch,
validate) but **stops before landing**: a successful run leaves a committed task
branch for the manager to squash. The worker's single outward action is to
submit; it never squashes to ``main`` itself.

Outcomes (the unified :class:`nightshift.lifecycle.Outcome`):

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
from typing import Any

from nightshift import playlists, repos
from nightshift.git import GitError
from nightshift.git.transport import prepare_worktree_base, publish_task_branch
from nightshift.git.worktrees import has_commits as worktree_has_commits
from nightshift.git.worktrees import setup_worktree, teardown_worktree
from nightshift.lifecycle import FailureKind, Outcome, RunStatus
from nightshift.model_id import split_model
from nightshift.preflight import (
    ensure_env_synced,
    preflight_cmd_from_blob,
    run_interruptible,
)
from nightshift.prompts import (
    build_prompt,
    extract_blocked_reason,
    extract_result_line,
    worker_env,
)
from nightshift.queue_config import DEFAULT_VALIDATE_CMD, validate_cmd_from_blob
from nightshift.task_files import materialize_brief, split_output_dir
from nightshift.worker.config import WorkerConfig


# Phase callback: (phase) -> None, lets the loop mirror phase into local status.
PhaseCb = Callable[[str], None]
# Log callback: (line) -> None, streamed to the manager + local tail.
LogCb = Callable[[str], None]


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
) -> Outcome:
    """Finalize a validated (landable) run.

    Cross-machine (a rendezvous remote is configured): publish the task branch to
    it and report ``(branch_ref, head_sha)`` so the manager can fetch + verify +
    land. A push failure lands nothing (``status="error"``, ``publish_failed``)
    and the caller keeps the worktree for a retry. Co-located (no remote):
    publish nothing and leave the branch for the manager to squash from the
    shared workspace — today's behavior.
    """
    if not cfg.rendezvous_remote:
        return Outcome(
            status=RunStatus.COMPLETED,
            result_line=result_line,
            landable=True,
            model=model,
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
        return Outcome(
            status=RunStatus.ERROR,
            result_line="publish failed",
            landable=False,
            model=model,
            backend=backend,
            failure_kind=FailureKind.PUBLISH_FAILED,
            failure_reason=str(exc),
            validate_cmd=validate_cmd,
            worktree=worktree,
            **tele,
        )
    on_log(f"  published {branch_ref} ({head_sha[:8]}) to {cfg.rendezvous_remote}\n")
    return Outcome(
        status=RunStatus.COMPLETED,
        result_line=result_line,
        landable=True,
        model=model,
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
) -> Outcome:
    """Run one work order to a landable (or failed) state. Never touches main."""
    from nightshift.backends import LAUNCH_FAILED, WorkerSpec, require_backend
    from nightshift.git.worktrees import worktree_dir

    workspace = cfg.workspace
    task = order["task"]
    repo = order["repo"]
    # The work order carries the queue *label* ("main"/<name>); the engine's
    # worktree/brief helpers take the internal queue arg (main -> None).
    queue = playlists.queue_from_tasks_rel(order.get("queue") or "main")
    config_blob = order.get("config", {})
    validate_argv, validate_display = validate_cmd_from_blob(config_blob)
    prompt_validate = validate_display or DEFAULT_VALIDATE_CMD
    preflight_argv, preflight_display = preflight_cmd_from_blob(config_blob)

    # Resolve the worktree path early (deterministic from task/queue/repo) so
    # every outcome carries it — even early failures that never cut the worktree.
    wt_path = str(worktree_dir(workspace, repo, task, queue))

    # Progress snapshot the ``fail`` closure reads: refined as resolution and
    # execution advance so every failure carries whatever is known by then.
    model = str(config_blob.get("model") or "auto")
    provider = ""
    tele: dict[str, Any] = {}
    validate_ran: str | None = None

    def fail(kind: FailureKind, reason: str | None, *, line: str | None = None) -> Outcome:
        """The one failure constructor: status=error, nothing landable, and the
        current model/backend/telemetry/worktree snapshot attached."""
        return Outcome(
            status=RunStatus.ERROR,
            result_line=line if line is not None else (reason or ""),
            landable=False,
            model=model,
            backend=provider,
            failure_kind=kind,
            failure_reason=reason,
            validate_cmd=validate_ran,
            worktree=wt_path,
            **tele,
        )

    resolved_model, model_error = cfg.resolve_model(config_blob.get("model"))
    if model_error:
        return fail(FailureKind.MODEL_UNAVAILABLE, model_error)
    assert resolved_model is not None
    model = resolved_model

    provider_name, bare_model = split_model(model)
    if provider_name is None:
        return fail(
            FailureKind.MODEL_UNAVAILABLE,
            f"model '{model}' is not provider-qualified (expected provider/model)",
        )
    provider = provider_name

    # Defensive availability guard.
    if not repos.repo_available(workspace, repo):
        return fail(
            FailureKind.REPO_UNAVAILABLE,
            f"repo '{repo}' is not available in the workspace",
        )

    try:
        backend = require_backend(provider)
    except KeyError:
        return fail(
            FailureKind.BACKEND_UNAVAILABLE,
            f"unknown provider '{provider}' in model '{model}'",
        )
    if not backend.available(config_blob):
        return fail(
            FailureKind.BACKEND_UNAVAILABLE,
            f"backend '{provider}' is not available on this worker",
        )

    on_phase("worker")
    scratch = materialize_brief(workspace, repo, task, order["body"], queue=queue)
    is_split = bool(config_blob.get("split", False))
    sdir: str | None = None
    if is_split:
        sdir_path = split_output_dir(workspace, repo, task, queue=queue)
        sdir_path.mkdir(parents=True, exist_ok=True)
        sdir = str(sdir_path)
    base = "HEAD"
    if cfg.rendezvous_remote:
        base = prepare_worktree_base(
            workspace, repo, cfg.rendezvous_remote, order.get("base_ref")
        )
    try:
        wt_dir = setup_worktree(workspace, repo, task, queue=queue, base=base)
    except GitError as exc:
        # A failed `worktree add` is task-fatal but typed — never a raw traceback.
        return fail(
            FailureKind.WORKTREE_FAILED,
            str(exc),
            line="worktree setup failed",
        )
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
            split=is_split,
            split_dir=sdir,
        )
        env = worker_env(wt_dir)

        # Environment preflight — before any model spend. Keep the worker's
        # shared venv in step with the committed lockfile so a dependency that
        # landed on another machine but is missing here fails cheaply *now*,
        # instead of after the agent runs, at validate time, as an import error.
        # Fast path is a fingerprint compare; a real change/miss self-heals via
        # ``uv sync --frozen`` (lockfile-exact). Runs against the repo root,
        # where the physical ``.venv`` lives (the worktree only symlinks it).
        if preflight_argv is not None:
            on_phase("preflight")
            on_log(f"  preflight: {preflight_display}...\n")
            pre = ensure_env_synced(
                workspace / repo,
                preflight_argv=preflight_argv,
                preflight_display=preflight_display,
                env=env,
            )
            if pre.synced:
                on_log("  preflight: environment synced to lockfile\n")
            if not pre.ok:
                return fail(
                    FailureKind.PREFLIGHT_FAILED,
                    pre.detail or f"{preflight_display} failed",
                    line="preflight failed (environment not provisioned)",
                )

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
            return fail(
                FailureKind.WORKER_LAUNCH,
                result.error,
                line="worker executable not found",
            )

        has_commits = worktree_has_commits(workspace, repo, task, queue=queue)

        blocked_reason = extract_blocked_reason("".join(captured))
        if blocked_reason and not has_commits:
            return Outcome(
                status=RunStatus.BLOCKED,
                result_line=f"blocked: {blocked_reason}",
                landable=False,
                model=model,
                backend=provider,
                failure_kind=FailureKind.BLOCKED,
                failure_reason=blocked_reason,
                worktree=wt_path,
                **tele,
            )

        if result.returncode != 0:
            reason = (
                result.error or f"worker [{provider}] exited {result.returncode}"
            )
            return fail(
                FailureKind.WORKER_ERROR, reason, line=reason.splitlines()[0][:120]
            )

        if is_split:
            return Outcome(
                status=RunStatus.COMPLETED,
                result_line="decomposition run",
                landable=False,
                model=model,
                backend=provider,
                worktree=wt_path,
                **tele,
            )

        if not has_commits:
            # Whether the agent actually landed on main directly is the
            # manager's call (land()'s adopt phase) — the worker just reports
            # a completed run with nothing on its branch.
            return Outcome(
                status=RunStatus.COMPLETED,
                result_line="no changes produced (worker emitted output only)",
                landable=False,
                model=model,
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
        validate_ran = validate_display
        validate = run_interruptible(
            validate_argv,
            cwd=wt_dir,
            env=env,
            should_abort=lambda: None,
        )
        if validate.returncode != 0:
            preserve = True
            tail = (validate.stdout[-1500:] + "\n" + validate.stderr[-500:]).strip()
            on_log(f"\n── validate failed (exit {validate.returncode}) ──\n")
            if validate.stdout:
                on_log(validate.stdout[-3000:])
            if validate.stderr:
                on_log(validate.stderr[-1500:])
            on_log("\n── end validate output ──\n")
            result_line = extract_result_line(
                validate.stdout, validate.stderr,
            ) or "validate failed"
            return fail(FailureKind.VALIDATION_ERROR, tail, line=result_line)

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
