"""Typed lifecycle vocabulary — every status, kind, and outcome shape, once —
plus the pure transition core.

This module is the single home for the run/lease/task-hold status strings and
for the one ``Outcome`` shape shared by the worker's executor, the wire (the
submit payload / ``SubmitBody``), and the store (see
``docs/spec/greenfield-task-lifecycle.md`` §"Typed vocabulary" and the Phase 1
plan in ``docs/spec/rebuild-in-place-migration-plan.md``).

Every enum is a :class:`~enum.StrEnum` whose values are today's literal wire
strings, so JSON payloads, DB rows, and the operator UI stay byte-identical
(migration ground rule 2). The greenfield's nested ``failure``/``telemetry``
submodels are adapted to a *flat* ``Outcome`` for the same reason — the typed
views are exposed as the :attr:`Outcome.failure` / :attr:`Outcome.telemetry`
accessors instead of nested wire keys.

Phase 4 grows the pure core (greenfield §"Transitions: pure core, atomic
shell"): :class:`Transition` is a value describing one atomic state change —
run-row updates, the lease status move, task effects, and the events to
append — computed by the pure functions :func:`on_submit`,
:func:`on_land_result`, :func:`on_split_result`, and :func:`on_deadline`, and
applied by ``NightshiftStore.apply_transition`` (CAS on the lease, single
transaction, events as a transactional outbox). Nothing here imports the
store, git, or HTTP; every policy input the old handler read from the store or
config arrives via :class:`SubmitPolicy`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum, auto
from typing import Any, assert_never

from pydantic import BaseModel


class RunStatus(StrEnum):
    """A run's lifecycle status (``runs.status`` column / event payloads)."""

    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ERROR = "error"
    SKIPPED = "skipped"
    ABORTED = "aborted"


# Terminal run statuses — every run reaching one of these gets ``finished_at``.
# ``blocked`` IS terminal: the *run* is over (the task is what stays held), so
# it finishes like any other outcome. Pre-Phase-1 it was missing from the
# hand-written tuples and blocked runs kept ``finished_at = NULL`` forever — a
# deliberate behavior fix.
RUN_TERMINAL_STATUSES = frozenset(RunStatus) - {RunStatus.RUNNING}

# Statuses an origin run may be in for an out-of-process resolve result to be
# honored (the resolve-result fence added in Phase 0).
RUN_RESOLVABLE_STATUSES = frozenset({RunStatus.ERROR, RunStatus.BLOCKED})


class LeaseStatus(StrEnum):
    """A lease's status (``leases.status`` column). The historical
    ``'submitted'`` status was never written by any code path and is dropped
    from the vocabulary (and from both stores' active filters)."""

    LEASED = "leased"
    RELEASED = "released"
    LANDED = "landed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# The one live lease status: a task with a lease in this set cannot be
# re-leased.
LEASE_ACTIVE_STATUSES = frozenset({LeaseStatus.LEASED})

# Statuses that stamp ``released_at`` when set. ``cancelled`` deliberately
# stays out to preserve today's store behavior exactly (operator stop/skip has
# never written released_at).
LEASE_RELEASED_AT_STATUSES = frozenset({
    LeaseStatus.RELEASED, LeaseStatus.LANDED, LeaseStatus.EXPIRED,
})


class TaskHoldKind(StrEnum):
    """Why a task is held out of dispatch (the ``tasks`` overlay ``state``
    column, plus the frontmatter-derived states the queue views surface)."""

    BLOCKED = "blocked"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    REPO_UNAVAILABLE = "repo_unavailable"


class FailureKind(StrEnum):
    """Failure taxonomy carried on outcomes/runs (``failure_kind``)."""

    # environment — the worker/box is at fault; the work never really started.
    MODEL_UNAVAILABLE = "model_unavailable"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    REPO_UNAVAILABLE = "repo_unavailable"
    PREFLIGHT_FAILED = "preflight_failed"
    WORKTREE_FAILED = "worktree_failed"
    WORKER_LAUNCH = "worker_launch"
    PUBLISH_FAILED = "publish_failed"
    # task — the work itself failed (or honestly declared a hold).
    WORKER_ERROR = "worker_error"
    VALIDATION_ERROR = "validation_error"
    BLOCKED = "blocked"
    # integration — landing failed; goes to resolve, not retry.
    MERGE_CONFLICT = "merge_conflict"
    MERGE_REJECTED = "merge_rejected"
    # legacy single-process runner kinds (engine.run_task / events.py);
    # retired with that path in Phase 9.
    REPO_CONFIG = "repo_config"
    DISK = "disk"
    ABORTED = "aborted"


class LandingMode(StrEnum):
    """Remote landing policy: local-only, direct push, or PR."""

    NONE = "none"
    PUSH = "push"
    PR = "pr"

    @property
    def is_remote(self) -> bool:
        """True when landing involves a remote (the old ``in ("push", "pr")``)."""
        match self:
            case LandingMode.PUSH | LandingMode.PR:
                return True
            case LandingMode.NONE:
                return False
            case _:
                assert_never(self)


class Telemetry(BaseModel):
    """Best-effort agent telemetry captured from the backend run (``None``
    when the backend can't report it), plus the validate command the worker
    actually ran and the worktree it used. Recorded on every outcome — a
    failed/no-change run still burned turns and tokens."""

    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    validate_cmd: str | None = None
    worktree: str | None = None


class Failure(BaseModel):
    """A typed (kind, reason) failure view over an :class:`Outcome`."""

    kind: FailureKind
    reason: str | None = None


class Outcome(Telemetry):
    """The one outcome shape: returned by the worker's executor, posted over
    the wire (``loop._submit``), embedded flat in the submit endpoint's body
    (``SubmitBody``), and the source of the run-row update + the local-store
    finish record.

    Inherits :class:`Telemetry` so the telemetry fields stay flat on the wire
    (byte-identical to the pre-Phase-1 payload); :attr:`telemetry` and
    :attr:`failure` expose the typed sub-views.
    """

    status: RunStatus
    result_line: str = ""
    landable: bool = False
    model: str | None = None
    backend: str = ""
    # Cross-machine landing (transport B): the WIP ref the worker published its
    # validated branch to, and the branch tip SHA the manager re-verifies after
    # fetching. Both None when co-located (no rendezvous remote configured).
    branch_ref: str | None = None
    head_sha: str | None = None
    failure_kind: FailureKind | None = None
    failure_reason: str | None = None

    @property
    def failure(self) -> Failure | None:
        if self.failure_kind is None:
            return None
        return Failure(kind=self.failure_kind, reason=self.failure_reason)

    @property
    def telemetry(self) -> Telemetry:
        return Telemetry(**{name: getattr(self, name) for name in Telemetry.model_fields})


# --------------------------------------------------------------------------- #
# Transitions — pure core (Phase 4)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AttemptRef:
    """Identity of one execution in today's storage vocabulary: the run row,
    its lease, and the (queue, task) it belongs to. Phase 8 merges these into
    a single attempt id."""

    run_id: str
    lease_id: str
    queue: str | None
    task: str


@dataclass(frozen=True)
class SubmitPolicy:
    """Everything the old ``worker_submit`` cascade read from config, the
    store, frontmatter, or ``app.state`` — computed by the caller, consumed by
    the pure transition functions.

    ``prior_no_progress_streak`` is the streak over *previous* runs (the
    current run is still ``running`` when the caller scans); the transition
    adds the current no-progress outcome itself. Phase 5 replaces the scan
    with a counter — keep this input isolated.
    """

    quarantine_threshold: int = 0
    prior_no_progress_streak: int = 0
    worker_quarantine: bool = False       # the worker's immediate-quarantine flag
    was_retry: bool = False               # task was `failed: true` in frontmatter
    watch_armed: bool = False             # phase-A two-in-a-row watch state
    queue_paused: bool = False            # queue already paused (any reason)
    split: bool = False                   # brief declares split (decomposition)
    evergreen: bool = False               # brief survives a land (never dropped)
    auto_resolve: bool = False            # cfg.auto_resolve
    pr_mode: bool = False                 # effective landing mode is PR


@dataclass(frozen=True)
class TaskHold:
    """A task-overlay upsert (``set_task_state``) applied in the transaction."""

    kind: TaskHoldKind
    reason: str | None = None
    retry_eligible: bool = False


@dataclass(frozen=True)
class FrontmatterFlag:
    """A boolean frontmatter write (quarantined/failed) with its companion
    reason field — a post-commit side effect (the .md file is the source of
    truth for these, not the DB)."""

    key: str
    value: bool
    reason_key: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class TaskEffects:
    """The task-level consequences of a transition. ``hold``/``clear_hold``
    ride the transaction; everything else executes after a successful apply,
    in field order below."""

    hold: TaskHold | None = None
    clear_hold: bool = False
    frontmatter: tuple[FrontmatterFlag, ...] = ()
    # New phase-A watch state (None = leave unchanged).
    watch_armed: bool | None = None
    # Final queue-pause reason to record (None = leave the pause map alone).
    pause_queue: str | None = None
    drop_brief: bool = False
    start_resolve: bool = False


@dataclass(frozen=True)
class TransitionEvent:
    """One state-change event to append inside the transaction (the outbox);
    broadcast to SSE clients happens after commit, from the returned ids."""

    kind: str
    run_id: str | None = None
    queue: str | None = None
    task: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class Transition:
    """One atomic state change, as a value. ``run_fields`` feed the run-row
    update, ``lease_status`` is the lease's new status (the CAS target),
    ``events`` are inserted in order in the same transaction, and ``response``
    is the exact submit HTTP body for this outcome."""

    ref: AttemptRef
    run_fields: dict[str, Any]
    lease_status: LeaseStatus
    effects: TaskEffects = field(default_factory=TaskEffects)
    events: tuple[TransitionEvent, ...] = ()
    response: dict[str, Any] = field(default_factory=dict)


class GitPhase(Enum):
    """on_submit's answer for a completed submit: the git/filesystem work the
    caller must run first, whose result feeds :func:`on_land_result` /
    :func:`on_split_result`."""

    LAND = auto()           # landable branch: full land()
    ADOPT_CHECK = auto()    # nothing landable: cheap adopt-or-nothing detection
    HARVEST_SPLIT = auto()  # decomposition run: harvest subtask briefs


@dataclass(frozen=True)
class LandResult:
    """Pure view of a landing attempt's result (mapped from
    ``manager.landing.LandingResult`` plus the computed LOC), so
    :func:`on_land_result` never imports the git layer."""

    landed: bool
    sha: str | None = None
    detail: str = ""
    conflict: bool = False
    recoverable: bool = False
    remote: str | None = None
    pushed: bool | None = None
    pr_url: str | None = None
    adopted: bool = False           # agent landed on main directly; HEAD adopted
    nothing_to_land: bool = False   # empty branch and main did not advance
    loc: int | None = None


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
            "task_quarantined", run_id=ref.run_id, queue=ref.queue, task=ref.task,
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
            "task_quarantined", run_id=ref.run_id, queue=ref.queue, task=ref.task,
            payload={"reason": reason, "streak": 1},
        ),
    )


def _hits_quarantine_threshold(policy: SubmitPolicy) -> bool:
    """Whether this no-progress outcome reaches the streak threshold (the
    current run counts as +1 on top of the prior streak)."""
    return (
        policy.quarantine_threshold > 0
        and policy.prior_no_progress_streak + 1 >= policy.quarantine_threshold
    )


def _looping_quarantine(
    ref: AttemptRef, policy: SubmitPolicy, detail: str
) -> tuple[FrontmatterFlag, tuple[TransitionEvent, ...]]:
    """Quarantine a task stuck re-executing without progress."""
    streak = policy.prior_no_progress_streak + 1
    reason = (
        f"quarantined after {streak} consecutive runs with no progress "
        f"({detail}); execution halted to protect budget — review the run "
        f"logs and edit or delete the task to release it"
    )
    return FrontmatterFlag("quarantined", True, "quarantine_reason", reason), (
        TransitionEvent(
            "task_quarantined", run_id=ref.run_id, queue=ref.queue, task=ref.task,
            payload={"reason": reason, "streak": streak},
        ),
    )


def _failure_ladder(
    ref: AttemptRef,
    policy: SubmitPolicy,
    *,
    quarantine_detail: str,
    failure_reason: str,
    retry_pause_after_quarantine: bool,
) -> tuple[bool, list[FrontmatterFlag], list[TransitionEvent], str | None, bool | None]:
    """The no-progress failure ladder shared by worker errors and no-change
    completions: worker-quarantine → streak-threshold quarantine → failed
    flag + watch arm (+ retry quarantine when the failed run was a retry).

    ``retry_pause_after_quarantine`` preserves a pre-Phase-4 placement
    asymmetry: a threshold-quarantined *error* on a retried task still pauses
    the queue with ``retry_failed``, while a threshold-quarantined no-change
    completion does not.

    Returns ``(quarantined, frontmatter, events, pause, watch)``.
    """
    frontmatter: list[FrontmatterFlag] = []
    events: list[TransitionEvent] = []
    if policy.worker_quarantine:
        flag, evs = _immediate_quarantine(ref, quarantine_detail)
        frontmatter.append(flag)
        events += evs
        return True, frontmatter, events, None, None
    if _hits_quarantine_threshold(policy):
        flag, evs = _looping_quarantine(ref, policy, quarantine_detail)
        frontmatter.append(flag)
        events += evs
        pause: str | None = None
        if retry_pause_after_quarantine and policy.was_retry:
            pause = "retry_failed"
            events.append(TransitionEvent(
                "queue_paused", queue=ref.queue,
                payload={"reason": "retry_failed", "task": ref.task},
            ))
        return True, frontmatter, events, pause, None
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
    return False, frontmatter, events, pause, True


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
        "task_result", run_id=ref.run_id, queue=ref.queue, task=ref.task,
        payload={"status": "blocked", "result_line": outcome.result_line},
    ))
    return Transition(
        ref=ref,
        run_fields=dict(
            status=RunStatus.BLOCKED,
            result_line=outcome.result_line,
            failure_kind=outcome.failure_kind or FailureKind.BLOCKED,
            failure_reason=outcome.failure_reason,
            **_base_run_fields(outcome),
        ),
        lease_status=LeaseStatus.RELEASED,
        effects=TaskEffects(
            hold=TaskHold(TaskHoldKind.BLOCKED, reason, retry_eligible=True),
            frontmatter=tuple(frontmatter),
            pause_queue=pause,
            watch_armed=True,
        ),
        events=tuple(events),
        response={"landed": False, "status": "blocked"},
    )


def _error_transition(
    ref: AttemptRef, outcome: Outcome, policy: SubmitPolicy
) -> Transition:
    """A worker error: validation failures block (recoverable branch), the
    quarantine ladder guards re-execution loops, everything else marks the
    task failed and arms the failure watch. Aborts/skips are neutral and go
    through :func:`_neutral_transition` instead."""
    events: list[TransitionEvent] = []
    frontmatter: list[FrontmatterFlag] = []
    hold: TaskHold | None = None
    pause: str | None = None
    watch: bool | None = None
    quarantined = False
    if outcome.failure_kind == FailureKind.VALIDATION_ERROR:
        # The agent DID produce commits; the branch is preserved and the work
        # is recoverable — block (needs resolve) rather than arm the policy.
        hold = TaskHold(
            TaskHoldKind.BLOCKED,
            "validation failed: " + (
                outcome.result_line or "validate command returned non-zero"
            ),
        )
        events.append(TransitionEvent(
            "task_blocked", queue=ref.queue, task=ref.task,
            payload={"reason": "validation_error", "detail": outcome.failure_reason},
        ))
    else:
        quarantined, frontmatter, events, pause, watch = _failure_ladder(
            ref, policy,
            quarantine_detail="worker error",
            failure_reason=(
                outcome.result_line or outcome.failure_reason or "worker error"
            ),
            retry_pause_after_quarantine=True,
        )
    events.append(TransitionEvent(
        "task_result", run_id=ref.run_id, queue=ref.queue, task=ref.task,
        payload={"status": outcome.status, "result_line": outcome.result_line},
    ))
    return Transition(
        ref=ref,
        run_fields=dict(
            status=outcome.status,
            result_line=outcome.result_line,
            failure_kind=outcome.failure_kind,
            failure_reason=outcome.failure_reason,
            **_base_run_fields(outcome),
        ),
        lease_status=LeaseStatus.RELEASED,
        effects=TaskEffects(
            hold=hold,
            frontmatter=tuple(frontmatter),
            pause_queue=pause,
            watch_armed=watch,
        ),
        events=tuple(events),
        response={"landed": False, "status": outcome.status, "quarantined": quarantined},
    )


def _neutral_transition(ref: AttemptRef, outcome: Outcome) -> Transition:
    """Operator-driven or indeterminate outcomes (aborted/skipped/running):
    record and release, no policy action."""
    return Transition(
        ref=ref,
        run_fields=dict(
            status=outcome.status,
            result_line=outcome.result_line,
            failure_kind=outcome.failure_kind,
            failure_reason=outcome.failure_reason,
            **_base_run_fields(outcome),
        ),
        lease_status=LeaseStatus.RELEASED,
        events=(TransitionEvent(
            "task_result", run_id=ref.run_id, queue=ref.queue, task=ref.task,
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
        run_fields=dict(
            status=RunStatus.COMPLETED,
            result_line=result_line,
            **_base_run_fields(outcome),
        ),
        lease_status=LeaseStatus.RELEASED,
        effects=TaskEffects(clear_hold=True),
        events=(
            TransitionEvent(
                "task_result", run_id=ref.run_id, queue=ref.queue, task=ref.task,
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
    with no commit; the run still counts toward the quarantine streak."""
    result_line = outcome.result_line or "no changes"
    quarantined, frontmatter, events, pause, watch = _failure_ladder(
        ref, policy,
        quarantine_detail="no changes produced",
        failure_reason="no changes produced",
        retry_pause_after_quarantine=False,
    )
    events.append(TransitionEvent(
        "task_result", run_id=ref.run_id, queue=ref.queue, task=ref.task,
        payload={"status": "completed", "result_line": result_line},
    ))
    events.append(TransitionEvent("queue_changed", queue=ref.queue))
    return Transition(
        ref=ref,
        run_fields=dict(
            status=RunStatus.COMPLETED,
            result_line=result_line,
            **_base_run_fields(outcome),
        ),
        lease_status=LeaseStatus.RELEASED,
        effects=TaskEffects(
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
    ref: AttemptRef, outcome: Outcome, land: LandResult, policy: SubmitPolicy
) -> Transition:
    """A confirmed land (squash or adopted agent land): record success, clear
    every hold, disarm the failure watch, consume the brief (non-evergreen)."""
    if land.adopted and not outcome.landable:
        # A non-landable submit whose agent landed on main directly: the run
        # records the adoption, not the worker's "no changes" line.
        result_line = "agent landed on main"
    else:
        result_line = outcome.result_line or land.detail or "landed"
    return Transition(
        ref=ref,
        run_fields=dict(
            status=RunStatus.COMPLETED,
            result_line=result_line,
            commit_sha=land.sha,
            loc=land.loc,
            remote=land.remote,
            pushed=land.pushed,
            **_base_run_fields(outcome),
        ),
        lease_status=LeaseStatus.LANDED,
        effects=TaskEffects(
            clear_hold=True,
            watch_armed=False,
            drop_brief=not policy.evergreen,
        ),
        events=(
            TransitionEvent(
                "task_result", run_id=ref.run_id, queue=ref.queue, task=ref.task,
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
    ref: AttemptRef, outcome: Outcome, land: LandResult, policy: SubmitPolicy
) -> Transition:
    """A refused land. The branch is preserved, so a conflict or recoverable
    rejection holds the task blocked (resolvable) and may auto-escalate."""
    failure_kind = (
        FailureKind.MERGE_CONFLICT if land.conflict else FailureKind.MERGE_REJECTED
    )
    events: list[TransitionEvent] = []
    hold: TaskHold | None = None
    pause: str | None = None
    watch: bool | None = None
    if land.conflict or land.recoverable:
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
        "task_result", run_id=ref.run_id, queue=ref.queue, task=ref.task,
        payload={"status": RunStatus.ERROR, "failure_kind": failure_kind},
    ))
    return Transition(
        ref=ref,
        run_fields=dict(
            status=RunStatus.ERROR,
            result_line=(
                land.detail.splitlines()[0][:200] if land.detail else "land failed"
            ),
            failure_kind=failure_kind,
            failure_reason=land.detail,
            **_base_run_fields(outcome),
        ),
        lease_status=LeaseStatus.RELEASED,
        effects=TaskEffects(
            hold=hold,
            pause_queue=pause,
            watch_armed=watch,
            start_resolve=(
                policy.auto_resolve
                and (land.conflict or land.recoverable)
                and not policy.pr_mode
            ),
        ),
        events=tuple(events),
        response={
            "landed": False, "conflict": land.conflict,
            "detail": land.detail, "resolving": False,
        },
    )


def on_land_result(
    ref: AttemptRef, outcome: Outcome, land: LandResult, policy: SubmitPolicy
) -> Transition:
    """The landing transition table: landed (including adopted agent lands),
    nothing-to-land (the no-change path), or refused (conflict / rejection)."""
    if land.nothing_to_land:
        return _no_change_transition(ref, outcome, policy)
    if land.landed:
        return _landed_transition(ref, outcome, land, policy)
    return _land_failed_transition(ref, outcome, land, policy)


def on_deadline(ref: AttemptRef) -> Transition:
    """Deadline expiry as a transition: the lease flips to ``expired``
    (stamping ``released_at``). Today's reclaim touches nothing else — the run
    row, the task overlay, and the event log are untouched — so the transition
    carries no other effects. Phase 7's reconciler consumes this per lease."""
    return Transition(
        ref=ref,
        run_fields={},
        lease_status=LeaseStatus.EXPIRED,
    )
