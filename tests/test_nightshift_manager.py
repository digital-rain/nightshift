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

from _workspace import build_workspace, make_target_repo
from nightshift.engine import setup_worktree
from nightshift.manager.app import _jsonable, create_app
from nightshift.manager.hub import Hub
from nightshift.manager.landing import canonical_head
from nightshift.manager.store import MemoryStore


def _seed(tmp_path: Path, tasks: dict[str, str], **kwargs) -> Path:
    """Build a two-root workspace seeded with ``tasks`` in the default queue.

    Returns the workspace path; the content store lives at
    ``<workspace>/nightshift-tasks`` and (by default) the target repo
    ``longitude`` is bound to the ``main`` queue, so a dispatched task resolves
    a present repo and lands cleanly. ``kwargs`` pass through to
    :func:`build_workspace` (e.g. ``main_repo`` / ``repos`` for the
    pause/blocked lifecycles).
    """
    return build_workspace(tmp_path, tasks=tasks, **kwargs)


def _client(workspace: Path, store: MemoryStore | None = None) -> TestClient:
    return TestClient(create_app(workspace, store=store or MemoryStore()))


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
# Multi-repo workspace lifecycle (two-root model)
# --------------------------------------------------------------------------- #


def test_queue_with_absent_repo_pauses_then_resumes_after_rescan(tmp_path: Path) -> None:
    # The main queue is bound to a repo that isn't on disk yet.
    workspace = _seed(tmp_path, {"10.work": "Do it."}, main_repo="ghost", repos=())
    store = MemoryStore()
    with _client(workspace, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # Polling pauses the task (repo_unavailable) and never dispatches it...
        first = client.post("/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"})
        assert first.json()["work"] is None
        # ...and a second poll does NOT re-warn (one warning per queue, deduped).
        second = client.post("/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"})
        assert second.json()["work"] is None

        state = asyncio.run(store.get_task_state(None, "10.work"))
        assert state is not None and state["state"] == "repo_unavailable"
        assert state["repo"] == "ghost"

        events = asyncio.run(store.events_since(0))
        warnings = [e for e in events if e["kind"] == "repo_unavailable"]
        assert len(warnings) == 1  # deduped to exactly one per queue
        assert warnings[0]["queue"] is None  # the main queue
        assert warnings[0]["payload"]["repo"] == "ghost"

        # A paused task records no run.
        assert client.get("/api/runs").json() == []

        # The repo appears on disk; a rescan auto-resumes (clears) the paused task.
        make_target_repo(workspace, "ghost")
        rescan = client.post("/api/repos/rescan")
        assert rescan.status_code == 200
        assert "ghost" in [r["name"] for r in rescan.json()["repos"]]
        assert asyncio.run(store.get_task_state(None, "10.work")) is None

        events = asyncio.run(store.events_since(0))
        resumed = [e for e in events if e["kind"] == "repos_changed"]
        assert resumed
        assert {"queue": "main", "task": "10.work"} in resumed[-1]["payload"]["resumed"]

        # A poll now dispatches it with the repo in the work order + run.repo set.
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert order is not None
        assert order["repo"] == "ghost"
        assert order["base_ref"]  # the now-present repo's canonical HEAD
        runs = client.get("/api/runs").json()
        assert len(runs) == 1
        assert runs[0]["repo"] == "ghost"


def test_malformed_task_repo_ref_blocks_not_dispatched(tmp_path: Path) -> None:
    # The queue default repo is valid + present, but the task pins an unsafe
    # (path-traversal) override that must be rejected as an authoring error.
    workspace = _seed(tmp_path, {"10.work": "---\nrepo: ../evil\n---\nDo it."})
    store = MemoryStore()
    with _client(workspace, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        assert client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"] is None

        blocked = client.get("/api/blocked").json()
        match = [b for b in blocked if b["task"] == "10.work"]
        assert match
        assert "invalid repo reference" in match[0]["blocked_reason"]

        # An authoring error is never dispatched and never records a run.
        assert client.get("/api/runs").json() == []


def test_create_and_edit_reject_malformed_repo_without_orphan(tmp_path: Path) -> None:
    # A malformed per-task repo override is an edit-time 400 on both create and
    # edit (matching the legacy server), and a rejected create must never orphan
    # a brief in the content store.
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    main_dir = workspace / "nightshift-tasks" / "main"
    with _client(workspace) as client:
        before = {p.name for p in main_dir.glob("*.md")}
        r = client.post("/api/tasks", json={"title": "Bad", "text": "x", "repo": "../evil"})
        assert r.status_code == 400
        assert {p.name for p in main_dir.glob("*.md")} == before  # no orphaned brief

        # A valid create still works and persists a clean override.
        ok = client.post(
            "/api/tasks", json={"title": "Good", "text": "x", "repo": "longitude"}
        )
        assert ok.status_code == 200
        slug = ok.json()["task"]
        assert "repo: longitude" in (main_dir / f"{slug}.md").read_text()

        # A malformed edit is a 400 and leaves the existing override intact.
        assert client.patch(f"/api/tasks/{slug}", json={"repo": "/abs"}).status_code == 400
        assert "repo: longitude" in (main_dir / f"{slug}.md").read_text()


def test_blocked_submit_records_reason_without_landing(tmp_path: Path) -> None:
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    tasks_root = workspace / "nightshift-tasks"
    repo_root = workspace / "longitude"
    head_before = canonical_head(repo_root)
    with _client(workspace) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        run_id, lease_id = order["run_id"], order["lease_id"]

        # An honest block carries its reason; the manager records it but lands
        # nothing and keeps the brief so a Resolve can pick it up.
        r = client.post(
            f"/api/worker/runs/{run_id}/submit",
            json={
                "worker_id": "w1", "lease_id": lease_id, "task": "10.hello",
                "queue": "main", "title": "hello", "status": "blocked",
                "result_line": "agent paused", "failure_reason": "needs human decision",
                "failure_kind": "blocked",
            },
        )
        assert r.json() == {"landed": False, "status": "blocked"}

        # The run and the task overlay both carry the reason.
        run = next(x for x in client.get("/api/runs").json() if x["id"] == run_id)
        assert run["status"] == "blocked"
        assert run["failure_reason"] == "needs human decision"
        blocked = client.get("/api/blocked").json()
        assert any(
            b["task"] == "10.hello" and "needs human decision" in b["blocked_reason"]
            for b in blocked
        )

        # Nothing landed: the brief stays in the queue and the target repo HEAD
        # is unchanged.
        assert (tasks_root / "main/10.hello.md").exists()
        assert canonical_head(repo_root) == head_before


def test_work_order_shape_carries_repo_task_path_and_base_ref(tmp_path: Path) -> None:
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    repo_root = workspace / "longitude"
    with _client(workspace) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert order["repo"] == "longitude"
        assert order["task_path"] == "nightshift-tasks/main/10.hello.md"
        assert order["base_ref"] == canonical_head(repo_root)


def test_content_store_commits_are_local_and_pushless(tmp_path: Path) -> None:
    # Empty main queue; commit_tasks gives the content store a local git repo.
    workspace = _seed(tmp_path, {})
    tasks_root = workspace / "nightshift-tasks"

    def store_log_subject() -> str:
        return subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=tasks_root, capture_output=True, text=True,
        ).stdout.strip()

    def store_remotes() -> str:
        return subprocess.run(
            ["git", "remote"], cwd=tasks_root, capture_output=True, text=True
        ).stdout.strip()

    # The content store has no remote — every lifecycle commit is purely local.
    assert store_remotes() == ""

    with _client(workspace) as client:
        # create → local commit in the content store.
        created = client.post(
            "/api/tasks", json={"title": "Ship the thing", "text": "Do it."}
        ).json()
        task = created["task"]
        assert store_log_subject() == f"nightshift: create task {task}"

        # edit → local commit.
        client.patch(f"/api/tasks/{task}", json={"repo": "longitude"})
        assert store_log_subject() == f"nightshift: edit task {task}"

        # complete → a real dispatch + land drops the brief as a local commit.
        # The new brief pins claude-sonnet-4-6 (from the task template), so the
        # worker advertises that model to be routed the task.
        models = ["claude-sonnet-4-6"]
        client.post(
            "/api/worker/checkin",
            json={"worker_id": "w1", "backend": "claude-code", "models": models},
        )
        order = client.post(
            "/api/worker/poll",
            json={"worker_id": "w1", "backend": "claude-code", "models": models},
        ).json()["work"]
        assert order is not None and order["repo"] == "longitude"

        # Simulate the worker leaving a committed, landable branch in the target.
        wt = setup_worktree(workspace, order["repo"], task)
        (wt / "GENERATED.txt").write_text("done\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "work"], cwd=wt, check=True, capture_output=True)

        r = client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"], "task": task,
                "queue": "main", "title": order["title"], "status": "completed",
                "landable": True, "backend": "claude-code", "model": "claude-sonnet-4-6",
            },
        )
        assert r.json()["landed"] is True
        assert store_log_subject() == f"nightshift: drop completed task {task}"
        assert not (tasks_root / "main" / f"{task}.md").exists()

    # No remote was ever configured or required across the whole lifecycle.
    assert store_remotes() == ""


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


def test_hub_stream_ends_on_server_shutdown() -> None:
    """A still-connected browser must not pin the SSE stream open when the
    server is shutting down (Ctrl-C). Without this, uvicorn hangs at "Waiting
    for connections to close" because the long-lived /api/events stream never
    ends on its own."""
    import anyio

    async def run() -> tuple[list[str], bool]:
        hub = Hub()

        async def snapshot():
            return {"cursor": 0, "workers": []}

        shutting_down = {"on": False}

        def is_shutting_down() -> bool:
            return shutting_down["on"]

        chunks: list[str] = []
        # Bounded so a regression (loop never exits) fails loudly here instead
        # of hanging the whole test run.
        with anyio.move_on_after(5) as scope:
            async for chunk in hub.stream(
                snapshot, heartbeat_seconds=0.01, is_shutting_down=is_shutting_down
            ):
                chunks.append(chunk)
                shutting_down["on"] = True  # flip after the snapshot frame
        return chunks, scope.cancel_called

    chunks, timed_out = anyio.run(run)
    assert not timed_out, "hub.stream did not exit on server shutdown"
    assert chunks


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
