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
append — computed by the pure functions in :mod:`nightshift.transitions`
(``on_submit``, ``on_land_result``, ``on_split_result``, ``on_deadline``, …;
split out in Phase 9 for module size) and applied by
``NightshiftStore.apply_transition`` (CAS on the lease, single transaction,
events as a transactional outbox). Nothing here imports the store, git, or
HTTP; every policy input the old handler read from the store or config
arrives via :class:`SubmitPolicy`.

Phase 5 makes retry data instead of history scans (greenfield §"Retry &
quarantine policy"): :class:`RetryPolicy` classifies failures
(:meth:`RetryPolicy.on_failure`), the persisted ``attempts_without_progress``
counter replaces the 50-run streak scan (transitions carry a
:class:`Progress` op that the store applies transactionally), ``RETRY``
failures set a backoff (``next_eligible_at``) that dispatch honors, and
environment failures cool the submitting worker down instead of counting
against the task.

Phase 8 merges lease + run into one *attempt* (greenfield §"The Attempt"):
:class:`AttemptState` is the single stored lifecycle column, transitions CAS
on it, and the legacy pair survives only as (a) the ``RunStatus`` wire
vocabulary on :class:`Outcome`/submits and (b) the ``fold_legacy`` /
``split_state`` table functions (now in :mod:`nightshift.lifecycle_compat`)
that pin migration ``20260731000004_nightshift_attempts.sql``'s CASE
expressions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum, auto
from typing import Any, assert_never

from pydantic import BaseModel


class RunStatus(StrEnum):
    """The WIRE outcome vocabulary: what a worker's submit (``SubmitBody`` /
    :class:`Outcome`) reports, and the ``status`` field the ``/api/runs*``
    views project. Since Phase 8 the STORED lifecycle column is
    :class:`AttemptState`; ``RunStatus`` survives because the worker wire
    format and the run views are byte-compat surfaces."""

    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ERROR = "error"
    SKIPPED = "skipped"
    ABORTED = "aborted"


class AttemptState(StrEnum):
    """The single stored lifecycle column (``attempts.state``), replacing the
    ``RunStatus`` × ``LeaseStatus`` pair (greenfield §"Typed vocabulary").
    Values are wire-safe lowercase strings.

    CLAIMED and SUBMITTED are unreachable today — dispatch creates the
    attempt already running (the poll handler builds the work order in the
    same request) and submit handling is synchronous — but they stay in the
    enum per the greenfield spec so the vocabulary doesn't fork.

    SKIPPED is a documented compat extension: today's ``skipped`` run status
    (a neutral worker submit) has no greenfield equivalent but must keep its
    wire projection.
    """

    CLAIMED = "claimed"
    RUNNING = "running"
    SUBMITTED = "submitted"
    LANDING = "landing"
    RESOLVING = "resolving"
    # terminal
    LANDED = "landed"
    NO_CHANGE = "no_change"
    BLOCKED = "blocked"
    FAILED = "failed"
    CONFLICT = "conflict"
    EXPIRED = "expired"
    ABORTED = "aborted"
    SKIPPED = "skipped"


# Terminal attempt states — every attempt reaching one of these gets
# ``finished_at`` (invariant 4), stamped by the store's applier and by
# ``update_attempt``.
ATTEMPT_TERMINAL_STATES = frozenset({
    AttemptState.LANDED, AttemptState.NO_CHANGE, AttemptState.BLOCKED,
    AttemptState.FAILED, AttemptState.CONFLICT, AttemptState.EXPIRED,
    AttemptState.ABORTED, AttemptState.SKIPPED,
})

# The live states: a task with an attempt in this set cannot be re-leased
# (the partial unique index ``attempts_live_task_uniq`` — keep this frozenset
# and the index predicate in ``20260731000004_nightshift_attempts.sql``
# byte-identical). RESOLVING is deliberately NOT live: resolve children never
# held leases, and including them would newly block dispatch of the task they
# are repairing.
ATTEMPT_LIVE_STATES = frozenset({AttemptState.RUNNING, AttemptState.LANDING})

# States an origin attempt may be in for an out-of-process resolve result to
# be honored (the resolve-result fence added in Phase 0).
ATTEMPT_RESOLVABLE_STATES = frozenset({
    AttemptState.FAILED, AttemptState.CONFLICT, AttemptState.BLOCKED,
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


# The merge-shaped failure kinds: an error outcome carrying one of these
# stores as CONFLICT (needs resolve), every other error as FAILED. The fold /
# split table functions below and the migration CASE rely on this being the
# ONLY route to CONFLICT.
MERGE_FAILURE_KINDS = frozenset({
    FailureKind.MERGE_CONFLICT, FailureKind.MERGE_REJECTED,
})


def error_state_for(kind: FailureKind | None) -> AttemptState:
    """The stored state for an ``error`` outcome: merge-shaped failures are
    CONFLICT (resolve owns the next step), everything else FAILED."""
    if kind in MERGE_FAILURE_KINDS:
        return AttemptState.CONFLICT
    return AttemptState.FAILED


def run_status_of(state: AttemptState | str) -> str:
    """Project an attempt state to the ``status`` string the ``/api/runs*``
    views (and the SSE ``runs`` snapshot) serve. All values are the
    pre-Phase-8 wire vocabulary (``aborted`` included — neutral submits
    already produced it) except ``expired``, the one new wire value: before
    this phase a run whose lease expired stayed ``running`` forever, and the
    Phase 8 zombie fix makes the projection truthful.
    """
    match AttemptState(state):
        case (
            AttemptState.CLAIMED
            | AttemptState.RUNNING
            | AttemptState.SUBMITTED
            | AttemptState.LANDING
            | AttemptState.RESOLVING
        ):
            return "running"
        case AttemptState.LANDED | AttemptState.NO_CHANGE:
            return "completed"
        case AttemptState.BLOCKED:
            return "blocked"
        case AttemptState.FAILED | AttemptState.CONFLICT:
            return "error"
        case AttemptState.SKIPPED:
            return "skipped"
        case AttemptState.ABORTED:
            return "aborted"
        case AttemptState.EXPIRED:
            return "expired"
        case _:
            assert_never(state)


# Sentinel worker_id for resolve children (they never held leases; the fold
# maps their lease-less running rows to RESOLVING).
RESOLVE_WORKER_ID = "manager:resolve"


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
    """Identity of one attempt: its id (the former run id — the work order's
    ``lease_id`` and ``run_id`` both carry it for wire compat) and the
    (queue, task) it belongs to."""

    id: str
    queue: str | None
    task: str


class RetryAction(StrEnum):
    """What a failure kind means for the task (greenfield §"Retry &
    quarantine policy"): retry it (counted, backed off), retry it elsewhere
    (an environment kind — the box is at fault, so the task stays untouched
    and the worker cools down), hold it for a resolve/operator, or
    quarantine."""

    RETRY = "retry"
    RETRY_ELSEWHERE = "retry_elsewhere"
    HOLD = "hold"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class Backoff:
    """Exponential retry backoff: the n-th consecutive no-progress attempt
    delays the next dispatch by ``base * 2**(n-1)`` seconds, capped."""

    base_seconds: float = 60.0
    cap_seconds: float = 3600.0

    def delay(self, attempts: int) -> float:
        """Seconds until a task is eligible again after ``attempts``
        consecutive no-progress attempts (>= 1). The exponent is clamped so a
        huge counter (quarantine disabled, hot-failing task) can't overflow —
        it just returns the cap."""
        return min(self.base_seconds * 2.0 ** min(attempts - 1, 63), self.cap_seconds)


@dataclass(frozen=True)
class RetryPolicy:
    """The declarative retry/quarantine policy (greenfield §"Retry &
    quarantine policy"). ``quarantine_after`` is wired from
    ``cfg.quarantine_threshold`` (0 disables the threshold guard);
    ``immediate_quarantine`` is the worker's quarantine mode (quarantine on
    the first counted failure)."""

    quarantine_after: int = 0
    immediate_quarantine: bool = False
    backoff: Backoff = Backoff()

    def on_failure(self, kind: FailureKind) -> RetryAction:
        match kind:
            case (
                FailureKind.MODEL_UNAVAILABLE
                | FailureKind.BACKEND_UNAVAILABLE
                | FailureKind.REPO_UNAVAILABLE
                | FailureKind.PREFLIGHT_FAILED
                | FailureKind.WORKTREE_FAILED
                | FailureKind.WORKER_LAUNCH
                | FailureKind.PUBLISH_FAILED
            ):
                # Environment: the box is at fault, not the task — never
                # counted, never held; the transition cools the worker down.
                return RetryAction.RETRY_ELSEWHERE
            case FailureKind.WORKER_ERROR:
                if self.immediate_quarantine:
                    return RetryAction.QUARANTINE
                return RetryAction.RETRY
            case FailureKind.VALIDATION_ERROR:
                # The agent DID produce commits; the branch is preserved and
                # recoverable — hold for resolve rather than spin retries.
                return RetryAction.HOLD
            case FailureKind.BLOCKED:
                return RetryAction.HOLD
            case FailureKind.MERGE_CONFLICT | FailureKind.MERGE_REJECTED:
                # Integration: goes to resolve, not retry.
                return RetryAction.HOLD
            case _:
                assert_never(kind)


# The kinds the classifier routes RETRY_ELSEWHERE (the worker/box is at fault;
# the work never really started). Derived from the classifier so the taxonomy
# lives in exactly one place; the policy knobs don't affect this axis.
ENVIRONMENT_FAILURE_KINDS = frozenset(
    kind for kind in FailureKind
    if RetryPolicy().on_failure(kind) is RetryAction.RETRY_ELSEWHERE
)


@dataclass(frozen=True)
class SubmitPolicy:
    """Everything the old ``worker_submit`` cascade read from config, the
    store, frontmatter, or ``app.state`` — computed by the caller, consumed by
    the pure transition functions.

    ``attempts_without_progress`` is the persisted counter *before* this
    outcome (read from the task row); the transition's :class:`Progress` op
    adds the current outcome itself.
    """

    retry: RetryPolicy = RetryPolicy()
    attempts_without_progress: int = 0
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


class Progress(Enum):
    """The transition's effect on the task's ``attempts_without_progress``
    counter, applied by the store inside the transaction. A land (or adopted
    agent land) resets it; task-category failures and no-change runs
    increment it; environment failures, aborts, and blocks are neutral."""

    NONE = auto()
    INCREMENT = auto()
    RESET = auto()


@dataclass(frozen=True)
class TaskEffects:
    """The task-level consequences of a transition. ``hold``/``clear_hold``
    and the ``progress`` counter op ride the transaction; everything else
    executes after a successful apply, in field order below."""

    hold: TaskHold | None = None
    clear_hold: bool = False
    # attempts_without_progress counter op (transactional, see Progress).
    progress: Progress = Progress.NONE
    # Retry backoff in seconds for a RETRY-classified failure: the store
    # stamps ``next_eligible_at = now + next_eligible_in`` alongside an
    # INCREMENT (None clears any stale backoff). Dispatch skips tasks whose
    # next_eligible_at hasn't elapsed.
    next_eligible_in: float | None = None
    # Environment failure: cool the *submitting* worker down (post-commit,
    # in-memory this phase) instead of counting against the task.
    worker_cooldown: bool = False
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
    """One atomic state change, as a value. ``state`` is the attempt's new
    state (the store CASes ``attempts.state`` from the caller's expected
    state to it), ``fields`` feed the attempt-row update (never ``state`` —
    the state IS the status), ``events`` are inserted in order in the same
    transaction, and ``response`` is the exact submit HTTP body for this
    outcome."""

    ref: AttemptRef
    fields: dict[str, Any]
    state: AttemptState
    effects: TaskEffects = field(default_factory=TaskEffects)
    events: tuple[TransitionEvent, ...] = ()
    response: dict[str, Any] = field(default_factory=dict)


class GitPhase(Enum):
    """``on_submit``'s answer for a completed submit: the git/filesystem
    work the caller must run first, whose result feeds ``on_land_result`` /
    ``on_split_result`` (:mod:`nightshift.transitions`)."""

    LAND = auto()           # landable branch: full land()
    ADOPT_CHECK = auto()    # nothing landable: cheap adopt-or-nothing detection
    HARVEST_SPLIT = auto()  # decomposition run: harvest subtask briefs


class LandKind(StrEnum):
    """Every way a land can end (git greenfield §0/§3) — the ONE result
    vocabulary shared by the git layer's pipeline and this transition core."""

    LANDED = "landed"
    NO_CHANGES = "no_changes"
    ADOPTED = "adopted"
    CONFLICT = "conflict"
    CHECKOUT_BEHIND = "checkout_behind"   # ref advanced; operator checkout left behind
    PUSH_REJECTED = "push_rejected"
    TRANSPORT_FAILED = "transport_failed"


# The kinds where canonical main advanced (a real land happened). ADOPTED and
# CHECKOUT_BEHIND are successes: the branch ref is authoritative.
LAND_SUCCESS_KINDS = frozenset({
    LandKind.LANDED, LandKind.CHECKOUT_BEHIND, LandKind.ADOPTED,
})


def land_failure_kind(kind: LandKind) -> FailureKind:
    """THE one mapping from a refused :class:`LandKind` to the ``failure_kind``
    the manager stores (behavior-compat: conflicts stay ``merge_conflict``;
    rejections and transport failures stay ``merge_rejected``)."""
    match kind:
        case LandKind.CONFLICT:
            return FailureKind.MERGE_CONFLICT
        case LandKind.PUSH_REJECTED | LandKind.TRANSPORT_FAILED:
            return FailureKind.MERGE_REJECTED
        case (
            LandKind.LANDED
            | LandKind.NO_CHANGES
            | LandKind.ADOPTED
            | LandKind.CHECKOUT_BEHIND
        ):
            raise ValueError(f"{kind} is not a land failure")
        case _:
            assert_never(kind)


@dataclass(frozen=True)
class LandOutcome:
    """The typed result of a landing attempt (git greenfield §0), produced by
    the git layer's plumbing pipeline and consumed by ``on_land_result``.
    Pure data — this module still never imports the git layer.

    ``dropped_commits`` is the never-silent casualty list: local commits an
    origin re-sync rescue could not replay (preserved only in the reflog).
    ``retryable`` refines TRANSPORT_FAILED: a fetch hiccup is retryable (hold
    the task blocked for a re-fetch); a verification refusal (missing
    remote/head_sha, mismatch) is not (release with an error, no hold).
    """

    kind: LandKind
    sha: str | None = None
    detail: str = ""
    conflicts: tuple[str, ...] = ()
    dropped_commits: tuple[str, ...] = ()
    pr_url: str | None = None
    remote: str | None = None       # remote action taken: 'push' | 'pr' | None
    # Whether the configured remote step succeeded. ``None`` when no remote
    # action was attempted (``landing_mode=none``).
    pushed: bool | None = None
    retryable: bool = False
    loc: int | None = None
