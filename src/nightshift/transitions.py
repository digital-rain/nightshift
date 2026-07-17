"""The pure transition functions (greenfield §"Transitions: pure core,
atomic shell"), split out of :mod:`nightshift.lifecycle` in Phase 9 purely for
module size — same functions, same semantics.

Each function computes a :class:`~nightshift.lifecycle.Transition` — a value
describing one atomic state change (attempt-row updates, the state CAS, task
effects, and the events to append) — applied by
``NightshiftStore.apply_transition``. Nothing here imports the store, git, or
HTTP; every policy input arrives via
:class:`~nightshift.lifecycle.SubmitPolicy`.
"""

from __future__ import annotations

from typing import Any, assert_never

from nightshift.lifecycle import (
    AttemptRef,
    AttemptState,
    FailureKind,
    FrontmatterFlag,
    GitPhase,
    LandKind,
    LandOutcome,
    Outcome,
    Progress,
    RetryAction,
    RunStatus,
    SubmitPolicy,
    TaskEffects,
    TaskHold,
    TaskHoldKind,
    Transition,
    TransitionEvent,
    WorkflowStepPolicy,
    error_state_for,
    land_failure_kind,
)
from nightshift.workflows import END, StepKind, format_visits


def _base_run_fields(outcome: Outcome) -> dict[str, Any]:
    """Fields every terminal update records: the model that ran plus the full
    telemetry (a failed/no-change run still burned turns and tokens)."""
    return {"model": outcome.model, **outcome.telemetry.model_dump()}


def _watch_failure(
    ref: AttemptRef, policy: SubmitPolicy
) -> tuple[str | None, tuple[TransitionEvent, ...]]:
    """Fold a failure into the phase-A two-in-a-row watch: a second failure
    while armed pauses the queue (unless it already is paused)."""
    if policy.watch_armed and not policy.queue_paused:
        return "consecutive_failures", (TransitionEvent(
            "queue_paused", queue=ref.queue,
            payload={"reason": "consecutive_failures", "task": ref.task},
        ),)
    return None, ()


def _retry_quarantine(
    ref: AttemptRef, detail: str
) -> tuple[FrontmatterFlag, tuple[TransitionEvent, ...]]:
    """Phase B: the retried task failed again — quarantine it and pause the
    queue with reason ``retry_failed``."""
    reason = (
        f"quarantined after failing again on retry ({detail}); "
        f"review the run logs and edit or delete the task to release it"
    )
    return FrontmatterFlag("quarantined", True, "quarantine_reason", reason), (
        TransitionEvent(
            "task_quarantined", run_id=ref.id, queue=ref.queue, task=ref.task,
            payload={"reason": reason, "streak": 2},
        ),
        TransitionEvent(
            "queue_paused", queue=ref.queue,
            payload={"reason": "retry_failed", "task": ref.task},
        ),
    )


def _immediate_quarantine(
    ref: AttemptRef, detail: str
) -> tuple[FrontmatterFlag, tuple[TransitionEvent, ...]]:
    """Quarantine on the first failure (worker quarantine mode)."""
    reason = (
        f"quarantined by worker on first failure ({detail}); "
        f"review the run logs and edit or delete the task to release it"
    )
    return FrontmatterFlag("quarantined", True, "quarantine_reason", reason), (
        TransitionEvent(
            "task_quarantined", run_id=ref.id, queue=ref.queue, task=ref.task,
            payload={"reason": reason, "streak": 1},
        ),
    )


def _looping_quarantine(
    ref: AttemptRef, attempts: int, detail: str
) -> tuple[FrontmatterFlag, tuple[TransitionEvent, ...]]:
    """Quarantine a task stuck re-executing without progress. ``attempts`` is
    the counter value including the current outcome."""
    reason = (
        f"quarantined after {attempts} consecutive runs with no progress "
        f"({detail}); execution halted to protect budget — review the run "
        f"logs and edit or delete the task to release it"
    )
    return FrontmatterFlag("quarantined", True, "quarantine_reason", reason), (
        TransitionEvent(
            "task_quarantined", run_id=ref.id, queue=ref.queue, task=ref.task,
            payload={"reason": reason, "streak": attempts},
        ),
    )


def _failure_ladder(
    ref: AttemptRef,
    policy: SubmitPolicy,
    *,
    quarantine_detail: str,
    failure_reason: str,
    retry_pause_after_quarantine: bool,
) -> tuple[
    bool, list[FrontmatterFlag], list[TransitionEvent],
    str | None, bool | None, float | None,
]:
    """The no-progress failure ladder shared by worker errors and no-change
    completions: worker-quarantine → counter-threshold quarantine → failed
    flag + watch arm + retry backoff (+ retry quarantine when the failed run
    was a retry). Every rung counts the outcome (the caller sets
    ``Progress.INCREMENT``); the threshold decision consumes the persisted
    counter via :class:`RetryPolicy` instead of the retired 50-run scan, so
    interleaved other-task runs can no longer mask a streak.

    ``retry_pause_after_quarantine`` preserves a pre-Phase-4 placement
    asymmetry: a threshold-quarantined *error* on a retried task still pauses
    the queue with ``retry_failed``, while a threshold-quarantined no-change
    completion does not.

    Returns ``(quarantined, frontmatter, events, pause, watch, backoff)``.
    """
    frontmatter: list[FrontmatterFlag] = []
    events: list[TransitionEvent] = []
    attempts = policy.attempts_without_progress + 1
    if policy.retry.immediate_quarantine:
        flag, evs = _immediate_quarantine(ref, quarantine_detail)
        frontmatter.append(flag)
        events += evs
        return True, frontmatter, events, None, None, None
    if policy.retry.quarantine_after > 0 and attempts >= policy.retry.quarantine_after:
        flag, evs = _looping_quarantine(ref, attempts, quarantine_detail)
        frontmatter.append(flag)
        events += evs
        pause: str | None = None
        if retry_pause_after_quarantine and policy.was_retry:
            pause = "retry_failed"
            events.append(TransitionEvent(
                "queue_paused", queue=ref.queue,
                payload={"reason": "retry_failed", "task": ref.task},
            ))
        return True, frontmatter, events, pause, None, None
    frontmatter.append(
        FrontmatterFlag("failed", True, "failed_reason", failure_reason)
    )
    pause, watch_events = _watch_failure(ref, policy)
    events += watch_events
    if policy.was_retry:
        flag, retry_events = _retry_quarantine(ref, failure_reason)
        frontmatter.append(flag)
        events += retry_events
        pause = "retry_failed"
    return False, frontmatter, events, pause, True, policy.retry.backoff.delay(attempts)


def _blocked_transition(
    ref: AttemptRef, outcome: Outcome, policy: SubmitPolicy
) -> Transition:
    """An honest block: record it, hold the task resolvable, never land."""
    reason = outcome.failure_reason or outcome.result_line or "blocked"
    events: list[TransitionEvent] = []
    frontmatter: list[FrontmatterFlag] = []
    pause, watch_events = _watch_failure(ref, policy)
    events += watch_events
    if policy.was_retry:
        flag, retry_events = _retry_quarantine(ref, reason)
        frontmatter.append(flag)
        events += retry_events
        pause = "retry_failed"
    events.append(TransitionEvent(
        "task_blocked", queue=ref.queue, task=ref.task, payload={"reason": reason},
    ))
    events.append(TransitionEvent(
        "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
        payload={"status": "blocked", "result_line": outcome.result_line},
    ))
    return Transition(
        ref=ref,
        fields=dict(
            result_line=outcome.result_line,
            failure_kind=outcome.failure_kind or FailureKind.BLOCKED,
            failure_reason=outcome.failure_reason,
            **_base_run_fields(outcome),
        ),
        state=AttemptState.BLOCKED,
        effects=TaskEffects(
            hold=TaskHold(TaskHoldKind.BLOCKED, reason, retry_eligible=True),
            frontmatter=tuple(frontmatter),
            pause_queue=pause,
            watch_armed=True,
        ),
        events=tuple(events),
        response={"landed": False, "status": "blocked"},
    )


def _error_fields(outcome: Outcome) -> dict[str, Any]:
    return dict(
        result_line=outcome.result_line,
        failure_kind=outcome.failure_kind,
        failure_reason=outcome.failure_reason,
        **_base_run_fields(outcome),
    )


def _environment_failure_transition(
    ref: AttemptRef, outcome: Outcome
) -> Transition:
    """An environment failure (the box is at fault, not the task): record it
    and release, but stay neutral to the task — no counter increment, no
    failed flag, no watch — and cool the submitting worker down so a broken
    box stops eating the queue while other workers retry the task."""
    return Transition(
        ref=ref,
        fields=_error_fields(outcome),
        state=error_state_for(outcome.failure_kind),
        effects=TaskEffects(worker_cooldown=True),
        events=(TransitionEvent(
            "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
            payload={"status": outcome.status, "result_line": outcome.result_line},
        ),),
        response={"landed": False, "status": outcome.status, "quarantined": False},
    )


def _hold_reason(kind: FailureKind, outcome: Outcome) -> str:
    """Operator-visible reason for a HOLD-classified error. Validation keeps
    its historical wording (wire compat); every other kind is prefixed with
    the kind so the hold says what actually happened (Phase 6 routes the
    MERGE_* kinds here)."""
    if kind is FailureKind.VALIDATION_ERROR:
        return "validation failed: " + (
            outcome.result_line or "validate command returned non-zero"
        )
    detail = outcome.result_line or outcome.failure_reason
    return f"{kind}: {detail}" if detail else str(kind)


def _error_transition(
    ref: AttemptRef, outcome: Outcome, policy: SubmitPolicy
) -> Transition:
    """A worker error, routed by :meth:`RetryPolicy.on_failure`: environment
    kinds cool the worker down (neutral to the task), HOLD kinds (validation,
    merge, blocked) block resolvable, and the rest climb the quarantine
    ladder. Aborts/skips are neutral and go through
    :func:`_neutral_transition` instead."""
    kind = outcome.failure_kind or FailureKind.WORKER_ERROR
    action = policy.retry.on_failure(kind)
    events: list[TransitionEvent] = []
    frontmatter: list[FrontmatterFlag] = []
    hold: TaskHold | None = None
    pause: str | None = None
    watch: bool | None = None
    backoff: float | None = None
    quarantined = False
    match action:
        case RetryAction.RETRY_ELSEWHERE:
            return _environment_failure_transition(ref, outcome)
        case RetryAction.HOLD:
            # The work is preserved and recoverable (a validation failure
            # keeps its branch; merge kinds go to resolve) — block (needs
            # resolve) rather than arm the policy. The counter still counts
            # the no-progress run.
            hold = TaskHold(TaskHoldKind.BLOCKED, _hold_reason(kind, outcome))
            events.append(TransitionEvent(
                "task_blocked", queue=ref.queue, task=ref.task,
                payload={"reason": str(kind), "detail": outcome.failure_reason},
            ))
        case RetryAction.RETRY | RetryAction.QUARANTINE:
            quarantined, frontmatter, events, pause, watch, backoff = _failure_ladder(
                ref, policy,
                quarantine_detail="worker error",
                failure_reason=(
                    outcome.result_line or outcome.failure_reason or "worker error"
                ),
                retry_pause_after_quarantine=True,
            )
        case _:
            assert_never(action)
    events.append(TransitionEvent(
        "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
        payload={"status": outcome.status, "result_line": outcome.result_line},
    ))
    return Transition(
        ref=ref,
        fields=_error_fields(outcome),
        state=error_state_for(kind),
        effects=TaskEffects(
            hold=hold,
            progress=Progress.INCREMENT,
            next_eligible_in=backoff,
            frontmatter=tuple(frontmatter),
            pause_queue=pause,
            watch_armed=watch,
        ),
        events=tuple(events),
        response={"landed": False, "status": outcome.status, "quarantined": quarantined},
    )


def _neutral_transition(ref: AttemptRef, outcome: Outcome) -> Transition:
    """Operator-driven or indeterminate outcomes (aborted/skipped/running):
    record and release, no policy action. A ``running`` submit is degenerate
    (no real worker sends it); pre-Phase-8 it left the row ``running`` with a
    released lease forever — the deliberate fix stores it as ABORTED (the
    HTTP response still echoes the wire status verbatim)."""
    state = (
        AttemptState.SKIPPED
        if outcome.status is RunStatus.SKIPPED
        else AttemptState.ABORTED
    )
    return Transition(
        ref=ref,
        fields=dict(
            result_line=outcome.result_line,
            failure_kind=outcome.failure_kind,
            failure_reason=outcome.failure_reason,
            **_base_run_fields(outcome),
        ),
        state=state,
        events=(TransitionEvent(
            "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
            payload={"status": outcome.status, "result_line": outcome.result_line},
        ),),
        response={"landed": False, "status": outcome.status, "quarantined": False},
    )


def on_submit(
    ref: AttemptRef, outcome: Outcome, policy: SubmitPolicy
) -> Transition | GitPhase:
    """The submit transition table. Returns the Transition to apply, or the
    :class:`GitPhase` the caller must execute first for completed submits
    (whose result then feeds :func:`on_land_result` / :func:`on_split_result`).
    """
    if policy.workflow_step is not None:
        return on_workflow_step(ref, outcome, policy)
    match outcome.status:
        case RunStatus.BLOCKED:
            return _blocked_transition(ref, outcome, policy)
        case RunStatus.ERROR:
            return _error_transition(ref, outcome, policy)
        case RunStatus.COMPLETED:
            if policy.split and not outcome.landable:
                return GitPhase.HARVEST_SPLIT
            if outcome.landable:
                return GitPhase.LAND
            return GitPhase.ADOPT_CHECK
        case RunStatus.RUNNING | RunStatus.SKIPPED | RunStatus.ABORTED:
            return _neutral_transition(ref, outcome)
        case _:
            assert_never(outcome.status)


def on_split_result(
    ref: AttemptRef, outcome: Outcome, created: list[str]
) -> Transition:
    """A decomposition run's harvest: complete the parent, clear its overlay,
    and report the enqueued subtasks. Split runs never land."""
    if created:
        result_line = (
            f"decomposed into {len(created)} subtask(s): " + ", ".join(created)
        )
    else:
        result_line = "decomposition run produced no subtasks"
    return Transition(
        ref=ref,
        fields=dict(
            result_line=result_line,
            **_base_run_fields(outcome),
        ),
        state=AttemptState.NO_CHANGE,
        # The parent is consumed by the split; its counter dies with it.
        effects=TaskEffects(clear_hold=True, progress=Progress.RESET),
        events=(
            TransitionEvent(
                "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
                payload={
                    "status": "completed",
                    "result_line": result_line,
                    "subtasks": created,
                },
            ),
            TransitionEvent("queue_changed", queue=ref.queue),
        ),
        response={
            "landed": False, "status": "completed",
            "split": True, "subtasks": created,
        },
    )


def _no_change_transition(
    ref: AttemptRef, outcome: Outcome, policy: SubmitPolicy
) -> Transition:
    """Completed but nothing landed and main didn't advance: record success
    with no commit; the run still counts toward the quarantine counter."""
    result_line = outcome.result_line or "no changes"
    quarantined, frontmatter, events, pause, watch, backoff = _failure_ladder(
        ref, policy,
        quarantine_detail="no changes produced",
        failure_reason="no changes produced",
        retry_pause_after_quarantine=False,
    )
    events.append(TransitionEvent(
        "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
        payload={"status": "completed", "result_line": result_line},
    ))
    events.append(TransitionEvent("queue_changed", queue=ref.queue))
    return Transition(
        ref=ref,
        fields=dict(
            result_line=result_line,
            **_base_run_fields(outcome),
        ),
        state=AttemptState.NO_CHANGE,
        effects=TaskEffects(
            progress=Progress.INCREMENT,
            next_eligible_in=backoff,
            frontmatter=tuple(frontmatter),
            pause_queue=pause,
            watch_armed=watch,
        ),
        events=tuple(events),
        response={
            "landed": False, "status": "completed",
            "no_changes": True, "quarantined": quarantined,
        },
    )


def _landed_transition(
    ref: AttemptRef, outcome: Outcome, land: LandOutcome, policy: SubmitPolicy
) -> Transition:
    """A confirmed land (squash, adopted agent land, or a land whose checkout
    advance was refused — the ref is authoritative): record success, clear
    every hold, disarm the failure watch, consume the brief (non-evergreen)."""
    if land.kind is LandKind.ADOPTED and not outcome.landable:
        # A non-landable submit whose agent landed on main directly: the run
        # records the adoption, not the worker's "no changes" line.
        result_line = "agent landed on main"
    else:
        result_line = outcome.result_line or land.detail or "landed"
    return Transition(
        ref=ref,
        fields=dict(
            result_line=result_line,
            commit_sha=land.sha,
            loc=land.loc,
            remote=land.remote,
            pushed=land.pushed,
            **_base_run_fields(outcome),
        ),
        state=AttemptState.LANDED,
        effects=TaskEffects(
            clear_hold=True,
            progress=Progress.RESET,
            watch_armed=False,
            drop_brief=not policy.evergreen,
        ),
        events=(
            TransitionEvent(
                "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
                payload={
                    "status": "completed",
                    "commit_sha": land.sha,
                    "remote": land.remote,
                    "pushed": land.pushed,
                    "pr_url": land.pr_url,
                },
            ),
            TransitionEvent("queue_changed", queue=ref.queue),
        ),
        response={
            "landed": True, "sha": land.sha, "remote": land.remote,
            "pushed": land.pushed, "pr_url": land.pr_url,
        },
    )


def _land_failed_transition(
    ref: AttemptRef, outcome: Outcome, land: LandOutcome, policy: SubmitPolicy
) -> Transition:
    """A refused land. The branch is preserved, so a conflict or recoverable
    rejection holds the task blocked (resolvable) and may auto-escalate; an
    unretryable transport failure releases with an error and no hold."""
    failure_kind = land_failure_kind(land.kind)
    resolvable = land.kind in (LandKind.CONFLICT, LandKind.PUSH_REJECTED) or (
        land.kind is LandKind.TRANSPORT_FAILED and land.retryable
    )
    events: list[TransitionEvent] = []
    hold: TaskHold | None = None
    pause: str | None = None
    watch: bool | None = None
    if resolvable:
        land_reason = "needs resolve: " + (
            land.detail.splitlines()[0] if land.detail else failure_kind
        )
        hold = TaskHold(TaskHoldKind.BLOCKED, land_reason, retry_eligible=False)
        watch = True
        pause, watch_events = _watch_failure(ref, policy)
        events += watch_events
        events.append(TransitionEvent(
            "task_blocked", queue=ref.queue, task=ref.task,
            payload={"reason": failure_kind, "detail": land.detail},
        ))
    events.append(TransitionEvent(
        "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
        payload={"status": RunStatus.ERROR, "failure_kind": failure_kind},
    ))
    return Transition(
        ref=ref,
        fields=dict(
            result_line=(
                land.detail.splitlines()[0][:200] if land.detail else "land failed"
            ),
            failure_kind=failure_kind,
            failure_reason=land.detail,
            **_base_run_fields(outcome),
        ),
        state=error_state_for(failure_kind),
        effects=TaskEffects(
            hold=hold,
            pause_queue=pause,
            watch_armed=watch,
            start_resolve=(
                policy.auto_resolve and resolvable and not policy.pr_mode
            ),
        ),
        events=tuple(events),
        response={
            "landed": False, "conflict": land.kind is LandKind.CONFLICT,
            "detail": land.detail, "resolving": False,
        },
    )


def on_land_result(
    ref: AttemptRef, outcome: Outcome, land: LandOutcome, policy: SubmitPolicy
) -> Transition:
    """The landing transition table (git greenfield §9), exhaustive over
    :class:`LandKind`: successes (including adopted agent lands and a land
    whose checkout advance was refused), nothing-to-land, or refused
    (conflict / rejection / transport failure)."""
    match land.kind:
        case LandKind.NO_CHANGES:
            return _no_change_transition(ref, outcome, policy)
        case LandKind.LANDED | LandKind.CHECKOUT_BEHIND | LandKind.ADOPTED:
            return _landed_transition(ref, outcome, land, policy)
        case LandKind.CONFLICT | LandKind.PUSH_REJECTED | LandKind.TRANSPORT_FAILED:
            return _land_failed_transition(ref, outcome, land, policy)
        case _:
            assert_never(land.kind)


def on_land_interrupted(ref: AttemptRef) -> Transition:
    """A ``LANDING`` attempt found at manager startup whose land can neither
    be verified (no ``Nightshift-Attempt`` trailer on main) nor re-enqueued
    (no surviving branch): the conservative park. CONFLICT (``merge_rejected``)
    with the task held blocked for a resolve — the branch (if any) is
    preserved, nothing is double-landed, and the operator (or auto-resolve via
    the Resolve button) recovers it. CAS: expected LANDING."""
    detail = "manager restarted mid-land; task branch preserved for resolve"
    return Transition(
        ref=ref,
        fields=dict(
            result_line=detail,
            failure_kind=FailureKind.MERGE_REJECTED,
            failure_reason=detail,
        ),
        state=AttemptState.CONFLICT,
        effects=TaskEffects(
            hold=TaskHold(
                TaskHoldKind.BLOCKED, f"needs resolve: {detail}",
                retry_eligible=False,
            ),
        ),
        events=(
            TransitionEvent(
                "task_blocked", queue=ref.queue, task=ref.task,
                payload={"reason": FailureKind.MERGE_REJECTED, "detail": detail},
            ),
            TransitionEvent(
                "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
                payload={
                    "status": RunStatus.ERROR,
                    "failure_kind": FailureKind.MERGE_REJECTED,
                },
            ),
        ),
    )


def on_land_recovered(
    ref: AttemptRef, sha: str, *, note: str | None = None
) -> Transition:
    """Startup recovery verified a mid-land attempt DID reach canonical main
    (its ``Nightshift-Attempt`` trailer is on a main commit): complete it as
    landed without re-running any git work. Mirrors the success effects of
    :func:`on_land_result` (clear holds, reset the counter, disarm the
    watch). ``note`` appends an operator-facing caveat to the result line
    (the PR-mode "trailer proves the squash, not the PR" case). CAS:
    expected LANDING."""
    result_line = "recovered: landed (manager restarted mid-land)" + (note or "")
    return Transition(
        ref=ref,
        fields=dict(result_line=result_line, commit_sha=sha),
        state=AttemptState.LANDED,
        effects=TaskEffects(
            clear_hold=True,
            progress=Progress.RESET,
            watch_armed=False,
        ),
        events=(
            TransitionEvent(
                "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
                payload={
                    "status": "completed",
                    "commit_sha": sha,
                    "result_line": result_line,
                },
            ),
            TransitionEvent("queue_changed", queue=ref.queue),
        ),
    )


def on_land_enqueued(
    ref: AttemptRef, *, branch_ref: str | None, head_sha: str | None
) -> Transition:
    """Entering the land phase: RUNNING → LANDING with the WIP ref/tip
    persisted for a restart re-enqueue. A CAS (expected RUNNING) so a stale
    submit can't enqueue a land for an attempt another actor already moved.
    No events — the land's completion transition reports the outcome."""
    return Transition(
        ref=ref,
        fields={"phase": "landing", "branch_ref": branch_ref, "head_sha": head_sha},
        state=AttemptState.LANDING,
    )


def on_operator_stop(ref: AttemptRef) -> Transition:
    """Operator stop/skip: abort a live attempt. Pre-Phase-8 the stop only
    cancelled the lease and the run row stayed ``running`` forever — the
    deliberate fix stores ABORTED (with ``finished_at``). The handler applies
    it CAS RUNNING first, then LANDING (a stop cancels mid-land too, exactly
    as today), and keeps emitting ``run_finished`` itself."""
    return Transition(
        ref=ref,
        fields={},
        state=AttemptState.ABORTED,
    )


# --------------------------------------------------------------------------- #
# Workflow step transitions (spec §6.3–§6.5)
# --------------------------------------------------------------------------- #


def _advance_visits(step_policy: WorkflowStepPolicy) -> dict[str, object | None]:
    """The engine_meta write that moves the cursor onto ``route_to`` and counts
    that entry (§6.4 entry-based counting)."""
    visits = dict(step_policy.visits)
    visits[step_policy.route_to] = visits.get(step_policy.route_to, 0) + 1
    return {
        "workflow_step": step_policy.route_to,
        "workflow_visits": format_visits(visits),
    }


def _workflow_budget_quarantine(
    ref: AttemptRef, outcome: Outcome, step_policy: WorkflowStepPolicy,
    *, write_artifact: tuple[str, str] | None,
) -> Transition:
    """Destination step's budget is exhausted: quarantine (the work was good,
    so the artifact still commits). Reuses the ``_failure_ladder`` quarantine
    shape (frontmatter flag + event)."""
    reason = step_policy.exhausted_reason or (
        f"workflow budget exhausted at '{step_policy.route_to}'"
    )
    flag = FrontmatterFlag("quarantined", True, "quarantine_reason", reason)
    ev = [
        TransitionEvent(
            "task_quarantined", run_id=ref.id, queue=ref.queue, task=ref.task,
            payload={"reason": reason},
        ),
        TransitionEvent(
            "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
            payload={"status": "completed", "result_line": outcome.result_line},
        ),
    ]
    return Transition(
        ref=ref,
        fields=dict(result_line=outcome.result_line, **_base_run_fields(outcome)),
        state=AttemptState.NO_CHANGE,
        effects=TaskEffects(
            frontmatter=(flag,),
            write_artifact=write_artifact,
        ),
        events=tuple(ev),
        response={"landed": False, "status": "completed", "quarantined": True},
    )


def _workflow_end(
    ref: AttemptRef, outcome: Outcome, step_policy: WorkflowStepPolicy,
    *, write_artifact: tuple[str, str] | None,
) -> Transition:
    """A doc step routing to $end: the workflow completes without a land. The
    brief + final artifact are retained (``completed`` flag); evergreen resets
    instead (clear meta + delete artifacts)."""
    result_line = outcome.result_line or "workflow complete"
    if step_policy.evergreen:
        effects = TaskEffects(
            progress=Progress.RESET,
            write_artifact=write_artifact,
            workflow_reset=True,
        )
    else:
        effects = TaskEffects(
            progress=Progress.RESET,
            write_artifact=write_artifact,
            frontmatter=(FrontmatterFlag("completed", True),),
        )
    return Transition(
        ref=ref,
        fields=dict(result_line=result_line, **_base_run_fields(outcome)),
        state=AttemptState.NO_CHANGE,
        effects=effects,
        events=(
            TransitionEvent(
                "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
                payload={"status": "completed", "result_line": result_line},
            ),
            TransitionEvent("queue_changed", queue=ref.queue),
        ),
        response={"landed": False, "status": "completed", "workflow_complete": True},
    )


def _workflow_doc_advance(
    ref: AttemptRef, outcome: Outcome, step_policy: WorkflowStepPolicy,
    *, write_artifact: tuple[str, str] | None,
) -> Transition:
    """A doc step completing and routing to a mid-workflow step: commit the
    artifact, advance the cursor (counting the destination visit), reset the
    no-progress counter, retain the brief."""
    result_line = outcome.result_line or f"produced '{step_policy.output}'"
    return Transition(
        ref=ref,
        fields=dict(result_line=result_line, **_base_run_fields(outcome)),
        state=AttemptState.NO_CHANGE,
        effects=TaskEffects(
            progress=Progress.RESET,
            write_artifact=write_artifact,
            engine_meta=_advance_visits(step_policy),
        ),
        events=(
            TransitionEvent(
                "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
                payload={"status": "completed", "result_line": result_line},
            ),
            TransitionEvent("queue_changed", queue=ref.queue),
        ),
        response={"landed": False, "status": "completed", "workflow_step": step_policy.route_to},
    )


def on_workflow_step(
    ref: AttemptRef, outcome: Outcome, policy: SubmitPolicy
) -> Transition | GitPhase:
    """Handles every submit whose ``policy.workflow_step`` is set. ``on_submit``
    delegates here. Doc steps advance the cursor / complete / quarantine; code
    and split steps hand back the git phase the caller executes next (whose
    result feeds :func:`on_workflow_land` / :func:`on_workflow_split`)."""
    step = policy.workflow_step
    assert step is not None

    # Failures never move the cursor — delegate verbatim (no engine_meta).
    match outcome.status:
        case RunStatus.BLOCKED:
            return _blocked_transition(ref, outcome, policy)
        case RunStatus.ERROR:
            return _error_transition(ref, outcome, policy)
        case RunStatus.RUNNING | RunStatus.SKIPPED | RunStatus.ABORTED:
            return _neutral_transition(ref, outcome)
        case RunStatus.COMPLETED:
            pass
        case _:
            assert_never(outcome.status)

    match step.kind:
        case StepKind.DOC:
            if outcome.document is None:
                # ``model_copy`` preserves the concrete outcome subclass (a
                # ``SubmitBody`` here) so optional-on-the-wire fields such as
                # ``backend`` don't fail a plain-``Outcome`` revalidation.
                err = outcome.model_copy(update={
                    "status": RunStatus.ERROR,
                    "failure_kind": FailureKind.WORKER_ERROR,
                    "failure_reason": "doc step produced no document",
                    "result_line": "doc step produced no document",
                })
                return _error_transition(ref, err, policy)
            write_artifact = (
                (step.output, outcome.document) if step.output else None
            )
            if step.route_to == END:
                return _workflow_end(ref, outcome, step, write_artifact=write_artifact)
            if step.dest_visits_exhausted:
                return _workflow_budget_quarantine(
                    ref, outcome, step, write_artifact=write_artifact,
                )
            return _workflow_doc_advance(
                ref, outcome, step, write_artifact=write_artifact,
            )
        case StepKind.CODE:
            # Landable code step → LAND (caller feeds on_workflow_land).
            if outcome.landable:
                return GitPhase.LAND
            return GitPhase.ADOPT_CHECK
        case StepKind.SPLIT:
            return GitPhase.HARVEST_SPLIT
        case _:
            assert_never(step.kind)


def on_workflow_land(
    ref: AttemptRef, outcome: Outcome, land: LandOutcome, policy: SubmitPolicy
) -> Transition:
    """Land-result counterpart for workflow code steps (mirrors
    :func:`on_land_result`). Successful lands advance the cursor or complete
    the workflow; failures delegate to :func:`_land_failed_transition`."""
    step = policy.workflow_step
    assert step is not None

    match land.kind:
        case LandKind.NO_CHANGES:
            return _no_change_transition(ref, outcome, policy)
        case LandKind.LANDED | LandKind.CHECKOUT_BEHIND | LandKind.ADOPTED:
            pass
        case LandKind.CONFLICT | LandKind.PUSH_REJECTED | LandKind.TRANSPORT_FAILED:
            return _land_failed_transition(ref, outcome, land, policy)
        case _:
            assert_never(land.kind)

    result_line = outcome.result_line or land.detail or "landed"
    base_fields = dict(
        result_line=result_line,
        commit_sha=land.sha,
        loc=land.loc,
        remote=land.remote,
        pushed=land.pushed,
        **_base_run_fields(outcome),
    )
    land_events = (
        TransitionEvent(
            "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
            payload={
                "status": "completed", "commit_sha": land.sha,
                "remote": land.remote, "pushed": land.pushed, "pr_url": land.pr_url,
            },
        ),
        TransitionEvent("queue_changed", queue=ref.queue),
    )
    land_response = {
        "landed": True, "sha": land.sha, "remote": land.remote,
        "pushed": land.pushed, "pr_url": land.pr_url,
    }

    if step.route_to == END:
        # $end from a code step: land-and-consume (non-evergreen) or retain+reset.
        effects = TaskEffects(
            clear_hold=True,
            progress=Progress.RESET,
            watch_armed=False,
            drop_brief=not step.evergreen,
            workflow_reset=step.evergreen,
        )
    elif step.dest_visits_exhausted:
        reason = step.exhausted_reason or (
            f"workflow budget exhausted at '{step.route_to}'"
        )
        return Transition(
            ref=ref,
            fields=base_fields,
            state=AttemptState.LANDED,
            effects=TaskEffects(
                clear_hold=True,
                progress=Progress.RESET,
                watch_armed=False,
                drop_brief=False,
                frontmatter=(
                    FrontmatterFlag("quarantined", True, "quarantine_reason", reason),
                ),
            ),
            events=land_events + (
                TransitionEvent(
                    "task_quarantined", run_id=ref.id, queue=ref.queue, task=ref.task,
                    payload={"reason": reason},
                ),
            ),
            response=land_response,
        )
    else:
        # Mid-workflow code step: advance the cursor, retain the brief.
        effects = TaskEffects(
            clear_hold=True,
            progress=Progress.RESET,
            watch_armed=False,
            drop_brief=False,
            engine_meta=_advance_visits(step),
        )

    return Transition(
        ref=ref,
        fields=base_fields,
        state=AttemptState.LANDED,
        effects=effects,
        events=land_events,
        response=land_response,
    )


def on_workflow_split(
    ref: AttemptRef, outcome: Outcome, created: list[str], policy: SubmitPolicy
) -> Transition:
    """Split-harvest counterpart for workflow split steps. Children created →
    :func:`on_split_result` shape (parent consumed unless evergreen). Zero
    children → parent retained, cursor stays put (no visit burned),
    ``Progress.INCREMENT`` so the no-change ladder bounds repeated empty
    splits."""
    step = policy.workflow_step
    assert step is not None

    if not created:
        result_line = "decomposition run produced no subtasks"
        return Transition(
            ref=ref,
            fields=dict(result_line=result_line, **_base_run_fields(outcome)),
            state=AttemptState.NO_CHANGE,
            effects=TaskEffects(progress=Progress.INCREMENT),
            events=(
                TransitionEvent(
                    "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
                    payload={
                        "status": "completed", "result_line": result_line,
                        "subtasks": [],
                    },
                ),
                TransitionEvent("queue_changed", queue=ref.queue),
            ),
            response={
                "landed": False, "status": "completed",
                "split": True, "subtasks": [],
            },
        )

    result_line = f"decomposed into {len(created)} subtask(s): " + ", ".join(created)
    effects = TaskEffects(
        clear_hold=True,
        progress=Progress.RESET,
        workflow_reset=step.evergreen,
    )
    return Transition(
        ref=ref,
        fields=dict(result_line=result_line, **_base_run_fields(outcome)),
        state=AttemptState.NO_CHANGE,
        effects=effects,
        events=(
            TransitionEvent(
                "task_result", run_id=ref.id, queue=ref.queue, task=ref.task,
                payload={
                    "status": "completed", "result_line": result_line,
                    "subtasks": created,
                },
            ),
            TransitionEvent("queue_changed", queue=ref.queue),
        ),
        response={
            "landed": False, "status": "completed",
            "split": True, "subtasks": created,
        },
    )


def on_deadline(ref: AttemptRef) -> Transition:
    """Deadline expiry as a transition: RUNNING → EXPIRED. Pre-Phase-8 the
    reclaim expired only the lease and the run row stayed ``running`` with
    ``finished_at = NULL`` forever — the deliberate fix makes EXPIRED a
    terminal attempt state (the applier stamps ``finished_at``); ``/api/runs``
    projects the new ``expired`` status. The task overlay and event log stay
    untouched, as today. LANDING attempts are structurally exempt (a
    different state)."""
    return Transition(
        ref=ref,
        fields={},
        state=AttemptState.EXPIRED,
    )
