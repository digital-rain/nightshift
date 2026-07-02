"""Tests for the nightshift manager store + schema (Phase 0).

The MemoryStore exercises the same async interface as the PgStore, so these
verify the CRUD/lease/stats semantics without a live database. The migration
test pins the schema shape the PgStore relies on.
"""

from __future__ import annotations

import asyncio

import pytest

from nightshift._paths import MIGRATIONS_DIR
from nightshift.manager.store import MemoryStore


MIGRATION = MIGRATIONS_DIR / "20260730000001_nightshift_schema.sql"
CAPABILITY_MIGRATION = (
    MIGRATIONS_DIR / "20260730000002_nightshift_capability_routing.sql"
)


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


def test_create_run_records_required_mcps() -> None:
    store = MemoryStore()
    _run(store.create_run(
        "r1", task="t1", queue=None, worker_id="w1",
        backend="cursor", model="auto", required_mcps=["slack", "github"],
    ))
    run = _run(store.get_run("r1"))
    assert run["required_mcps"] == ["slack", "github"]


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
# leases
# --------------------------------------------------------------------------- #


def test_lease_is_exclusive_per_task() -> None:
    store = MemoryStore()
    _run(store.register_worker("w1", backend="claude-code", queues=None, priorities=None))
    first = _run(
        store.acquire_lease(
            task="t1", queue=None, worker_id="w1", model="auto",
            base_ref="abc", ttl_seconds=60,
        )
    )
    assert first is not None
    # A second active lease on the same (queue, task) is refused.
    second = _run(
        store.acquire_lease(
            task="t1", queue=None, worker_id="w1", model="auto",
            base_ref="abc", ttl_seconds=60,
        )
    )
    assert second is None
    # Releasing frees it for a fresh lease.
    _run(store.set_lease_status(first["id"], "released"))
    third = _run(
        store.acquire_lease(
            task="t1", queue=None, worker_id="w1", model="auto",
            base_ref="abc", ttl_seconds=60,
        )
    )
    assert third is not None


def test_expired_leases_are_reclaimed() -> None:
    store = MemoryStore()
    _run(store.register_worker("w1", backend="claude-code", queues=None, priorities=None))
    lease = _run(
        store.acquire_lease(
            task="t1", queue=None, worker_id="w1", model="auto",
            base_ref="abc", ttl_seconds=-1,  # already expired
        )
    )
    assert lease is not None
    reclaimed = _run(store.reclaim_expired_leases())
    assert [r["task"] for r in reclaimed] == ["t1"]
    # Reclaimed → task is leasable again.
    again = _run(
        store.acquire_lease(
            task="t1", queue=None, worker_id="w1", model="auto",
            base_ref="abc", ttl_seconds=60,
        )
    )
    assert again is not None


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
# runs + stats
# --------------------------------------------------------------------------- #


def test_runs_and_stats_by_backend() -> None:
    store = MemoryStore()
    _run(store.create_run(
        "r1", task="t1", queue=None, worker_id="w1",
        backend="claude-code", model="claude-opus-4-8",
    ))
    _run(store.update_run("r1", status="completed", loc=42, commit_sha="deadbeef"))
    _run(store.create_run(
        "r2", task="t2", queue=None, worker_id="w2",
        backend="ollama", model="llama3.1",
    ))
    _run(store.update_run("r2", status="error", failure_kind="validation_error"))

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
    # Two runs on the same model/backend (one completed, one errored) + a third
    # in a playlist queue on a different backend.
    _run(store.create_run(
        "r1", task="t1", queue=None, worker_id="w1",
        backend="claude-code", model="claude-opus-4-8",
    ))
    _run(store.update_run(
        "r1", status="completed", loc=10,
        turns=7, input_tokens=1000, output_tokens=200, cost_usd=0.05,
    ))
    _run(store.create_run(
        "r2", task="t2", queue=None, worker_id="w1",
        backend="claude-code", model="claude-opus-4-8",
    ))
    # A failed run still burned turns + tokens — it must count in the rollups.
    _run(store.update_run(
        "r2", status="error", failure_kind="validation_error",
        turns=3, input_tokens=500, output_tokens=100, cost_usd=0.02,
    ))
    _run(store.create_run(
        "r3", task="t3", queue="alpha", worker_id="w2",
        backend="ollama", model="llama3.1",
    ))
    _run(store.update_run(
        "r3", status="completed", loc=5,
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


def test_blocked_run_gets_finished_at() -> None:
    """`blocked` is a terminal run status: the run is over (the *task* is what
    stays held), so it must get finished_at like every other terminal status.
    Pre-Phase-1 this drifted: blocked runs kept finished_at = NULL forever."""
    store = MemoryStore()
    _run(store.create_run(
        "r1", task="t1", queue=None, worker_id="w1",
        backend="claude-code", model="auto",
    ))
    _run(store.update_run("r1", status="blocked", result_line="blocked: needs creds"))
    run = _run(store.get_run("r1"))
    assert run["status"] == "blocked"
    assert run["finished_at"] is not None


def test_update_run_raises_on_unknown_fields() -> None:
    """The updatable-field set derives from the Outcome/Telemetry models;
    unknown fields raise instead of being silently dropped."""
    store = MemoryStore()
    _run(store.create_run(
        "r1", task="t1", queue=None, worker_id="w1",
        backend="claude-code", model="auto",
    ))
    with pytest.raises(ValueError, match="nonsense_field"):
        _run(store.update_run("r1", nonsense_field=1))


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
