"""Tests for the CI gate: refresh duty, fix spawn, dispatch hold, UI payloads.

Task 4 covers the refresh duty only (this file's first block below). Tasks 5
and 6 extend this same harness with fix-spawn and dispatch-hold coverage.
"""

from __future__ import annotations

import json
import shutil
from functools import partial
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from _workspace import build_workspace
from nightshift.ci import CiState, CiStatus
from nightshift.lifecycle import TaskHoldKind
from nightshift.manager.app import create_app
from nightshift.manager.store_sqlite import SqliteStore
from nightshift.queue_config import set_ci_monitoring
from nightshift.repos import DEFAULT_TASKS_REPO


class _StubCi:
    """Stands in for reconciler.check_repo_ci; counts calls so we can assert
    the duty skipped the subprocess entirely."""

    def __init__(self, status: CiStatus) -> None:
        self.status = status
        self.calls = 0

    def __call__(self, repo_root: Path, *, branch: str = "main") -> CiStatus:
        self.calls += 1
        return self.status


def _build(tmp_path, monkeypatch, *, monitoring: bool):
    ws = build_workspace(tmp_path, tasks={"alpha": "do the thing"})
    # `build_workspace`'s `queues=` mapping writes each queue's config.json
    # wholesale (it has no per-key merge), so setting a switch on the *main*
    # queue without clobbering the `repo` binding main_repo already wrote
    # goes through the real merge-preserving helper instead.
    if monitoring:
        set_ci_monitoring(ws / DEFAULT_TASKS_REPO, "main", True)
    stub = _StubCi(CiStatus(CiState.GREEN, head_sha="aaa"))
    monkeypatch.setattr("nightshift.manager.reconciler.check_repo_ci", stub)
    store = SqliteStore()
    return ws, store, stub, TestClient(create_app(ws, store=store))


@pytest.fixture
def gate(tmp_path, monkeypatch):
    ws, store, stub, client = _build(tmp_path, monkeypatch, monitoring=True)
    with client:
        yield ws, store, stub, client


@pytest.fixture
def unmonitored(tmp_path, monkeypatch):
    ws, store, stub, client = _build(tmp_path, monkeypatch, monitoring=False)
    with client:
        yield ws, store, stub, client


def _call(client: TestClient, fn, *a, **kw):
    return client.portal.call(partial(fn, *a, **kw))


def _reconcile(client: TestClient) -> None:
    client.portal.call(client.app.state.reconciler.reconcile_once)


def _open_throttle(client: TestClient) -> None:
    client.app.state.reconciler._ci_checked_at.clear()


def _tasks_dir(ws: Path, queue: str = "main") -> Path:
    # tasks_repo defaults to "nightshift-tasks" (OperatorConfig.tasks_repo).
    return ws / "nightshift-tasks" / queue


def _checkin(client: TestClient, worker_id: str = "w1") -> None:
    client.post(
        "/api/worker/checkin",
        json={"worker_id": worker_id, "backend": "claude-code"},
    )


def _poll(client: TestClient, worker_id: str = "w1") -> dict | None:
    return client.post(
        "/api/worker/poll",
        json={"worker_id": worker_id, "backend": "claude-code", "models": ["auto"]},
    ).json()["work"]


# --------------------------------------------------------------------------- #
# Task 4: the reconciler refresh duty
# --------------------------------------------------------------------------- #


def test_refresh_records_state_for_a_monitored_queue(gate):
    _ws, store, stub, client = gate
    # `with client:` already ran one startup reconcile pass (app.py's lifespan
    # calls `Reconciler.startup()`, which itself calls `reconcile_once()`), so
    # the per-repo throttle is already primed — open it back up to force a
    # real recheck against the flipped stub status.
    stub.status = CiStatus(CiState.RED, head_sha="bbb", url="u", detail="pytest: failure")
    _open_throttle(client)
    _reconcile(client)
    assert _call(client, store.repo_ci)["longitude"]["state"] == "red"


def test_unmonitored_queue_never_shells_out(unmonitored):
    _ws, store, stub, client = unmonitored
    _reconcile(client)
    assert stub.calls == 0
    assert _call(client, store.repo_ci) == {}


def test_transition_emits_a_repo_ci_event(gate):
    _ws, store, stub, client = gate
    cursor = _call(client, store.max_event_id)
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="pytest: failure")
    _open_throttle(client)
    _reconcile(client)
    events = [
        e
        for e in _call(client, store.events_since, cursor)
        if e.get("kind") == "repo_ci"
    ]
    assert events and events[-1]["payload"]["state"] == "red"


def test_steady_state_emits_nothing_new(gate):
    """A repo that stays green must not churn the operator's event feed."""
    _ws, store, stub, client = gate
    _reconcile(client)
    cursor = _call(client, store.max_event_id)
    _open_throttle(client)
    _reconcile(client)
    assert not [
        e
        for e in _call(client, store.events_since, cursor)
        if e.get("kind") == "repo_ci"
    ]


def test_throttle_suppresses_an_immediate_recheck(gate):
    _ws, _store, stub, client = gate
    _reconcile(client)
    _reconcile(client)
    assert stub.calls == 1


# --------------------------------------------------------------------------- #
# Task 5: spawning the CI-resolution task
# --------------------------------------------------------------------------- #
#
# Staleness note: the plan's Task 5 pseudocode omits `_open_throttle(client)`
# before each test's *first* `_reconcile(client)` call. The `gate` fixture's
# `with client:` already ran a startup reconcile (green, sha "aaa"), which
# primes `_ci_checked_at` — without re-opening the throttle here, the first
# explicit reconcile in these tests is a no-op (the 120s cadence default
# suppresses the recheck) and the red transition never gets recorded, so the
# assertions fail for the wrong reason. Every first reconcile below opens the
# throttle first.


def test_red_spawns_one_ci_resolution_task(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(
        CiState.RED, head_sha="bbb", url="https://gh/run/1", detail="pytest: failure"
    )
    _open_throttle(client)
    _reconcile(client)

    row = _call(client, store.repo_ci)["longitude"]
    assert row["fix_task"] and row["fix_sha"] == "bbb"

    brief = (_tasks_dir(ws) / f"{row['fix_task']}.md").read_text()
    assert "kind: ci_resolution" in brief  # the Stats/History category tag
    assert "/fix" in brief
    assert "pytest: failure" in brief
    assert "https://gh/run/1" in brief


def test_same_red_sha_does_not_respawn(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _open_throttle(client)
    _reconcile(client)
    first = _call(client, store.repo_ci)["longitude"]["fix_task"]
    _open_throttle(client)
    _reconcile(client)
    assert _call(client, store.repo_ci)["longitude"]["fix_task"] == first
    assert len(list(_tasks_dir(ws).glob("fix-ci-*.md"))) == 1


def test_red_green_red_spawns_a_second_task(gate):
    ws, _store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail one")
    _open_throttle(client)
    _reconcile(client)
    _open_throttle(client)
    stub.status = CiStatus(CiState.GREEN, head_sha="ccc")
    _reconcile(client)
    _open_throttle(client)
    stub.status = CiStatus(CiState.RED, head_sha="ddd", detail="fail two")
    _reconcile(client)
    assert len(list(_tasks_dir(ws).glob("fix-ci-*.md"))) == 2


# --------------------------------------------------------------------------- #
# Task 6: the dispatch hold
# --------------------------------------------------------------------------- #
#
# Staleness note: the plan's Step 1 pseudocode drives these two tests through
# a full `_reconcile(client)` (== `reconcile_once`, which runs the CI-refresh
# duty *and* the hold duty in one pass) and then asserts `_poll(client) is
# None`. But `_refresh_repo_ci` already auto-spawns the CI-resolution task the
# moment it sees red (Task 5, already built) -- and that fix task is exactly
# the one thing this gate must let through (see the exemption test below), so
# once it exists it is the queue's only dispatchable candidate and `_poll`
# legitimately returns *it*, not None. Asserting `is None` after a full
# reconcile is therefore incompatible with the already-built spawn behaviour.
# These two tests instead drive `_reconcile_holds` directly (via
# `store.set_repo_ci` + the reconciler's private hold duty) to pin the
# hold/clear logic in isolation, without the spawn duty in the loop; the
# spawn+exemption interaction has its own test below and in Task 5's file.


def _reconcile_holds(client: TestClient) -> None:
    client.portal.call(client.app.state.reconciler._reconcile_holds)


def test_red_holds_the_queues_tasks(gate):
    _ws, store, _stub, client = gate
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="bbb", url=None, detail=None)
    _reconcile_holds(client)
    assert _call(client, store.get_task_state, None, "alpha")["state"] == "ci_red"
    _checkin(client)
    assert _poll(client) is None


def test_green_clears_the_hold_and_resumes(gate):
    _ws, store, _stub, client = gate
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="bbb", url=None, detail=None)
    _reconcile_holds(client)
    _checkin(client)
    assert _poll(client) is None

    _call(client, store.set_repo_ci, "longitude", state="green",
          head_sha="ccc", url=None, detail=None)
    _reconcile_holds(client)
    assert not _call(client, store.get_task_state, None, "alpha")
    assert _poll(client) is not None


@pytest.mark.parametrize("state", ["pending", "unknown", "green"])
def test_only_red_gates(gate, state):
    """Fail-open: CI latency and unknown state must never stall a queue."""
    _ws, store, _stub, client = gate
    _call(client, store.set_repo_ci, "longitude", state=state,
          head_sha="aaa", url=None, detail=None)
    _checkin(client)
    assert _poll(client) is not None


def test_unmonitored_queue_dispatches_through_red(unmonitored):
    _ws, store, _stub, client = unmonitored
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="bbb", url=None, detail=None)
    _checkin(client)
    assert _poll(client) is not None


def test_the_ci_resolution_task_is_not_held_by_its_own_gate(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _open_throttle(client)
    _reconcile(client)
    fix = _call(client, store.repo_ci)["longitude"]["fix_task"]
    assert not _call(client, store.get_task_state, None, fix)
    # The full reconcile_once pipeline (refresh -> holds), not just the
    # isolated _reconcile_holds helper above, must have written the ci_red
    # hold onto the queue's *other* task -- otherwise this test's dispatch
    # assertion below could pass vacuously via the read-only worker_poll path
    # alone even if the reconciler's hold-write were broken.
    assert _call(client, store.get_task_state, None, "alpha")["state"] == "ci_red"
    _checkin(client)
    order = _poll(client)
    assert order and order["task"] == fix     # the fix is what dispatches


# --------------------------------------------------------------------------- #
# Regression coverage for the reviewed defects
# --------------------------------------------------------------------------- #


_FIX_STEM = "fix-ci-longitude-main-is-red-at-bbb"


def test_red_reflap_at_the_same_sha_readopts_the_fix_brief(gate):
    """red -> unknown -> red at the SAME sha: `set_repo_ci` NULLs the fix
    marker on any state change, so the second red re-enters the spawn path and
    `create_task` collides on the identical title. The existing brief must be
    re-adopted (marker restored) or it loses its by-name exemption, is held
    `ci_red` itself, and nothing can ever turn the repo green again."""
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _open_throttle(client)
    _reconcile(client)
    first = _call(client, store.repo_ci)["longitude"]["fix_task"]
    assert first

    stub.status = CiStatus(CiState.UNKNOWN, head_sha="bbb", detail="gh timed out")
    _open_throttle(client)
    _reconcile(client)
    assert not _call(client, store.repo_ci)["longitude"]["fix_task"]

    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _open_throttle(client)
    _reconcile(client)

    row = _call(client, store.repo_ci)["longitude"]
    assert row["fix_task"] == first and row["fix_sha"] == "bbb"
    assert len(list(_tasks_dir(ws).glob("fix-ci-*.md"))) == 1
    # The exemption is back: the fix brief is unheld and dispatches.
    assert not _call(client, store.get_task_state, None, first)
    _checkin(client)
    order = _poll(client)
    assert order and order["task"] == first


def test_ci_red_never_clobbers_an_operator_blocked_row(gate):
    """A task already `blocked` with an operator-actionable reason must not be
    taken over by the CI gate: the upsert NULLs `blocked_reason`, `reset`
    404s, and the green-side clear would then DELETE the row and auto-release
    a task blocked for an unrelated reason."""
    _ws, store, _stub, client = gate
    _call(
        client, store.set_task_state, None, "alpha", "blocked",
        blocked_reason="validation failed: pytest exited 1",
    )
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="bbb", url=None, detail=None)
    _reconcile_holds(client)

    row = _call(client, store.get_task_state, None, "alpha")
    assert row["state"] == "blocked"
    assert row["blocked_reason"] == "validation failed: pytest exited 1"

    # ... and going green must not delete the foreign hold.
    _call(client, store.set_repo_ci, "longitude", state="green",
          head_sha="ccc", url=None, detail=None)
    _reconcile_holds(client)
    row = _call(client, store.get_task_state, None, "alpha")
    assert row and row["state"] == "blocked"
    assert row["blocked_reason"] == "validation failed: pytest exited 1"


def test_reset_releases_a_ci_red_hold(gate):
    """The operator needs an escape from an orphan `ci_red` row."""
    _ws, store, _stub, client = gate
    _call(client, store.set_task_state, None, "alpha", "ci_red", repo="longitude")
    resp = client.post("/api/tasks/alpha/reset?queue=")
    assert resp.status_code == 200, resp.text
    assert resp.json()["prior_state"] == "ci_red"
    assert not _call(client, store.get_task_state, None, "alpha")


def test_only_the_tagged_fix_brief_is_exempt(tmp_path, monkeypatch):
    """The exemption matches on task NAME only, so a same-stem brief in
    another queue bound to the same red repo also escapes the gate."""
    ws = build_workspace(
        tmp_path,
        tasks={"alpha": "do the thing"},
        queues={
            "side": {
                "tasks": {_FIX_STEM: "an unrelated brief that happens to collide"},
                "config": {"repo": "longitude", "order": [_FIX_STEM]},
            }
        },
    )
    set_ci_monitoring(ws / DEFAULT_TASKS_REPO, "main", True)
    stub = _StubCi(CiStatus(CiState.RED, head_sha="bbb", detail="fail"))
    monkeypatch.setattr("nightshift.manager.reconciler.check_repo_ci", stub)
    store = SqliteStore()
    with TestClient(create_app(ws, store=store)) as client:
        _open_throttle(client)
        _reconcile(client)
        fix = _call(client, store.repo_ci)["longitude"]["fix_task"]
        assert fix == _FIX_STEM
        # The real, `kind: ci_resolution`-tagged brief is exempt ...
        assert not _call(client, store.get_task_state, None, fix)
        # ... the same-stem impostor in the other queue is not.
        assert _call(client, store.get_task_state, "side", _FIX_STEM)[
            "state"
        ] == "ci_red"


def test_the_fix_task_still_gets_the_other_holds(gate):
    """The exemption is a `continue` in the candidate walk, so it skips the
    `repo_error` and `repo_unavailable` arms too. It must apply to the
    `ci_red` arm only."""
    ws, store, _stub, client = gate
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="bbb", url=None, detail=None)
    _call(client, store.set_repo_ci_fix, "longitude", fix_task="alpha", fix_sha="bbb")
    shutil.rmtree(ws / "longitude")
    _reconcile_holds(client)
    assert _call(client, store.get_task_state, None, "alpha")[
        "state"
    ] == "repo_unavailable"


def test_fix_brief_avoids_a_paused_queue(tmp_path, monkeypatch):
    """Spawning the fix into a paused queue closes the gate with no
    dispatchable fix -- the same deadlock family as the re-flap."""
    ws = build_workspace(
        tmp_path,
        tasks={"alpha": "do the thing"},
        queues={
            "side": {
                "tasks": {},
                "config": {"repo": "longitude", "order": [], "ci_monitoring": True},
            }
        },
    )
    set_ci_monitoring(ws / DEFAULT_TASKS_REPO, "main", True)
    # Green at startup: the lifespan's own reconcile pass must not spawn the
    # brief before the test has paused the queue.
    stub = _StubCi(CiStatus(CiState.GREEN, head_sha="aaa"))
    monkeypatch.setattr("nightshift.manager.reconciler.check_repo_ci", stub)
    store = SqliteStore()
    with TestClient(create_app(ws, store=store)) as client:
        _call(client, store.set_queue_pause, "main", "operator paused")
        stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
        _open_throttle(client)
        _reconcile(client)
        fix = _call(client, store.repo_ci)["longitude"]["fix_task"]
        assert fix
        assert (ws / "nightshift-tasks" / "side" / f"{fix}.md").exists()
        assert not (_tasks_dir(ws) / f"{fix}.md").exists()


# --------------------------------------------------------------------------- #
# Task 7: the /api/repos payload -- `monitored` + `ci`
# --------------------------------------------------------------------------- #


def test_repos_payload_carries_monitoring_and_ci(gate):
    _ws, _store, stub, client = gate
    stub.status = CiStatus(
        CiState.RED, head_sha="bbb", url="https://gh/run/1", detail="pytest: failure"
    )
    _open_throttle(client)
    _reconcile(client)
    repos = {r["name"]: r for r in client.get("/api/repos").json()["repos"]}
    assert repos["longitude"]["monitored"] is True
    assert repos["longitude"]["ci"]["state"] == "red"
    assert repos["longitude"]["ci"]["url"] == "https://gh/run/1"
    assert repos["longitude"]["ci"]["detail"] == "pytest: failure"


def test_unmonitored_repo_reports_monitored_false(unmonitored):
    _ws, _store, _stub, client = unmonitored
    repos = {r["name"]: r for r in client.get("/api/repos").json()["repos"]}
    assert repos["longitude"]["monitored"] is False
    assert repos["longitude"]["ci"] is None


# --------------------------------------------------------------------------- #
# Task 8: the CI monitoring switch -- PUT /api/queue/ci-monitoring
# --------------------------------------------------------------------------- #


def test_toggling_monitoring_persists_and_takes_effect(unmonitored):
    ws, _store, stub, client = unmonitored
    assert client.put("/api/queue/ci-monitoring",
                      json={"queue": None, "enabled": True}).status_code == 200

    cfg = json.loads((_tasks_dir(ws) / "config.json").read_text())
    assert cfg["ci_monitoring"] is True

    _reconcile(client)
    assert stub.calls == 1        # now polled, where before it was not


def test_toggling_off_stops_polling_and_clears_holds(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    # `with client:` (TestClient.__enter__) already ran one startup reconcile
    # pass, priming the per-repo throttle -- open it back up so this first
    # `_reconcile` actually rechecks against the flipped (red) stub status
    # instead of a vacuous no-op (see test_refresh_records_state_for_a_monitored_queue).
    _open_throttle(client)
    _reconcile(client)
    assert _call(client, store.get_task_state, None, "alpha")["state"] == "ci_red"

    client.put("/api/queue/ci-monitoring", json={"queue": None, "enabled": False})
    _open_throttle(client)
    _reconcile(client)
    assert not _call(client, store.get_task_state, None, "alpha")
    _checkin(client)
    assert _poll(client) is not None


def test_round_trip_on_queue_bindings_and_playlist_info(unmonitored):
    """The switch round-trips through both surfaces this task owns: the Repos
    page's queue-bindings list (``/api/repos``'s ``queues``) and the
    playlist/library detail payload (``/api/main/info``). The endpoint's own
    response is the refreshed repos payload."""
    _ws, _store, _stub, client = unmonitored
    before_repos = {q["queue"]: q for q in client.get("/api/repos").json()["queues"]}
    assert before_repos["main"]["ci_monitoring"] is False
    assert client.get("/api/main/info").json()["ci_monitoring"] is False

    resp = client.put("/api/queue/ci-monitoring", json={"queue": None, "enabled": True})
    assert resp.status_code == 200
    after = {q["queue"]: q for q in resp.json()["queues"]}
    assert after["main"]["ci_monitoring"] is True

    repos = {q["queue"]: q for q in client.get("/api/repos").json()["queues"]}
    assert repos["main"]["ci_monitoring"] is True
    assert client.get("/api/main/info").json()["ci_monitoring"] is True


def test_round_trip_on_a_named_playlist(tmp_path, monkeypatch):
    """Same round trip, but on a real playlist rather than main -- exercises
    ``/api/playlists/{name}`` (the plan's "playlist info" surface) instead of
    ``/api/main/info``, and the ``queue`` body field as a name instead of
    ``null``."""
    ws = build_workspace(
        tmp_path,
        tasks={"alpha": "do the thing"},
        queues={"side": {"tasks": {}, "config": {"repo": "longitude", "order": []}}},
    )
    stub = _StubCi(CiStatus(CiState.GREEN, head_sha="aaa"))
    monkeypatch.setattr("nightshift.manager.reconciler.check_repo_ci", stub)
    store = SqliteStore()
    with TestClient(create_app(ws, store=store)) as client:
        assert client.get("/api/playlists/side").json()["ci_monitoring"] is False

        resp = client.put(
            "/api/queue/ci-monitoring", json={"queue": "side", "enabled": True}
        )
        assert resp.status_code == 200

        assert client.get("/api/playlists/side").json()["ci_monitoring"] is True
        repos = {q["queue"]: q for q in client.get("/api/repos").json()["queues"]}
        assert repos["side"]["ci_monitoring"] is True


def test_unknown_queue_404s(unmonitored):
    _ws, _store, _stub, client = unmonitored
    resp = client.put(
        "/api/queue/ci-monitoring", json={"queue": "nope", "enabled": True}
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Task 9: the playlists list payload -- `ci_state`
# --------------------------------------------------------------------------- #
#
# Staleness note: the plan's Step 1 pseudocode reads `pls["main"]` off
# `/api/playlists`. `playlists.list_playlists` deliberately excludes the
# default queue ("main") -- it's shown separately as the "library" row -- so
# "main" never appears in this endpoint's payload, and `client.get(...).json()`
# is a plain list (no `{"playlists": [...]}` wrapper; see app.js's
# `getJSON("/api/playlists")`). These tests instead bind a real, named
# playlist ("side") to the monitored repo, matching what this endpoint
# actually returns.


def _build_playlist(tmp_path, monkeypatch, *, monitoring: bool):
    queue_config: dict[str, object] = {"repo": "longitude", "order": []}
    if monitoring:
        queue_config["ci_monitoring"] = True
    ws = build_workspace(
        tmp_path,
        tasks={"alpha": "do the thing"},
        queues={"side": {"tasks": {}, "config": queue_config}},
    )
    stub = _StubCi(CiStatus(CiState.GREEN, head_sha="aaa"))
    monkeypatch.setattr("nightshift.manager.reconciler.check_repo_ci", stub)
    store = SqliteStore()
    return ws, store, stub, TestClient(create_app(ws, store=store))


@pytest.fixture
def playlist_monitored(tmp_path, monkeypatch):
    ws, store, stub, client = _build_playlist(tmp_path, monkeypatch, monitoring=True)
    with client:
        yield ws, store, stub, client


@pytest.fixture
def playlist_unmonitored(tmp_path, monkeypatch):
    ws, store, stub, client = _build_playlist(tmp_path, monkeypatch, monitoring=False)
    with client:
        yield ws, store, stub, client


@pytest.mark.parametrize("state,expected", [
    ("green", "green"), ("red", "red"),
    ("pending", "pending"), ("unknown", "unknown"),
])
def test_playlist_carries_its_ci_state(playlist_monitored, state, expected):
    _ws, store, _stub, client = playlist_monitored
    _call(client, store.set_repo_ci, "longitude", state=state,
          head_sha="aaa", url=None, detail=None)
    pls = {p["name"]: p for p in client.get("/api/playlists").json()}
    assert pls["side"]["ci_state"] == expected


def test_unmonitored_playlist_has_no_ci_state(playlist_unmonitored):
    _ws, store, _stub, client = playlist_unmonitored
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="aaa", url=None, detail=None)
    pls = {p["name"]: p for p in client.get("/api/playlists").json()}
    assert pls["side"]["ci_state"] is None


def test_playlist_ci_state_is_repo_level_not_per_queue_switch(gate):
    """A playlist's own `ci_monitoring` switch can be off while its repo is
    still monitored (because another queue bound to the same repo has it on)
    -- the reconciler's own hold logic gates on the repo, not the queue's own
    switch (see `Reconciler._monitored_repos`/`_reconcile_holds`), so the dot
    must agree: it reads off the repo, not this playlist's own switch."""
    ws, store, _stub, client = gate  # "main" already has ci_monitoring on
    # Add an unmonitored "side" playlist bound to the same repo as "main".
    (ws / "nightshift-tasks" / "side").mkdir()
    (ws / "nightshift-tasks" / "side" / "config.json").write_text(
        json.dumps({"repo": "longitude", "order": []})
    )
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="aaa", url=None, detail=None)
    pls = {p["name"]: p for p in client.get("/api/playlists").json()}
    assert pls["side"]["ci_state"] == "red"


# --------------------------------------------------------------------------- #
# Task 10: CI resolution in History and Stats (attempts.kind)
# --------------------------------------------------------------------------- #


def test_ci_resolution_attempt_is_tagged(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _open_throttle(client)
    _reconcile(client)
    _checkin(client)
    order = _poll(client)
    attempt = _call(client, store.get_attempt, order["run_id"])
    assert attempt["kind"] == "ci_resolution"

    # The store row alone isn't the wire: History and Stats read the
    # `/api/runs`/`/api/analytics/runs` projections (views.py), which
    # whitelist keys and previously dropped `kind` on the floor.
    runs = {r["id"]: r for r in client.get("/api/runs").json()}
    assert runs[order["run_id"]]["kind"] == "ci_resolution"
    analytics_runs = {r["id"]: r for r in client.get("/api/analytics/runs").json()}
    assert analytics_runs[order["run_id"]]["kind"] == "ci_resolution"


def test_ordinary_attempt_has_no_kind(unmonitored):
    _ws, store, _stub, client = unmonitored
    _checkin(client)
    order = _poll(client)
    attempt = _call(client, store.get_attempt, order["run_id"])
    assert not attempt.get("kind")


# --------------------------------------------------------------------------
# Transient gh failures must not churn state. Observed live on 2026-09-01:
# one `gh run list` timeout flipped a repo green -> unknown -> green, which
# clears every hold and the fix marker for a repo that never actually changed.
# --------------------------------------------------------------------------

def _state_of(client, store, repo="longitude"):
    return (_call(client, store.repo_ci).get(repo) or {}).get("state")


def test_one_transient_gh_failure_keeps_the_last_known_state(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="pytest: failure")
    _open_throttle(client)
    _reconcile(client)
    assert _state_of(client, store) == "red"

    # gh goes down: UNKNOWN carrying transient=True.
    stub.status = CiStatus(CiState.UNKNOWN, detail="gh timed out", transient=True)
    _open_throttle(client)
    _reconcile(client)
    assert _state_of(client, store) == "red", (
        "a single gh blip must not release a red repo's holds"
    )


def test_repeated_transient_failures_eventually_degrade_to_unknown(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="pytest: failure")
    _open_throttle(client)
    _reconcile(client)
    assert _state_of(client, store) == "red"

    stub.status = CiStatus(CiState.UNKNOWN, detail="gh timed out", transient=True)
    for _ in range(3):
        _open_throttle(client)
        _reconcile(client)
    assert _state_of(client, store) == "unknown", (
        "gh persistently broken must fail open rather than pin a stale red"
    )


def test_a_real_unknown_still_writes_through(gate):
    """Not every UNKNOWN is transient: gh answering 'no runs' is real news."""
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="pytest: failure")
    _open_throttle(client)
    _reconcile(client)
    assert _state_of(client, store) == "red"

    stub.status = CiStatus(CiState.UNKNOWN, detail="no workflow runs on branch")
    _open_throttle(client)
    _reconcile(client)
    assert _state_of(client, store) == "unknown"


def test_a_recovered_gh_resets_the_miss_counter(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="pytest: failure")
    _open_throttle(client)
    _reconcile(client)

    stub.status = CiStatus(CiState.UNKNOWN, detail="blip", transient=True)
    for _ in range(2):
        _open_throttle(client)
        _reconcile(client)
    # Recovery before the tolerance is reached.
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="pytest: failure")
    _open_throttle(client)
    _reconcile(client)
    # Two more blips must not tip it over: the counter restarted.
    stub.status = CiStatus(CiState.UNKNOWN, detail="blip", transient=True)
    for _ in range(2):
        _open_throttle(client)
        _reconcile(client)
    assert _state_of(client, store) == "red"


# --------------------------------------------------------------------------
# A held playlist must say so. ci_red / repo_unavailable rows never reach
# /api/blocked (it filters state = 'blocked'), so without this the queue just
# looks idle while its tasks sit undispatched.
# --------------------------------------------------------------------------

def _side_playlist(ws, repo="longitude"):
    (ws / "nightshift-tasks" / "side").mkdir(exist_ok=True)
    (ws / "nightshift-tasks" / "side" / "config.json").write_text(
        json.dumps({"repo": repo, "order": []})
    )


def test_playlist_reports_its_ci_hold(gate):
    ws, store, _stub, client = gate
    _side_playlist(ws)
    _call(client, store.set_repo_ci, "longitude", state="red", head_sha="aaa",
          url="https://gh/run/1", detail="CI Validate: failure")
    _call(client, store.set_task_state, "side", "alpha", TaskHoldKind.CI_RED,
          repo="longitude")

    pls = {p["name"]: p for p in client.get("/api/playlists").json()}
    hold = pls["side"]["hold"]
    assert hold["kind"] == "ci_red"
    assert hold["tasks"] == 1
    # The reason travels with it, so the row can explain itself.
    assert hold["detail"] == "CI Validate: failure"
    assert hold["url"] == "https://gh/run/1"


def test_playlist_counts_every_held_task(gate):
    ws, store, _stub, client = gate
    _side_playlist(ws)
    _call(client, store.set_repo_ci, "longitude", state="red", head_sha="aaa",
          url=None, detail=None)
    for task in ("alpha", "beta", "gamma"):
        _call(client, store.set_task_state, "side", task, TaskHoldKind.CI_RED,
              repo="longitude")
    pls = {p["name"]: p for p in client.get("/api/playlists").json()}
    assert pls["side"]["hold"]["tasks"] == 3


def test_an_unheld_playlist_reports_no_hold(gate):
    ws, store, _stub, client = gate
    _side_playlist(ws)
    pls = {p["name"]: p for p in client.get("/api/playlists").json()}
    assert pls["side"]["hold"] is None


def test_a_repo_unavailable_hold_also_surfaces(gate):
    """The same invisibility applies to repo_unavailable, so it rides along."""
    ws, store, _stub, client = gate
    _side_playlist(ws)
    _call(client, store.set_task_state, "side", "alpha",
          TaskHoldKind.REPO_UNAVAILABLE, repo="longitude")
    pls = {p["name"]: p for p in client.get("/api/playlists").json()}
    assert pls["side"]["hold"]["kind"] == "repo_unavailable"
    assert pls["side"]["hold"]["tasks"] == 1
