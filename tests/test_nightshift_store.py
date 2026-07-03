"""Tests for the nightshift manager store + schema (Phase 0/4/8).

The MemoryStore exercises the same async interface as the PgStore, so these
verify the CRUD/attempt/stats semantics without a live database. The migration
tests pin the schema shape the PgStore relies on. The Phase 4 section covers
``apply_transition``: the CAS fence and the all-or-nothing outbox write. The
Phase 8 section tests the lifecycle invariants (greenfield §"Invariants" 1–4)
directly against the merged ``attempts`` surface.
"""

from __future__ import annotations

import asyncio

import pytest

from nightshift._paths import MIGRATIONS_DIR
from nightshift.lifecycle import (
    ATTEMPT_TERMINAL_STATES,
    AttemptRef,
    AttemptState,
    Progress,
    TaskEffects,
    TaskHold,
    TaskHoldKind,
    Transition,
    TransitionEvent,
    on_deadline,
    on_operator_stop,
)
from nightshift.manager.store import MemoryStore


MIGRATION = MIGRATIONS_DIR / "20260730000001_nightshift_schema.sql"
CAPABILITY_MIGRATION = (
    MIGRATIONS_DIR / "20260730000002_nightshift_capability_routing.sql"
)
RETRY_COUNTERS_MIGRATION = (
    MIGRATIONS_DIR / "20260731000002_nightshift_retry_counters.sql"
)
ATTEMPTS_MIGRATION = MIGRATIONS_DIR / "20260731000004_nightshift_attempts.sql"


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# worker registry + advertised capabilities
# --------------------------------------------------------------------------- #


def test_register_and_list_workers() -> None:
    store = MemoryStore()
    _run(store.register_worker("w1", backend="claude-code", queues=None, priorities=None))
    _run(store.register_worker("w2", backend="ollama", queues=["main"], priorities=[0, 1]))
    workers = _run(store.list_workers())
    assert [w["id"] for w in workers] == ["w1", "w2"]
    assert workers[1]["queues"] == ["main"]
    assert workers[1]["priorities"] == [0, 1]
    assert workers[0]["status"] == "idle"


def test_register_worker_advertises_models_and_mcps() -> None:
    store = MemoryStore()
    _run(store.register_worker(
        "w1", backend="cursor", queues=None, priorities=None,
        models=["claude-opus-4-8", "gpt-5.5"], mcps=["slack", "github"],
    ))
    w = _run(store.get_worker("w1"))
    assert w["models"] == ["claude-opus-4-8", "gpt-5.5"]
    assert w["mcps"] == ["slack", "github"]
    # A worker that advertises nothing still lists empty (not null) capabilities.
    _run(store.register_worker("w2", backend="ollama", queues=None, priorities=None))
    w2 = _run(store.get_worker("w2"))
    assert w2["models"] == []
    assert w2["mcps"] == []


def test_queue_dedication_round_trip() -> None:
    store = MemoryStore()
    assert _run(store.queue_dedication()) == {}
    _run(store.set_queue_dedication("ops", ["w-trusted", "w-backup"]))
    assert _run(store.queue_dedication()) == {"ops": ["w-trusted", "w-backup"]}
    # An empty list clears the dedication for that queue.
    _run(store.set_queue_dedication("ops", []))
    assert _run(store.queue_dedication()) == {}


def test_create_attempt_records_required_mcps() -> None:
    store = MemoryStore()
    _run(store.create_attempt(
        "r1", task="t1", queue=None, worker_id="w1",
        backend="cursor", model="auto", base_ref=None, ttl_seconds=60,
        required_mcps=["slack", "github"],
    ))
    attempt = _run(store.get_attempt("r1"))
    assert attempt["required_mcps"] == ["slack", "github"]


def test_worker_status_and_stale_expiry() -> None:
    store = MemoryStore()
    _run(store.register_worker("w1", backend="claude-code", queues=None, priorities=None))
    _run(store.set_worker_status("w1", status="busy", current_task="t", current_queue=None))
    assert _run(store.get_worker("w1"))["status"] == "busy"
    # ttl 0 → already stale → marked offline.
    stale = _run(store.expire_stale_workers(0))
    assert stale == ["w1"]
    assert _run(store.get_worker("w1"))["status"] == "offline"


# --------------------------------------------------------------------------- #
# attempts: invariant 1 (one live attempt per task) + deadline expiry
# --------------------------------------------------------------------------- #


def _attempt(store: MemoryStore, attempt_id: str, task: str = "t1", **kw):
    kw.setdefault("queue", None)
    kw.setdefault("worker_id", "w1")
    kw.setdefault("backend", "claude-code")
    kw.setdefault("model", "auto")
    kw.setdefault("base_ref", "abc")
    kw.setdefault("ttl_seconds", 60)
    return store.create_attempt(attempt_id, task=task, **kw)


def test_invariant_1_one_live_attempt_per_task() -> None:
    """Greenfield invariant 1: at most one attempt per (queue, task) is in a
    live state — the second create_attempt is refused while the first is
    RUNNING or LANDING, and allowed again once it is terminal."""
    store = MemoryStore()
    first = _run(_attempt(store, "r1"))
    assert first is not None
    assert _run(_attempt(store, "r2")) is None
    # Still refused mid-land (LANDING is live).
    _run(store.update_attempt("r1", state=AttemptState.LANDING))
    assert _run(_attempt(store, "r2")) is None
    # Terminal frees the task for a fresh attempt.
    _run(store.update_attempt("r1", state=AttemptState.FAILED))
    third = _run(_attempt(store, "r3"))
    assert third is not None
    # A RESOLVING child is NOT live: it never blocks dispatch of its task.
    _run(store.update_attempt("r3", state=AttemptState.CONFLICT))
    child = _run(_attempt(store, "r4", state="resolving", worker_id="manager:resolve"))
    assert child is not None
    assert child["deadline_at"] is None
    assert _run(_attempt(store, "r5")) is not None


def test_deadline_expiry_via_on_deadline_transition() -> None:
    """Phase 8: expiry is the on_deadline transition (RUNNING → EXPIRED, a
    terminal state with finished_at), replacing reclaim_expired_leases."""
    store = MemoryStore()
    _run(_attempt(store, "r1", ttl_seconds=-1))  # already overdue
    ref = AttemptRef(id="r1", queue=None, task="t1")
    assert _run(store.apply_transition(
        on_deadline(ref), expected_status=AttemptState.RUNNING,
    )) is not None
    row = _run(store.get_attempt("r1"))
    assert row["state"] == "expired"
    assert row["finished_at"] is not None
    # Expired → the task is dispatchable again.
    assert _run(_attempt(store, "r2")) is not None


# --------------------------------------------------------------------------- #
# task-state overlay (blocked)
# --------------------------------------------------------------------------- #


def test_blocked_task_state() -> None:
    store = MemoryStore()
    _run(store.set_task_state(None, "t1", "blocked", blocked_reason="no ollama worker"))
    blocked = _run(store.list_blocked())
    assert len(blocked) == 1
    assert blocked[0]["task"] == "t1"
    assert blocked[0]["blocked_reason"] == "no ollama worker"
    _run(store.clear_task_state(None, "t1"))
    assert _run(store.list_blocked()) == []


def test_retryable_tasks_includes_failed_state() -> None:
    store = MemoryStore()
    _run(store.set_task_state(None, "a", "failed", blocked_reason="worker error"))
    rows = _run(store.retryable_tasks(None))
    assert [r["task"] for r in rows] == ["a"]


def test_retryable_tasks_includes_retry_eligible_blocked_only() -> None:
    store = MemoryStore()
    _run(store.set_task_state(None, "honest", "blocked", blocked_reason="agent declined", retry_eligible=True))
    _run(store.set_task_state(None, "conflict", "blocked", blocked_reason="needs resolve", retry_eligible=False))
    rows = {r["task"] for r in _run(store.retryable_tasks(None))}
    assert rows == {"honest"}


def test_retryable_tasks_scoped_to_queue() -> None:
    store = MemoryStore()
    _run(store.set_task_state(None, "a", "failed"))
    _run(store.set_task_state("other", "b", "failed"))
    rows = _run(store.retryable_tasks(None))
    assert [r["task"] for r in rows] == ["a"]


# --------------------------------------------------------------------------- #
# attempts + stats
# --------------------------------------------------------------------------- #


def test_attempts_and_stats_by_backend() -> None:
    store = MemoryStore()
    _run(_attempt(store, "r1", backend="claude-code", model="claude-opus-4-8"))
    _run(store.update_attempt(
        "r1", state=AttemptState.LANDED, loc=42, commit_sha="deadbeef",
    ))
    _run(_attempt(store, "r2", task="t2", worker_id="w2",
                  backend="ollama", model="llama3.1"))
    _run(store.update_attempt(
        "r2", state=AttemptState.FAILED, failure_kind="validation_error",
    ))

    overall = _run(store.stats_overall())
    assert overall["total_runs"] == 2
    assert overall["completed"] == 1
    assert overall["errored"] == 1
    assert overall["total_loc"] == 42

    by_backend = {row["backend"]: row for row in _run(store.stats_by_backend())}
    assert by_backend["claude-code"]["completed"] == 1
    assert by_backend["claude-code"]["total_loc"] == 42
    assert by_backend["ollama"]["errored"] == 1


def test_turns_and_tokens_roll_up_per_model_backend_queue() -> None:
    store = MemoryStore()
    # Two attempts on the same model/backend (one landed, one failed) + a
    # third in a playlist queue on a different backend.
    _run(_attempt(store, "r1", backend="claude-code", model="claude-opus-4-8"))
    _run(store.update_attempt(
        "r1", state=AttemptState.LANDED, loc=10,
        turns=7, input_tokens=1000, output_tokens=200, cost_usd=0.05,
    ))
    _run(_attempt(store, "r2", task="t2", backend="claude-code",
                  model="claude-opus-4-8"))
    # A failed attempt still burned turns + tokens — it counts in the rollups.
    _run(store.update_attempt(
        "r2", state=AttemptState.FAILED, failure_kind="validation_error",
        turns=3, input_tokens=500, output_tokens=100, cost_usd=0.02,
    ))
    _run(_attempt(store, "r3", task="t3", queue="alpha", worker_id="w2",
                  backend="ollama", model="llama3.1"))
    _run(store.update_attempt(
        "r3", state=AttemptState.NO_CHANGE, loc=5,
        turns=1, input_tokens=300, output_tokens=80, cost_usd=None,
    ))

    overall = _run(store.stats_overall())
    assert overall["total_turns"] == 11           # 7 + 3 + 1
    assert overall["total_input_tokens"] == 1800  # 1000 + 500 + 300
    assert overall["total_output_tokens"] == 380  # 200 + 100 + 80
    assert overall["total_tokens"] == 2180
    assert round(overall["total_cost_usd"], 4) == 0.07  # ollama cost is None

    by_model = {r["model"]: r for r in _run(store.stats_by_model())}
    assert by_model["claude-opus-4-8"]["total_runs"] == 2
    assert by_model["claude-opus-4-8"]["total_turns"] == 10
    assert by_model["claude-opus-4-8"]["total_tokens"] == 1800
    assert round(by_model["claude-opus-4-8"]["avg_turns"], 1) == 5.0

    by_queue = {r["queue"]: r for r in _run(store.stats_by_queue())}
    # main queue key is "" (the playlist is "alpha").
    assert by_queue[""]["total_turns"] == 10
    assert by_queue["alpha"]["total_turns"] == 1


def test_blocked_attempt_gets_finished_at() -> None:
    """`blocked` is a terminal state: the attempt is over (the *task* is what
    stays held), so it must get finished_at like every other terminal state.
    Pre-Phase-1 this drifted: blocked runs kept finished_at = NULL forever."""
    store = MemoryStore()
    _run(_attempt(store, "r1"))
    _run(store.update_attempt(
        "r1", state=AttemptState.BLOCKED, result_line="blocked: needs creds",
    ))
    row = _run(store.get_attempt("r1"))
    assert row["state"] == "blocked"
    assert row["finished_at"] is not None


def test_update_attempt_raises_on_unknown_fields() -> None:
    """The updatable-field set derives from the Outcome/Telemetry models;
    unknown fields raise instead of being silently dropped."""
    store = MemoryStore()
    _run(_attempt(store, "r1"))
    with pytest.raises(ValueError, match="nonsense_field"):
        _run(store.update_attempt("r1", nonsense_field=1))


# --------------------------------------------------------------------------- #
# events (SSE delta stream source)
# --------------------------------------------------------------------------- #


def test_events_are_monotonic_and_cursorable() -> None:
    store = MemoryStore()
    first = _run(store.append_event("queue_changed", queue=None))
    second = _run(store.append_event("task_started", run_id="r1", task="t1"))
    assert second > first
    assert _run(store.max_event_id()) == second
    # Delta stream from a cursor yields only newer events.
    new = _run(store.events_since(first))
    assert [e["id"] for e in new] == [second]
    # Run-scoped lookup for late-join log backfill.
    run_events = _run(store.run_events("r1"))
    assert [e["kind"] for e in run_events] == ["task_started"]


# --------------------------------------------------------------------------- #
# apply_transition: CAS fence + all-or-nothing outbox (Phase 4)
# --------------------------------------------------------------------------- #


async def _live_attempt(store: MemoryStore) -> AttemptRef:
    """Create one running attempt, returning its identity."""
    row = await store.create_attempt(
        "r1", task="t1", queue=None, worker_id="w1",
        backend="claude-code", model="auto", base_ref="abc", ttl_seconds=60,
    )
    assert row is not None
    return AttemptRef(id="r1", queue=None, task="t1")


def _error_transition(ref: AttemptRef) -> Transition:
    return Transition(
        ref=ref,
        fields={"result_line": "boom"},
        state=AttemptState.FAILED,
        effects=TaskEffects(hold=TaskHold(TaskHoldKind.BLOCKED, "boom")),
        events=(
            TransitionEvent("task_blocked", queue=None, task="t1",
                            payload={"reason": "boom"}),
            TransitionEvent("task_result", run_id="r1", queue=None, task="t1",
                            payload={"status": "error"}),
        ),
    )


def test_apply_transition_writes_attempt_overlay_and_events_together() -> None:
    """Greenfield invariant 3: state and events change together (the success
    half — the failure halves are the CAS and bad-fields tests below)."""
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        ids = await store.apply_transition(
            _error_transition(ref),
            expected_status=AttemptState.RUNNING, expected_worker_id="w1",
        )
        assert ids is not None and len(ids) == 2
        row = await store.get_attempt("r1")
        assert row["state"] == "failed"
        assert row["result_line"] == "boom"
        assert row["finished_at"] is not None
        assert row["released_at"] is not None
        hold = await store.get_task_state(None, "t1")
        assert hold["state"] == "blocked"
        assert hold["blocked_reason"] == "boom"
        events = await store.events_since(0)
        assert [e["id"] for e in events] == ids
        assert [e["kind"] for e in events] == ["task_blocked", "task_result"]

    _run(scenario())


def test_invariant_2_submit_for_non_live_attempt_writes_nothing() -> None:
    """Greenfield invariant 2: an apply whose CAS misses (wrong worker or a
    state that already moved) writes nothing — no attempt fields, no overlay,
    no events."""
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        # Wrong worker: the submit fence.
        assert await store.apply_transition(
            _error_transition(ref),
            expected_status=AttemptState.RUNNING, expected_worker_id="w2",
        ) is None
        # Expired attempt: the expected state no longer matches.
        await store.update_attempt("r1", state=AttemptState.EXPIRED)
        assert await store.apply_transition(
            _error_transition(ref),
            expected_status=AttemptState.RUNNING, expected_worker_id="w1",
        ) is None
        # Nothing was written by either rejected apply.
        row = await store.get_attempt("r1")
        assert row["state"] == "expired"
        assert row["result_line"] is None
        assert await store.get_task_state(None, "t1") is None
        assert await store.events_since(0) == []

    _run(scenario())


def test_apply_transition_concurrent_applies_one_wins() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        landed = Transition(
            ref=ref,
            fields={"commit_sha": "abc"},
            state=AttemptState.LANDED,
            effects=TaskEffects(clear_hold=True),
            events=(TransitionEvent("task_result", run_id="r1", task="t1",
                                    payload={"status": "completed"}),),
        )
        results = await asyncio.gather(
            store.apply_transition(
                landed,
                expected_status=AttemptState.RUNNING, expected_worker_id="w1",
            ),
            store.apply_transition(
                _error_transition(ref),
                expected_status=AttemptState.RUNNING, expected_worker_id="w1",
            ),
        )
        wins = [r for r in results if r is not None]
        assert len(wins) == 1
        # Exactly one apply's events exist; the loser wrote nothing.
        events = await store.events_since(0)
        assert [e["id"] for e in events] == wins[0]

    _run(scenario())


def test_invariant_3_bad_fields_write_neither_state_nor_events() -> None:
    """Greenfield invariant 3, failure half: validation happens before any
    mutation — a malformed transition can't leave partial state."""
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        bad = Transition(
            ref=ref,
            fields={"nonsense_field": 1},
            state=AttemptState.FAILED,
            events=(TransitionEvent("task_result", run_id="r1"),),
        )
        with pytest.raises(ValueError, match="nonsense_field"):
            await store.apply_transition(
                bad,
                expected_status=AttemptState.RUNNING, expected_worker_id="w1",
            )
        row = await store.get_attempt("r1")
        assert row["state"] == "running"
        assert await store.events_since(0) == []

    _run(scenario())


def test_invariant_4_every_terminal_attempt_has_finished_at() -> None:
    """Greenfield invariant 4: every terminal state stamps finished_at — via
    apply_transition AND via the direct update_attempt path (the resolve
    writer); non-terminal states leave it NULL."""
    async def scenario() -> None:
        store = MemoryStore()
        # apply_transition path: one attempt per terminal state.
        for n, state in enumerate(sorted(ATTEMPT_TERMINAL_STATES)):
            rid = f"rt{n}"
            await store.create_attempt(
                rid, task=f"t{n}", queue=None, worker_id="w1",
                backend="b", model="m", base_ref=None, ttl_seconds=60,
            )
            applied = await store.apply_transition(
                Transition(
                    ref=AttemptRef(id=rid, queue=None, task=f"t{n}"),
                    fields={},
                    state=AttemptState(state),
                ),
                expected_status=AttemptState.RUNNING,
            )
            assert applied is not None
            row = await store.get_attempt(rid)
            assert row["finished_at"] is not None, state
            assert row["released_at"] is not None, state
        # update_attempt path (the resolve-result direct write).
        await store.create_attempt(
            "ru", task="tu", queue=None, worker_id="w1",
            backend="b", model="m", base_ref=None, ttl_seconds=60,
        )
        row = await store.get_attempt("ru")
        assert row["finished_at"] is None  # live → NULL
        await store.update_attempt("ru", state=AttemptState.LANDING)
        assert (await store.get_attempt("ru"))["finished_at"] is None
        await store.update_attempt("ru", state=AttemptState.LANDED)
        assert (await store.get_attempt("ru"))["finished_at"] is not None

    _run(scenario())


def test_operator_stop_transition_aborts_a_live_attempt() -> None:
    """Phase 8 behavior fix: stop/skip applies on_operator_stop (ABORTED with
    finished_at) instead of cancelling a lease around a zombie row."""
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        assert await store.apply_transition(
            on_operator_stop(ref), expected_status=AttemptState.RUNNING,
        ) is not None
        row = await store.get_attempt("r1")
        assert row["state"] == "aborted"
        assert row["finished_at"] is not None

    _run(scenario())


# --------------------------------------------------------------------------- #
# retry counters + backoff (Phase 5)
# --------------------------------------------------------------------------- #


def _counted_error(ref: AttemptRef, *, backoff: float | None = 60.0) -> Transition:
    """A plain worker error: counts toward the task, backs the retry off."""
    return Transition(
        ref=ref,
        fields={"result_line": "boom"},
        state=AttemptState.FAILED,
        effects=TaskEffects(progress=Progress.INCREMENT, next_eligible_in=backoff),
    )


def test_increment_creates_a_pure_counter_row_invisible_to_views() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        await store.apply_transition(
            _counted_error(ref),
            expected_status=AttemptState.RUNNING, expected_worker_id="w1",
        )
        row = await store.get_task_state(None, "t1")
        assert row["attempts_without_progress"] == 1
        assert row["next_eligible_at"] is not None
        assert row["state"] is None
        # A counter-only row is not a hold: every state view ignores it.
        assert await store.list_blocked() == []
        assert await store.tasks_in_state("blocked") == []
        assert await store.retryable_tasks(None) == []
        assert [r["task"] for r in await store.tasks_backing_off()] == ["t1"]

    _run(scenario())


def test_increment_without_backoff_clears_a_stale_one() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        await store.apply_transition(
            _counted_error(ref),
            expected_status=AttemptState.RUNNING, expected_worker_id="w1",
        )
        # Revive and fail again, this time without backoff (e.g. a
        # quarantining outcome): the counter grows, the stale backoff goes.
        await store.update_attempt("r1", state=AttemptState.RUNNING)
        await store.apply_transition(
            _counted_error(ref, backoff=None),
            expected_status=AttemptState.RUNNING, expected_worker_id="w1",
        )
        row = await store.get_task_state(None, "t1")
        assert row["attempts_without_progress"] == 2
        assert row["next_eligible_at"] is None

    _run(scenario())


def test_hold_write_preserves_the_counter_and_backoff() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        await store.apply_transition(
            _counted_error(ref),
            expected_status=AttemptState.RUNNING, expected_worker_id="w1",
        )
        # A later hold (transactional) and an operator upsert both leave the
        # retry fields alone.
        await store.update_attempt("r1", state=AttemptState.RUNNING)
        await store.apply_transition(
            _error_transition(ref),
            expected_status=AttemptState.RUNNING, expected_worker_id="w1",
        )
        row = await store.get_task_state(None, "t1")
        assert row["state"] == "blocked"
        assert row["attempts_without_progress"] == 1
        assert row["next_eligible_at"] is not None
        await store.set_task_state(None, "t1", "repo_unavailable", repo="r")
        row = await store.get_task_state(None, "t1")
        assert row["attempts_without_progress"] == 1
        assert row["next_eligible_at"] is not None

    _run(scenario())


def test_landed_reset_deletes_the_counter_row() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        await store.apply_transition(
            _counted_error(ref),
            expected_status=AttemptState.RUNNING, expected_worker_id="w1",
        )
        await store.update_attempt("r1", state=AttemptState.RUNNING)
        landed = Transition(
            ref=ref,
            fields={"commit_sha": "abc"},
            state=AttemptState.LANDED,
            effects=TaskEffects(clear_hold=True, progress=Progress.RESET),
        )
        await store.apply_transition(
            landed, expected_status=AttemptState.RUNNING, expected_worker_id="w1"
        )
        assert await store.get_task_state(None, "t1") is None

    _run(scenario())


def test_clear_hold_demotes_but_keeps_a_nonzero_counter() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        await store.apply_transition(
            _counted_error(ref),
            expected_status=AttemptState.RUNNING, expected_worker_id="w1",
        )
        await store.set_task_state(None, "t1", "blocked", blocked_reason="x")
        # A hold-clear without progress (e.g. a split parent's overlay clear)
        # must not erase retry state.
        await store.update_attempt("r1", state=AttemptState.RUNNING)
        cleared = Transition(
            ref=ref,
            fields={},
            state=AttemptState.NO_CHANGE,
            effects=TaskEffects(clear_hold=True),
        )
        await store.apply_transition(
            cleared, expected_status=AttemptState.RUNNING, expected_worker_id="w1"
        )
        row = await store.get_task_state(None, "t1")
        assert row["state"] is None
        assert row["blocked_reason"] is None
        assert row["attempts_without_progress"] == 1
        assert await store.list_blocked() == []

    _run(scenario())


def test_clear_task_state_demotes_or_resets() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        await store.apply_transition(
            _counted_error(ref),
            expected_status=AttemptState.RUNNING, expected_worker_id="w1",
        )
        await store.set_task_state(None, "t1", "blocked", blocked_reason="x")
        # Default clear: the hold and backoff go (explicit release =
        # dispatchable now), the counter survives.
        await store.clear_task_state(None, "t1")
        row = await store.get_task_state(None, "t1")
        assert row["state"] is None
        assert row["next_eligible_at"] is None
        assert row["attempts_without_progress"] == 1
        # reset_progress (a landed resolve): the row is gone entirely.
        await store.clear_task_state(None, "t1", reset_progress=True)
        assert await store.get_task_state(None, "t1") is None
        # Clearing a clean hold still deletes the row (pre-phase behavior).
        await store.set_task_state(None, "t2", "blocked", blocked_reason="y")
        await store.clear_task_state(None, "t2")
        assert await store.get_task_state(None, "t2") is None

    _run(scenario())


def test_clear_task_backoff_only_touches_the_backoff() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        ref = await _live_attempt(store)
        await store.apply_transition(
            _counted_error(ref),
            expected_status=AttemptState.RUNNING, expected_worker_id="w1",
        )
        await store.clear_task_backoff(None, "t1")
        row = await store.get_task_state(None, "t1")
        assert row["next_eligible_at"] is None
        assert row["attempts_without_progress"] == 1
        assert await store.tasks_backing_off() == []

    _run(scenario())


# --------------------------------------------------------------------------- #
# migration shape
# --------------------------------------------------------------------------- #


def test_migration_defines_schema_tables_and_views() -> None:
    sql = MIGRATION.read_text()
    assert "CREATE SCHEMA IF NOT EXISTS nightshift;" in sql
    for table in ("workers", "leases", "tasks", "runs", "events"):
        assert f"CREATE TABLE nightshift.{table}" in sql
    for view in (
        "stats_overall", "stats_by_worker", "stats_by_backend",
        "stats_by_model", "stats_by_queue",
    ):
        assert f"CREATE VIEW nightshift.{view}" in sql
    # Per-run agent telemetry columns the rollups aggregate.
    for col in ("turns", "input_tokens", "output_tokens", "cost_usd"):
        assert col in sql
    assert "total_cost_usd" in sql
    # Active-lease exclusivity is enforced at the DB layer.
    assert "leases_active_task_uniq" in sql
    # migrate:down drops the schema cleanly.
    assert "DROP SCHEMA IF EXISTS nightshift;" in sql


def test_migration_has_up_and_down_sections() -> None:
    sql = MIGRATION.read_text()
    assert "-- migrate:up" in sql
    assert "-- migrate:down" in sql
    assert sql.index("-- migrate:up") < sql.index("-- migrate:down")


def test_retry_counters_migration_shape() -> None:
    sql = RETRY_COUNTERS_MIGRATION.read_text()
    assert "-- migrate:up" in sql and "-- migrate:down" in sql
    assert sql.index("-- migrate:up") < sql.index("-- migrate:down")
    # The two Phase 5 columns, idempotently added and reversibly dropped.
    assert "ADD COLUMN IF NOT EXISTS attempts_without_progress" in sql
    assert "ADD COLUMN IF NOT EXISTS next_eligible_at" in sql
    assert "DROP COLUMN IF EXISTS attempts_without_progress" in sql
    assert "DROP COLUMN IF EXISTS next_eligible_at" in sql
    # Pure counter rows have no hold: state becomes nullable (and the down
    # section removes those rows before restoring NOT NULL).
    assert "ALTER COLUMN state DROP NOT NULL" in sql
    assert "ALTER COLUMN state SET NOT NULL" in sql
    assert "DELETE FROM nightshift.tasks WHERE state IS NULL;" in sql
    # The one-shot backfill reconstructs the streak scan's semantics and
    # never overwrites a live nonzero counter (idempotent re-runs).
    assert "commit_sha IS NULL" in sql
    assert "attempts_without_progress = 0" in sql


def test_attempts_migration_shape() -> None:
    """Phase 8: the lease+run merge. The fold/split CASE semantics are pinned
    by the fold_legacy/split_state round-trip tests in test_lifecycle.py; this
    pins the file's structure (tables, indexes, views, both directions)."""
    sql = ATTEMPTS_MIGRATION.read_text()
    assert "-- migrate:up" in sql and "-- migrate:down" in sql
    up = sql[sql.index("-- migrate:up"):sql.index("-- migrate:down")]
    down = sql[sql.index("-- migrate:down"):]
    # up: the attempts table with the single state column and the live-set
    # partial unique index (invariant 1 at the DB layer).
    assert "CREATE TABLE IF NOT EXISTS nightshift.attempts" in up
    assert "state          text NOT NULL" in up
    assert "attempts_live_task_uniq" in up
    assert "WHERE state IN ('landing', 'running')" in up
    # up: the fold copies runs (with their latest lease) and lease-only rows,
    # then drops the source tables.
    assert "LEFT JOIN latest_lease" in up
    assert "DROP TABLE nightshift.runs;" in up
    assert "DROP TABLE nightshift.leases;" in up
    # The five stats views are recreated over attempts in up and over runs in
    # down, with the state-vocabulary buckets.
    for view in (
        "stats_overall", "stats_by_worker", "stats_by_backend",
        "stats_by_model", "stats_by_queue",
    ):
        assert f"CREATE VIEW nightshift.{view}" in up
        assert f"CREATE VIEW nightshift.{view}" in down
    assert "state IN ('landed', 'no_change')" in up
    assert "state IN ('failed', 'conflict')" in up
    # down: leases + runs come back via the split CASE; attempts goes away.
    assert "CREATE TABLE IF NOT EXISTS nightshift.leases" in down
    assert "CREATE TABLE IF NOT EXISTS nightshift.runs" in down
    assert "DROP TABLE nightshift.attempts;" in down
    # Idempotence guards on both data moves.
    assert "to_regclass('nightshift.runs')" in up
    assert "to_regclass('nightshift.attempts')" in down
    # The internal-only landing re-enqueue columns exist (never projected).
    assert "branch_ref" in up and "head_sha" in up


def test_capability_migration_adds_columns_and_queue_routing() -> None:
    sql = CAPABILITY_MIGRATION.read_text()
    # Advertised capabilities on workers + declared connectors on runs.
    assert "ALTER TABLE nightshift.workers" in sql
    assert "models jsonb" in sql
    assert "mcps   jsonb" in sql or "mcps jsonb" in sql
    assert "required_mcps jsonb" in sql
    # Manager-side queue dedication table.
    assert "CREATE TABLE nightshift.queue_routing" in sql
    # Reversible.
    assert "-- migrate:up" in sql and "-- migrate:down" in sql
    assert "DROP TABLE IF EXISTS nightshift.queue_routing;" in sql
