"""Manager service tests (Phase 1): worker API flow + operator API + hub.

Uses an injected in-memory store and a real (tiny) git repo so the worker
checkin -> poll -> events -> submit handshake and the operator endpoints are
exercised end to end without Postgres.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from _workspace import build_workspace, make_target_repo
from nightshift.enhance import EnhanceError, EnhanceResult
from nightshift.git.worktrees import setup_worktree
from nightshift.lifecycle import AttemptState
from nightshift.manager import api_operator
from nightshift.manager.app import create_app
from nightshift.manager.hub import Hub
from nightshift.manager.landing import canonical_head
from nightshift.manager.store_sqlite import SqliteStore
from nightshift.manager.wire import jsonable
from nightshift.task_files import (
    ORIGINAL_BRIEF_MARKER,
    artifacts_dir,
    read_artifacts,
    read_task,
)


def _find_field(
    resp: dict[str, Any], surface: str, key: str,
) -> dict[str, Any] | None:
    for tier in resp.get("tiers", []):
        if tier["surface"] != surface:
            continue
        for cat in tier["categories"]:
            for field in cat["fields"]:
                if field["key"] == key:
                    return field
    return None


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


def _client(workspace: Path, store: SqliteStore | None = None) -> TestClient:
    return TestClient(create_app(workspace, store=store or SqliteStore()))


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
        assert order["config"]["validate_cmd"] == "true"
        assert order["base_ref"]  # canonical HEAD pinned

        run = next(x for x in client.get("/api/runs").json() if x["id"] == order["run_id"])
        assert run["validate_cmd"] == "true"

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
    store = SqliteStore()
    with _client(root, store) as client:
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

        # A non-completed submit releases the lease and marks the task "failed"
        # so it is NOT immediately re-dispatched (phase-A drain policy).
        r = client.post(
            f"/api/worker/runs/{run_id}/submit",
            json={
                "worker_id": "w1", "lease_id": lease_id, "task": "10.hello",
                "queue": "main", "title": "hello", "status": "error",
                "result_line": "worker bailed", "failure_kind": "worker_error",
            },
        )
        assert r.json()["landed"] is False
        # The task is now in "failed" state -- it won't dispatch again
        # immediately. But since it's the only task and phase B admits it,
        # the next poll *does* get it (phase B: no ready tasks left, earliest
        # failed task admitted) once its retry backoff elapses (Phase 5:
        # a failed task backs off instead of re-leasing instantly).
        _clear_backoff(store, "10.hello")
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
        resp = client.get("/api/settings").json()
        landing_field = _find_field(resp, "manager", "landing_mode")
        assert landing_field is not None
        assert landing_field["effective"] == "none"
        assert client.get("/api/workers").json() == []
        assert client.get("/api/leases").json() == []
        # /api/info carries the config-driven UI refresh cadence (AGENTS.md:
        # cadences are never hardcoded client-side).
        info = client.get("/api/info").json()
        assert info["brand_name"] == "Nightshift Manager"
        assert info["refresh_ms"] == 20000


def test_settings_wip_ref_prefix_roundtrip(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.hello": "Do a thing."})
    with _client(root) as client:
        data = client.get("/api/settings").json()
        wip_field = _find_field(data, "manager", "wip_ref_prefix")
        assert wip_field is not None
        assert wip_field["stored"] == "nightshift-wip"

        # A valid value is normalized + persisted to manager.json.
        ok = client.put(
            "/api/settings",
            json={"manager": {"wip_ref_prefix": "  acme/wip/ "}},
        )
        assert ok.status_code == 200
        wip_after = _find_field(ok.json(), "manager", "wip_ref_prefix")
        assert wip_after["stored"] == "acme/wip"

        # An unsafe value is rejected and persists nothing.
        assert client.put(
            "/api/settings",
            json={"manager": {"wip_ref_prefix": "bad prefix"}},
        ).status_code == 400

    # Saved to manager.json, so a fresh manager (restart) reads the new value.
    with _client(root) as restarted:
        resp = restarted.get("/api/settings").json()
        wip_restarted = _find_field(resp, "manager", "wip_ref_prefix")
        assert (
            wip_restarted["stored"] == "acme/wip"
        )


def test_jsonable_coerces_decimal_cost() -> None:
    # Postgres hands `numeric` (cost_usd, avg_turns) back as Decimal; the stats
    # endpoints must not 500 serializing it under the PgStore.
    from decimal import Decimal

    out = jsonable({"total_cost_usd": Decimal("0.1234"), "total_turns": 8})
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
                "validate_cmd": "just validate",
            },
        )
        assert r.json()["landed"] is False

        # The run row carries the telemetry...
        run = next(x for x in client.get("/api/runs").json() if x["id"] == run_id)
        assert run["turns"] == 8
        assert run["input_tokens"] == 1500
        assert run["validate_cmd"] == "just validate"
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
# Re-execution loop guard (quarantine / dead-letter)
# --------------------------------------------------------------------------- #


def _clear_backoff(store: SqliteStore, task: str, queue: str | None = None) -> None:
    """Release a task's retry backoff so a test can re-dispatch it without
    waiting out next_eligible_at (Phase 5: failed tasks back off)."""
    asyncio.run(store.clear_task_backoff(queue, task))


def _poll_and_submit_no_change(client: TestClient, task: str) -> dict[str, Any]:
    """Drive one poll → completed-but-no-commit submit cycle for ``task``."""
    order = client.post(
        "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
    ).json()["work"]
    assert order is not None and order["task"] == task
    # The worker streams some output before deciding there's nothing to do.
    client.post(
        f"/api/worker/runs/{order['run_id']}/events",
        json={"events": [{"type": "task_log", "task": task, "line": "already done?\n"}]},
    )
    resp = client.post(
        f"/api/worker/runs/{order['run_id']}/submit",
        json={
            "worker_id": "w1", "lease_id": order["lease_id"], "task": task,
            "queue": "main", "title": task, "status": "completed",
            "landable": False, "backend": "claude-code", "model": "claude-sonnet-4-6",
        },
    ).json()
    return {**resp, "run_id": order["run_id"]}


def test_repeated_no_change_quarantines_and_halts_dispatch(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.loop": "Move a setting that's already moved."})
    store = SqliteStore()
    with _client(root, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # First no-change run: under threshold (default 2), still runnable.
        first = _poll_and_submit_no_change(client, "10.loop")
        assert first["quarantined"] is False

        # Second no-change run in a row hits the threshold → quarantined.
        _clear_backoff(store, "10.loop")
        second = _poll_and_submit_no_change(client, "10.loop")
        assert second["quarantined"] is True

        # Budget-critical: the worker is now handed nothing for this task.
        assert client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"] is None

        # The operator can see it, with the quarantine reason.
        blocked = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert "10.loop" in blocked
        assert "quarantined" in blocked["10.loop"]["blocked_reason"]


def test_patch_quarantined_false_releases_task_for_dispatch(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.loop": "Move a setting that's already moved."})
    store = SqliteStore()
    with _client(root, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # Drive two no-change runs to trigger quarantine (threshold=2).
        # The first no-change also sets failed=true in frontmatter.
        _poll_and_submit_no_change(client, "10.loop")
        _clear_backoff(store, "10.loop")
        second = _poll_and_submit_no_change(client, "10.loop")
        assert second["quarantined"] is True

        # Task is blocked — poll returns nothing.
        assert client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"] is None

        # Operator sets the task back to "ready" via the detail pane,
        # clearing both quarantined and failed (both are frontmatter flags).
        r = client.patch(
            "/api/tasks/10.loop",
            json={"quarantined": False, "failed": False, "disabled": False, "completed": False},
        )
        assert r.status_code == 200
        assert r.json()["quarantined"] is False
        assert r.json()["failed"] is False

        # Frontmatter-backed states cleared — task is dispatchable again.
        blocked = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert "10.loop" not in blocked
        work = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert work is not None
        assert work["task"] == "10.loop"


def test_quarantine_threshold_zero_disables_the_guard(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.loop": "Idempotent task."})
    os.environ["NIGHTSHIFT_QUARANTINE_THRESHOLD"] = "0"
    try:
        store = SqliteStore()
        with _client(root, store) as client:
            client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
            # First no-change submit: quarantine guard disabled (threshold=0),
            # but failure policy marks task as failed.
            out = _poll_and_submit_no_change(client, "10.loop")
            assert out["quarantined"] is False
            # Phase B retries the task (only failed task in queue).
            _clear_backoff(store, "10.loop")
            out = _poll_and_submit_no_change(client, "10.loop")
            assert out["quarantined"] is False
            # Retry failure quarantines the task and pauses the queue.
            state = client.get("/api/state").json()
            assert state["queues"]["main"]["pause_reason"] == "retry_failed"
    finally:
        del os.environ["NIGHTSHIFT_QUARANTINE_THRESHOLD"]


def test_interleaved_runs_no_longer_mask_a_no_progress_streak(tmp_path: Path) -> None:
    """Window-eviction regression (Phase 5 deliberate fix): pre-phase the
    streak was scanned over the queue's 50 most recent runs, so a busy
    neighbor task could evict an older failure from the window and a looping
    task never reached the threshold. The persisted per-task counter cannot
    be masked by other tasks' runs — this quarantines; the scanner did not."""
    root = _seed(tmp_path, {
        "10.loop": "Move a setting that's already moved.",
        "11.noise": "Busy neighbor.",
    })
    store = SqliteStore()
    with _client(root, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # First failure: counter = 1 (threshold is 2).
        first = _poll_and_submit_no_change(client, "10.loop")
        assert first["quarantined"] is False

        # 55 interleaved runs of the neighbor — enough to evict 10.loop's
        # failure from the old scan window. Aborts are neutral to every
        # policy, so the neighbor stays pending throughout.
        for _ in range(55):
            order = client.post(
                "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
            ).json()["work"]
            assert order is not None and order["task"] == "11.noise"
            client.post(
                f"/api/worker/runs/{order['run_id']}/submit",
                json={
                    "worker_id": "w1", "lease_id": order["lease_id"],
                    "task": "11.noise", "queue": "main", "title": "noise",
                    "status": "aborted", "landable": False,
                },
            )

        # The operator re-readies the looping task (clears failed + backoff;
        # the counter survives, exactly like the run history used to).
        r = client.patch("/api/tasks/10.loop", json={"failed": False})
        assert r.status_code == 200

        # Its second consecutive no-progress run reaches the threshold.
        second = _poll_and_submit_no_change(client, "10.loop")
        assert second["quarantined"] is True
        state = asyncio.run(store.get_task_state(None, "10.loop"))
        assert state["attempts_without_progress"] == 2
        blocked = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert "2 consecutive runs with no progress" in (
            blocked["10.loop"]["blocked_reason"]
        )


def test_backoff_excludes_failed_task_until_eligible(tmp_path: Path) -> None:
    """Phase 5 deliberate fix: an errored task backs off (next_eligible_at)
    instead of re-leasing immediately, and dispatch honors the backoff."""
    root = _seed(tmp_path, {"10.flaky": "Fails once."})
    store = SqliteStore()
    with _client(root, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"],
                "task": "10.flaky", "queue": "main", "title": "flaky",
                "status": "error", "result_line": "worker bailed",
                "failure_kind": "worker_error",
            },
        )
        state = asyncio.run(store.get_task_state(None, "10.flaky"))
        assert state["attempts_without_progress"] == 1
        assert state["next_eligible_at"] is not None

        # An immediate poll offers nothing: phase B would admit the failed
        # task, but its backoff hasn't elapsed.
        assert client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"] is None

        # Simulate the backoff elapsing (inject a past timestamp).
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(
            timespec="microseconds"
        )
        with store._db_lock:
            store._db.execute(
                "UPDATE nightshift.tasks SET next_eligible_at = ?1 "
                "WHERE queue = '' AND task = '10.flaky'",
                (past,),
            )
        again = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert again is not None and again["task"] == "10.flaky"


def test_environment_failure_cools_worker_not_task(tmp_path: Path) -> None:
    """Phase 5 deliberate fix: an environment failure never counts against
    the task — the submitting worker gets a scoped cooldown while another
    worker picks the task right up."""
    root = _seed(tmp_path, {"10.envy": "Needs a healthy box."})
    store = SqliteStore()
    with _client(root, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        client.post("/api/worker/checkin", json={"worker_id": "w2", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert order is not None and order["task"] == "10.envy"
        r = client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"],
                "task": "10.envy", "queue": "main", "title": "envy",
                "status": "error", "result_line": "model quota exhausted",
                "failure_kind": "model_unavailable",
            },
        )
        assert r.json() == {"landed": False, "status": "error", "quarantined": False}

        # No task blame: no counter row, no failed flag, no backoff.
        assert asyncio.run(store.get_task_state(None, "10.envy")) is None
        blocked = client.get("/api/blocked").json()
        assert all(b["task"] != "10.envy" for b in blocked)

        # The submitting worker is cooled down for this queue...
        assert client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"] is None
        # ...but a healthy worker takes the task immediately.
        other = client.post(
            "/api/worker/poll", json={"worker_id": "w2", "backend": "claude-code"}
        ).json()["work"]
        assert other is not None and other["task"] == "10.envy"


def test_land_resets_the_retry_counter(tmp_path: Path) -> None:
    """Per greenfield: a land resets attempts_without_progress (here: the
    counter row is deleted outright — reset is free)."""
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    store = SqliteStore()
    with _client(workspace, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        # One failure first, so there is a nonzero counter to reset.
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"],
                "task": "10.hello", "queue": "main", "title": "hello",
                "status": "error", "result_line": "worker bailed",
                "failure_kind": "worker_error",
            },
        )
        state = asyncio.run(store.get_task_state(None, "10.hello"))
        assert state["attempts_without_progress"] == 1

        # Release it for retry, then land for real. Phase 7: the land is async
        # (the submit returns queued); drain before asserting its effects.
        client.patch("/api/tasks/10.hello", json={"failed": False})
        order = _poll_with_landable_branch(client, workspace, "10.hello")
        r = _submit_completed(client, order)
        assert r.json()["queued"] is True
        _drain_git(client)
        run = next(
            x for x in client.get("/api/runs").json() if x["id"] == order["run_id"]
        )
        assert run["status"] == "completed" and run["commit_sha"]
        assert asyncio.run(store.get_task_state(None, "10.hello")) is None


def test_run_log_reconstructed_from_task_log_events(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.hello": "Do a thing."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        run_id = order["run_id"]
        client.post(
            f"/api/worker/runs/{run_id}/events",
            json={"events": [
                {"type": "task_log", "task": "10.hello", "line": "line one\n"},
                {"type": "task_status", "task": "10.hello", "phase": "worker"},
                {"type": "task_log", "task": "10.hello", "line": "line two\n"},
            ]},
        )
        # The manager rebuilds the log from persisted task_log events so a
        # finished run's output is viewable after the fact.
        data = client.get(f"/api/runs/{run_id}/10.hello/log").json()
        assert data["text"] == "line one\nline two\n"


# --------------------------------------------------------------------------- #
# Multi-repo workspace lifecycle (two-root model)
# --------------------------------------------------------------------------- #


def test_queue_with_absent_repo_pauses_then_resumes_after_rescan(tmp_path: Path) -> None:
    # The main queue is bound to a repo that isn't on disk yet.
    workspace = _seed(tmp_path, {"10.work": "Do it."}, main_repo="ghost", repos=())
    store = SqliteStore()
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
    store = SqliteStore()
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


def test_patch_task_saves_detail_pane_fields(tmp_path: Path) -> None:
    # Regression: the manager's ``TaskUpdate`` once declared only repo/loop, so
    # the detail pane's other fields (disabled, priority, model, title, body…)
    # were silently dropped by ``model_dump(exclude_unset=True)`` and every save
    # of those toggles failed with "no fields to update". Every editable field
    # the pane sends must round-trip, and model/priority must normalise like the
    # legacy server.
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    main_dir = workspace / "nightshift-tasks" / "main"
    with _client(workspace) as client:
        # The toggle the user reported: flipping ``enabled`` (→ disabled) saves.
        r = client.patch("/api/tasks/10.hello", json={"disabled": True})
        assert r.status_code == 200
        assert r.json()["disabled"] is True
        assert "disabled: true" in (main_dir / "10.hello.md").read_text()

        # And changing priority saves (and is range-validated).
        assert client.patch("/api/tasks/10.hello", json={"priority": 1}).status_code == 200
        assert "priority: 1" in (main_dir / "10.hello.md").read_text()
        assert client.patch("/api/tasks/10.hello", json={"priority": 9}).status_code == 400

        # The full detail-pane payload (title, body, toggles, model) round-trips,
        # and a "default" model pin clears the key (inherit the config default).
        full = client.patch(
            "/api/tasks/10.hello",
            json={
                "title": "Hello v2",
                "body": "Reworked.",
                "evergreen": True,
                "draft": True,
                "model": "default",
                "priority": 2,
            },
        )
        assert full.status_code == 200
        text = (main_dir / "10.hello.md").read_text()
        assert "title: Hello v2" in text
        assert "Reworked." in text
        assert "model:" not in text  # "default" cleared the pin

        # A genuinely empty PATCH is still a 400 (the guard still fires).
        assert client.patch("/api/tasks/10.hello", json={}).status_code == 400


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


# --------------------------------------------------------------------------- #
# Submit fence: a submit is only honored while its lease is live and owned by
# the submitting worker. Otherwise 409 with NO store or git writes (closes the
# stale-worker double-land hole).
# --------------------------------------------------------------------------- #


def _poll_with_landable_branch(
    client: TestClient, workspace: Path, task: str
) -> dict[str, Any]:
    """Checkin + poll ``task`` for w1, then leave a committed landable branch in
    the target repo the way a co-located worker would."""
    client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
    order = client.post(
        "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
    ).json()["work"]
    assert order is not None and order["task"] == task
    wt = setup_worktree(workspace, order["repo"], task)
    (wt / "GENERATED.txt").write_text("done\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "work"], cwd=wt, check=True, capture_output=True)
    return order


def _submit_completed(
    client: TestClient, order: dict[str, Any], *, worker_id: str = "w1"
):
    return client.post(
        f"/api/worker/runs/{order['run_id']}/submit",
        json={
            "worker_id": worker_id, "lease_id": order["lease_id"],
            "task": order["task"], "queue": "main", "title": order["title"],
            "status": "completed", "landable": True, "backend": "claude-code",
        },
    )


def _drain_git(client: TestClient) -> None:
    """Phase 7: a landable submit enqueues the land on the repo executor and
    returns immediately ({"queued": true}); block until every git job and its
    completion transition have applied (the executor drain seam)."""
    assert client.portal is not None, "drain needs the context-managed client"
    client.portal.call(client.app.state.drain_git_jobs)


def test_submit_with_expired_lease_rejected_without_writes(tmp_path: Path) -> None:
    """An expired lease (the task may already be re-leased to another worker)
    must reject the slow worker's eventual submit: 409, nothing lands, the run
    and lease rows are untouched, and the brief stays in the queue."""
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    tasks_root = workspace / "nightshift-tasks"
    repo_root = workspace / "longitude"
    store = SqliteStore()
    with _client(workspace, store) as client:
        order = _poll_with_landable_branch(client, workspace, "10.hello")
        head_before = canonical_head(repo_root)

        # The attempt's TTL elapses and the reconciler expires it.
        asyncio.run(store.update_attempt(order["lease_id"], state=AttemptState.EXPIRED))

        r = _submit_completed(client, order)
        assert r.status_code == 409

        # No git writes: nothing landed, the branch is preserved.
        assert canonical_head(repo_root) == head_before
        # No store writes: the attempt stays expired (not resurrected to
        # landed). Phase 8 behavior fix: /api/runs truthfully projects
        # "expired" instead of the pre-phase zombie "running".
        run = next(x for x in client.get("/api/runs").json() if x["id"] == order["run_id"])
        assert run["status"] == "expired"
        attempt = asyncio.run(store.get_attempt(order["lease_id"]))
        assert attempt["state"] == "expired"
        # The brief was not dropped.
        assert (tasks_root / "main/10.hello.md").exists()


def test_submit_with_cancelled_lease_rejected(tmp_path: Path) -> None:
    """A transport stop cancels the lease; the stopped worker's eventual submit
    must not land."""
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    repo_root = workspace / "longitude"
    store = SqliteStore()
    with _client(workspace, store) as client:
        order = _poll_with_landable_branch(client, workspace, "10.hello")
        head_before = canonical_head(repo_root)
        # A transport stop aborts the attempt (the Phase 8 stop fix).
        asyncio.run(store.update_attempt(order["lease_id"], state=AttemptState.ABORTED))

        assert _submit_completed(client, order).status_code == 409
        assert canonical_head(repo_root) == head_before


def test_submit_from_wrong_worker_rejected(tmp_path: Path) -> None:
    """A submit must come from the worker holding the lease."""
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    repo_root = workspace / "longitude"
    with _client(workspace) as client:
        order = _poll_with_landable_branch(client, workspace, "10.hello")
        head_before = canonical_head(repo_root)
        client.post("/api/worker/checkin", json={"worker_id": "w2", "backend": "claude-code"})

        assert _submit_completed(client, order, worker_id="w2").status_code == 409
        assert canonical_head(repo_root) == head_before
        run = next(x for x in client.get("/api/runs").json() if x["id"] == order["run_id"])
        assert run["status"] == "running"


def test_submit_with_unknown_lease_rejected(tmp_path: Path) -> None:
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    with _client(workspace) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        r = client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": "no-such-lease", "task": "10.hello",
                "queue": "main", "title": "hello", "status": "completed",
                "landable": False,
            },
        )
        assert r.status_code == 409


def test_submit_adopts_agent_land_when_main_advanced(tmp_path: Path) -> None:
    """A worker that reports not-landable but main advanced during the run gets
    its commit recorded (agent self-landed on main)."""
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    tasks_root = workspace / "nightshift-tasks"
    repo_root = workspace / "longitude"
    with _client(workspace) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        run_id, lease_id = order["run_id"], order["lease_id"]
        base_ref = order["base_ref"]

        # Simulate the agent landing on main directly during the worker run.
        (repo_root / "README.md").write_text("# agent landed\n")
        subprocess.run(
            ["git", "commit", "-am", "feat: agent landed on main"],
            cwd=repo_root, check=True, capture_output=True,
        )
        assert canonical_head(repo_root) != base_ref

        r = client.post(
            f"/api/worker/runs/{run_id}/submit",
            json={
                "worker_id": "w1", "lease_id": lease_id, "task": "10.hello",
                "queue": "main", "title": "hello", "status": "completed",
                "result_line": "no changes produced (worker emitted output only)",
                "landable": False,
            },
        )
        body = r.json()
        assert body["landed"] is True
        assert body["sha"] == canonical_head(repo_root)

        run = next(x for x in client.get("/api/runs").json() if x["id"] == run_id)
        assert run["status"] == "completed"
        assert run["commit_sha"] == canonical_head(repo_root)
        assert not (tasks_root / "main/10.hello.md").exists()


# --------------------------------------------------------------------------- #
# Phase 4: a submit's store mutations happen via exactly one store call
# (apply_transition). There is no window between "former write steps" for a
# crash to leave partial state.
# --------------------------------------------------------------------------- #


# Everything a submit may legitimately touch while armed: the single write
# step, reads, the worker-registry status write, and lifespan teardown.
_ARMED_ALLOWED = frozenset({
    "apply_transition",
    "set_worker_status",
    "close",
    # reads
    "get_attempt", "get_task_state", "get_worker",
    "list_attempts", "list_workers", "list_blocked", "live_attempts",
    "tasks_in_state", "retryable_tasks", "queue_dedication",
    "events_since", "max_event_id", "run_events",
    "stats_overall", "stats_by_worker", "stats_by_backend",
    "stats_by_model", "stats_by_queue",
    # Phase 7: queue pauses live in the store; the submit path reads them for
    # the failure-watch policy (still not a write).
    "queue_pauses", "queue_modes",
})


class _TransitionOnlyStore(SqliteStore):
    """Deny-by-default once armed: any store method outside _ARMED_ALLOWED
    raises, so every submit-path mutation of runs/leases/overlay/events must
    ride the single apply_transition call — including write methods added to
    the store in the future."""

    armed = False
    applies = 0

    def __getattribute__(self, name: str) -> Any:
        attr = super().__getattribute__(name)
        if (
            not name.startswith("_")
            and callable(attr)
            and name not in _ARMED_ALLOWED
            and super().__getattribute__("armed")
        ):
            raise AssertionError(
                f"submit called store.{name}(); "
                "apply_transition is the single write step"
            )
        return attr

    async def apply_transition(self, t: Any, **kw: Any) -> list[int] | None:
        self.applies += 1
        return await super().apply_transition(t, **kw)


def test_submit_commits_through_exactly_one_store_write(tmp_path: Path) -> None:
    """A blocked submit's run update, lease release, blocked overlay, and both
    events all commit through one apply_transition call — no legacy writes."""
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    store = _TransitionOnlyStore()
    with _client(workspace, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]

        store.armed = True
        r = client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"],
                "task": "10.hello", "queue": "main", "title": "hello",
                "status": "blocked", "landable": False,
                "result_line": "blocked: needs credentials",
                "failure_kind": "blocked", "failure_reason": "needs credentials",
            },
        )
        assert r.status_code == 200
        assert r.json() == {"landed": False, "status": "blocked"}
        assert store.applies == 1

        run = next(x for x in client.get("/api/runs").json() if x["id"] == order["run_id"])
        assert run["status"] == "blocked"
        assert run["finished_at"] is not None
        attempt = asyncio.run(store.get_attempt(order["lease_id"]))
        assert attempt["state"] == "blocked"
        assert attempt["released_at"] is not None
        hold = asyncio.run(store.get_task_state(None, "10.hello"))
        assert hold["state"] == "blocked"
        assert hold["blocked_reason"] == "needs credentials"
        kinds = [e["kind"] for e in asyncio.run(store.run_events(order["run_id"]))]
        assert "task_result" in kinds


class _CrashAfterApplyStore(SqliteStore):
    """Simulates the process dying immediately after the single write step."""

    async def apply_transition(self, t: Any, **kw: Any) -> list[int] | None:
        await super().apply_transition(t, **kw)
        raise RuntimeError("simulated crash after the single write step")


def test_crash_after_apply_leaves_consistent_state(tmp_path: Path) -> None:
    """Killing the handler right after apply leaves run terminal + lease
    released + events present together — never a partial subset, because
    there is only one write step."""
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    store = _CrashAfterApplyStore()
    with _client(workspace, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        with pytest.raises(RuntimeError, match="simulated crash"):
            client.post(
                f"/api/worker/runs/{order['run_id']}/submit",
                json={
                    "worker_id": "w1", "lease_id": order["lease_id"],
                    "task": "10.hello", "queue": "main", "title": "hello",
                    "status": "error", "landable": False,
                    "result_line": "worker bailed", "failure_kind": "worker_error",
                },
            )
        attempt = asyncio.run(store.get_attempt(order["run_id"]))
        assert attempt["state"] == "failed"
        assert attempt["finished_at"] is not None
        assert attempt["released_at"] is not None
        kinds = [e["kind"] for e in asyncio.run(store.run_events(order["run_id"]))]
        assert "task_result" in kinds


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


def test_work_order_strips_original_brief_and_carries_enhanced(tmp_path: Path) -> None:
    """Workers only see the effective brief: the preserved pre-enhancement tail
    never enters the work order, while the ``enhanced`` frontmatter flag rides
    along for attempt attribution."""
    workspace = _seed(tmp_path, {
        "10.hello": (
            "---\nenhanced: true\n---\n"
            f"The enhanced spec.\n\n{ORIGINAL_BRIEF_MARKER}\nthe raw ask\n"
        ),
    })
    with _client(workspace) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert order["body"] == "The enhanced spec."
        assert ORIGINAL_BRIEF_MARKER not in order["body"]
        assert "the raw ask" not in order["body"]
        assert order["enhanced"] is True
        # The flag is stamped onto the attempt row and projected on the run view.
        run = next(
            r for r in client.get("/api/runs").json() if r["id"] == order["run_id"]
        )
        assert run["enhanced"] is True
        assert run["rating"] is None


def test_work_order_enhanced_defaults_false(tmp_path: Path) -> None:
    workspace = _seed(tmp_path, {"10.plain": "Just do the thing."})
    with _client(workspace) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert order["enhanced"] is False
        run = next(
            r for r in client.get("/api/runs").json() if r["id"] == order["run_id"]
        )
        assert run["enhanced"] is False


def test_post_task_enhance_rewrites_and_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enhance-on-create: the rewrite becomes the effective body, the typed
    text survives as the original brief, the file is stamped ``enhanced``, and
    the request lands in the enhancements telemetry."""
    workspace = _seed(tmp_path, {})

    def fake_enhance(title: str, text: str, *, model: str, env: dict) -> EnhanceResult:
        assert title == "Fix ops" and text == "make it nicer"
        return EnhanceResult(
            text="A precise, self-contained spec.",
            model=model,
            usage={"input_tokens": 100, "output_tokens": 40},
        )

    monkeypatch.setattr(api_operator, "enhance_brief", fake_enhance)
    with _client(workspace) as client:
        r = client.post(
            "/api/tasks",
            json={"title": "Fix ops", "text": "make it nicer", "enhance": True},
        )
        assert r.status_code == 200
        task = r.json()["task"]

        brief = client.get(f"/api/tasks/{task}").json()
        assert brief["body"] == "A precise, self-contained spec."
        assert brief["original_brief"] == "make it nicer"
        assert brief["frontmatter"]["enhanced"] is True

        stats = client.get("/api/stats").json()
        assert {"by_enhanced", "enhancements"} <= set(stats)
        summary = stats["enhancements"]
        assert summary["total"] == 1
        assert summary["succeeded"] == 1
        assert summary["failed"] == 0
        assert summary["total_input_tokens"] == 100
        assert summary["total_output_tokens"] == 40


def test_post_task_enhance_failure_is_502_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed enhancement refuses the create (the operator's draft survives
    client-side) — no task file, no queue entry — but the failed request is
    still recorded in the telemetry."""
    workspace = _seed(tmp_path, {})

    def fake_enhance(title: str, text: str, *, model: str, env: dict) -> EnhanceResult:
        raise EnhanceError("vendor down")

    monkeypatch.setattr(api_operator, "enhance_brief", fake_enhance)
    with _client(workspace) as client:
        r = client.post(
            "/api/tasks",
            json={"title": "Fix ops", "text": "make it nicer", "enhance": True},
        )
        assert r.status_code == 502
        assert "vendor down" in r.json()["error"]
        assert client.get("/api/queue").json() == []
        assert not (workspace / "nightshift-tasks/main/fix-ops.md").exists()

        summary = client.get("/api/stats").json()["enhancements"]
        assert summary["total"] == 1
        assert summary["succeeded"] == 0
        assert summary["failed"] == 1


def test_post_task_without_enhance_never_calls_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _seed(tmp_path, {})

    def explode(*args: object, **kwargs: object) -> EnhanceResult:
        raise AssertionError("enhance_brief must not be called")

    monkeypatch.setattr(api_operator, "enhance_brief", explode)
    with _client(workspace) as client:
        r = client.post("/api/tasks", json={"title": "Plain", "text": "just do it"})
        assert r.status_code == 200
        brief = client.get(f"/api/tasks/{r.json()['task']}").json()
        assert brief["body"] == "just do it"
        assert brief["original_brief"] == ""
        assert client.get("/api/stats").json()["enhancements"]["total"] == 0


def test_patch_task_edits_original_brief(tmp_path: Path) -> None:
    workspace = _seed(tmp_path, {})
    with _client(workspace) as client:
        client.post("/api/tasks", json={"title": "Halves", "text": "spec"})
        r = client.patch("/api/tasks/halves", json={"original_brief": "raw ask"})
        assert r.status_code == 200
        brief = client.get("/api/tasks/halves").json()
        assert brief["body"] == "spec"
        assert brief["original_brief"] == "raw ask"


def test_run_rating_round_trip(tmp_path: Path) -> None:
    """The thumbs endpoint: up/down persist onto the run view, null clears,
    junk is a 400, and an unknown run is a 404."""
    workspace = _seed(tmp_path, {"10.hello": "Do a thing."})
    with _client(workspace) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        run_id = order["run_id"]

        r = client.patch(f"/api/runs/{run_id}/rating", json={"rating": "up"})
        assert r.status_code == 200 and r.json() == {"id": run_id, "rating": "up"}
        run = next(x for x in client.get("/api/runs").json() if x["id"] == run_id)
        assert run["rating"] == "up"

        # Re-rating overwrites; null clears.
        client.patch(f"/api/runs/{run_id}/rating", json={"rating": "down"})
        run = next(x for x in client.get("/api/runs").json() if x["id"] == run_id)
        assert run["rating"] == "down"
        client.patch(f"/api/runs/{run_id}/rating", json={"rating": None})
        run = next(x for x in client.get("/api/runs").json() if x["id"] == run_id)
        assert run["rating"] is None

        assert client.patch(
            f"/api/runs/{run_id}/rating", json={"rating": "meh"}
        ).status_code == 400
        assert client.patch(
            "/api/runs/nope/rating", json={"rating": "up"}
        ).status_code == 404


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
        # The new brief requests ``auto`` (from the task template); ``auto`` is
        # always routable, so any advertised model lets the worker pick up the task.
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
        # Phase 7: the land is queued on the repo executor; drain, then the
        # brief drop is a local content-store commit exactly as before.
        assert r.json()["queued"] is True
        _drain_git(client)
        assert store_log_subject() == f"nightshift: drop completed task {task}"
        assert not (tasks_root / "main" / f"{task}.md").exists()

    # No remote was ever configured or required across the whole lifecycle.
    assert store_remotes() == ""


# --------------------------------------------------------------------------- #
# Resolve (out-of-process conflict resolution)
# --------------------------------------------------------------------------- #


class _FakeProc:
    """Stand-in for a live resolve subprocess (always reports running)."""

    def poll(self) -> int | None:
        return None


def _stub_spawn(app) -> list[dict[str, Any]]:
    """Replace the resolver launcher with a recorder that registers a live fake
    process (so the per-repo concurrency cap is exercised). Returns the call log.
    """
    calls: list[dict[str, Any]] = []

    def fake(child_run_id, *, task, queue, repo, title, origin_run_id):
        calls.append({
            "run_id": child_run_id, "task": task, "queue": queue,
            "repo": repo, "origin_run_id": origin_run_id,
        })
        app.state.resolves[child_run_id] = {
            "proc": _FakeProc(), "repo": repo, "task": task,
            "queue": queue, "origin_run_id": origin_run_id,
        }
        return True

    app.state.spawn_resolve = fake
    return calls


def test_poll_syncs_origin_before_pinning_base_ref(tmp_path: Path) -> None:
    """In a remote-landing mode the poll integrates origin/main before pinning
    base_ref, so a dispatched worker starts from the freshest merged state."""
    from _workspace import add_remote, make_bare_remote

    root = _seed(
        tmp_path, {"10.x": "do"},
        config={"landing_mode": "push", "rendezvous_remote": "origin"},
    )
    repo_root = root / "longitude"
    bare = make_bare_remote(tmp_path / "origin.git")
    add_remote(repo_root, "origin", bare)
    # Another actor advances origin/main after our local clone was made.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "o@o"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "o"], check=True)
    (other / "origin.txt").write_text("from origin\n")
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "commit", "-m", "origin work"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "push", "origin", "main"], check=True, capture_output=True)
    origin_tip = subprocess.run(
        ["git", "-C", str(other), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert order is not None
        # base_ref tracks the advanced origin tip; the advance is now local too.
        assert order["base_ref"] == origin_tip
        assert (repo_root / "origin.txt").exists()


def test_resolve_endpoint_spawns_and_caps(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.x": "do a thing"})
    store = SqliteStore()
    asyncio.run(store.create_attempt(
        "r1", task="10.x", queue="main", worker_id="w1", backend="claude-code",
        model="auto", base_ref=None, ttl_seconds=60, title="X", repo="longitude",
    ))
    app = create_app(root, store=store)
    calls = _stub_spawn(app)
    with TestClient(app) as client:
        r = client.post("/api/runs/r1/10.x/resolve")
        assert r.status_code == 202
        child = r.json()["run_id"]
        assert child and calls[0]["origin_run_id"] == "r1"
        assert calls[0]["repo"] == "longitude"
        # A fresh resolve run was recorded.
        runs = client.get("/api/runs").json()
        assert any(run["id"] == child for run in runs)
        # The per-repo cap (default 1) blocks a second concurrent resolve.
        r2 = client.post("/api/runs/r1/10.x/resolve")
        assert r2.status_code == 409


def test_resolve_endpoint_unknown_run_404(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.x": "do"})
    app = create_app(root, store=SqliteStore())
    _stub_spawn(app)
    with TestClient(app) as client:
        assert client.post("/api/runs/nope/10.x/resolve").status_code == 404


def test_resolve_result_completes_task(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.x": "do"})
    store = SqliteStore()
    asyncio.run(store.create_attempt(
        "c1", task="10.x", queue="main", worker_id="manager:resolve",
        backend="claude-code", model="auto", base_ref=None, ttl_seconds=0,
        title="X", repo="longitude", state="resolving",
    ))
    app = create_app(root, store=store)
    with TestClient(app) as client:
        r = client.post(
            "/api/worker/runs/c1/resolve-result",
            json={
                "task": "10.x", "queue": "main", "status": "completed",
                "landed": True, "sha": "deadbeef", "result_line": "resolved: landed",
                "remote": "push", "pushed": True,
            },
        )
        assert r.json()["ok"] is True
        run = next(x for x in client.get("/api/runs").json() if x["id"] == "c1")
        assert run["status"] == "completed"
        assert run["commit_sha"] == "deadbeef"


def test_resolve_result_failure_reblocks_task(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.x": "do"})
    store = SqliteStore()
    asyncio.run(store.create_attempt(
        "c1", task="10.x", queue="main", worker_id="manager:resolve",
        backend="claude-code", model="auto", base_ref=None, ttl_seconds=0,
        title="X", repo="longitude", state="resolving",
    ))
    app = create_app(root, store=store)
    with TestClient(app) as client:
        r = client.post(
            "/api/worker/runs/c1/resolve-result",
            json={
                "task": "10.x", "queue": "main", "status": "error",
                "landed": False, "failure_kind": "merge_conflict",
                "failure_reason": "still conflicts",
            },
        )
        assert r.json()["ok"] is True
        run = next(x for x in client.get("/api/runs").json() if x["id"] == "c1")
        assert run["status"] == "error"
        blocked = client.get("/api/blocked").json()
        assert any(b["task"] == "10.x" for b in blocked)


def test_resolve_result_with_unresolvable_origin_rejected(tmp_path: Path) -> None:
    """A stale resolve report (its origin run already landed via another route,
    e.g. a re-lease) is rejected with 409 and NO writes: the resolve run, the
    origin run, the task overlay, and the brief are all untouched."""
    root = _seed(tmp_path, {"10.x": "do"})
    tasks_root = root / "nightshift-tasks"
    store = SqliteStore()
    asyncio.run(store.create_attempt(
        "o1", task="10.x", queue="main", worker_id="w1", backend="claude-code",
        model="auto", base_ref=None, ttl_seconds=60, title="X", repo="longitude",
    ))
    asyncio.run(store.update_attempt(
        "o1", state=AttemptState.LANDED, commit_sha="abc123",
    ))
    asyncio.run(store.create_attempt(
        "c1", task="10.x", queue="main", worker_id="manager:resolve",
        backend="claude-code", model="auto", base_ref=None, ttl_seconds=0,
        title="X", repo="longitude", state="resolving",
    ))
    app = create_app(root, store=store)
    with TestClient(app) as client:
        r = client.post(
            "/api/worker/runs/c1/resolve-result",
            json={
                "task": "10.x", "queue": "main", "origin_run_id": "o1",
                "status": "completed", "landed": True, "sha": "deadbeef",
            },
        )
        assert r.status_code == 409
        runs = {x["id"]: x for x in client.get("/api/runs").json()}
        assert runs["c1"]["status"] == "running"          # resolve run untouched
        assert runs["o1"]["commit_sha"] == "abc123"       # origin not clobbered
        assert (tasks_root / "main/10.x.md").exists()     # brief not dropped

        # An origin run that no longer exists is equally unresolvable.
        r2 = client.post(
            "/api/worker/runs/c1/resolve-result",
            json={
                "task": "10.x", "queue": "main", "origin_run_id": "gone",
                "status": "completed", "landed": True, "sha": "deadbeef",
            },
        )
        assert r2.status_code == 409


def test_resolve_result_updates_resolvable_origin(tmp_path: Path) -> None:
    """The happy path still works: an origin run held in a resolvable state
    (error/blocked) is completed alongside the resolve run."""
    root = _seed(tmp_path, {"10.x": "do"})
    store = SqliteStore()
    asyncio.run(store.create_attempt(
        "o1", task="10.x", queue="main", worker_id="w1", backend="claude-code",
        model="auto", base_ref=None, ttl_seconds=60, title="X", repo="longitude",
    ))
    asyncio.run(store.update_attempt(
        "o1", state=AttemptState.CONFLICT, failure_kind="merge_conflict",
    ))
    asyncio.run(store.create_attempt(
        "c1", task="10.x", queue="main", worker_id="manager:resolve",
        backend="claude-code", model="auto", base_ref=None, ttl_seconds=0,
        title="X", repo="longitude", state="resolving",
    ))
    app = create_app(root, store=store)
    with TestClient(app) as client:
        r = client.post(
            "/api/worker/runs/c1/resolve-result",
            json={
                "task": "10.x", "queue": "main", "origin_run_id": "o1",
                "status": "completed", "landed": True, "sha": "deadbeef",
            },
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        runs = {x["id"]: x for x in client.get("/api/runs").json()}
        assert runs["c1"]["status"] == "completed"
        assert runs["o1"]["status"] == "completed"
        assert runs["o1"]["commit_sha"] == "deadbeef"


def test_conflict_auto_escalates_to_resolve(tmp_path: Path) -> None:
    """A landing conflict on submit (auto_resolve on by default) spawns the
    out-of-process resolver instead of merely blocking the task."""
    root = _seed(tmp_path, {"10.edit": "edit the readme"})
    repo_root = root / "longitude"
    app = create_app(root, store=SqliteStore())
    calls = _stub_spawn(app)
    with TestClient(app) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert order is not None

        # Worker branch edits README one way...
        wt = setup_worktree(root, order["repo"], "10.edit")
        (wt / "README.md").write_text("branch change\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "branch"], cwd=wt, check=True, capture_output=True)
        # ...main advances with a conflicting edit to the same file.
        (repo_root / "README.md").write_text("main change\n")
        subprocess.run(
            ["git", "commit", "-am", "main edit"], cwd=repo_root, check=True, capture_output=True
        )

        r = client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"], "task": "10.edit",
                "queue": "main", "title": order["title"], "status": "completed",
                "landable": True, "backend": "claude-code",
            },
        )
        # Phase 7: the conflict surfaces when the queued land completes (the
        # submit response no longer carries the final land result); the
        # auto-escalation still spawns the resolver from the completion.
        assert r.json()["queued"] is True
        _drain_git(client)
        assert len(calls) == 1 and calls[0]["task"] == "10.edit"
        run = next(
            x for x in client.get("/api/runs").json() if x["id"] == order["run_id"]
        )
        assert run["status"] == "error"
        assert run["failure_kind"] == "merge_conflict"


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
# Failure pause/retry policy (phase A + B)
# --------------------------------------------------------------------------- #


def _poll_and_submit_error(client: TestClient, task: str, *, worker_id: str = "w1") -> dict[str, Any]:
    order = client.post(
        "/api/worker/poll", json={"worker_id": worker_id, "backend": "claude-code"}
    ).json()["work"]
    assert order is not None and order["task"] == task
    resp = client.post(
        f"/api/worker/runs/{order['run_id']}/submit",
        json={
            "worker_id": worker_id, "lease_id": order["lease_id"], "task": task,
            "queue": order.get("queue", "main"), "title": task, "status": "error",
            "result_line": "boom", "failure_kind": "worker_error",
            "landable": False, "backend": "claude-code", "model": "claude-sonnet-4-6",
        },
    ).json()
    return resp


def test_single_failure_leaves_queue_running_and_marks_task_failed(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.a": "Do a thing.", "20.b": "Do another thing."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        _poll_and_submit_error(client, "10.a")

        # Failed is now in frontmatter (single source of truth).
        brief = client.get("/api/tasks/10.a").json()
        assert brief["failed"] is True

        # Also surfaced in /api/blocked for the "needs attention" list.
        blocked = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert blocked["10.a"]["state"] == "failed"

        state = client.get("/api/state").json()
        assert state["queues"]["main"]["state"] != "paused"

        work = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert work is not None and work["task"] == "20.b"


def test_two_unrelated_failures_in_a_row_pause_the_queue(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.a": "Do a thing.", "20.b": "Do another thing."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        _poll_and_submit_error(client, "10.a")
        _poll_and_submit_error(client, "20.b")

        state = client.get("/api/state").json()
        assert state["queues"]["main"]["state"] == "paused"
        assert state["queues"]["main"]["pause_reason"] == "consecutive_failures"

        assert client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"] is None


def test_success_between_failures_does_not_pause(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.a": "Do a.", "20.b": "Do b.", "30.c": "Do c."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        _poll_and_submit_error(client, "10.a")

        # 20.b succeeds: land a real commit to count as a landed success.
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert order["task"] == "20.b"
        wt = setup_worktree(
            Path(client.app.state.workspace), order["repo"], "20.b"
        )
        (wt / "RESULT.txt").write_text("done\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "work"], cwd=wt, check=True, capture_output=True)
        client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"], "task": "20.b",
                "queue": "main", "title": "20.b", "status": "completed",
                "landable": True, "backend": "claude-code", "model": "claude-sonnet-4-6",
            },
        )
        # Phase 7: the land (whose completion disarms the failure watch) is
        # async — drain before the next failure so the success is on record.
        _drain_git(client)
        _poll_and_submit_error(client, "30.c")

        state = client.get("/api/state").json()
        assert state["queues"]["main"]["state"] != "paused"


def test_play_clears_consecutive_failure_pause(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.a": "Do a thing.", "20.b": "Do another thing."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        _poll_and_submit_error(client, "10.a")
        _poll_and_submit_error(client, "20.b")
        assert client.get("/api/state").json()["queues"]["main"]["state"] == "paused"

        client.post("/api/transport", json={"action": "play", "queue": None})
        state = client.get("/api/state").json()
        assert state["queues"]["main"]["state"] != "paused"


def test_validation_error_blocks_task_not_failed(tmp_path: Path) -> None:
    """A validation failure means the agent DID produce commits — the branch is
    preserved. The task should be blocked (needs resolve), not 'failed', and it
    should NOT arm the two-in-a-row failure watch."""
    root = _seed(tmp_path, {"10.a": "A.", "20.b": "B."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # 10.a fails validation — this should block the task, not mark it failed.
        work = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert work["task"] == "10.a"
        client.post(
            f"/api/worker/runs/{work['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": work["lease_id"], "task": "10.a",
                "queue": "main", "title": "10.a", "status": "error",
                "result_line": "validate failed", "failure_kind": "validation_error",
                "failure_reason": "lint errors",
                "landable": False, "backend": "claude-code", "model": "claude-sonnet-4-6",
            },
        )
        blocked = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert blocked["10.a"]["state"] == "blocked"
        assert "validation failed" in blocked["10.a"]["blocked_reason"]

        # 20.b also fails validation — queue should NOT pause because
        # validation errors don't count toward two-in-a-row.
        work2 = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert work2["task"] == "20.b"
        client.post(
            f"/api/worker/runs/{work2['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": work2["lease_id"], "task": "20.b",
                "queue": "main", "title": "20.b", "status": "error",
                "result_line": "validate failed", "failure_kind": "validation_error",
                "failure_reason": "type errors",
                "landable": False, "backend": "claude-code", "model": "claude-sonnet-4-6",
            },
        )
        state = client.get("/api/state").json()
        assert state["queues"]["main"]["state"] != "paused"
        assert state["queues"]["main"].get("pause_reason") is None


def test_full_failure_lifecycle_drain_then_retry_then_quarantine(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.a": "A.", "20.b": "B."})
    store = SqliteStore()
    with _client(root, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # Phase A: 10.a fails (armed), 20.b is still ready and dispatches fine.
        _poll_and_submit_error(client, "10.a")

        # Verify failed is in frontmatter.
        brief = client.get("/api/tasks/10.a").json()
        assert brief["failed"] is True

        work = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert work["task"] == "20.b"

        # 20.b fails too (no success in between) -> queue pauses.
        client.post(
            f"/api/worker/runs/{work['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": work["lease_id"], "task": "20.b",
                "queue": "main", "title": "20.b", "status": "error",
                "result_line": "boom2", "failure_kind": "worker_error",
                "landable": False, "backend": "claude-code", "model": "claude-sonnet-4-6",
            },
        )
        state = client.get("/api/state").json()
        assert state["queues"]["main"]["pause_reason"] == "consecutive_failures"
        assert client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"] is None

        # Operator presses Play -> clears failed flags on both tasks, unpauses.
        client.post("/api/transport", json={"action": "play", "queue": None})
        assert client.get("/api/tasks/10.a").json()["failed"] is False
        assert client.get("/api/tasks/20.b").json()["failed"] is False

        # 10.a dispatches as a fresh attempt (not a Phase B retry).
        retry = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert retry is not None and retry["task"] == "10.a"

        # Fails again -> quarantined (counter threshold: 2 consecutive
        # no-progress runs). No retry_failed pause since this isn't tracked
        # as a retry (play cleared the failed flag).
        client.post(
            f"/api/worker/runs/{retry['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": retry["lease_id"], "task": "10.a",
                "queue": "main", "title": "10.a", "status": "error",
                "result_line": "boom3", "failure_kind": "worker_error",
                "landable": False, "backend": "claude-code", "model": "claude-sonnet-4-6",
            },
        )
        blocked = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert blocked["10.a"]["state"] == "quarantined"

        brief = client.get("/api/tasks/10.a").json()
        assert brief["quarantined"] is True
        assert brief["quarantine_reason"]

        # 20.b dispatches next (also released by play).
        retry2 = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert retry2 is not None and retry2["task"] == "20.b"


def test_unrelated_save_does_not_clear_quarantine(tmp_path: Path) -> None:
    """Regression: saving an unrelated field (title) must not accidentally clear
    system-set quarantine or failed state (the original divergent-state bug)."""
    root = _seed(tmp_path, {"10.loop": "Move a setting that's already moved."})
    store = SqliteStore()
    with _client(root, store) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # Drive two no-change runs to trigger quarantine.
        _poll_and_submit_no_change(client, "10.loop")
        _clear_backoff(store, "10.loop")
        second = _poll_and_submit_no_change(client, "10.loop")
        assert second["quarantined"] is True

        # Verify quarantine is in frontmatter.
        brief = client.get("/api/tasks/10.loop").json()
        assert brief["quarantined"] is True

        # Operator edits only the title — must NOT clear quarantine.
        r = client.patch("/api/tasks/10.loop", json={"title": "New Title"})
        assert r.status_code == 200

        # Quarantine is still intact.
        brief2 = client.get("/api/tasks/10.loop").json()
        assert brief2["quarantined"] is True
        assert brief2["title"] == "New Title"

        # Still excluded from dispatch.
        assert client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"] is None


def test_unrelated_save_does_not_clear_failed(tmp_path: Path) -> None:
    """Same as quarantine test but for the failed state."""
    root = _seed(tmp_path, {"10.a": "Do a thing.", "20.b": "Do another thing."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        _poll_and_submit_error(client, "10.a")

        brief = client.get("/api/tasks/10.a").json()
        assert brief["failed"] is True

        # Edit only the title — must NOT clear failed.
        r = client.patch("/api/tasks/10.a", json={"title": "Renamed"})
        assert r.status_code == 200

        brief2 = client.get("/api/tasks/10.a").json()
        assert brief2["failed"] is True
        assert brief2["title"] == "Renamed"


def test_toggle_failed_off_releases_for_dispatch(tmp_path: Path) -> None:
    """Toggling failed off via PATCH releases the task for normal dispatch
    (not just Phase B retry)."""
    root = _seed(tmp_path, {"10.a": "Do a thing.", "20.b": "Do another thing."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        _poll_and_submit_error(client, "10.a")
        _poll_and_submit_error(client, "20.b")

        # Queue is paused (two consecutive failures).
        state = client.get("/api/state").json()
        assert state["queues"]["main"]["pause_reason"] == "consecutive_failures"

        brief = client.get("/api/tasks/10.a").json()
        assert brief["failed"] is True

        # Unpause and toggle failed off on 10.a only.
        client.post("/api/transport", json={"action": "play", "queue": None})
        r = client.patch("/api/tasks/10.a", json={"failed": False})
        assert r.status_code == 200

        brief2 = client.get("/api/tasks/10.a").json()
        assert brief2["failed"] is False

        # 10.a dispatchable again (as normal ready task, not a Phase B retry).
        work = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert work is not None and work["task"] == "10.a"


def test_play_clears_failed_and_dispatches(tmp_path: Path) -> None:
    """Pressing play on a queue with failed tasks clears the failed state
    and makes them immediately dispatchable — no separate PATCH needed."""
    root = _seed(tmp_path, {"10.a": "Do a thing.", "20.b": "Do another thing."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        _poll_and_submit_error(client, "10.a")
        _poll_and_submit_error(client, "20.b")

        assert client.get("/api/tasks/10.a").json()["failed"] is True
        assert client.get("/api/tasks/20.b").json()["failed"] is True
        assert client.get("/api/state").json()["queues"]["main"]["state"] == "paused"

        client.post("/api/transport", json={"action": "play", "queue": None})

        assert client.get("/api/tasks/10.a").json()["failed"] is False
        assert client.get("/api/tasks/20.b").json()["failed"] is False

        work = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert work is not None and work["task"] == "10.a"


def test_reenable_disabled_failed_task_clears_failed(tmp_path: Path) -> None:
    """Disabling a failed task and re-enabling it clears the failed state."""
    root = _seed(tmp_path, {"10.a": "Do a thing.", "20.b": "Do another thing."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        _poll_and_submit_error(client, "10.a")

        brief = client.get("/api/tasks/10.a").json()
        assert brief["failed"] is True

        client.patch("/api/tasks/10.a", json={"disabled": True})
        brief2 = client.get("/api/tasks/10.a").json()
        assert brief2["disabled"] is True
        assert brief2["failed"] is True

        client.patch("/api/tasks/10.a", json={"disabled": False})
        brief3 = client.get("/api/tasks/10.a").json()
        assert brief3["disabled"] is False
        assert brief3["failed"] is False

        work = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert work is not None and work["task"] == "10.a"


def test_multiple_failed_tasks_independently_recoverable(tmp_path: Path) -> None:
    """Two failed tasks in the same queue can each be independently recovered
    via separate PATCH+play without leaving residual error state."""
    root = _seed(tmp_path, {"10.a": "A.", "20.b": "B.", "30.c": "C."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        _poll_and_submit_error(client, "10.a")
        _poll_and_submit_error(client, "20.b")

        assert client.get("/api/tasks/10.a").json()["failed"] is True
        assert client.get("/api/tasks/20.b").json()["failed"] is True

        # Recover only 10.a via PATCH + play.
        client.post("/api/transport", json={"action": "play", "queue": None})
        client.patch("/api/tasks/10.a", json={"failed": False})

        brief_a = client.get("/api/tasks/10.a").json()
        assert brief_a["failed"] is False

        # 20.b was also released by play.
        brief_b = client.get("/api/tasks/20.b").json()
        assert brief_b["failed"] is False

        work = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert work is not None and work["task"] == "10.a"

        # No residual blocked state for either task.
        blocked = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert "10.a" not in blocked
        assert "20.b" not in blocked


def test_reset_clears_blocked_task(tmp_path: Path) -> None:
    """POST /api/tasks/{task}/reset clears a blocked task's DB overlay."""
    root = _seed(tmp_path, {"10.a": "A.", "20.b": "B."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # 10.a fails validation -> blocked.
        work = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert work["task"] == "10.a"
        client.post(
            f"/api/worker/runs/{work['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": work["lease_id"], "task": "10.a",
                "queue": "main", "title": "10.a", "status": "error",
                "result_line": "validate failed", "failure_kind": "validation_error",
                "failure_reason": "lint errors",
                "landable": False, "backend": "claude-code", "model": "claude-sonnet-4-6",
            },
        )
        blocked = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert blocked["10.a"]["state"] == "blocked"

        # Reset clears the block.
        r = client.post("/api/tasks/10.a/reset")
        assert r.status_code == 200
        assert r.json()["released"] is True

        # No longer blocked.
        blocked2 = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert "10.a" not in blocked2


def test_reset_not_blocked_returns_404(tmp_path: Path) -> None:
    """POST /api/tasks/{task}/reset on a non-blocked task returns 404."""
    root = _seed(tmp_path, {"10.a": "Do a thing."})
    with _client(root) as client:
        r = client.post("/api/tasks/10.a/reset")
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# DSN resolution — Nightshift owns its own store DSN
# --------------------------------------------------------------------------- #


def _write_manager_block(tmp_path: Path, manager: dict) -> Path:
    ns_dir = tmp_path / ".nightshift"
    ns_dir.mkdir(parents=True, exist_ok=True)
    (ns_dir / "manager.json").write_text(
        json.dumps({"default_model": "auto"})
    )
    return tmp_path


def test_dsn_unset_is_none(tmp_path: Path, monkeypatch) -> None:
    from nightshift.config.manager import load_manager_config

    monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
    root = _write_manager_block(tmp_path, {})
    assert load_manager_config(root).dsn is None


def test_dsn_from_env(tmp_path: Path, monkeypatch) -> None:
    from nightshift.config.manager import load_manager_config

    monkeypatch.setenv("NIGHTSHIFT_PG_DSN", "postgresql://db/nightshift")
    root = _write_manager_block(tmp_path, {})
    assert load_manager_config(root).dsn == "postgresql://db/nightshift"


def test_dsn_env_overrides_block(tmp_path: Path, monkeypatch) -> None:
    from nightshift.config.manager import load_manager_config

    monkeypatch.setenv("NIGHTSHIFT_PG_DSN", "postgresql://env-host/nightshift")
    root = _write_manager_block(tmp_path, {"dsn": "postgresql://db/nightshift"})
    assert load_manager_config(root).dsn == "postgresql://env-host/nightshift"


def test_dsn_absent_is_none(tmp_path: Path, monkeypatch) -> None:
    from nightshift.config.manager import load_manager_config

    monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
    root = _write_manager_block(tmp_path, {})
    assert load_manager_config(root).dsn is None


def test_open_store_no_dsn_is_sqlite(monkeypatch) -> None:
    # open_store with no arg + no NIGHTSHIFT_PG_DSN falls back to SqliteStore.
    from nightshift.manager.store import open_store
    from nightshift.manager.store_sqlite import SqliteStore

    monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
    store = asyncio.run(open_store())
    assert isinstance(store, SqliteStore)


# --------------------------------------------------------------------------- #
# Phase 8 API snapshots: /api/runs, /api/leases, and the SSE snapshot frame
# keep their pre-attempts shapes byte-for-byte (views over the merged table).
# --------------------------------------------------------------------------- #


# The exact pre-Phase-8 run-row wire keys (order included: JSON objects keep
# insertion order and the previous rows were column-ordered), extended with
# the token-usage-granularity fields (cache splits + raw usage payload) added
# alongside input_tokens/output_tokens, and with the enhance-tracking fields
# (enhanced flag + operator rating) — see manager/views.py RUN_VIEW_KEYS.
RUN_WIRE_KEYS = [
    "id", "task", "queue", "worker_id", "backend", "model", "repo",
    "required_mcps", "status", "phase", "result_line", "commit_sha", "loc",
    "remote", "pushed", "turns", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "usage",
    "cost_usd", "failure_kind", "failure_reason", "validate_cmd", "worktree",
    "title", "body", "started_at", "finished_at", "enhanced", "rating", "notes",
]

# The exact pre-Phase-8 lease-row wire keys.
LEASE_WIRE_KEYS = [
    "id", "task", "queue", "worker_id", "run_id", "status", "model",
    "base_ref", "acquired_at", "heartbeat_at", "expires_at", "released_at",
]


def _seed_every_state(store: SqliteStore) -> dict[str, str]:
    """One attempt per storable state (distinct tasks — invariant 1 allows a
    single live attempt per task). Returns {state: attempt id}."""
    scenarios: list[tuple[str, AttemptState | None, dict[str, Any]]] = [
        ("running", None, {}),
        ("landing", AttemptState.LANDING, {"phase": "landing"}),
        ("resolving", None, {}),  # created with state="resolving" below
        ("landed", AttemptState.LANDED, {"commit_sha": "abc123", "loc": 3}),
        ("no_change", AttemptState.NO_CHANGE, {"result_line": "no changes"}),
        ("blocked", AttemptState.BLOCKED, {"failure_kind": "blocked"}),
        ("failed", AttemptState.FAILED, {"failure_kind": "worker_error"}),
        ("conflict", AttemptState.CONFLICT, {"failure_kind": "merge_conflict"}),
        ("skipped", AttemptState.SKIPPED, {}),
        ("aborted", AttemptState.ABORTED, {}),
        ("expired", AttemptState.EXPIRED, {}),
    ]

    async def seed() -> dict[str, str]:
        ids: dict[str, str] = {}
        for n, (name, state, fields) in enumerate(scenarios):
            rid = f"r-{name}"
            row = await store.create_attempt(
                rid, task=f"{n}.task-{name}", queue=None, worker_id="w1",
                backend="claude-code", model="auto", base_ref="base",
                ttl_seconds=600, title=name,
                state="resolving" if name == "resolving" else "running",
            )
            assert row is not None
            updates: dict[str, Any] = dict(fields)
            if state is not None:
                updates["state"] = state
            if updates:
                await store.update_attempt(rid, **updates)
            ids[name] = rid
        return ids

    return asyncio.run(seed())


def test_api_runs_snapshot_keys_and_status_projection(tmp_path: Path) -> None:
    root = _seed(tmp_path, {})
    store = SqliteStore()
    with _client(root, store) as client:
        # Seed after lifespan startup: a pre-seeded LANDING attempt would be
        # parked by the manager's own interrupted-land recovery.
        ids = _seed_every_state(store)
        rows = {r["id"]: r for r in client.get("/api/runs").json()}
        assert set(ids.values()) <= set(rows)
        expected_status = {
            "running": "running", "landing": "running", "resolving": "running",
            "landed": "completed", "no_change": "completed",
            "blocked": "blocked", "failed": "error", "conflict": "error",
            "skipped": "skipped", "aborted": "aborted", "expired": "expired",
        }
        for name, rid in ids.items():
            row = rows[rid]
            # EXACT key set and order — the pre-Phase-8 run row shape; none of
            # the internal attempt columns (state, base_ref, acquired_at,
            # heartbeat_at, deadline_at, released_at, branch_ref, head_sha)
            # may ever leak onto the wire.
            assert list(row) == RUN_WIRE_KEYS, name
            assert row["status"] == expected_status[name], name
        # phase passes through (worker-reported or the manager's "landing").
        assert rows[ids["landing"]]["phase"] == "landing"
        # Terminal scenarios carry finished_at; live ones don't.
        assert rows[ids["landed"]]["finished_at"] is not None
        assert rows[ids["expired"]]["finished_at"] is not None
        assert rows[ids["running"]]["finished_at"] is None
        assert rows[ids["landed"]]["commit_sha"] == "abc123"

        # The ?queue= filter (the endpoint's only filter param) still shapes
        # identically; the seeded attempts live in the default ("main") queue.
        filtered = client.get("/api/runs?queue=main").json()
        assert filtered and all(list(r) == RUN_WIRE_KEYS for r in filtered)


def test_api_analytics_runs_landed_flag_and_shape(tmp_path: Path) -> None:
    """The analytics endpoint exposes an explicit `landed` flag (true only for
    the LANDED state, not NO_CHANGE) so the cost-per-landed-change KPI can
    separate a real change from a no-change completion — the distinction
    /api/runs deliberately collapses."""
    from nightshift.manager.views import ANALYTICS_RUN_KEYS

    root = _seed(tmp_path, {})
    store = SqliteStore()
    with _client(root, store) as client:
        ids = _seed_every_state(store)
        rows = {r["id"]: r for r in client.get("/api/analytics/runs").json()}
        assert set(ids.values()) <= set(rows)
        for rid, row in rows.items():
            assert list(row) == list(ANALYTICS_RUN_KEYS), rid
        # Only the LANDED attempt is a landed change.
        assert rows[ids["landed"]]["landed"] is True
        assert rows[ids["no_change"]]["landed"] is False
        assert rows[ids["failed"]]["landed"] is False
        # status still projects the same coarse view (both completed).
        assert rows[ids["landed"]]["status"] == "completed"
        assert rows[ids["no_change"]]["status"] == "completed"
        # A future `since` bound returns nothing (time-window filter wired).
        empty = client.get("/api/analytics/runs?since=2999-01-01T00:00:00%2B00:00").json()
        assert empty == []


def test_api_leases_snapshot_live_only_with_lease_keys(tmp_path: Path) -> None:
    root = _seed(tmp_path, {})
    store = SqliteStore()
    with _client(root, store) as client:
        ids = _seed_every_state(store)
        leases = client.get("/api/leases").json()
        # Live attempts only: RUNNING + LANDING (RESOLVING never held a lease).
        assert {le["id"] for le in leases} == {ids["running"], ids["landing"]}
        for le in leases:
            assert list(le) == LEASE_WIRE_KEYS
            assert le["status"] == "leased"
            assert le["run_id"] == le["id"]
            assert le["expires_at"] is not None
            assert le["released_at"] is None


def test_sse_snapshot_frame_carries_the_same_run_and_lease_shapes(
    tmp_path: Path,
) -> None:
    root = _seed(tmp_path, {})
    store = SqliteStore()
    with _client(root, store) as client:
        ids = _seed_every_state(store)
        # The endpoint ends its stream once the (fake) server reports shutdown;
        # with should_exit already True we get exactly the snapshot frame and a
        # clean close (TestClient cannot abort an endless SSE response).
        client.app.state.uvicorn_server = SimpleNamespace(should_exit=True)
        frame: dict[str, Any] | None = None
        with client.stream("GET", "/api/events") as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    frame = json.loads(line[len("data: "):])
                    break
        assert frame is not None and frame["type"] == "snapshot"
        assert {r["id"] for r in frame["runs"]} >= set(ids.values())
        for row in frame["runs"]:
            assert list(row) == RUN_WIRE_KEYS
        assert {le["id"] for le in frame["leases"]} == {
            ids["running"], ids["landing"],
        }
        for le in frame["leases"]:
            assert list(le) == LEASE_WIRE_KEYS


# --------------------------------------------------------------------------- #
# Workflows — Phase 6: submit wiring, artifact custody, cursor advance
# --------------------------------------------------------------------------- #

_TASKS_ROOT_NAME = "nightshift-tasks"


def _poll(client: TestClient, task: str | None = None) -> dict[str, Any] | None:
    order = client.post(
        "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
    ).json()["work"]
    if task is not None:
        assert order is not None and order["task"] == task, order
    return order


def _submit_doc(
    client: TestClient, order: dict[str, Any], *,
    document: str, signal: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "worker_id": "w1", "lease_id": order["lease_id"], "task": order["task"],
        "queue": "main", "title": order["task"], "status": "completed",
        "landable": False, "backend": "claude-code", "model": "claude-sonnet-4-6",
        "document": document,
    }
    if signal is not None:
        payload["signal"] = signal
    return client.post(
        f"/api/worker/runs/{order['run_id']}/submit", json=payload,
    ).json()


def _step_of(root: Path, task: str) -> tuple[str | None, str | None]:
    fm = read_task(root / _TASKS_ROOT_NAME, task)["frontmatter"]
    return fm.get("workflow_step"), fm.get("workflow_visits")


def _wf_seed(tmp_path: Path, workflow: str, task: str = "10.feature") -> Path:
    brief = f"---\nworkflow: {workflow}\npriority: 1\n---\nBuild the feature."
    return _seed(tmp_path, {task: brief})


def test_workflow_doc_step_commits_artifact_and_advances_cursor(
    tmp_path: Path,
) -> None:
    root = _wf_seed(tmp_path, "plan-review-implement")
    tasks_root = root / _TASKS_ROOT_NAME
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # Dispatch stamps the first step + visit; the order embeds the doc block.
        order = _poll(client, "10.feature")
        wf = order["config"]["workflow"]
        assert wf["name"] == "plan-review-implement"
        assert wf["step"] == "plan" and wf["kind"] == "doc"
        assert wf["output"] == "plan"
        assert _step_of(root, "10.feature") == ("plan", "plan:1")

        # A doc submit commits the artifact and advances the cursor to review.
        resp = _submit_doc(client, order, document="# The Plan\nDo X then Y.")
        assert resp["status"] == "completed" and resp["workflow_step"] == "review"
        arts = read_artifacts(tasks_root, "10.feature", ["plan"])
        assert arts["plan"].startswith("# The Plan")
        # Entry-based counting: the visit map accumulates every step entered.
        assert _step_of(root, "10.feature") == ("review", "plan:1,review:1")

        # The next poll dispatches step 2 with the plan artifact embedded.
        order2 = _poll(client, "10.feature")
        wf2 = order2["config"]["workflow"]
        assert wf2["step"] == "review"
        assert wf2["artifacts"]["plan"].startswith("# The Plan")


def test_workflow_doc_signal_skips_to_declared_destination(tmp_path: Path) -> None:
    root = _wf_seed(tmp_path, "plan-review-implement")
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = _poll(client, "10.feature")
        # plan emits `plan-trivial` → declared route to implement (skip review/revise).
        resp = _submit_doc(
            client, order, document="Trivial.", signal="plan-trivial",
        )
        assert resp["workflow_step"] == "implement"
        assert _step_of(root, "10.feature")[0] == "implement"


def test_workflow_undeclared_signal_is_ignored(tmp_path: Path) -> None:
    root = _wf_seed(tmp_path, "plan-review-implement")
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = _poll(client, "10.feature")
        # `review-clear` is not declared on the plan step → route via next (review).
        resp = _submit_doc(
            client, order, document="Plan.", signal="review-clear",
        )
        assert resp["workflow_step"] == "review"


def test_workflow_doc_missing_document_is_worker_error(tmp_path: Path) -> None:
    root = _wf_seed(tmp_path, "plan-review-implement")
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = _poll(client, "10.feature")
        resp = client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"],
                "task": "10.feature", "queue": "main", "title": "10.feature",
                "status": "completed", "landable": False,
            },
        ).json()
        # No document → coerced to a worker error; cursor stays on plan.
        assert resp.get("landed") in (False, None)
        assert _step_of(root, "10.feature")[0] == "plan"


def test_workflow_oversized_document_is_worker_error(tmp_path: Path) -> None:
    root = _wf_seed(tmp_path, "plan-review-implement")
    tasks_root = root / _TASKS_ROOT_NAME
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = _poll(client, "10.feature")
        resp = _submit_doc(client, order, document="x" * (256 * 1024 + 1))
        # Over-cap coerces to worker_error before the transition: cursor unmoved,
        # no artifact committed.
        assert resp.get("landed") in (False, None)
        assert _step_of(root, "10.feature")[0] == "plan"
        assert not artifacts_dir(tasks_root, "10.feature").exists()


def test_workflow_code_step_lands_and_consumes_at_end(tmp_path: Path) -> None:
    root = _wf_seed(tmp_path, "plan-review-implement")
    tasks_root = root / _TASKS_ROOT_NAME
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # Walk the three doc steps to reach the implement (code, $end) step.
        order = _poll(client, "10.feature")
        _submit_doc(client, order, document="Plan.")
        order = _poll(client, "10.feature")
        _submit_doc(client, order, document="Review.")
        order = _poll(client, "10.feature")
        _submit_doc(client, order, document="Revised plan.")

        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "implement"
        assert order["config"]["workflow"]["kind"] == "code"
        # The implement step embeds the plan artifact for the implementor.
        assert order["config"]["workflow"]["artifacts"]["plan"].strip() == "Revised plan."

        # A landing code step at $end lands and consumes the brief + artifacts.
        wt = setup_worktree(root, order["repo"], "10.feature")
        (wt / "GENERATED.txt").write_text("done\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "impl"], cwd=wt, check=True, capture_output=True)
        resp = _submit_completed(client, order).json()
        # Async land: the response queues; draining runs the completion, which
        # drops the brief + artifacts (non-evergreen $end from a code step).
        assert resp.get("queued") is True
        _drain_git(client)
        assert not (tasks_root / "main" / "10.feature.md").exists()
        assert not artifacts_dir(tasks_root, "10.feature").exists()


def _install_workflow(root: Path, definition: dict[str, Any]) -> None:
    """Drop an operator workflow definition so ``load_workflows`` picks it up
    at ``create_app`` time (call before ``_client``)."""
    wf_dir = root / ".nightshift" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / f"{definition['name']}.json").write_text(json.dumps(definition))


# A looping-verify definition (spec §12): implement routes back to verify until
# verify signals `verify-clear`. Every cyclic step declares max_visits.
_VERIFY_LOOP_DEF = {
    "name": "verify-loop",
    "steps": [
        {
            "id": "implement", "kind": "code", "role": "implementor",
            "inputs": ["brief"], "max_turns": None, "next": "verify",
            "max_visits": 3,
        },
        {
            "id": "verify", "kind": "doc", "role": "implementor",
            "prompt": "workflow-verify.md", "inputs": ["brief"], "output": "gaps",
            "max_turns": 30, "signals": {"verify-clear": "$end"},
            "next": "gap-plan", "max_visits": 3,
        },
        {
            "id": "gap-plan", "kind": "doc", "role": "planner",
            "prompt": "workflow-gap-plan.md", "inputs": ["brief", "gaps"],
            "output": "plan", "max_turns": 30, "next": "implement",
            "max_visits": 3,
        },
    ],
}


def test_workflow_verify_loop_round_trip(tmp_path: Path) -> None:
    """Spec §12: a looping-verify definition runs on the engine unmodified —
    implement lands → verify → gap-plan → implement, with visits counted on
    each cursor entry."""
    root = _seed(tmp_path, {
        "10.feature": "---\nworkflow: verify-loop\npriority: 1\n---\nBuild it.",
    })
    _install_workflow(root, _VERIFY_LOOP_DEF)
    tasks_root = root / _TASKS_ROOT_NAME
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # First step is a code step (implement); land it → cursor enters verify.
        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "implement"
        assert _step_of(root, "10.feature") == ("implement", "implement:1")
        wt = setup_worktree(root, order["repo"], "10.feature")
        (wt / "GENERATED.txt").write_text("v1\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "v1"], cwd=wt, check=True, capture_output=True)
        assert _submit_completed(client, order).json().get("queued") is True
        _drain_git(client)
        assert _step_of(root, "10.feature") == ("verify", "implement:1,verify:1")

        # Verify finds gaps (no clear signal) → routes to gap-plan.
        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "verify"
        _submit_doc(client, order, document="Gap: missing tests.")
        assert _step_of(root, "10.feature")[0] == "gap-plan"

        # gap-plan's default next re-enters implement (second entry).
        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "gap-plan"
        _submit_doc(client, order, document="Plan: add tests.")
        step, visits = _step_of(root, "10.feature")
        assert step == "implement"
        assert "implement:2" in visits

        # A second verify pass that clears routes to $end and consumes the brief.
        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "implement"
        wt = setup_worktree(root, order["repo"], "10.feature")
        (wt / "GENERATED.txt").write_text("v2\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "v2"], cwd=wt, check=True, capture_output=True)
        assert _submit_completed(client, order).json().get("queued") is True
        _drain_git(client)
        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "verify"
        _submit_doc(client, order, document="All good.", signal="verify-clear")
        # $end from a doc step: non-evergreen → brief retained as completed,
        # the final artifact kept (spec §6.3).
        assert (tasks_root / "main" / "10.feature.md").exists()


def test_workflow_budget_quarantine_on_entry(tmp_path: Path) -> None:
    """Entering a step whose max_visits is exhausted quarantines the task
    (the work still commits)."""
    definition = {
        "name": "tight-loop",
        "steps": [
            {
                "id": "plan", "kind": "doc", "role": "planner",
                "prompt": "workflow-plan.md", "inputs": ["brief"],
                "output": "plan", "max_turns": 30, "next": "implement",
                "max_visits": 1,
            },
            {
                "id": "implement", "kind": "code", "role": "implementor",
                "inputs": ["brief", "plan"], "max_turns": None, "next": "plan",
                "max_visits": 2,
            },
        ],
    }
    root = _seed(tmp_path, {
        "10.feature": "---\nworkflow: tight-loop\npriority: 1\n---\nBuild it.",
    })
    _install_workflow(root, definition)
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        # plan(1) → implement(1); implement's next loops to plan, whose budget
        # (max_visits=1) is already spent → entering it again quarantines.
        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "plan"
        _submit_doc(client, order, document="Plan.")
        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "implement"
        wt = setup_worktree(root, order["repo"], "10.feature")
        (wt / "GENERATED.txt").write_text("v1\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "v1"], cwd=wt, check=True, capture_output=True)
        assert _submit_completed(client, order).json().get("queued") is True
        _drain_git(client)
        blocked = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert "10.feature" in blocked
        assert "budget exhausted" in blocked["10.feature"]["blocked_reason"]


def test_workflow_quarantine_clear_resumes_at_recorded_step(tmp_path: Path) -> None:
    """A quarantine leaves the cursor untouched; clearing it re-dispatches the
    task at its recorded ``workflow_step`` (spec §6.5)."""
    root = _seed(tmp_path, {
        "10.feature": "---\nworkflow: plan-review-implement\npriority: 1\n---\nBuild it.",
    })
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        # Advance to review, then quarantine the task there via the worker flag.
        order = _poll(client, "10.feature")
        _submit_doc(client, order, document="Plan.")
        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "review"
        client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"],
                "task": "10.feature", "queue": "main", "title": "10.feature",
                "status": "error", "landable": False, "quarantine": True,
                "failure_kind": "worker_error", "failure_reason": "boom",
                "backend": "claude-code", "model": "claude-sonnet-4-6",
            },
        )
        # Cursor unmoved by the failure; task not served while quarantined.
        assert _step_of(root, "10.feature")[0] == "review"
        assert _poll(client) is None

        # Operator clears the quarantine → the task resumes at review.
        client.patch("/api/tasks/10.feature", json={"quarantined": False})
        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "review"


def test_workflow_evergreen_end_resets_cursor_and_artifacts(tmp_path: Path) -> None:
    """Evergreen $end (spec §6.5): the brief is retained, but the engine meta
    lane is cleared and artifacts deleted, so the next dispatch restarts at
    the first step."""
    root = _seed(tmp_path, {
        "10.feature": (
            "---\nworkflow: verify-loop\nevergreen: true\npriority: 1\n---\nWatch it."
        ),
    })
    _install_workflow(root, _VERIFY_LOOP_DEF)
    tasks_root = root / _TASKS_ROOT_NAME
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        # implement(code) lands → verify.
        order = _poll(client, "10.feature")
        wt = setup_worktree(root, order["repo"], "10.feature")
        (wt / "GENERATED.txt").write_text("v1\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "v1"], cwd=wt, check=True, capture_output=True)
        _submit_completed(client, order)
        _drain_git(client)
        # verify clears immediately → $end from a doc step, evergreen reset.
        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "verify"
        _submit_doc(client, order, document="All good.", signal="verify-clear")

        assert (tasks_root / "main" / "10.feature.md").exists()
        # Engine meta cleared and artifacts gone → next dispatch restarts at
        # the first step.
        assert _step_of(root, "10.feature") == (None, None)
        assert not artifacts_dir(tasks_root, "10.feature").exists()
        order = _poll(client, "10.feature")
        assert order["config"]["workflow"]["step"] == "implement"
        assert _step_of(root, "10.feature") == ("implement", "implement:1")


def test_workflow_unknown_definition_blocks_task(tmp_path: Path) -> None:
    root = _wf_seed(tmp_path, "no-such-workflow")
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        # The candidate carries a workflow_error → the task is blocked, not served.
        assert _poll(client) is None
        blocked = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert "10.feature" in blocked
        assert "no-such-workflow" in blocked["10.feature"]["blocked_reason"]


def test_non_workflow_submit_untouched(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.plain": "---\npriority: 1\n---\nDo a thing."})
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = _poll(client, "10.plain")
        assert "workflow" not in order["config"]
        resp = client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"], "task": "10.plain",
                "queue": "main", "title": "10.plain", "status": "completed",
                "landable": False, "backend": "claude-code", "model": "claude-sonnet-4-6",
            },
        ).json()
        assert "workflow_step" not in resp
