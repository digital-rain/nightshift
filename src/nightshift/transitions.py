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
    error_state_for,
    land_failure_kind,
)


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
