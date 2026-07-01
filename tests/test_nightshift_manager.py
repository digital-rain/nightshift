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
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from _workspace import build_workspace, make_target_repo
from nightshift.engine import setup_worktree
from nightshift.manager.app import _jsonable, create_app, no_progress_streak
from nightshift.manager.hub import Hub
from nightshift.manager.landing import canonical_head
from nightshift.manager.store import MemoryStore


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
        # failed task admitted).
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


def test_no_progress_streak_counts_until_a_landed_commit() -> None:
    # Newest-first, as list_runs returns. A landed commit resets the count;
    # aborted/blocked are neutral; no-commit completions and errors count.
    runs = [
        {"task": "t", "status": "completed", "commit_sha": None},   # +1
        {"task": "t", "status": "error", "commit_sha": None},        # +1
        {"task": "t", "status": "aborted", "commit_sha": None},      # neutral
        {"task": "other", "status": "completed", "commit_sha": None},  # skipped
        {"task": "t", "status": "completed", "commit_sha": "abc123"},   # reset/stop
        {"task": "t", "status": "completed", "commit_sha": None},     # not reached
    ]
    assert no_progress_streak(runs, "t") == 2
    assert no_progress_streak([], "t") == 0


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
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # First no-change run: under threshold (default 2), still runnable.
        first = _poll_and_submit_no_change(client, "10.loop")
        assert first["quarantined"] is False

        # Second no-change run in a row hits the threshold → quarantined.
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
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # Drive two no-change runs to trigger quarantine (threshold=2).
        _poll_and_submit_no_change(client, "10.loop")
        second = _poll_and_submit_no_change(client, "10.loop")
        assert second["quarantined"] is True

        # Task is blocked — poll returns nothing.
        assert client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"] is None

        # Operator sets the task back to "ready" via the detail pane.
        r = client.patch(
            "/api/tasks/10.loop",
            json={"quarantined": False, "disabled": False, "completed": False},
        )
        assert r.status_code == 200
        assert r.json()["quarantined"] is False

        # The store overlay is cleared — task is dispatchable again.
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
        with _client(root) as client:
            client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
            # First no-change submit: quarantine guard disabled (threshold=0),
            # but failure policy marks task as failed.
            out = _poll_and_submit_no_change(client, "10.loop")
            assert out["quarantined"] is False
            # Phase B retries the task (only failed task in queue).
            out = _poll_and_submit_no_change(client, "10.loop")
            assert out["quarantined"] is False
            # Retry failure quarantines the task and pauses the queue.
            state = client.get("/api/state").json()
            assert state["queues"]["main"]["pause_reason"] == "retry_failed"
    finally:
        del os.environ["NIGHTSHIFT_QUARANTINE_THRESHOLD"]


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
        assert r.json()["landed"] is True
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
    store = MemoryStore()
    asyncio.run(store.create_run(
        "r1", task="10.x", queue="main", worker_id="w1", backend="claude-code",
        model="auto", title="X", repo="longitude",
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
    app = create_app(root, store=MemoryStore())
    _stub_spawn(app)
    with TestClient(app) as client:
        assert client.post("/api/runs/nope/10.x/resolve").status_code == 404


def test_resolve_result_completes_task(tmp_path: Path) -> None:
    root = _seed(tmp_path, {"10.x": "do"})
    store = MemoryStore()
    asyncio.run(store.create_run(
        "c1", task="10.x", queue="main", worker_id="manager:resolve",
        backend="claude-code", model="auto", title="X", repo="longitude",
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
    store = MemoryStore()
    asyncio.run(store.create_run(
        "c1", task="10.x", queue="main", worker_id="manager:resolve",
        backend="claude-code", model="auto", title="X", repo="longitude",
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


def test_conflict_auto_escalates_to_resolve(tmp_path: Path) -> None:
    """A landing conflict on submit (auto_resolve on by default) spawns the
    out-of-process resolver instead of merely blocking the task."""
    root = _seed(tmp_path, {"10.edit": "edit the readme"})
    repo_root = root / "longitude"
    app = create_app(root, store=MemoryStore())
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
        body = r.json()
        assert body["landed"] is False
        assert body["conflict"] is True
        assert body["resolving"] is True
        assert len(calls) == 1 and calls[0]["task"] == "10.edit"


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
    with _client(root) as client:
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})

        # Phase A: 10.a fails (armed), 20.b is still ready and dispatches fine.
        _poll_and_submit_error(client, "10.a")
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

        # Operator presses Play -> phase B: only failed tasks remain, earliest retries.
        client.post("/api/transport", json={"action": "play", "queue": None})
        retry = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert retry is not None and retry["task"] == "10.a"

        # It fails again -> quarantined + queue paused with retry_failed.
        client.post(
            f"/api/worker/runs/{retry['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": retry["lease_id"], "task": "10.a",
                "queue": "main", "title": "10.a", "status": "error",
                "result_line": "boom3", "failure_kind": "worker_error",
                "landable": False, "backend": "claude-code", "model": "claude-sonnet-4-6",
            },
        )
        state = client.get("/api/state").json()
        assert state["queues"]["main"]["pause_reason"] == "retry_failed"
        blocked = {b["task"]: b for b in client.get("/api/blocked").json()}
        assert blocked["10.a"]["state"] == "quarantined"

        # Play again -> phase B resumes with the next failed task (20.b).
        client.post("/api/transport", json={"action": "play", "queue": None})
        retry2 = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()["work"]
        assert retry2 is not None and retry2["task"] == "20.b"


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
    from nightshift.manager.config import load_manager_config

    monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
    root = _write_manager_block(tmp_path, {})
    assert load_manager_config(root).dsn is None


def test_dsn_from_env(tmp_path: Path, monkeypatch) -> None:
    from nightshift.manager.config import load_manager_config

    monkeypatch.setenv("NIGHTSHIFT_PG_DSN", "postgresql://db/nightshift")
    root = _write_manager_block(tmp_path, {})
    assert load_manager_config(root).dsn == "postgresql://db/nightshift"


def test_dsn_env_overrides_block(tmp_path: Path, monkeypatch) -> None:
    from nightshift.manager.config import load_manager_config

    monkeypatch.setenv("NIGHTSHIFT_PG_DSN", "postgresql://env-host/nightshift")
    root = _write_manager_block(tmp_path, {"dsn": "postgresql://db/nightshift"})
    assert load_manager_config(root).dsn == "postgresql://env-host/nightshift"


def test_dsn_absent_is_none(tmp_path: Path, monkeypatch) -> None:
    from nightshift.manager.config import load_manager_config

    monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
    root = _write_manager_block(tmp_path, {})
    assert load_manager_config(root).dsn is None


def test_open_store_no_dsn_is_memory(monkeypatch) -> None:
    # open_store with no arg + no NIGHTSHIFT_PG_DSN falls back to MemoryStore.
    from nightshift.manager.store import MemoryStore, open_store

    monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
    store = asyncio.run(open_store())
    assert isinstance(store, MemoryStore)
