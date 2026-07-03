"""Phase 7 verification: async land + reconciler + durable queue pause.

Covers the plan's three named checks — land-under-load (heartbeats keep
flowing while a slow land runs), restart (a paused queue stays paused across
app recreation on the same store; a mid-land run is conservatively parked for
resolve), and the poll-path zero-store-writes assertion — plus unit-ish tests
for each reconciler duty (deadline expiry, hold set/clear, resolve reaping,
terminal worktree/branch GC).
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from _workspace import build_workspace
from nightshift.git.worktrees import setup_worktree
from nightshift.lifecycle import AttemptState, LandKind, LandOutcome
from nightshift.manager.app import create_app
from nightshift.manager.scheduler import (
    UNROUTABLE_REASON_PREFIXES,
    TaskCandidate,
    unroutable,
)
from nightshift.manager.store_sqlite import SqliteStore


def _client(workspace: Path, store: SqliteStore | None = None) -> TestClient:
    return TestClient(create_app(workspace, store=store or SqliteStore()))


def _checkin(client: TestClient, worker_id: str = "w1", **extra: Any) -> None:
    client.post(
        "/api/worker/checkin",
        json={"worker_id": worker_id, "backend": "claude-code", **extra},
    )


def _poll(client: TestClient, worker_id: str = "w1", **extra: Any) -> dict | None:
    return client.post(
        "/api/worker/poll",
        json={"worker_id": worker_id, "backend": "claude-code", **extra},
    ).json()["work"]


def _reconcile(client: TestClient) -> None:
    client.portal.call(client.app.state.reconciler.reconcile_once)


def _drain_git(client: TestClient) -> None:
    client.portal.call(client.app.state.drain_git_jobs)


def _poll_with_landable_branch(
    client: TestClient, workspace: Path, task: str
) -> dict[str, Any]:
    _checkin(client)
    order = _poll(client)
    assert order is not None and order["task"] == task
    wt = setup_worktree(workspace, order["repo"], task)
    (wt / "GENERATED.txt").write_text("done\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "work"], cwd=wt, check=True, capture_output=True)
    return order


# --------------------------------------------------------------------------- #
# Land-under-load: the Phase 0 symptom (a slow land starves heartbeats/polls)
# is structurally gone — the land is a queued executor job, the handler
# returns immediately, and every other endpoint keeps serving.
# --------------------------------------------------------------------------- #


def test_heartbeats_and_polls_flow_while_a_slow_land_runs(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    land_gate = threading.Event()
    land_entered = threading.Event()

    def slow_land(*args: Any, **kwargs: Any) -> LandOutcome:
        land_entered.set()
        assert land_gate.wait(10)
        return LandOutcome(kind=LandKind.LANDED, sha="deadbeef")

    monkeypatch.setattr("nightshift.manager.api_worker.land_locked", slow_land)

    with _client(workspace) as client:
        order = _poll_with_landable_branch(client, workspace, "10.hello")
        r = client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"],
                "task": "10.hello", "queue": "main", "title": order["title"],
                "status": "completed", "landable": True, "backend": "claude-code",
            },
        )
        # The submit returned while the land is still parked inside the job.
        assert r.status_code == 200
        assert r.json() == {"landed": None, "status": "landing", "queued": True}
        assert land_entered.wait(10)
        assert not land_gate.is_set()

        # Heartbeat and poll keep answering while the land is stuck.
        hb = client.post(
            "/api/worker/heartbeat",
            json={"worker_id": "w1", "lease_id": order["lease_id"]},
        )
        assert hb.status_code == 200 and hb.json()["ok"] is True
        assert _poll(client) is None  # only task is leased; response is prompt

        # The run is visibly mid-land: still running, phase "landing".
        run = next(
            x for x in client.get("/api/runs").json() if x["id"] == order["run_id"]
        )
        assert run["status"] == "running" and run["phase"] == "landing"

        # Release the land; the deferred on_land_result transition applies.
        land_gate.set()
        _drain_git(client)
        run = next(
            x for x in client.get("/api/runs").json() if x["id"] == order["run_id"]
        )
        assert run["status"] == "completed" and run["commit_sha"] == "deadbeef"
        attempt = asyncio.run(
            client.app.state.store.get_attempt(order["lease_id"])
        )
        assert attempt["state"] == "landed"
        # The brief drop (a tasks-repo executor job) also went through.
        assert not (workspace / "nightshift-tasks/main/10.hello.md").exists()


def _gated_land(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """Patch the land job with one parked on a gate; returns (entered, gate)."""
    gate = threading.Event()
    entered = threading.Event()

    def slow_land(*args: Any, **kwargs: Any) -> LandOutcome:
        entered.set()
        assert gate.wait(10)
        return LandOutcome(kind=LandKind.LANDED, sha="deadbeef")

    monkeypatch.setattr("nightshift.manager.api_worker.land_locked", slow_land)
    return entered, gate


def test_mid_land_attempt_is_exempt_from_deadline_expiry(
    tmp_path: Path, monkeypatch
) -> None:
    """Nothing heartbeats an attempt once the worker went idle at enqueue, so
    queue-wait + a slow land can outlive the TTL — with the push possibly
    already on origin. The reconciler must not expire a LANDING attempt (that
    would make the deferred CAS drop the completed land and re-dispatch the
    task); since Phase 8 the exemption is structural — expiry only CASes
    RUNNING attempts. The startup recovery pass owns mid-land casualties."""
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    land_entered, land_gate = _gated_land(monkeypatch)
    store = SqliteStore()

    with _client(workspace, store) as client:
        order = _poll_with_landable_branch(client, workspace, "10.hello")
        r = client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"],
                "task": "10.hello", "queue": "main", "title": order["title"],
                "status": "completed", "landable": True, "backend": "claude-code",
            },
        )
        assert r.json()["queued"] is True
        assert land_entered.wait(10)

        # The TTL elapses while the land job is parked; the tick must skip it.
        asyncio.run(store.heartbeat_attempt(order["lease_id"], -60))
        _reconcile(client)
        attempt = asyncio.run(store.get_attempt(order["lease_id"]))
        assert attempt["state"] == "landing"

        # And the land result still applies once the job finishes.
        land_gate.set()
        _drain_git(client)
        run = next(
            x for x in client.get("/api/runs").json() if x["id"] == order["run_id"]
        )
        assert run["status"] == "completed" and run["commit_sha"] == "deadbeef"
        attempt = asyncio.run(store.get_attempt(order["lease_id"]))
        assert attempt["state"] == "landed"


def test_discarded_land_result_leaves_an_operator_visible_trace(
    tmp_path: Path, monkeypatch
) -> None:
    """When an attempt IS consumed mid-land (operator stop/skip), the deferred
    CAS refuses the result — correctly — but the discard must not be silent:
    the run's event log records what was thrown away (existing task_log wire
    kind, no new vocabulary)."""
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    land_entered, land_gate = _gated_land(monkeypatch)
    store = SqliteStore()

    with _client(workspace, store) as client:
        order = _poll_with_landable_branch(client, workspace, "10.hello")
        client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"],
                "task": "10.hello", "queue": "main", "title": order["title"],
                "status": "completed", "landable": True, "backend": "claude-code",
            },
        )
        assert land_entered.wait(10)
        # An operator stop aborts the attempt while the land job is parked
        # (the Phase 8 stop fix: ABORTED, terminal, instead of a cancelled
        # lease around a zombie row).
        asyncio.run(store.update_attempt(order["lease_id"], state=AttemptState.ABORTED))
        land_gate.set()
        _drain_git(client)

        # The CAS refused the stale result: the abort stands, nothing landed...
        run = next(
            x for x in client.get("/api/runs").json() if x["id"] == order["run_id"]
        )
        assert run["status"] == "aborted"
        assert run["commit_sha"] is None
        # ...but the discard is on the record, with the land kind and sha.
        logs = [
            e for e in asyncio.run(store.run_events(order["run_id"]))
            if e["kind"] == "task_log"
            and "land result discarded" in (e.get("payload") or {}).get("line", "")
        ]
        assert len(logs) == 1
        assert logs[0]["payload"]["land_kind"] == "landed"
        assert logs[0]["payload"]["sha"] == "deadbeef"


# --------------------------------------------------------------------------- #
# Restart: durable queue pause + conservative parking of a mid-land run.
# --------------------------------------------------------------------------- #


def test_paused_queue_stays_paused_across_manager_restart(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    store = SqliteStore()

    with _client(workspace, store) as client:
        client.post("/api/transport", json={"action": "pause", "queue": None})
        state = client.get("/api/state").json()
        assert state["queues"]["main"]["state"] == "paused"
        assert state["queues"]["main"]["pause_reason"] == "operator"

    # A new app instance on the same store: the pause survived (pre-Phase 7 it
    # lived in app.state and a restart silently unpaused the queue).
    with _client(workspace, store) as client:
        state = client.get("/api/state").json()
        assert state["queues"]["main"]["state"] == "paused"
        assert state["queues"]["main"]["pause_reason"] == "operator"
        _checkin(client)
        r = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
        ).json()
        assert r["work"] is None
        assert r["queue_pauses"] == {"main": "operator"}


def test_restart_parks_mid_land_run_for_resolve(tmp_path: Path) -> None:
    """A LANDING attempt found at startup is a previous process's abandoned
    executor job. With no `Nightshift-Attempt` trailer on main AND no surviving
    task branch to re-enqueue from, the recovery ladder bottoms out at the
    conservative park: attempt CONFLICT (merge_rejected), task blocked for
    resolve — never lost, never double-landed."""
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    store = SqliteStore()

    async def _seed_mid_land() -> str:
        run_id = "run-mid-land"
        await store.create_attempt(
            run_id, task="10.hello", queue=None, worker_id="w1",
            backend="claude-code", model="auto", base_ref=None,
            ttl_seconds=600, title="hello", repo="longitude",
        )
        await store.update_attempt(
            run_id, state=AttemptState.LANDING, phase="landing",
        )
        return run_id

    run_id = asyncio.run(_seed_mid_land())

    # "Restart": a fresh app on the same store; the lifespan startup pass runs
    # before any request is served.
    with _client(workspace, store) as client:
        run = next(x for x in client.get("/api/runs").json() if x["id"] == run_id)
        assert run["status"] == "error"
        assert run["failure_kind"] == "merge_rejected"
        assert "restarted mid-land" in run["result_line"]
        attempt = asyncio.run(store.get_attempt(run_id))
        assert attempt["state"] == "conflict"
        assert attempt["finished_at"] is not None
        hold = asyncio.run(store.get_task_state(None, "10.hello"))
        assert hold["state"] == "blocked"
        assert hold["blocked_reason"].startswith("needs resolve:")
        # The brief is preserved (nothing was dropped or duplicated).
        assert (workspace / "nightshift-tasks/main/10.hello.md").exists()

        events_before = [
            e["kind"]
            for e in asyncio.run(store.run_events(run_id))
        ]
        assert "task_result" in events_before

        # Idempotent: a second startup pass on the (now terminal) attempt is a
        # no-op — the CAS on LANDING refuses to double-apply.
        client.portal.call(client.app.state.reconciler.startup)
        assert [
            e["kind"] for e in asyncio.run(store.run_events(run_id))
        ] == events_before


# --------------------------------------------------------------------------- #
# Poll path: a no-work poll performs ZERO store writes (reclaim, reaping, and
# hold writes all moved to the reconciler).
# --------------------------------------------------------------------------- #

# Every store method a no-work poll may touch — all reads. Anything else
# (including reads added later without thought) fails loudly.
_POLL_READS = frozenset({
    "queue_pauses", "queue_dedication", "live_attempts", "list_blocked",
    "tasks_backing_off", "retryable_tasks", "get_task_state",
})


class _ReadOnlyWhenArmed(SqliteStore):
    armed = False
    calls: list[str] = []

    def __getattribute__(self, name: str) -> Any:
        attr = super().__getattribute__(name)
        if (
            not name.startswith("_")
            and callable(attr)
            and super().__getattribute__("armed")
        ):
            _ReadOnlyWhenArmed.calls.append(name)
            if name not in _POLL_READS:
                raise AssertionError(
                    f"no-work poll called store.{name}(); the hot path is reads only"
                )
        return attr


def test_no_work_poll_performs_zero_store_writes(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, tasks={})  # nothing to dispatch
    store = _ReadOnlyWhenArmed()
    _ReadOnlyWhenArmed.calls = []
    with _client(workspace, store) as client:
        _checkin(client)  # registry writes happen here, before arming
        store.armed = True
        try:
            r = client.post(
                "/api/worker/poll",
                json={"worker_id": "w1", "backend": "claude-code"},
            )
        finally:
            store.armed = False
        assert r.status_code == 200
        assert r.json()["work"] is None
        # The wrapper actually observed the poll (guards against a vacuous pass).
        assert "live_attempts" in _ReadOnlyWhenArmed.calls


# --------------------------------------------------------------------------- #
# Reconciler duties, one by one.
# --------------------------------------------------------------------------- #


def test_reconciler_expires_overdue_attempts(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    store = SqliteStore()
    with _client(workspace, store) as client:
        _checkin(client)
        order = _poll(client)
        assert order is not None
        # Age the attempt past its deadline (a negative TTL back-dates expiry).
        asyncio.run(store.heartbeat_attempt(order["lease_id"], -60))

        _reconcile(client)

        attempt = asyncio.run(store.get_attempt(order["lease_id"]))
        assert attempt["state"] == "expired"
        # Phase 8 behavior fix: EXPIRED is terminal (finished_at stamped) and
        # /api/runs truthfully projects "expired" — pre-phase the run row
        # stayed "running" with finished_at NULL forever.
        assert attempt["finished_at"] is not None
        run = next(
            x for x in client.get("/api/runs").json() if x["id"] == order["run_id"]
        )
        assert run["status"] == "expired"
        # The task is dispatchable again.
        again = _poll(client)
        assert again is not None and again["task"] == "10.hello"


def test_reconciler_sets_then_clears_no_capable_worker_hold(tmp_path: Path) -> None:
    workspace = build_workspace(
        tmp_path, tasks={"10.ml": "---\nmodel: llama3.1\n---\nNeeds ollama."}
    )
    with _client(workspace) as client:
        # The startup pass already ran with no capable worker online.
        hold = asyncio.run(client.app.state.store.get_task_state(None, "10.ml"))
        assert hold["state"] == "blocked"
        assert "no live worker provides" in hold["blocked_reason"]

        # A worker advertising the pinned model checks in; the next tick clears
        # the hold (the reconciler owns un-setting what it set).
        _checkin(client, "w-ollama", models=["llama3.1"])
        _reconcile(client)
        hold = asyncio.run(client.app.state.store.get_task_state(None, "10.ml"))
        assert hold is None or hold.get("state") is None
        order = _poll(client, "w-ollama", models=["llama3.1"])
        assert order is not None and order["task"] == "10.ml"


def test_reconciler_sets_then_clears_dedicated_offline_hold(tmp_path: Path) -> None:
    """The second unroutable-hold flavor (queue dedicated only to offline
    workers) follows the same set/clear lifecycle as the pinned-model one."""
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    with _client(workspace) as client:
        _checkin(client)  # w1 online, but the queue is about to be dedicated
        client.put("/api/queue/dedication", json={"worker_ids": ["w-away"]})
        _reconcile(client)
        hold = asyncio.run(client.app.state.store.get_task_state(None, "10.hello"))
        assert hold["state"] == "blocked"
        assert hold["blocked_reason"].startswith("queue '")
        assert "dedicated to offline" in hold["blocked_reason"]
        assert _poll(client) is None

        # The dedicated worker comes online; the next tick clears the hold.
        _checkin(client, "w-away")
        _reconcile(client)
        hold = asyncio.run(client.app.state.store.get_task_state(None, "10.hello"))
        assert hold is None or hold.get("state") is None
        order = _poll(client, "w-away")
        assert order is not None and order["task"] == "10.hello"


def test_unroutable_reasons_match_the_exported_prefixes() -> None:
    """Drift check for the reconciler <-> scheduler coupling: every reason
    unroutable() can produce must start with one of the exported prefixes the
    reconciler auto-clears by, and every prefix must be producible — rewording
    a reason string without going through scheduler's constructors fails here
    before it silently breaks hold clearing."""
    candidates = {
        None: [
            TaskCandidate(queue=None, task="10.model", priority=5, model="pinned-x"),
            TaskCandidate(
                queue=None, task="20.mcp", priority=5, model="auto",
                required_mcps=("jira",),
            ),
            TaskCandidate(queue=None, task="30.dedicated", priority=5, model="auto"),
        ]
    }
    pairs = unroutable(
        candidates,
        available_models=set(),
        available_mcps=set(),
        dedication={"main": ["w-offline"]},
        online_workers=set(),
    )
    # All three axes fire: unadvertised model, unadvertised connector,
    # dedicated-to-offline queue.
    assert {c.task for c, _ in pairs} == {"10.model", "20.mcp", "30.dedicated"}
    for _, reason in pairs:
        assert reason.startswith(UNROUTABLE_REASON_PREFIXES), reason
    for prefix in UNROUTABLE_REASON_PREFIXES:
        assert any(reason.startswith(prefix) for _, reason in pairs), prefix


def test_reconciler_sets_then_clears_repo_unavailable_hold(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    hidden = tmp_path / "longitude-hidden"
    (workspace / "longitude").rename(hidden)

    with _client(workspace) as client:
        # Startup pass: the repo is gone -> the task is paused, not blocked.
        hold = asyncio.run(client.app.state.store.get_task_state(None, "10.hello"))
        assert hold["state"] == "repo_unavailable"
        _checkin(client)
        assert _poll(client) is None

        # The repo reappears (re-cloned); the next tick clears the pause.
        hidden.rename(workspace / "longitude")
        _reconcile(client)
        hold = asyncio.run(client.app.state.store.get_task_state(None, "10.hello"))
        assert hold is None or hold.get("state") is None
        order = _poll(client)
        assert order is not None and order["task"] == "10.hello"


def test_rescan_resets_repo_warning_dedup_for_the_reconciler(tmp_path: Path) -> None:
    """/api/repos/rescan promises "re-warn from scratch". The dedup set is
    shared by reference with the reconciler, so the reset must mutate it in
    place — rebinding app.state would leave the reconciler deduping against a
    stale object and the re-warn would silently never happen."""
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    (workspace / "longitude").rename(tmp_path / "longitude-hidden")
    store = SqliteStore()

    def _warning_count() -> int:
        return sum(
            1 for e in asyncio.run(store.events_since(0))
            if e["kind"] == "repo_unavailable"
        )

    with _client(workspace, store) as client:
        # Startup pass warned once; further ticks dedup.
        assert _warning_count() == 1
        _reconcile(client)
        assert _warning_count() == 1

        # Rescan (repo still missing) resets the dedup; the next tick re-warns.
        client.post("/api/repos/rescan")
        _reconcile(client)
        assert _warning_count() == 2


class _FakeProc:
    def __init__(self, exited: bool) -> None:
        self._exited = exited

    def poll(self) -> int | None:
        return 0 if self._exited else None


def test_reconciler_reaps_finished_resolves(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, tasks={})
    with _client(workspace) as client:
        client.app.state.resolves.update({
            "run-done": {"proc": _FakeProc(exited=True), "repo": "longitude",
                         "task": "10.a", "queue": None, "origin_run_id": None},
            "run-live": {"proc": _FakeProc(exited=False), "repo": "longitude",
                         "task": "20.b", "queue": None, "origin_run_id": None},
        })
        _reconcile(client)
        assert set(client.app.state.resolves) == {"run-live"}


def test_one_failing_duty_does_not_starve_the_rest_of_the_tick(
    tmp_path: Path, monkeypatch
) -> None:
    """Duty isolation: a duty that raises is logged and the remaining duties
    still run in the same tick — the loop never dies."""
    workspace = build_workspace(tmp_path, tasks={})
    with _client(workspace) as client:
        reconciler = client.app.state.reconciler

        async def boom() -> None:
            raise RuntimeError("deadline duty exploded")

        monkeypatch.setattr(reconciler, "_expire_deadlines", boom)
        client.app.state.resolves["run-done"] = {
            "proc": _FakeProc(exited=True), "repo": "longitude",
            "task": "10.a", "queue": None, "origin_run_id": None,
        }
        _reconcile(client)  # must not raise
        # A later duty (resolve reaping) still did its work.
        assert client.app.state.resolves == {}


def _task_local_branches(repo_root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/task-local/"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def test_reconciler_gcs_abandoned_worktrees_but_never_live_work(tmp_path: Path) -> None:
    """A task-local branch whose brief is gone, with no lease and no hold, is
    provably abandoned -> torn down. A branch whose brief is still queued (or
    whose task is held for resolve) is live -> untouched."""
    workspace = build_workspace(tmp_path, tasks={"10.keep": "Still queued."})
    repo_root = workspace / "longitude"
    with _client(workspace) as client:
        wt_gone = setup_worktree(workspace, "longitude", "99.gone")
        wt_keep = setup_worktree(workspace, "longitude", "10.keep")
        assert len(_task_local_branches(repo_root)) == 2

        _reconcile(client)

        assert _task_local_branches(repo_root) == ["task-local/main/10.keep"]
        assert not wt_gone.exists()
        assert wt_keep.exists()

        # A held task's branch survives even after its brief is dropped: the
        # branch is the thing a resolve recovers.
        asyncio.run(client.app.state.store.set_task_state(
            None, "10.keep", "blocked", blocked_reason="needs resolve: conflict"
        ))
        (workspace / "nightshift-tasks/main/10.keep.md").unlink()
        _reconcile(client)
        assert _task_local_branches(repo_root) == ["task-local/main/10.keep"]
        assert wt_keep.exists()
