"""Typed lifecycle vocabulary — every status, kind, and outcome shape, once.

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
"""

from __future__ import annotations

from enum import StrEnum
from typing import assert_never

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
