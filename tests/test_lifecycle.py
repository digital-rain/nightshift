"""Phase 1 (typed vocabulary) verification tests.

The unified ``Outcome`` model replaces the five hand-synced outcome shapes
(``ExecuteOutcome``, the submit payload dict, ``SubmitBody``, the local-store
finish dict, the ``worker_submit`` telemetry re-pack). These tests pin the
wire contract: the submit payload a worker posts must carry exactly the same
keys as before the unification.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nightshift.lifecycle import (
    LEASE_ACTIVE_STATUSES,
    RUN_TERMINAL_STATUSES,
    FailureKind,
    LandingMode,
    LeaseStatus,
    Outcome,
    RunStatus,
    Telemetry,
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
