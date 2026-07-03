"""Phase 1 (typed vocabulary) + Phase 4 (pure transition core) tests.

The unified ``Outcome`` model replaces the five hand-synced outcome shapes
(``ExecuteOutcome``, the submit payload dict, ``SubmitBody``, the local-store
finish dict, the ``worker_submit`` telemetry re-pack). These tests pin the
wire contract: the submit payload a worker posts must carry exactly the same
keys as before the unification.

The Phase 4 half is the transition table: every submit outcome × policy
context, every land-result kind, and deadline expiry, exercised purely — no
store, no git, no HTTP. Since Phase 8 transitions carry an ``AttemptState``
(the merged lease+run lifecycle column); the fold/split round-trip tests pin
the data migration's CASE expressions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nightshift.lifecycle import (
    ATTEMPT_LIVE_STATES,
    ATTEMPT_TERMINAL_STATES,
    ENVIRONMENT_FAILURE_KINDS,
    MERGE_FAILURE_KINDS,
    RESOLVE_WORKER_ID,
    AttemptRef,
    AttemptState,
    Backoff,
    FailureKind,
    GitPhase,
    LandingMode,
    LandKind,
    LandOutcome,
    Outcome,
    Progress,
    RetryAction,
    RetryPolicy,
    RunStatus,
    SubmitPolicy,
    TaskEffects,
    TaskHoldKind,
    Telemetry,
    Transition,
    run_status_of,
)
from nightshift.lifecycle_compat import fold_legacy, split_state
from nightshift.transitions import (
    on_deadline,
    on_land_enqueued,
    on_land_recovered,
    on_land_result,
    on_operator_stop,
    on_split_result,
    on_submit,
)
from nightshift.worker.config import WorkerConfig
from nightshift.worker.local_store import LocalStore
from nightshift.worker.loop import WorkerLoop


# The submit payload keys as posted by WorkerLoop._submit BEFORE the Outcome
# unification (Phase 0 wire format), plus the token-usage-granularity fields
# (cache splits + raw usage payload) added alongside turns/input/output/cost.
# Ground rule: byte-identical wire, modulo additive telemetry fields.
PREVIOUS_SUBMIT_PAYLOAD_KEYS = frozenset({
    "worker_id", "lease_id", "task", "queue", "repo", "title",
    "status", "result_line", "backend", "model", "landable",
    "branch_ref", "head_sha", "failure_kind", "failure_reason",
    "turns", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "usage",
    "cost_usd", "validate_cmd", "worktree", "quarantine",
})

# The envelope the loop adds around the Outcome (lease/task identity + the
# worker-side quarantine flag).
ENVELOPE_KEYS = frozenset({
    "worker_id", "lease_id", "task", "queue", "repo", "title", "quarantine",
})

# The local-store finish record keys as written to the worker's runs.jsonl
# BEFORE the Outcome unification: run identity + the outcome fields the local
# Now/History UI shows + the manager's land result, plus the started_at /
# finished_at stamps LocalStore.finish adds itself, plus the token-usage-
# granularity fields (not in _LOCAL_HISTORY_EXCLUDE, so they ride along like
# turns/input_tokens/output_tokens/cost_usd). Ground rule: the on-disk format
# stays byte-identical, modulo additive telemetry fields.
PREVIOUS_LOCAL_FINISH_KEYS = frozenset({
    "run_id", "task", "queue", "title", "repo",
    "model", "backend", "status", "failure_kind", "result_line",
    "commit_sha", "landed", "quarantined",
    "turns", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "usage",
    "cost_usd", "worktree",
    "started_at", "finished_at",
})


def test_outcome_model_dump_matches_previous_submit_payload_keys() -> None:
    """Outcome.model_dump() + the loop envelope == the pre-phase payload keys."""
    outcome = Outcome(status=RunStatus.COMPLETED)
    dump = outcome.model_dump()
    assert set(dump) & ENVELOPE_KEYS == set(), "outcome/envelope keys must not overlap"
    assert set(dump) | ENVELOPE_KEYS == PREVIOUS_SUBMIT_PAYLOAD_KEYS


class _CapturingClient:
    """Records the submit payload instead of talking to a manager."""

    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    def submit(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payload = payload
        return {"landed": False}


def test_loop_submit_posts_exactly_the_previous_payload_keys(tmp_path: Path) -> None:
    """End-to-end schema equality: the JSON body _submit posts is key-identical
    to the pre-unification hand-written dict."""
    client = _CapturingClient()
    cfg = WorkerConfig(workspace=tmp_path, worker_id="w1", manager_url="http://x")
    loop = WorkerLoop(cfg, client, LocalStore(tmp_path))
    order = {
        "run_id": "r1", "lease_id": "l1", "task": "10.demo",
        "queue": "main", "repo": "longitude", "title": "demo",
    }
    outcome = Outcome(
        status=RunStatus.ERROR,
        result_line="worker bailed",
        landable=False,
        model="claude-code/claude-sonnet-4-6",
        backend="claude-code",
        failure_kind=FailureKind.WORKER_ERROR,
        failure_reason="exit 1",
        turns=3,
        worktree=str(tmp_path / "wt"),
    )
    loop._submit(order, outcome)

    assert client.payload is not None
    assert set(client.payload) == PREVIOUS_SUBMIT_PAYLOAD_KEYS
    assert client.payload["status"] == "error"
    assert client.payload["failure_kind"] == "worker_error"
    assert client.payload["model"] == "claude-code/claude-sonnet-4-6"


def test_local_finish_record_matches_previous_on_disk_keys(tmp_path: Path) -> None:
    """Schema equality for the fifth shape: the JSONL history row _submit hands
    to LocalStore.finish is key-identical to the pre-unification hand-written
    finish dict (Outcome minus transport-only fields + the land result)."""
    client = _CapturingClient()
    cfg = WorkerConfig(workspace=tmp_path, worker_id="w1", manager_url="http://x")
    local = LocalStore(tmp_path)
    loop = WorkerLoop(cfg, client, local)
    order = {
        "run_id": "r1", "lease_id": "l1", "task": "10.demo",
        "queue": "main", "repo": "longitude", "title": "demo",
    }
    # begin() so the row carries started_at, exactly as a real run does.
    local.begin(
        run_id="r1", task="10.demo", queue="main", title="demo",
        model="claude-code/claude-sonnet-4-6", backend="claude-code",
        repo="longitude",
    )
    outcome = Outcome(
        status=RunStatus.COMPLETED,
        result_line="validated",
        landable=True,
        model="claude-code/claude-sonnet-4-6",
        backend="claude-code",
        turns=5,
        validate_cmd="just validate",
        worktree=str(tmp_path / "wt"),
    )
    loop._submit(order, outcome)

    row = local.history()[0]
    assert set(row) == PREVIOUS_LOCAL_FINISH_KEYS
    assert row["status"] == "completed"
    assert row["model"] == "claude-code/claude-sonnet-4-6"
    assert row["landed"] is False  # the capturing client landed nothing
    # Transport/diagnostic-only Outcome fields must stay off the disk format.
    assert not {"landable", "branch_ref", "head_sha", "failure_reason", "validate_cmd"} & set(row)


def test_local_finish_record_carries_analytics_fields(tmp_path: Path) -> None:
    """The worker's on-disk record must carry every field the shared analytics
    module reads client-side (the worker adapter does no server-side reshaping):
    landing + telemetry + dimensions + timestamps. Guards the worker side of the
    analytics record contract against a future field drop."""
    # Fields the shared analytics adapter (assets/ui/analytics.js) consumes.
    ANALYTICS_CONSUMED = {
        "task", "queue", "model", "backend", "status", "landed",
        "turns", "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
        "cost_usd", "usage", "failure_kind", "started_at", "finished_at",
    }
    client = _CapturingClient()
    cfg = WorkerConfig(workspace=tmp_path, worker_id="w1", manager_url="http://x")
    local = LocalStore(tmp_path)
    loop = WorkerLoop(cfg, client, local)
    order = {
        "run_id": "r1", "lease_id": "l1", "task": "10.demo",
        "queue": "main", "repo": "longitude", "title": "demo",
    }
    local.begin(
        run_id="r1", task="10.demo", queue="main", title="demo",
        model="anthropic/claude-sonnet-4-6", backend="anthropic", repo="longitude",
    )
    outcome = Outcome(
        status=RunStatus.COMPLETED, result_line="validated", landable=True,
        model="anthropic/claude-sonnet-4-6", backend="anthropic", turns=5,
        input_tokens=1500, output_tokens=400,
        cache_read_input_tokens=200, cache_creation_input_tokens=100,
        usage={"input_tokens": 1500}, cost_usd=0.09,
        worktree=str(tmp_path / "wt"),
    )
    loop._submit(order, outcome)
    row = local.history()[0]
    missing = ANALYTICS_CONSUMED - set(row)
    assert not missing, f"analytics fields missing from worker record: {missing}"


def test_telemetry_slice_matches_the_old_worker_submit_repack() -> None:
    """`outcome.telemetry.model_dump()` reproduces the dict worker_submit used
    to hand-assemble (the re-packing dict that Phase 1 deletes), extended with
    the token-usage-granularity fields (cache splits + raw usage payload)."""
    outcome = Outcome(
        status=RunStatus.COMPLETED, turns=8, input_tokens=1500,
        output_tokens=400, cache_read_input_tokens=200,
        cache_creation_input_tokens=100, usage={"input_tokens": 1500},
        cost_usd=0.09, validate_cmd="just validate", worktree="/w/t",
    )
    assert outcome.telemetry.model_dump() == {
        "turns": 8, "input_tokens": 1500, "output_tokens": 400,
        "cache_read_input_tokens": 200, "cache_creation_input_tokens": 100,
        "usage": {"input_tokens": 1500},
        "cost_usd": 0.09, "validate_cmd": "just validate", "worktree": "/w/t",
    }
    assert set(Telemetry.model_fields) == {
        "turns", "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens", "usage",
        "cost_usd", "validate_cmd", "worktree",
    }


def test_enum_values_are_todays_wire_strings() -> None:
    """StrEnum values must be byte-identical to the strings on the wire/in DB."""
    assert RunStatus.COMPLETED == "completed"
    assert RunStatus.BLOCKED == "blocked"
    assert RunStatus.ERROR == "error"
    assert LandingMode.NONE == "none"
    assert LandingMode.PUSH == "push"
    assert LandingMode.PR == "pr"
    # Phase 8: the stored lifecycle column is AttemptState (lowercase,
    # wire-safe); the live set is exactly the partial-unique-index predicate.
    assert AttemptState.NO_CHANGE == "no_change"
    assert AttemptState.LANDING == "landing"
    assert ATTEMPT_LIVE_STATES == {AttemptState.RUNNING, AttemptState.LANDING}
    assert AttemptState.BLOCKED in ATTEMPT_TERMINAL_STATES
    assert AttemptState.EXPIRED in ATTEMPT_TERMINAL_STATES


def test_run_status_projection_covers_every_attempt_state() -> None:
    """The /api/runs `status` projection: every state maps to a wire string;
    the pre-Phase-8 values are unchanged and `expired` is the one new value
    (the zombie-expiry fix made the projection truthful)."""
    assert {run_status_of(s) for s in AttemptState} == {
        "running", "completed", "blocked", "error", "skipped", "aborted",
        "expired",
    }
    assert run_status_of(AttemptState.LANDING) == "running"
    assert run_status_of(AttemptState.RESOLVING) == "running"
    assert run_status_of(AttemptState.LANDED) == "completed"
    assert run_status_of(AttemptState.NO_CHANGE) == "completed"
    assert run_status_of(AttemptState.CONFLICT) == "error"
    assert run_status_of(AttemptState.EXPIRED) == "expired"


def test_landing_mode_is_remote_is_exhaustive() -> None:
    assert LandingMode.PUSH.is_remote is True
    assert LandingMode.PR.is_remote is True
    assert LandingMode.NONE.is_remote is False


# --------------------------------------------------------------------------- #
# Phase 4: the transition table (pure — no store, no git, no HTTP)
# --------------------------------------------------------------------------- #


REF = AttemptRef(id="r1", queue=None, task="10.demo")


def _outcome(status: RunStatus, **kw: Any) -> Outcome:
    return Outcome(status=status, model="m1", turns=3, **kw)


def _kinds(t: Transition) -> list[str]:
    return [ev.kind for ev in t.events]


def test_on_submit_covers_every_run_status() -> None:
    """Every RunStatus maps to a Transition or a GitPhase — nothing falls
    through (the match is exhaustive by construction; this pins it)."""
    for status in RunStatus:
        computed = on_submit(REF, _outcome(status), SubmitPolicy())
        assert isinstance(computed, Transition | GitPhase)


# ---- blocked ---------------------------------------------------------------- #


def test_blocked_holds_task_resolvable_and_arms_watch() -> None:
    out = _outcome(RunStatus.BLOCKED, result_line="blocked: manual step",
                   failure_reason="manual step")
    t = on_submit(REF, out, SubmitPolicy())
    assert isinstance(t, Transition)
    assert t.state is AttemptState.BLOCKED
    assert "status" not in t.fields  # the state IS the status since Phase 8
    assert t.fields["failure_kind"] == FailureKind.BLOCKED
    assert t.fields["turns"] == 3
    assert t.effects.hold is not None
    assert t.effects.hold.kind == TaskHoldKind.BLOCKED
    assert t.effects.hold.reason == "manual step"
    assert t.effects.hold.retry_eligible is True
    assert t.effects.watch_armed is True
    assert t.effects.pause_queue is None
    assert _kinds(t) == ["task_blocked", "task_result"]
    assert t.events[1].payload == {
        "status": "blocked", "result_line": "blocked: manual step",
    }
    assert t.response == {"landed": False, "status": "blocked"}


def test_blocked_second_failure_pauses_queue() -> None:
    t = on_submit(REF, _outcome(RunStatus.BLOCKED), SubmitPolicy(watch_armed=True))
    assert isinstance(t, Transition)
    assert t.effects.pause_queue == "consecutive_failures"
    assert _kinds(t) == ["queue_paused", "task_blocked", "task_result"]
    assert t.events[0].payload == {
        "reason": "consecutive_failures", "task": "10.demo",
    }


def test_blocked_already_paused_queue_does_not_re_pause() -> None:
    t = on_submit(
        REF, _outcome(RunStatus.BLOCKED),
        SubmitPolicy(watch_armed=True, queue_paused=True),
    )
    assert isinstance(t, Transition)
    assert t.effects.pause_queue is None
    assert _kinds(t) == ["task_blocked", "task_result"]


def test_blocked_on_retry_quarantines_and_pauses_retry_failed() -> None:
    t = on_submit(REF, _outcome(RunStatus.BLOCKED), SubmitPolicy(was_retry=True))
    assert isinstance(t, Transition)
    assert t.effects.pause_queue == "retry_failed"
    assert [f.key for f in t.effects.frontmatter] == ["quarantined"]
    assert t.effects.frontmatter[0].reason_key == "quarantine_reason"
    assert _kinds(t) == [
        "task_quarantined", "queue_paused", "task_blocked", "task_result",
    ]
    assert t.events[0].payload["streak"] == 2
    assert t.events[1].payload == {"reason": "retry_failed", "task": "10.demo"}


# ---- error ------------------------------------------------------------------ #


def test_error_marks_failed_and_arms_watch() -> None:
    out = _outcome(RunStatus.ERROR, result_line="worker bailed",
                   failure_kind=FailureKind.WORKER_ERROR)
    t = on_submit(REF, out, SubmitPolicy())
    assert isinstance(t, Transition)
    assert t.state is AttemptState.FAILED
    assert t.effects.hold is None
    assert [(f.key, f.value, f.reason) for f in t.effects.frontmatter] == [
        ("failed", True, "worker bailed"),
    ]
    assert t.effects.watch_armed is True
    assert _kinds(t) == ["task_result"]
    assert t.response == {"landed": False, "status": "error", "quarantined": False}


def test_error_second_failure_pauses_queue() -> None:
    t = on_submit(REF, _outcome(RunStatus.ERROR), SubmitPolicy(watch_armed=True))
    assert isinstance(t, Transition)
    assert t.effects.pause_queue == "consecutive_failures"
    assert _kinds(t) == ["queue_paused", "task_result"]


def test_error_validation_failure_blocks_without_arming_policy() -> None:
    out = _outcome(
        RunStatus.ERROR, result_line="validate failed",
        failure_kind=FailureKind.VALIDATION_ERROR, failure_reason="exit 2",
    )
    t = on_submit(REF, out, SubmitPolicy(watch_armed=True, was_retry=True))
    assert isinstance(t, Transition)
    assert t.effects.hold is not None
    assert t.effects.hold.reason == "validation failed: validate failed"
    assert t.effects.hold.retry_eligible is False
    assert t.effects.frontmatter == ()
    assert t.effects.watch_armed is None
    assert t.effects.pause_queue is None
    # HOLD still counts the no-progress attempt (no backoff — the hold itself
    # keeps the task out of dispatch until resolved).
    assert t.effects.progress is Progress.INCREMENT
    assert t.effects.next_eligible_in is None
    assert _kinds(t) == ["task_blocked", "task_result"]
    assert t.events[0].payload == {"reason": "validation_error", "detail": "exit 2"}
    assert t.response["quarantined"] is False


def test_error_non_validation_hold_kind_gets_a_kind_derived_reason() -> None:
    # Only VALIDATION_ERROR keeps the historical "validation failed:" wording;
    # every other HOLD kind (Phase 6 routes MERGE_* here) must say what it is.
    out = _outcome(
        RunStatus.ERROR, result_line="rebase stopped",
        failure_kind=FailureKind.MERGE_CONFLICT, failure_reason="conflict in a.py",
    )
    t = on_submit(REF, out, SubmitPolicy())
    assert isinstance(t, Transition)
    # A merge-kind error stores CONFLICT (resolve owns the next step).
    assert t.state is AttemptState.CONFLICT
    assert t.effects.hold is not None
    assert t.effects.hold.reason == "merge_conflict: rebase stopped"
    assert t.effects.progress is Progress.INCREMENT
    assert t.effects.next_eligible_in is None
    assert _kinds(t) == ["task_blocked", "task_result"]
    assert t.events[0].payload == {
        "reason": "merge_conflict", "detail": "conflict in a.py",
    }
    # No detail at all: the reason is just the kind.
    bare = on_submit(
        REF,
        _outcome(RunStatus.ERROR, failure_kind=FailureKind.BLOCKED),
        SubmitPolicy(),
    )
    assert isinstance(bare, Transition)
    assert bare.state is AttemptState.FAILED  # non-merge kinds stay FAILED
    assert bare.effects.hold is not None
    assert bare.effects.hold.reason == "blocked"


def test_error_worker_quarantine_flag_quarantines_immediately() -> None:
    t = on_submit(
        REF, _outcome(RunStatus.ERROR),
        SubmitPolicy(retry=RetryPolicy(immediate_quarantine=True)),
    )
    assert isinstance(t, Transition)
    assert [f.key for f in t.effects.frontmatter] == ["quarantined"]
    assert "worker on first failure (worker error)" in t.effects.frontmatter[0].reason
    assert t.effects.watch_armed is None
    assert _kinds(t) == ["task_quarantined", "task_result"]
    assert t.events[0].payload["streak"] == 1
    assert t.response["quarantined"] is True


def test_error_counter_threshold_quarantines() -> None:
    policy = SubmitPolicy(
        retry=RetryPolicy(quarantine_after=3), attempts_without_progress=2,
    )
    t = on_submit(REF, _outcome(RunStatus.ERROR), policy)
    assert isinstance(t, Transition)
    assert [f.key for f in t.effects.frontmatter] == ["quarantined"]
    assert "3 consecutive runs with no progress (worker error)" in (
        t.effects.frontmatter[0].reason
    )
    assert _kinds(t) == ["task_quarantined", "task_result"]
    assert t.events[0].payload["streak"] == 3
    # The quarantined outcome still counts (the counter reaches the threshold
    # value); the hold itself keeps the task out of dispatch, not backoff.
    assert t.effects.progress is Progress.INCREMENT
    assert t.effects.next_eligible_in is None
    assert t.response["quarantined"] is True


def test_error_below_counter_threshold_is_a_plain_failure_with_backoff() -> None:
    policy = SubmitPolicy(
        retry=RetryPolicy(quarantine_after=3), attempts_without_progress=1,
    )
    t = on_submit(REF, _outcome(RunStatus.ERROR), policy)
    assert isinstance(t, Transition)
    assert [f.key for f in t.effects.frontmatter] == ["failed"]
    assert t.effects.progress is Progress.INCREMENT
    # Second consecutive no-progress attempt -> base * 2.
    assert t.effects.next_eligible_in == 120.0
    assert t.response["quarantined"] is False


def test_error_threshold_quarantine_on_retry_pauses_retry_failed() -> None:
    policy = SubmitPolicy(
        retry=RetryPolicy(quarantine_after=2), attempts_without_progress=1,
        was_retry=True,
    )
    t = on_submit(REF, _outcome(RunStatus.ERROR), policy)
    assert isinstance(t, Transition)
    assert t.effects.pause_queue == "retry_failed"
    assert _kinds(t) == ["task_quarantined", "queue_paused", "task_result"]


def test_error_on_retry_quarantines_and_pauses() -> None:
    t = on_submit(REF, _outcome(RunStatus.ERROR), SubmitPolicy(was_retry=True))
    assert isinstance(t, Transition)
    assert [f.key for f in t.effects.frontmatter] == ["failed", "quarantined"]
    assert t.effects.pause_queue == "retry_failed"
    assert _kinds(t) == ["task_quarantined", "queue_paused", "task_result"]


# ---- neutral (aborted / skipped / running) ----------------------------------- #


def test_aborted_and_skipped_are_neutral() -> None:
    # Phase 8 behavior fix: a neutral submit stores SKIPPED or ABORTED — the
    # degenerate `status="running"` submit (no real worker sends it) maps to
    # ABORTED instead of the pre-phase forever-"running" zombie row.
    expected_state = {
        RunStatus.ABORTED: AttemptState.ABORTED,
        RunStatus.SKIPPED: AttemptState.SKIPPED,
        RunStatus.RUNNING: AttemptState.ABORTED,
    }
    for status in (RunStatus.ABORTED, RunStatus.SKIPPED, RunStatus.RUNNING):
        t = on_submit(REF, _outcome(status, result_line="stopped"), SubmitPolicy(
            watch_armed=True, was_retry=True,
            retry=RetryPolicy(immediate_quarantine=True),
        ))
        assert isinstance(t, Transition)
        assert t.state is expected_state[status]
        assert t.effects.hold is None
        assert t.effects.frontmatter == ()
        assert t.effects.watch_armed is None
        assert t.effects.pause_queue is None
        assert t.effects.progress is Progress.NONE
        assert _kinds(t) == ["task_result"]
        assert t.response == {
            "landed": False, "status": status, "quarantined": False,
        }


# ---- completed → git phase dispatch ------------------------------------------ #


def test_completed_landable_requires_the_land_phase() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True)
    assert on_submit(REF, out, SubmitPolicy()) is GitPhase.LAND
    # landable wins over split (a split run never submits landable=True).
    assert on_submit(REF, out, SubmitPolicy(split=True)) is GitPhase.LAND


def test_completed_split_requires_the_harvest_phase() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=False)
    assert on_submit(REF, out, SubmitPolicy(split=True)) is GitPhase.HARVEST_SPLIT


def test_completed_non_landable_requires_the_adopt_check() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=False)
    assert on_submit(REF, out, SubmitPolicy()) is GitPhase.ADOPT_CHECK


# ---- split harvest ------------------------------------------------------------ #


def test_split_result_reports_subtasks_and_clears_hold() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=False)
    t = on_split_result(REF, out, ["20.a", "21.b"])
    assert t.state is AttemptState.NO_CHANGE
    assert t.fields["result_line"] == "decomposed into 2 subtask(s): 20.a, 21.b"
    assert t.effects.clear_hold is True
    assert t.effects.progress is Progress.RESET
    assert _kinds(t) == ["task_result", "queue_changed"]
    assert t.events[0].payload["subtasks"] == ["20.a", "21.b"]
    assert t.response == {
        "landed": False, "status": "completed",
        "split": True, "subtasks": ["20.a", "21.b"],
    }


def test_split_result_with_no_subtasks() -> None:
    t = on_split_result(REF, _outcome(RunStatus.COMPLETED, landable=False), [])
    assert t.fields["result_line"] == "decomposition run produced no subtasks"


# ---- land results -------------------------------------------------------------- #


def _no_change(**kw: Any) -> LandOutcome:
    return LandOutcome(kind=LandKind.NO_CHANGES, **kw)


def test_nothing_to_land_records_no_changes_and_counts_as_failure() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=False)
    t = on_land_result(REF, out, _no_change(), SubmitPolicy())
    assert t.state is AttemptState.NO_CHANGE
    assert t.fields["result_line"] == "no changes"
    assert [(f.key, f.reason) for f in t.effects.frontmatter] == [
        ("failed", "no changes produced"),
    ]
    assert t.effects.watch_armed is True
    # A no-change run counts toward the counter and earns a backoff.
    assert t.effects.progress is Progress.INCREMENT
    assert t.effects.next_eligible_in == 60.0
    assert _kinds(t) == ["task_result", "queue_changed"]
    assert t.response == {
        "landed": False, "status": "completed",
        "no_changes": True, "quarantined": False,
    }


def test_nothing_to_land_keeps_the_workers_result_line() -> None:
    out = _outcome(
        RunStatus.COMPLETED, landable=False,
        result_line="no changes produced (worker emitted output only)",
    )
    t = on_land_result(REF, out, _no_change(), SubmitPolicy())
    assert t.fields["result_line"] == (
        "no changes produced (worker emitted output only)"
    )


def test_nothing_to_land_hits_quarantine_threshold() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=False)
    policy = SubmitPolicy(
        retry=RetryPolicy(quarantine_after=2), attempts_without_progress=1,
    )
    t = on_land_result(REF, out, _no_change(), policy)
    assert [f.key for f in t.effects.frontmatter] == ["quarantined"]
    assert "2 consecutive runs with no progress (no changes produced)" in (
        t.effects.frontmatter[0].reason
    )
    assert t.effects.watch_armed is None
    assert _kinds(t) == ["task_quarantined", "task_result", "queue_changed"]
    assert t.response["quarantined"] is True


def test_nothing_to_land_worker_quarantine_is_immediate() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=False)
    t = on_land_result(
        REF, out, _no_change(),
        SubmitPolicy(retry=RetryPolicy(immediate_quarantine=True)),
    )
    assert t.events[0].payload["streak"] == 1
    assert t.response["quarantined"] is True


def test_nothing_to_land_on_retry_quarantines_and_pauses() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=False)
    t = on_land_result(REF, out, _no_change(), SubmitPolicy(was_retry=True))
    assert [f.key for f in t.effects.frontmatter] == ["failed", "quarantined"]
    assert t.effects.pause_queue == "retry_failed"
    assert _kinds(t) == [
        "task_quarantined", "queue_paused", "task_result", "queue_changed",
    ]


def test_landed_clears_state_disarms_watch_and_drops_brief() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True, result_line="did the thing")
    land = LandOutcome(
        kind=LandKind.LANDED, sha="abc123", remote="push", pushed=True, loc=42,
    )
    t = on_land_result(REF, out, land, SubmitPolicy(watch_armed=True))
    assert t.state is AttemptState.LANDED
    assert t.fields["result_line"] == "did the thing"
    assert t.fields["commit_sha"] == "abc123"
    assert t.fields["loc"] == 42
    assert t.fields["remote"] == "push"
    assert t.fields["pushed"] is True
    assert t.effects.clear_hold is True
    assert t.effects.progress is Progress.RESET
    assert t.effects.watch_armed is False
    assert t.effects.drop_brief is True
    assert _kinds(t) == ["task_result", "queue_changed"]
    assert t.events[0].payload == {
        "status": "completed", "commit_sha": "abc123",
        "remote": "push", "pushed": True, "pr_url": None,
    }
    assert t.response == {
        "landed": True, "sha": "abc123", "remote": "push",
        "pushed": True, "pr_url": None,
    }


def test_landed_evergreen_keeps_the_brief() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True)
    t = on_land_result(
        REF, out, LandOutcome(kind=LandKind.LANDED, sha="abc"),
        SubmitPolicy(evergreen=True),
    )
    assert t.effects.drop_brief is False


def test_adopted_non_landable_records_agent_landed_on_main() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=False, result_line="no changes")
    land = LandOutcome(
        kind=LandKind.ADOPTED, sha="abc", detail="adopted agent land on main",
    )
    t = on_land_result(REF, out, land, SubmitPolicy())
    assert t.state is AttemptState.LANDED
    assert t.fields["result_line"] == "agent landed on main"
    assert t.response["landed"] is True


def test_adopted_landable_keeps_the_workers_result_line() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True, result_line="validated")
    land = LandOutcome(
        kind=LandKind.ADOPTED, sha="abc", detail="adopted agent land on main",
    )
    t = on_land_result(REF, out, land, SubmitPolicy())
    assert t.fields["result_line"] == "validated"


def test_land_conflict_blocks_for_resolve_and_arms_watch() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True)
    land = LandOutcome(kind=LandKind.CONFLICT, detail="squash conflicts\non x")
    t = on_land_result(REF, out, land, SubmitPolicy())
    assert t.state is AttemptState.CONFLICT
    assert t.fields["result_line"] == "squash conflicts"
    assert t.fields["failure_kind"] == FailureKind.MERGE_CONFLICT
    assert t.effects.hold is not None
    assert t.effects.hold.reason == "needs resolve: squash conflicts"
    assert t.effects.hold.retry_eligible is False
    assert t.effects.watch_armed is True
    assert t.effects.start_resolve is False
    assert _kinds(t) == ["task_blocked", "task_result"]
    assert t.events[1].payload == {
        "status": "error", "failure_kind": "merge_conflict",
    }
    assert t.response == {
        "landed": False, "conflict": True,
        "detail": "squash conflicts\non x", "resolving": False,
    }


def test_land_conflict_auto_resolve_starts_a_resolve() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True)
    land = LandOutcome(kind=LandKind.CONFLICT, detail="conflict")
    t = on_land_result(REF, out, land, SubmitPolicy(auto_resolve=True))
    assert t.effects.start_resolve is True
    # PR mode lands via GitHub — never escalated to the local resolver.
    t = on_land_result(
        REF, out, land, SubmitPolicy(auto_resolve=True, pr_mode=True)
    )
    assert t.effects.start_resolve is False


def test_land_recoverable_rejection_blocks_as_merge_rejected() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True)
    land = LandOutcome(kind=LandKind.PUSH_REJECTED, detail="push rejected")
    t = on_land_result(REF, out, land, SubmitPolicy(watch_armed=True))
    assert t.state is AttemptState.CONFLICT
    assert t.fields["failure_kind"] == FailureKind.MERGE_REJECTED
    assert t.effects.hold is not None
    assert t.effects.pause_queue == "consecutive_failures"
    assert _kinds(t) == ["queue_paused", "task_blocked", "task_result"]


def test_land_unrecoverable_failure_releases_without_hold() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True)
    land = LandOutcome(kind=LandKind.TRANSPORT_FAILED, detail="head_sha mismatch")
    t = on_land_result(REF, out, land, SubmitPolicy(watch_armed=True))
    assert t.effects.hold is None
    assert t.effects.watch_armed is None
    assert t.effects.pause_queue is None
    assert t.effects.start_resolve is False
    assert _kinds(t) == ["task_result"]


def test_land_failed_empty_detail_defaults_result_line() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True)
    t = on_land_result(
        REF, out, LandOutcome(kind=LandKind.TRANSPORT_FAILED), SubmitPolicy()
    )
    assert t.fields["result_line"] == "land failed"


def test_retryable_transport_failure_holds_for_retry() -> None:
    """A fetch hiccup (retryable TRANSPORT_FAILED) keeps the WIP ref usable:
    the task is held blocked so a re-submit can re-fetch."""
    out = _outcome(RunStatus.COMPLETED, landable=True)
    land = LandOutcome(
        kind=LandKind.TRANSPORT_FAILED, retryable=True, detail="fetch failed",
    )
    t = on_land_result(REF, out, land, SubmitPolicy())
    assert t.fields["failure_kind"] == FailureKind.MERGE_REJECTED
    assert t.effects.hold is not None


def test_checkout_behind_is_a_landed_success() -> None:
    """A land whose checkout advance was refused (operator WIP overlap) is
    still a success: the ref is authoritative."""
    out = _outcome(RunStatus.COMPLETED, landable=True, result_line="")
    land = LandOutcome(
        kind=LandKind.CHECKOUT_BEHIND, sha="abc123",
        detail="landed (abc123); your uncommitted changes kept the checkout from advancing",
    )
    t = on_land_result(REF, out, land, SubmitPolicy())
    assert t.state is AttemptState.LANDED
    assert t.fields["commit_sha"] == "abc123"
    assert t.effects.clear_hold is True
    assert "checkout" in t.fields["result_line"]


def test_every_land_kind_has_a_transition() -> None:
    """Exhaustiveness pin: every LandKind produces a Transition (a newly-added
    kind that falls through raises via assert_never instead)."""
    out = _outcome(RunStatus.COMPLETED, landable=True)
    for kind in LandKind:
        t = on_land_result(
            REF, out, LandOutcome(kind=kind, sha="abc"), SubmitPolicy()
        )
        assert isinstance(t, Transition)


# ---- deadline ------------------------------------------------------------------ #


def test_on_deadline_expires_the_attempt_and_nothing_else() -> None:
    # Phase 8 behavior fix: EXPIRED is terminal (the applier stamps
    # finished_at), unlike the pre-phase zombie that stayed "running" forever.
    t = on_deadline(REF)
    assert t.state is AttemptState.EXPIRED
    assert t.fields == {}
    assert t.events == ()
    assert t.effects.hold is None
    assert t.effects.clear_hold is False
    assert t.effects.frontmatter == ()
    assert t.response == {}


def test_on_operator_stop_aborts_with_no_policy_action() -> None:
    # Phase 8 behavior fix: stop/skip aborts the attempt (terminal, stamps
    # finished_at) instead of cancelling a lease around a forever-running row.
    t = on_operator_stop(REF)
    assert t.state is AttemptState.ABORTED
    assert t.fields == {}
    assert t.events == ()
    assert t.effects.hold is None
    assert t.effects.frontmatter == ()
    assert t.response == {}


def test_on_land_enqueued_moves_to_landing_and_persists_the_refs() -> None:
    t = on_land_enqueued(REF, branch_ref="refs/nightshift/wip/x", head_sha="h1")
    assert t.state is AttemptState.LANDING
    assert t.fields == {
        "phase": "landing",
        "branch_ref": "refs/nightshift/wip/x",
        "head_sha": "h1",
    }
    assert t.events == ()
    assert t.effects == TaskEffects()  # no task effects at enqueue time


def test_on_land_recovered_lands_with_the_found_sha() -> None:
    t = on_land_recovered(REF, "abc123")
    assert t.state is AttemptState.LANDED
    assert t.fields["commit_sha"] == "abc123"
    assert "recovered" in t.fields["result_line"]
    assert t.effects.clear_hold is True
    assert t.effects.progress is Progress.RESET
    assert t.effects.watch_armed is False
    assert _kinds(t) == ["task_result", "queue_changed"]
    assert t.events[0].payload["commit_sha"] == "abc123"


# ---- Phase 5: RetryPolicy / backoff / environment failures --------------------- #


def test_retry_policy_classifies_every_failure_kind() -> None:
    policy = RetryPolicy()
    expected = {
        # environment: retried elsewhere, never counted against the task.
        FailureKind.MODEL_UNAVAILABLE: RetryAction.RETRY_ELSEWHERE,
        FailureKind.BACKEND_UNAVAILABLE: RetryAction.RETRY_ELSEWHERE,
        FailureKind.REPO_UNAVAILABLE: RetryAction.RETRY_ELSEWHERE,
        FailureKind.PREFLIGHT_FAILED: RetryAction.RETRY_ELSEWHERE,
        FailureKind.WORKTREE_FAILED: RetryAction.RETRY_ELSEWHERE,
        FailureKind.WORKER_LAUNCH: RetryAction.RETRY_ELSEWHERE,
        FailureKind.PUBLISH_FAILED: RetryAction.RETRY_ELSEWHERE,
        # task: retried under the counter threshold.
        FailureKind.WORKER_ERROR: RetryAction.RETRY,
        # recoverable/holdable: an operator or resolve owns the next step.
        FailureKind.VALIDATION_ERROR: RetryAction.HOLD,
        FailureKind.BLOCKED: RetryAction.HOLD,
        FailureKind.MERGE_CONFLICT: RetryAction.HOLD,
        FailureKind.MERGE_REJECTED: RetryAction.HOLD,
    }
    assert {k: policy.on_failure(k) for k in FailureKind} == expected
    # The module-level frozenset is derived from this classification.
    assert ENVIRONMENT_FAILURE_KINDS == {
        k for k, action in expected.items()
        if action is RetryAction.RETRY_ELSEWHERE
    }


def test_retry_policy_immediate_quarantine_only_escalates_task_failures() -> None:
    policy = RetryPolicy(immediate_quarantine=True)
    assert policy.on_failure(FailureKind.WORKER_ERROR) is RetryAction.QUARANTINE
    # Environment failures are never the task's fault, even in quarantine mode.
    for kind in ENVIRONMENT_FAILURE_KINDS:
        assert policy.on_failure(kind) is RetryAction.RETRY_ELSEWHERE


def test_backoff_doubles_from_base_and_caps() -> None:
    backoff = Backoff(base_seconds=60.0, cap_seconds=3600.0)
    assert [backoff.delay(n) for n in (1, 2, 3, 4)] == [60.0, 120.0, 240.0, 480.0]
    assert backoff.delay(10) == 3600.0
    # A huge counter (quarantine disabled, hot-failing task) must not
    # overflow the exponent — it just returns the cap.
    assert backoff.delay(10_000) == 3600.0


def test_environment_failure_is_neutral_to_the_task_and_cools_the_worker() -> None:
    for kind in sorted(ENVIRONMENT_FAILURE_KINDS):
        out = _outcome(
            RunStatus.ERROR, result_line="box broke", failure_kind=kind,
        )
        # Even a threshold-ready counter or quarantine-mode worker must not
        # blame the task for its environment.
        t = on_submit(REF, out, SubmitPolicy(
            retry=RetryPolicy(quarantine_after=2, immediate_quarantine=True),
            attempts_without_progress=5,
            watch_armed=True,
            was_retry=True,
        ))
        assert isinstance(t, Transition)
        assert t.fields["failure_kind"] == kind
        assert t.state is AttemptState.FAILED  # environment kinds are never merge kinds
        assert t.effects.hold is None
        assert t.effects.frontmatter == ()
        assert t.effects.progress is Progress.NONE
        assert t.effects.next_eligible_in is None
        assert t.effects.worker_cooldown is True
        assert t.effects.watch_armed is None
        assert t.effects.pause_queue is None
        assert _kinds(t) == ["task_result"]
        assert t.response == {
            "landed": False, "status": "error", "quarantined": False,
        }


def test_task_failures_do_not_cool_the_worker() -> None:
    t = on_submit(REF, _outcome(RunStatus.ERROR), SubmitPolicy())
    assert isinstance(t, Transition)
    assert t.effects.worker_cooldown is False


def test_error_backoff_grows_with_the_persisted_counter() -> None:
    t1 = on_submit(REF, _outcome(RunStatus.ERROR), SubmitPolicy())
    t3 = on_submit(
        REF, _outcome(RunStatus.ERROR), SubmitPolicy(attempts_without_progress=2)
    )
    assert isinstance(t1, Transition) and isinstance(t3, Transition)
    assert t1.effects.next_eligible_in == 60.0
    assert t3.effects.next_eligible_in == 240.0


# --------------------------------------------------------------------------- #
# Phase 8: fold/split — the Python twins of the data migration's CASEs
# --------------------------------------------------------------------------- #


# Every REACHABLE pre-Phase-8 (lease.status, run.status, phase, failure_kind,
# worker_id) combo and the state it folds to. Zombie combos (see fold_legacy's
# docstring) canonicalize and are tested separately below.
REACHABLE_LEGACY_COMBOS: list[tuple[tuple[str | None, str, str | None, str | None, str | None], AttemptState]] = [
    # (lease, run, phase, failure_kind, worker_id) -> state
    (("leased", "running", None, None, "w1"), AttemptState.RUNNING),
    (("leased", "running", "validate", None, "w1"), AttemptState.RUNNING),
    (("leased", "running", "landing", None, "w1"), AttemptState.LANDING),
    (("landed", "completed", None, None, "w1"), AttemptState.LANDED),
    (("released", "completed", None, None, "w1"), AttemptState.NO_CHANGE),
    (("released", "blocked", None, "blocked", "w1"), AttemptState.BLOCKED),
    (("released", "error", None, "merge_conflict", "w1"), AttemptState.CONFLICT),
    (("released", "error", None, "merge_rejected", "w1"), AttemptState.CONFLICT),
    (("released", "error", None, "worker_error", "w1"), AttemptState.FAILED),
    (("released", "error", None, None, "w1"), AttemptState.FAILED),
    # Retired legacy single-process-runner kinds: FailureKind no longer has
    # them, but old runs rows still carry the strings — the migration CASE
    # (and fold_legacy, which string-compares) folds them to FAILED.
    (("released", "error", None, "repo_config", "w1"), AttemptState.FAILED),
    (("released", "error", None, "disk", "w1"), AttemptState.FAILED),
    (("released", "error", None, "aborted", "w1"), AttemptState.FAILED),
    (("released", "aborted", None, None, "w1"), AttemptState.ABORTED),
    (("released", "skipped", None, None, "w1"), AttemptState.SKIPPED),
    (("cancelled", "running", None, None, "w1"), AttemptState.ABORTED),
    (("expired", "running", None, None, "w1"), AttemptState.EXPIRED),
    # Lease-less rows: resolve children and legacy single-process runs.
    ((None, "running", None, None, RESOLVE_WORKER_ID), AttemptState.RESOLVING),
    ((None, "completed", None, None, RESOLVE_WORKER_ID), AttemptState.NO_CHANGE),
    ((None, "error", None, "merge_conflict", RESOLVE_WORKER_ID), AttemptState.CONFLICT),
    ((None, "error", None, "worker_error", None), AttemptState.FAILED),
    ((None, "blocked", None, "blocked", None), AttemptState.BLOCKED),
    ((None, "aborted", None, None, None), AttemptState.ABORTED),
    ((None, "skipped", None, None, None), AttemptState.SKIPPED),
]


def test_fold_legacy_covers_every_reachable_combo() -> None:
    for (lease, run, phase, kind, worker), expected in REACHABLE_LEGACY_COMBOS:
        folded = fold_legacy(
            lease, run, phase=phase, failure_kind=kind, worker_id=worker,
        )
        assert folded is expected, (lease, run, phase, kind, worker)


def test_fold_legacy_canonicalizes_the_zombie_combos() -> None:
    """The documented Phase 8 behavior fixes: combos that pre-phase left a
    forever-'running' run row now fold to a truthful terminal state."""
    # A cancelled/expired lease dominates whatever the run row says.
    assert fold_legacy("cancelled", "completed") is AttemptState.ABORTED
    assert fold_legacy("expired", "error") is AttemptState.EXPIRED
    # A released lease around a still-running run: the degenerate neutral
    # submit — canonicalizes to ABORTED.
    assert fold_legacy("released", "running") is AttemptState.ABORTED
    # An orphaned lease-less running row that is NOT a resolve child.
    assert fold_legacy(None, "running", worker_id="w9") is AttemptState.ABORTED


def test_split_then_fold_round_trips_every_storable_state() -> None:
    """Law 1: fold(split(s)) == s. LANDING refolds via the phase column and
    CONFLICT via failure_kind — both copied verbatim by the migration."""
    for state in AttemptState:
        if state in (AttemptState.CLAIMED, AttemptState.SUBMITTED):
            with pytest.raises(ValueError):
                split_state(state)
            continue
        lease, run = split_state(state)
        refolded = fold_legacy(
            lease, run,
            phase="landing" if state is AttemptState.LANDING else None,
            failure_kind=(
                "merge_conflict" if state is AttemptState.CONFLICT else None
            ),
            worker_id=(
                RESOLVE_WORKER_ID if state is AttemptState.RESOLVING else "w1"
            ),
        )
        assert refolded is state, state


def test_fold_then_split_round_trips_non_degenerate_combos() -> None:
    """Law 2: split(fold(combo)) == combo for every combo whose lease/run pair
    survives unchanged (ABORTED canonicalizes released- and cancel-shaped
    aborts to ('cancelled', 'aborted'); EXPIRED and RESOLVING keep 'running'
    as the run half exactly as pre-phase)."""
    canonical = {
        # combos whose split differs from the input (documented canonicalization)
        ("released", "aborted"): ("cancelled", "aborted"),
        ("cancelled", "running"): ("cancelled", "aborted"),
        (None, "aborted"): ("cancelled", "aborted"),
        (None, "completed"): ("released", "completed"),
        (None, "error"): ("released", "error"),
        (None, "blocked"): ("released", "blocked"),
        (None, "skipped"): ("released", "skipped"),
    }
    for (lease, run, phase, kind, worker), state in REACHABLE_LEGACY_COMBOS:
        if state is AttemptState.RESOLVING:
            assert split_state(state) == (None, "running")
            continue
        expected = canonical.get((lease, run), (lease, run))
        assert split_state(state) == expected, (lease, run, state)


def test_merge_failure_kinds_is_exactly_the_conflict_route() -> None:
    assert MERGE_FAILURE_KINDS == {
        FailureKind.MERGE_CONFLICT, FailureKind.MERGE_REJECTED,
    }
    for kind in FailureKind:
        t = on_submit(REF, _outcome(RunStatus.ERROR, failure_kind=kind), SubmitPolicy())
        assert isinstance(t, Transition)
        expected = (
            AttemptState.CONFLICT if kind in MERGE_FAILURE_KINDS
            else AttemptState.FAILED
        )
        assert t.state is expected, kind
