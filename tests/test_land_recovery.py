"""Phase 8: the land idempotency trailer + startup recovery ladder.

Every manager land stamps ``Nightshift-Attempt: <attempt id>`` on the squash
commit; startup recovery scans main for it before re-running a LANDING job.
This is the enforcement of greenfield invariant 5 (a land is applied to main
at most once): the trailer check runs FIRST, so a re-enqueue can never race a
land that already completed. Real git repos, no mocks on the git layer.

The ladder's last rung (neither trailer nor branch → park) is covered by
``test_reconciler.py::test_restart_parks_mid_land_run_for_resolve``.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from _workspace import build_workspace
from nightshift.engine import setup_worktree
from nightshift.git.landing import attempt_trailer_line, find_landed_attempt
from nightshift.lifecycle import AttemptState, LandKind, TaskHoldKind
from nightshift.manager.app import create_app
from nightshift.manager.landing import canonical_head, land
from nightshift.manager.store import MemoryStore


def _client(workspace: Path, store: MemoryStore | None = None) -> TestClient:
    return TestClient(create_app(workspace, store=store or MemoryStore()))


def _poll_with_landable_branch(
    client: TestClient, workspace: Path, task: str
) -> dict[str, Any]:
    client.post(
        "/api/worker/checkin",
        json={"worker_id": "w1", "backend": "claude-code"},
    )
    order = client.post(
        "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"}
    ).json()["work"]
    assert order is not None and order["task"] == task
    wt = setup_worktree(workspace, order["repo"], task)
    (wt / "GENERATED.txt").write_text("done\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "work"], cwd=wt, check=True, capture_output=True)
    return order


def _tip_message(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%B", "main"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout


def _commit_count(repo_root: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "main"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout
    return int(out.strip())


def _seed_landing_attempt(store: MemoryStore, run_id: str, task: str) -> None:
    """A LANDING attempt exactly as a crashed process would have left it."""
    async def seed() -> None:
        row = await store.create_attempt(
            run_id, task=task, queue=None, worker_id="w1",
            backend="claude-code", model="auto", base_ref=None,
            ttl_seconds=600, title="hello", repo="longitude",
        )
        assert row is not None
        await store.update_attempt(
            run_id, state=AttemptState.LANDING, phase="landing",
        )

    asyncio.run(seed())


# --------------------------------------------------------------------------- #
# (a) the trailer producer: every manager land carries it
# --------------------------------------------------------------------------- #


def test_manager_land_stamps_the_attempt_trailer(tmp_path: Path) -> None:
    """The end-to-end submit → land flow puts `Nightshift-Attempt: <run id>`
    on the squash commit as a proper git trailer."""
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    repo_root = workspace / "longitude"
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
        assert r.json() == {"landed": None, "status": "landing", "queued": True}
        client.portal.call(client.app.state.drain_git_jobs)

        message = _tip_message(repo_root)
        assert message.startswith("task: ")
        assert attempt_trailer_line(order["run_id"]) in message
        # find_landed_attempt (the recovery probe) sees it via git's trailer
        # parser — pinning the %(trailers:key=...) format string against real
        # git output.
        assert find_landed_attempt(repo_root, order["run_id"]) == (
            canonical_head(repo_root)
        )
        assert find_landed_attempt(repo_root, "some-other-attempt") is None


def test_direct_land_without_attempt_id_carries_no_trailer(tmp_path: Path) -> None:
    """Legacy/CLI callers pass no attempt_id → no trailer (documented: only
    manager lands are recovery-eligible)."""
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    repo_root = workspace / "longitude"
    wt = setup_worktree(workspace, "longitude", "10.hello")
    (wt / "GENERATED.txt").write_text("done\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "work"], cwd=wt, check=True, capture_output=True)

    outcome = land(workspace, "longitude", "10.hello", "hello", queue=None)
    assert outcome.kind is LandKind.LANDED
    assert "Nightshift-Attempt" not in _tip_message(repo_root)


# --------------------------------------------------------------------------- #
# (b) recovery, trailer found: complete as LANDED, never re-land
# (this is the invariant-5 test: the land is applied to main at most once)
# --------------------------------------------------------------------------- #


def test_invariant_5_recovery_with_trailer_does_not_reland(tmp_path: Path) -> None:
    """The crash window AFTER the push but BEFORE the terminal store write:
    the trailer is on main, the attempt is still LANDING. Startup recovery
    must complete the attempt from the trailer (LANDED, commit_sha set, brief
    dropped for a non-evergreen task) and land nothing twice — main's tip and
    commit count are unchanged."""
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    repo_root = workspace / "longitude"
    store = MemoryStore()
    _seed_landing_attempt(store, "run-x", "10.hello")

    # The previous process's land job DID complete: trailer on main.
    wt = setup_worktree(workspace, "longitude", "10.hello")
    (wt / "GENERATED.txt").write_text("done\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "work"], cwd=wt, check=True, capture_output=True)
    outcome = land(
        workspace, "longitude", "10.hello", "hello", queue=None,
        attempt_id="run-x",
    )
    assert outcome.kind is LandKind.LANDED
    tip = canonical_head(repo_root)
    commits = _commit_count(repo_root)

    # "Restart": startup recovery runs in the lifespan before any request.
    with _client(workspace, store) as client:
        attempt = asyncio.run(store.get_attempt("run-x"))
        assert attempt["state"] == "landed"
        assert attempt["commit_sha"] == tip
        assert attempt["finished_at"] is not None
        assert "recovered" in attempt["result_line"]
        # Nothing re-landed: same tip, same commit count.
        assert canonical_head(repo_root) == tip
        assert _commit_count(repo_root) == commits
        # The brief was consumed (non-evergreen landed task).
        assert not (workspace / "nightshift-tasks/main/10.hello.md").exists()
        # The run view reports the recovered land.
        run = next(x for x in client.get("/api/runs").json() if x["id"] == "run-x")
        assert run["status"] == "completed"
        assert run["commit_sha"] == tip

        # Idempotent: a second startup pass finds no LANDING attempt.
        client.portal.call(client.app.state.reconciler.startup)
        assert canonical_head(repo_root) == tip
        assert _commit_count(repo_root) == commits


# --------------------------------------------------------------------------- #
# (c) recovery, no trailer but the branch survived: re-enqueue, land once
# --------------------------------------------------------------------------- #


def test_recovery_reenqueues_interrupted_land_with_surviving_branch(
    tmp_path: Path,
) -> None:
    """The crash window BEFORE the push: no trailer anywhere, but the local
    task branch survived. Recovery re-enqueues the SAME land job — the work
    lands exactly once and now carries the trailer."""
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    repo_root = workspace / "longitude"
    store = MemoryStore()
    _seed_landing_attempt(store, "run-x", "10.hello")

    # The worker's branch exists with committed work; nothing was landed.
    wt = setup_worktree(workspace, "longitude", "10.hello")
    (wt / "GENERATED.txt").write_text("done\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "work"], cwd=wt, check=True, capture_output=True)
    commits_before = _commit_count(repo_root)

    with _client(workspace, store) as client:
        # Startup blocked on the re-enqueued land job; it has already applied.
        attempt = asyncio.run(store.get_attempt("run-x"))
        assert attempt["state"] == "landed"
        # The land path records the abbreviated sha (as the submit path does).
        assert canonical_head(repo_root).startswith(attempt["commit_sha"])
        assert _commit_count(repo_root) == commits_before + 1
        # The re-run stamped the trailer, so a SECOND restart would take the
        # trailer path (exactly-once across repeated crashes).
        assert find_landed_attempt(repo_root, "run-x") == canonical_head(repo_root)
        # The landed work is on main and the brief was consumed.
        assert (repo_root / "GENERATED.txt").exists()
        assert not (workspace / "nightshift-tasks/main/10.hello.md").exists()
        run = next(x for x in client.get("/api/runs").json() if x["id"] == "run-x")
        assert run["status"] == "completed"


# --------------------------------------------------------------------------- #
# (c') the branch survived but the re-run cannot land: the rung-2 failure park
# --------------------------------------------------------------------------- #


def test_rung2_reenqueue_that_cannot_land_parks_as_conflict(
    tmp_path: Path,
) -> None:
    """Rung 2 with a surviving branch that has NOTHING new to squash (the
    crash hit before the worker's commit ever reached the branch): the
    re-run's squash is empty → CONFLICT, and the false-conflict trailer
    re-check finds nothing on main (nothing was ever pushed), so the attempt
    parks via ``on_land_result``: CONFLICT with ``finished_at``, ``/api/runs``
    projects "error", the task is held blocked for a resolve, and the brief
    is NOT consumed."""
    workspace = build_workspace(tmp_path, tasks={"10.hello": "Do a thing."})
    repo_root = workspace / "longitude"
    store = MemoryStore()
    _seed_landing_attempt(store, "run-x", "10.hello")

    # The branch exists (rung 2 fires) but sits exactly at main: empty squash.
    setup_worktree(workspace, "longitude", "10.hello")
    tip = canonical_head(repo_root)
    commits = _commit_count(repo_root)

    with _client(workspace, store) as client:
        attempt = asyncio.run(store.get_attempt("run-x"))
        assert attempt["state"] == "conflict"
        assert attempt["finished_at"] is not None
        assert attempt["failure_kind"] == "merge_conflict"
        # Nothing landed: main untouched, no trailer anywhere.
        assert canonical_head(repo_root) == tip
        assert _commit_count(repo_root) == commits
        assert find_landed_attempt(repo_root, "run-x") is None
        run = next(x for x in client.get("/api/runs").json() if x["id"] == "run-x")
        assert run["status"] == "error"
        # Parked for the operator: blocked hold, brief preserved for the
        # resolve (a park never consumes work).
        hold = asyncio.run(store.get_task_state(None, "10.hello"))
        assert hold is not None and hold["state"] == TaskHoldKind.BLOCKED
        assert str(hold["blocked_reason"]).startswith("needs resolve")
        assert (workspace / "nightshift-tasks/main/10.hello.md").exists()
