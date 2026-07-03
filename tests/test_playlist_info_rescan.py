"""Playlist-info page + workspace-rescan tests.

Covers the shared core (rename + rescan helpers in :mod:`nightshift.playlists`),
the manager store's queue-rename migration, and the operator endpoints on both
the manager and the legacy server that back the new playlist-info page and the
Playlists-page "Rescan" button.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from _workspace import build_workspace
from nightshift import playlists as playlists_mod
from nightshift.lifecycle import AttemptState
from nightshift.manager.app import create_app as create_manager_app
from nightshift.manager.store import MemoryStore
from nightshift.server.app import create_app as create_server_app


# --------------------------------------------------------------------------- #
# Shared core: rename + rescan helpers
# --------------------------------------------------------------------------- #


def test_rename_playlist_moves_dir_and_keeps_config(tmp_path: Path) -> None:
    build_workspace(
        tmp_path,
        queues={"alpha": {"config": {"repo": "longitude", "order": []}}},
    )
    tasks_root = tmp_path / "nightshift-tasks"
    (tasks_root / "alpha" / "10.task.md").write_text("Do a thing.")

    new = playlists_mod.rename_playlist(tasks_root, "alpha", "Beta Queue")
    assert new == "beta-queue"
    assert not (tasks_root / "alpha").exists()
    assert (tasks_root / "beta-queue" / "config.json").exists()
    # Tasks + the repo binding travel with the directory.
    assert (tasks_root / "beta-queue" / "10.task.md").exists()
    cfg = json.loads((tasks_root / "beta-queue" / "config.json").read_text())
    assert cfg["repo"] == "longitude"


def test_rename_playlist_guards(tmp_path: Path) -> None:
    build_workspace(tmp_path, queues={"alpha": {"config": {}}, "beta": {"config": {}}})
    tasks_root = tmp_path / "nightshift-tasks"

    # Renaming the main queue is refused.
    try:
        playlists_mod.rename_playlist(tasks_root, "main", "x")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    # Unknown source.
    try:
        playlists_mod.rename_playlist(tasks_root, "ghost", "x")
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass
    # Target already exists.
    try:
        playlists_mod.rename_playlist(tasks_root, "alpha", "beta")
        raise AssertionError("expected FileExistsError")
    except FileExistsError:
        pass
    # Same-slug no-op is allowed.
    assert playlists_mod.rename_playlist(tasks_root, "alpha", "alpha") == "alpha"


def test_rescan_into_playlists(tmp_path: Path) -> None:
    # Two target repos + the content store; an existing playlist named after one.
    build_workspace(
        tmp_path,
        main_repo="longitude",
        repos=("longitude", "widgets"),
        queues={"widgets": {"config": {"order": []}}},
    )
    tasks_root = tmp_path / "nightshift-tasks"
    result = playlists_mod.rescan_into_playlists(
        tasks_root,
        ["longitude", "widgets", "nightshift-tasks"],
        skip={"nightshift-tasks"},
    )
    assert result["created"] == ["longitude"]
    assert result["configured"] == ["widgets"]
    names = {p["name"] for p in playlists_mod.list_playlists(tasks_root)}
    assert {"longitude", "widgets"} <= names
    assert "nightshift-tasks" not in names
    for repo in ("longitude", "widgets"):
        cfg = json.loads((tasks_root / repo / "config.json").read_text())
        assert cfg["repo"] == repo


# --------------------------------------------------------------------------- #
# Manager store: queue rename migrates every queue-keyed row
# --------------------------------------------------------------------------- #


def test_memory_store_rename_queue() -> None:
    store = MemoryStore()

    async def scenario() -> None:
        await store.create_attempt(
            "r1", task="t1", queue="alpha", worker_id="w1",
            backend="claude-code", model="auto", base_ref=None, ttl_seconds=60,
        )
        await store.create_attempt(
            "r2", task="t2", queue="alpha", worker_id="w1",
            backend="claude-code", model="auto", base_ref="ref", ttl_seconds=60,
        )
        await store.set_task_state("alpha", "t3", "blocked", blocked_reason="x")
        await store.set_queue_dedication("alpha", ["w1"])

        await store.rename_queue("alpha", "beta")

        assert sorted(
            r["queue"] for r in await store.list_attempts(queue="beta")
        ) == ["beta", "beta"]
        assert await store.list_attempts(queue="alpha") == []
        live = await store.live_attempts()
        assert all(a["queue"] == "beta" for a in live)
        assert await store.get_task_state("alpha", "t3") is None
        assert (await store.get_task_state("beta", "t3"))["state"] == "blocked"
        ded = await store.queue_dedication()
        assert ded == {"beta": ["w1"]}

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Manager endpoints
# --------------------------------------------------------------------------- #


def _mgr(workspace: Path, store: MemoryStore | None = None) -> TestClient:
    return TestClient(create_manager_app(workspace, store=store or MemoryStore()))


def test_manager_playlist_create_info_rename_repo(tmp_path: Path) -> None:
    build_workspace(tmp_path, repos=("longitude",))
    with _mgr(tmp_path) as client:
        # Create.
        r = client.post("/api/playlists", json={"name": "Alpha"})
        assert r.status_code == 201
        assert r.json()["name"] == "alpha"

        # Info exposes name + repository (the aliased repo binding, initially None).
        info = client.get("/api/playlists/alpha").json()
        assert info["name"] == "alpha"
        assert info["repository"] is None

        # Set the repository via the info PUT.
        r = client.put("/api/playlists/alpha", json={"repository": "longitude"})
        assert r.status_code == 200
        assert r.json()["repository"] == "longitude"
        assert client.get("/api/queue/repo?queue=alpha").json()["repo"] == "longitude"

        # Rename carries the repo binding across.
        r = client.put("/api/playlists/alpha", json={"name": "Gamma"})
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "gamma"
        assert body["repository"] == "longitude"
        assert client.get("/api/playlists/alpha").status_code == 404
        assert client.get("/api/playlists/gamma").json()["repository"] == "longitude"


def test_manager_playlist_validate_command_persists(tmp_path: Path) -> None:
    # The info page's "Validate command" field must round-trip through the
    # manager's PUT + GET: set a custom command, then re-read it (mirrors the
    # user flow of editing the field, saving, leaving, and re-opening get-info).
    build_workspace(
        tmp_path, repos=("longitude",),
        queues={"longitude": {"config": {"repo": "longitude", "order": []}}},
    )
    with _mgr(tmp_path) as client:
        # No custom command yet -> inherits the default (reported as None).
        assert client.get("/api/playlists/longitude").json()["validate"] is None

        # Save a custom validate command.
        r = client.put(
            "/api/playlists/longitude", json={"validate": "just longitude-no-db"}
        )
        assert r.status_code == 200
        assert r.json()["validate"] == "just longitude-no-db"

        # Re-opening get-info shows the saved command (it was persisted on disk).
        assert (
            client.get("/api/playlists/longitude").json()["validate"]
            == "just longitude-no-db"
        )
        cfg = json.loads(
            (tmp_path / "nightshift-tasks" / "longitude" / "config.json").read_text()
        )
        assert cfg["validate"] == "just longitude-no-db"

        # Editing to another custom command replaces it.
        r = client.put(
            "/api/playlists/longitude", json={"validate": "just validate-no-db"}
        )
        assert r.status_code == 200
        assert client.get("/api/playlists/longitude").json()["validate"] == (
            "just validate-no-db"
        )

        # Clearing it (empty string) disables validation: persisted as "".
        r = client.put("/api/playlists/longitude", json={"validate": ""})
        assert r.status_code == 200
        assert client.get("/api/playlists/longitude").json()["validate"] == ""


def test_manager_rename_migrates_store_and_blocks_running(tmp_path: Path) -> None:
    build_workspace(
        tmp_path, repos=("longitude",),
        queues={"alpha": {"config": {"repo": "longitude", "order": []}}},
    )
    store = MemoryStore()
    # A finished attempt: rename must migrate its queue key but not be blocked
    # by it (only LIVE attempts block a rename).
    asyncio.run(store.create_attempt(
        "r1", task="t1", queue="alpha", worker_id="w1",
        backend="claude-code", model="auto", base_ref=None, ttl_seconds=60,
    ))
    asyncio.run(store.update_attempt("r1", state=AttemptState.NO_CHANGE))
    with _mgr(tmp_path, store) as client:
        r = client.put("/api/playlists/alpha", json={"name": "beta"})
        assert r.status_code == 200
        runs = asyncio.run(store.list_attempts(queue="beta"))
        assert [run["task"] for run in runs] == ["t1"]

        # A live attempt on the (now beta) queue blocks a further rename.
        asyncio.run(store.create_attempt(
            "r2", task="t1", queue="beta", worker_id="w1", model="auto",
            backend="claude-code", base_ref="ref", ttl_seconds=60,
        ))
        r = client.put("/api/playlists/beta", json={"name": "gamma"})
        assert r.status_code == 409


def test_manager_rescan_and_delete(tmp_path: Path) -> None:
    build_workspace(tmp_path, main_repo="longitude", repos=("longitude", "widgets"))
    with _mgr(tmp_path) as client:
        r = client.post("/api/playlists/rescan", json={})
        assert r.status_code == 200
        data = r.json()
        assert set(data["created"]) == {"longitude", "widgets"}
        names = {p["name"] for p in data["playlists"]}
        assert {"longitude", "widgets"} <= names
        assert "nightshift-tasks" not in names
        assert client.get("/api/playlists/widgets").json()["repository"] == "widgets"

        # Delete one.
        r = client.delete("/api/playlists/widgets")
        assert r.status_code == 200
        assert client.get("/api/playlists/widgets").status_code == 404


def test_manager_rename_rejects_bad_repo(tmp_path: Path) -> None:
    build_workspace(tmp_path, queues={"alpha": {"config": {}}})
    with _mgr(tmp_path) as client:
        r = client.put("/api/playlists/alpha", json={"repository": "../escape"})
        assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Legacy server endpoints
# --------------------------------------------------------------------------- #


def _srv(workspace: Path) -> TestClient:
    return TestClient(create_server_app(workspace))


def test_server_playlist_info_rename_rescan(tmp_path: Path) -> None:
    build_workspace(
        tmp_path, main_repo="longitude", repos=("longitude", "widgets"),
        queues={"alpha": {"config": {"order": []}}},
    )
    with _srv(tmp_path) as client:
        # Info.
        info = client.get("/api/playlists/alpha").json()
        assert info["name"] == "alpha"
        assert info["repository"] is None

        # Rename + repo in one PUT.
        r = client.put(
            "/api/playlists/alpha", json={"name": "beta", "repository": "longitude"}
        )
        assert r.status_code == 200
        assert r.json() == {
            "name": "beta",
            "task_count": 0,
            "repository": "longitude",
            "validate": None,
            "disabled": False,
        }
        assert client.get("/api/playlists/alpha").status_code == 404

        # Rescan materialises a playlist per workspace repo.
        r = client.post("/api/playlists/rescan", json={})
        assert r.status_code == 200
        names = {p["name"] for p in r.json()["playlists"]}
        assert {"longitude", "widgets", "beta"} <= names
        assert client.get("/api/playlists/longitude").json()["repository"] == "longitude"
