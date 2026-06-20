"""Manager service tests (Phase 1): worker API flow + operator API + hub.

Uses an injected in-memory store and a real (tiny) git repo so the worker
checkin -> poll -> events -> submit handshake and the operator endpoints are
exercised end to end without Postgres.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from starlette.testclient import TestClient

from nightshift.manager.app import _jsonable, create_app
from nightshift.manager.hub import Hub
from nightshift.manager.store import MemoryStore


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _seed(tmp_path: Path, tasks: dict[str, str]) -> Path:
    (tmp_path / "config.json").write_text(
        json.dumps({"model": "auto", "validate": "true", "default_model": "auto"})
    )
    (tmp_path / ".tasks").mkdir(parents=True, exist_ok=True)
    for name, content in tasks.items():
        (tmp_path / ".tasks" / f"{name}.md").write_text(content)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@test")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def _client(root: Path) -> TestClient:
    return TestClient(create_app(root, store=MemoryStore()))


def test_checkin_poll_handshake(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.hello": "---\npriority: 1\n---\nDo a thing."})
    with _client(root) as client:
        r = client.post(
            "/api/worker/checkin",
            json={"worker_id": "w1", "backend": "claude-code"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert "cadences" in r.json()

        # Worker appears in the registry snapshot.
        workers = client.get("/api/workers").json()
        assert [w["id"] for w in workers] == ["w1"]

        # Poll returns a work order for the only task.
        r = client.post(
            "/api/worker/poll",
            json={"worker_id": "w1", "backend": "claude-code"},
        )
        order = r.json()["work"]
        assert order is not None
        assert order["task"] == "10.hello"
        assert order["queue"] == "main"
        assert order["config"]["model"] == "auto"
        assert order["base_ref"]  # canonical HEAD pinned

        # The task is now leased — a second worker gets nothing.
        client.post("/api/worker/checkin", json={"worker_id": "w2", "backend": "ollama"})
        r2 = client.post(
            "/api/worker/poll",
            json={"worker_id": "w2", "backend": "ollama"},
        )
        assert r2.json()["work"] is None


def test_pinned_model_without_backend_is_blocked(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.ml": "---\nmodel: llama3.1\n---\nNeeds ollama."})
    with _client(root) as client:
        # Only a claude-code worker is online; the ollama-pinned task can't run.
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        r = client.post("/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"})
        assert r.json()["work"] is None
        # And it is surfaced as blocked, not silently pending.
        blocked = client.get("/api/blocked").json()
        assert any(b["task"] == "10.ml" for b in blocked)


def test_run_events_and_failed_submit_release(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.hello": "Do a thing."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        run_id = order["run_id"]
        lease_id = order["lease_id"]

        # Stream a couple of run events.
        r = client.post(
            f"/api/worker/runs/{run_id}/events",
            json={"events": [
                {"type": "task_status", "task": "10.hello", "phase": "worker"},
                {"type": "task_log", "task": "10.hello", "line": "working...\n"},
            ]},
        )
        assert r.json()["ok"] is True
        run_events = client.get(f"/api/runs/{run_id}/events").json()
        kinds = [e["kind"] for e in run_events]
        assert "task_status" in kinds and "task_log" in kinds

        # A non-completed submit releases the lease without touching git.
        r = client.post(
            f"/api/worker/runs/{run_id}/submit",
            json={
                "worker_id": "w1", "lease_id": lease_id, "task": "10.hello",
                "queue": "main", "title": "hello", "status": "error",
                "result_line": "worker bailed", "failure_kind": "worker_error",
            },
        )
        assert r.json()["landed"] is False
        # Lease freed → task pollable again.
        again = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert again is not None and again["task"] == "10.hello"


def test_operator_endpoints(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.hello": "Do a thing."})
    with _client(root) as client:
        assert client.get("/api/queue").status_code == 200
        stats = client.get("/api/stats").json()
        # The rollups the Workers page renders are all present.
        assert {"overall", "by_worker", "by_backend", "by_model", "by_queue"} <= set(stats)
        assert client.get("/api/settings").json()["landing_mode"] == "none"
        assert client.get("/api/workers").json() == []
        assert client.get("/api/leases").json() == []


def test_jsonable_coerces_decimal_cost() -> None:
    # Postgres hands `numeric` (cost_usd, avg_turns) back as Decimal; the stats
    # endpoints must not 500 serializing it under the PgStore.
    from decimal import Decimal

    out = _jsonable({"total_cost_usd": Decimal("0.1234"), "total_turns": 8})
    assert isinstance(out["total_cost_usd"], float)
    assert round(out["total_cost_usd"], 4) == 0.1234
    assert out["total_turns"] == 8
    # Plain dumpability is the real guarantee.
    assert json.dumps(out)


def test_submit_records_turns_tokens_for_rollups(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.hello": "Do a thing."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        run_id, lease_id = order["run_id"], order["lease_id"]

        # Completed-but-nothing-to-land submit carrying agent telemetry (no git).
        r = client.post(
            f"/api/worker/runs/{run_id}/submit",
            json={
                "worker_id": "w1", "lease_id": lease_id, "task": "10.hello",
                "queue": "main", "title": "hello", "status": "completed",
                "landable": False, "backend": "claude-code", "model": "claude-opus-4-8",
                "turns": 8, "input_tokens": 1500, "output_tokens": 400, "cost_usd": 0.09,
            },
        )
        assert r.json()["landed"] is False

        # The run row carries the telemetry...
        run = next(x for x in client.get("/api/runs").json() if x["id"] == run_id)
        assert run["turns"] == 8
        assert run["input_tokens"] == 1500
        assert run["output_tokens"] == 400

        # ...and it rolls up per backend and per model.
        stats = client.get("/api/stats").json()
        by_backend = {r["backend"]: r for r in stats["by_backend"]}
        assert by_backend["claude-code"]["total_turns"] == 8
        assert by_backend["claude-code"]["total_tokens"] == 1900
        by_model = {r["model"]: r for r in stats["by_model"]}
        assert by_model["claude-opus-4-8"]["total_turns"] == 8
        assert round(by_model["claude-opus-4-8"]["total_cost_usd"], 2) == 0.09


# --------------------------------------------------------------------------- #
# Hub
# --------------------------------------------------------------------------- #


def test_hub_snapshot_then_deltas() -> None:
    async def scenario() -> list[dict]:
        hub = Hub()

        async def snapshot():
            return {"cursor": 0, "workers": []}

        frames: list[dict] = []
        gen = hub.stream(snapshot, heartbeat_seconds=0.05)
        # First frame is the snapshot.
        first = await gen.__anext__()
        frames.append(json.loads(first[len("data: "):]))
        # Publish a delta and read it.
        await hub.publish({"id": 1, "kind": "queue_changed"})
        second = await gen.__anext__()
        frames.append(json.loads(second[len("data: "):]))
        await gen.aclose()
        return frames

    frames = asyncio.run(scenario())
    assert frames[0]["type"] == "snapshot"
    assert frames[1]["type"] == "event"
    assert frames[1]["kind"] == "queue_changed"


# --------------------------------------------------------------------------- #
# DSN resolution — Nightshift owns its own store DSN
# --------------------------------------------------------------------------- #


def _write_manager_block(tmp_path: Path, manager: dict) -> Path:
    (tmp_path / "config.json").write_text(
        json.dumps({"default_model": "auto", "manager": manager})
    )
    return tmp_path


def test_dsn_unset_is_none(tmp_path: Path, monkeypatch) -> None:
    from nightshift.manager.config import load_manager_config

    monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
    root = _write_manager_block(tmp_path, {})
    assert load_manager_config(root).dsn is None


def test_dsn_from_manager_block(tmp_path: Path, monkeypatch) -> None:
    from nightshift.manager.config import load_manager_config

    monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
    root = _write_manager_block(tmp_path, {"dsn": "postgresql://db/nightshift"})
    assert load_manager_config(root).dsn == "postgresql://db/nightshift"


def test_dsn_env_overrides_block(tmp_path: Path, monkeypatch) -> None:
    from nightshift.manager.config import load_manager_config

    monkeypatch.setenv("NIGHTSHIFT_PG_DSN", "postgresql://env-host/nightshift")
    root = _write_manager_block(tmp_path, {"dsn": "postgresql://db/nightshift"})
    assert load_manager_config(root).dsn == "postgresql://env-host/nightshift"


def test_dsn_does_not_recycle_long_pg_dsn(tmp_path: Path, monkeypatch) -> None:
    # The whole point: longitude's DSN must never silently become Nightshift's.
    from nightshift.manager.config import load_manager_config

    monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
    monkeypatch.setenv("LONG_PG_DSN", "postgresql://longitude/longitude")
    monkeypatch.setenv("DATABASE_URL", "postgresql://longitude/other")
    root = _write_manager_block(tmp_path, {})
    assert load_manager_config(root).dsn is None


def test_open_store_no_dsn_is_memory(monkeypatch) -> None:
    # open_store with no arg + no NIGHTSHIFT_PG_DSN must fall back to MemoryStore
    # and must not consult LONG_PG_DSN.
    from nightshift.manager.store import MemoryStore, open_store

    monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
    monkeypatch.setenv("LONG_PG_DSN", "postgresql://longitude/longitude")
    store = asyncio.run(open_store())
    assert isinstance(store, MemoryStore)
