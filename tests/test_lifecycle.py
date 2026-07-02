"""Phase 1 (typed vocabulary) + Phase 4 (pure transition core) tests.

The unified ``Outcome`` model replaces the five hand-synced outcome shapes
(``ExecuteOutcome``, the submit payload dict, ``SubmitBody``, the local-store
finish dict, the ``worker_submit`` telemetry re-pack). These tests pin the
wire contract: the submit payload a worker posts must carry exactly the same
keys as before the unification.

The Phase 4 half is the transition table: every submit outcome × policy
context, every land-result kind, and deadline expiry, exercised purely — no
store, no git, no HTTP.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nightshift.lifecycle import (
    ENVIRONMENT_FAILURE_KINDS,
    LEASE_ACTIVE_STATUSES,
    RUN_TERMINAL_STATUSES,
    AttemptRef,
    Backoff,
    FailureKind,
    GitPhase,
    LandingMode,
    LandResult,
    LeaseStatus,
    Outcome,
    Progress,
    RetryAction,
    RetryPolicy,
    RunStatus,
    SubmitPolicy,
    TaskHoldKind,
    Telemetry,
    Transition,
    on_deadline,
    on_land_result,
    on_split_result,
    on_submit,
)
from nightshift.worker.config import WorkerConfig
from nightshift.worker.local_store import LocalStore
from nightshift.worker.loop import WorkerLoop


# The submit payload keys as posted by WorkerLoop._submit BEFORE the Outcome
# unification (Phase 0 wire format). Ground rule: byte-identical wire.
PREVIOUS_SUBMIT_PAYLOAD_KEYS = frozenset({
    "worker_id", "lease_id", "task", "queue", "repo", "title",
    "status", "result_line", "backend", "model", "landable",
    "branch_ref", "head_sha", "failure_kind", "failure_reason",
    "turns", "input_tokens", "output_tokens", "cost_usd",
    "validate_cmd", "worktree", "quarantine",
})

# The envelope the loop adds around the Outcome (lease/task identity + the
# worker-side quarantine flag).
ENVELOPE_KEYS = frozenset({
    "worker_id", "lease_id", "task", "queue", "repo", "title", "quarantine",
})

# The local-store finish record keys as written to the worker's runs.jsonl
# BEFORE the Outcome unification: run identity + the outcome fields the local
# Now/History UI shows + the manager's land result, plus the started_at /
# finished_at stamps LocalStore.finish adds itself. Ground rule: the on-disk
# format stays byte-identical.
PREVIOUS_LOCAL_FINISH_KEYS = frozenset({
    "run_id", "task", "queue", "title", "repo",
    "model", "backend", "status", "failure_kind", "result_line",
    "commit_sha", "landed", "quarantined",
    "turns", "input_tokens", "output_tokens", "cost_usd", "worktree",
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


def test_telemetry_slice_matches_the_old_worker_submit_repack() -> None:
    """`outcome.telemetry.model_dump()` reproduces the dict worker_submit used
    to hand-assemble (the re-packing dict that Phase 1 deletes)."""
    outcome = Outcome(
        status=RunStatus.COMPLETED, turns=8, input_tokens=1500,
        output_tokens=400, cost_usd=0.09, validate_cmd="just validate",
        worktree="/w/t",
    )
    assert outcome.telemetry.model_dump() == {
        "turns": 8, "input_tokens": 1500, "output_tokens": 400,
        "cost_usd": 0.09, "validate_cmd": "just validate", "worktree": "/w/t",
    }
    assert set(Telemetry.model_fields) == {
        "turns", "input_tokens", "output_tokens", "cost_usd",
        "validate_cmd", "worktree",
    }


def test_enum_values_are_todays_wire_strings() -> None:
    """StrEnum values must be byte-identical to the strings on the wire/in DB."""
    assert RunStatus.COMPLETED == "completed"
    assert RunStatus.BLOCKED == "blocked"
    assert RunStatus.ERROR == "error"
    assert LeaseStatus.LEASED == "leased"
    assert LandingMode.NONE == "none"
    assert LandingMode.PUSH == "push"
    assert LandingMode.PR == "pr"
    # blocked is terminal (the finished_at fix); leases have exactly one
    # active status now that the dead 'submitted' status is gone.
    assert RunStatus.BLOCKED in RUN_TERMINAL_STATUSES
    assert LEASE_ACTIVE_STATUSES == {LeaseStatus.LEASED}


def test_landing_mode_is_remote_is_exhaustive() -> None:
    assert LandingMode.PUSH.is_remote is True
    assert LandingMode.PR.is_remote is True
    assert LandingMode.NONE.is_remote is False


# --------------------------------------------------------------------------- #
# Phase 4: the transition table (pure — no store, no git, no HTTP)
# --------------------------------------------------------------------------- #


REF = AttemptRef(run_id="r1", lease_id="l1", queue=None, task="10.demo")


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
    assert t.run_fields["status"] == RunStatus.BLOCKED
    assert t.run_fields["failure_kind"] == FailureKind.BLOCKED
    assert t.run_fields["turns"] == 3
    assert t.lease_status == LeaseStatus.RELEASED
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
    assert t.run_fields["status"] == RunStatus.ERROR
    assert t.lease_status == LeaseStatus.RELEASED
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
        REF, _outcome(RunStatus.ERROR, failure_kind=FailureKind.DISK), SubmitPolicy()
    )
    assert isinstance(bare, Transition)
    assert bare.effects.hold is not None
    assert bare.effects.hold.reason == "disk"


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
    for status in (RunStatus.ABORTED, RunStatus.SKIPPED, RunStatus.RUNNING):
        t = on_submit(REF, _outcome(status, result_line="stopped"), SubmitPolicy(
            watch_armed=True, was_retry=True,
            retry=RetryPolicy(immediate_quarantine=True),
        ))
        assert isinstance(t, Transition)
        assert t.lease_status == LeaseStatus.RELEASED
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
    assert t.run_fields["status"] == RunStatus.COMPLETED
    assert t.run_fields["result_line"] == "decomposed into 2 subtask(s): 20.a, 21.b"
    assert t.lease_status == LeaseStatus.RELEASED
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
    assert t.run_fields["result_line"] == "decomposition run produced no subtasks"


# ---- land results -------------------------------------------------------------- #


def _no_change(**kw: Any) -> LandResult:
    return LandResult(landed=False, nothing_to_land=True, **kw)


def test_nothing_to_land_records_no_changes_and_counts_as_failure() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=False)
    t = on_land_result(REF, out, _no_change(), SubmitPolicy())
    assert t.run_fields["status"] == RunStatus.COMPLETED
    assert t.run_fields["result_line"] == "no changes"
    assert t.lease_status == LeaseStatus.RELEASED
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
    assert t.run_fields["result_line"] == (
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
    land = LandResult(
        landed=True, sha="abc123", remote="push", pushed=True, loc=42,
    )
    t = on_land_result(REF, out, land, SubmitPolicy(watch_armed=True))
    assert t.run_fields["status"] == RunStatus.COMPLETED
    assert t.run_fields["result_line"] == "did the thing"
    assert t.run_fields["commit_sha"] == "abc123"
    assert t.run_fields["loc"] == 42
    assert t.run_fields["remote"] == "push"
    assert t.run_fields["pushed"] is True
    assert t.lease_status == LeaseStatus.LANDED
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
        REF, out, LandResult(landed=True, sha="abc"), SubmitPolicy(evergreen=True)
    )
    assert t.effects.drop_brief is False


def test_adopted_non_landable_records_agent_landed_on_main() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=False, result_line="no changes")
    land = LandResult(
        landed=True, sha="abc", detail="adopted agent land on main", adopted=True,
    )
    t = on_land_result(REF, out, land, SubmitPolicy())
    assert t.run_fields["result_line"] == "agent landed on main"
    assert t.response["landed"] is True


def test_adopted_landable_keeps_the_workers_result_line() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True, result_line="validated")
    land = LandResult(
        landed=True, sha="abc", detail="adopted agent land on main", adopted=True,
    )
    t = on_land_result(REF, out, land, SubmitPolicy())
    assert t.run_fields["result_line"] == "validated"


def test_land_conflict_blocks_for_resolve_and_arms_watch() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True)
    land = LandResult(landed=False, conflict=True, detail="squash conflicts\non x")
    t = on_land_result(REF, out, land, SubmitPolicy())
    assert t.run_fields["status"] == RunStatus.ERROR
    assert t.run_fields["result_line"] == "squash conflicts"
    assert t.run_fields["failure_kind"] == FailureKind.MERGE_CONFLICT
    assert t.lease_status == LeaseStatus.RELEASED
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
    land = LandResult(landed=False, conflict=True, detail="conflict")
    t = on_land_result(REF, out, land, SubmitPolicy(auto_resolve=True))
    assert t.effects.start_resolve is True
    # PR mode lands via GitHub — never escalated to the local resolver.
    t = on_land_result(
        REF, out, land, SubmitPolicy(auto_resolve=True, pr_mode=True)
    )
    assert t.effects.start_resolve is False


def test_land_recoverable_rejection_blocks_as_merge_rejected() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True)
    land = LandResult(landed=False, recoverable=True, detail="push rejected")
    t = on_land_result(REF, out, land, SubmitPolicy(watch_armed=True))
    assert t.run_fields["failure_kind"] == FailureKind.MERGE_REJECTED
    assert t.effects.hold is not None
    assert t.effects.pause_queue == "consecutive_failures"
    assert _kinds(t) == ["queue_paused", "task_blocked", "task_result"]


def test_land_unrecoverable_failure_releases_without_hold() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True)
    land = LandResult(landed=False, detail="head_sha mismatch")
    t = on_land_result(REF, out, land, SubmitPolicy(watch_armed=True))
    assert t.effects.hold is None
    assert t.effects.watch_armed is None
    assert t.effects.pause_queue is None
    assert t.effects.start_resolve is False
    assert _kinds(t) == ["task_result"]


def test_land_failed_empty_detail_defaults_result_line() -> None:
    out = _outcome(RunStatus.COMPLETED, landable=True)
    t = on_land_result(REF, out, LandResult(landed=False), SubmitPolicy())
    assert t.run_fields["result_line"] == "land failed"


# ---- deadline ------------------------------------------------------------------ #


def test_on_deadline_expires_the_lease_and_nothing_else() -> None:
    t = on_deadline(REF)
    assert t.lease_status == LeaseStatus.EXPIRED
    assert t.run_fields == {}
    assert t.events == ()
    assert t.effects.hold is None
    assert t.effects.clear_hold is False
    assert t.effects.frontmatter == ()
    assert t.response == {}


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
        FailureKind.REPO_CONFIG: RetryAction.HOLD,
        FailureKind.DISK: RetryAction.HOLD,
        FailureKind.ABORTED: RetryAction.HOLD,
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
        assert t.run_fields["failure_kind"] == kind
        assert t.lease_status == LeaseStatus.RELEASED
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
