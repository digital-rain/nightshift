"""Operator playlist + repo endpoints — ``/api/playlists*`` and ``/api/repos*``.

Split out of ``manager/api_operator.py`` in Phase 9 purely for module size;
handler logic is unchanged. Endpoints are registered onto the shared FastAPI
app by :func:`register_playlist_api`; the shared wiring (store accessor, event
emitter, content-store committer, queue helpers) is injected by
``register_operator_api`` under the same names the handler bodies always used.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from nightshift import playlists as playlists_mod
from nightshift import repos
from nightshift.lifecycle import TaskHoldKind
from nightshift.manager.scheduler import queue_label
from nightshift.manager.store import NightshiftStore
from nightshift.manager.wire import EmitFn, normalize_repo
from nightshift.queue_config import (
    normalize_validate_command,
    save_queue_config_value,
)
from nightshift.spawn_daily import load_queue_config
from nightshift.task_files import list_queue


class PlaylistCreate(BaseModel):
    name: str


class PlaylistUpdate(BaseModel):
    """Edit a playlist from its info page. ``name`` renames the queue (its
    on-disk dir + every queue-keyed DB row); ``repository`` is the alias the UI
    shows for the queue's default ``repo`` binding; ``validate`` is the queue's
    validate command. All optional; only the fields present in the request are
    applied."""

    # ``validate`` on the wire; the field is named ``validate_cmd`` to avoid
    # shadowing ``BaseModel.validate``.
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = None
    repository: str | None = None
    validate_cmd: str | None = Field(default=None, alias="validate")
    # Hide the playlist from the default Playlists view and exclude it from the
    # scheduler's candidate set; ``False`` re-enables it. ``None`` leaves it
    # untouched.
    disabled: bool | None = None


def register_playlist_api(
    app: FastAPI,
    *,
    workspace: Path,
    tasks_root: Path,
    tasks_repo: str,
    _store: Callable[[], NightshiftStore],
    _emit: EmitFn,
    _queue_from_label: Callable[[str | None], str | None],
    _all_queues: Callable[[], list[str | None]],
    _queue_repo: Callable[[str | None], str | None],
    _commit: Callable[[str], Awaitable[None]],
) -> None:
    """Register the playlist and repo endpoints (see module docstring)."""
    @app.get("/api/playlists")
    def get_playlists() -> JSONResponse:
        return JSONResponse(playlists_mod.list_playlists(tasks_root))

    @app.post("/api/playlists")
    async def post_playlist(req: PlaylistCreate) -> JSONResponse:
        try:
            created = playlists_mod.create_playlist(tasks_root, req.name)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileExistsError as exc:
            return JSONResponse(
                {"error": f"playlist already exists: {exc}"}, status_code=409
            )
        await _commit(f"nightshift: create playlist {created['name']}")
        await _emit("queue_changed", queue=created["name"])
        return JSONResponse(created, status_code=201)

    def _playlist_info(name: str) -> dict[str, Any]:
        """The playlist-info payload: its name, task count, the ``repo`` binding
        aliased to ``repository``, and the queue's ``validate`` command for the
        info page. ``validate`` is the raw stored value: ``None`` when the queue
        inherits the engine default, ``""`` when validation is explicitly
        disabled, else the custom command."""
        cfg = load_queue_config(tasks_root, playlists_mod.tasks_rel(name))
        count = len(list((tasks_root / name).glob("*.md")))
        return {
            "name": name,
            "task_count": count,
            "repository": cfg.get("repo"),
            "validate": cfg.get("validate"),
            "disabled": playlists_mod.is_disabled(tasks_root, name),
        }

    @app.get("/api/main/info")
    def get_main_info() -> JSONResponse:
        """The main queue's info payload, mirroring the per-playlist info
        endpoint so the playlist-info screen can display the "library"."""
        cfg = load_queue_config(tasks_root, playlists_mod.tasks_rel(None))
        count = len(list_queue(tasks_root, playlists_mod.tasks_rel(None)))
        return JSONResponse({
            "name": "library",
            "task_count": count,
            "repository": cfg.get("repo"),
            "validate": cfg.get("validate"),
            "disabled": False,
        })

    @app.get("/api/playlists/{name}/tasks")
    def get_playlist_tasks(name: str) -> JSONResponse:
        """List a playlist's tasks without making it active, so the Add-from
        picker can preview and copy individual tasks."""
        if not playlists_mod.exists(tasks_root, name):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        return JSONResponse(list_queue(tasks_root, playlists_mod.tasks_rel(name)))

    @app.get("/api/playlists/{name}")
    def get_playlist(name: str) -> JSONResponse:
        if not playlists_mod.exists(tasks_root, name):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        return JSONResponse(_playlist_info(name))

    @app.put("/api/playlists/{name}")
    async def put_playlist(name: str, req: PlaylistUpdate) -> JSONResponse:
        if not playlists_mod.exists(tasks_root, name):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        current = name
        # An active lease on this queue means a worker is mid-run against it;
        # renaming the dir + DB rows under it would strand that run.
        if req.name is not None and playlists_mod.slugify_name(req.name) != name:
            active = await _store().live_attempts()
            if any(_queue_from_label(le["queue"]) == name for le in active):
                return JSONResponse(
                    {"error": "playlist has a running task; stop it first"},
                    status_code=409,
                )
            try:
                new_name = playlists_mod.rename_playlist(tasks_root, name, req.name)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            except FileExistsError as exc:
                return JSONResponse(
                    {"error": f"playlist already exists: {exc}"}, status_code=409
                )
            except FileNotFoundError:
                return JSONResponse({"error": "playlist not found"}, status_code=404)
            await _store().rename_queue(name, new_name)
            await _commit(f"nightshift: rename playlist {name} -> {new_name}")
            await _emit(
                "queue_changed",
                queue=new_name,
                payload={"renamed_from": name},
            )
            current = new_name
        # ``repository`` aliases the queue's default repo binding. A sent value
        # (incl. "" -> cleared) is normalized + persisted; an unset field is left
        # untouched (PATCH-like semantics on a PUT body of optional fields).
        if "repository" in req.model_dump(exclude_unset=True):
            try:
                repo_value = normalize_repo(req.repository)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            save_queue_config_value(
                tasks_root, "repo", repo_value, playlists_mod.tasks_rel(current)
            )
            await _commit(f"nightshift: set repo {queue_label(current)}")
            await _emit("queue_changed", queue=current, payload={"repo": repo_value})
        # ``validate`` is the queue's validate command. A whitespace-only value
        # (or the empty-quote literals) normalizes to "" — a deliberate "disable
        # validation" signal that never falls back to the inherited default; any
        # other value is stored stripped. An unset field is left untouched.
        if "validate_cmd" in req.model_dump(exclude_unset=True):
            cmd = normalize_validate_command(str(req.validate_cmd or ""))
            save_queue_config_value(
                tasks_root, "validate", cmd, playlists_mod.tasks_rel(current)
            )
            await _commit(f"nightshift: set validate {queue_label(current)}")
            await _emit("queue_changed", queue=current, payload={"validate": cmd})
        # Disabling hides the queue and drops it from the scheduler's candidate
        # set; a no-op for an in-flight lease, which keeps draining until done.
        if req.disabled is not None:
            playlists_mod.set_playlist_disabled(tasks_root, current, req.disabled)
            verb = "disable" if req.disabled else "enable"
            await _commit(f"nightshift: {verb} playlist {current}")
            await _emit(
                "queue_changed", queue=current, payload={"disabled": req.disabled}
            )
        return JSONResponse(_playlist_info(current))

    @app.delete("/api/playlists/{name}")
    async def remove_playlist(name: str) -> JSONResponse:
        active = await _store().live_attempts()
        if any(_queue_from_label(le["queue"]) == name for le in active):
            return JSONResponse(
                {"error": "playlist has a running task; stop it first"},
                status_code=409,
            )
        if not playlists_mod.delete_playlist(tasks_root, name):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        await _commit(f"nightshift: delete playlist {name}")
        await _emit("queue_changed", queue=name)
        return JSONResponse({"name": name, "deleted": True})

    @app.post("/api/playlists/rescan")
    async def rescan_playlists() -> JSONResponse:
        """Scan the workspace's immediate children for git repos and materialise
        one playlist per repo (name = repo dir name), binding each playlist's
        default repo to the discovered repo. The content-store repo is skipped.
        """
        repo_names = repos.known_repos(workspace)
        result = playlists_mod.rescan_into_playlists(
            tasks_root, repo_names, skip={tasks_repo}
        )
        if result["created"] or result["configured"]:
            await _commit("nightshift: rescan workspace repos into playlists")
        await _emit("queue_changed", payload=result)
        return JSONResponse(
            {**result, "playlists": playlists_mod.list_playlists(tasks_root)}
        )

    # ----- repos (multi-repo workspace) ----------------------------------- #

    def _repos_payload() -> dict[str, Any]:
        """The known-repos set, per-queue repo bindings, and warnings.

        The known set is the workspace's direct children with ``.git``; per-queue
        repo comes from each queue's ``config.json``. A queue whose configured
        repo is set but absent surfaces a single warning (matching the
        one-warning-per-queue pause rule)."""
        known = repos.known_repos(workspace)
        queues_payload: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        for q in _all_queues():
            label = queue_label(q)
            repo = _queue_repo(q)
            available = bool(repo) and repos.repo_available(workspace, repo)
            queues_payload.append({"queue": label, "repo": repo, "available": available})
            if repo and not available:
                warnings.append({"queue": label, "repo": repo})
        return {
            "workspace": str(workspace),
            "tasks_repo": tasks_repo,
            "repos": [{"name": name, "available": True} for name in known],
            "queues": queues_payload,
            "warnings": warnings,
        }

    @app.get("/api/repos")
    def get_repos() -> JSONResponse:
        return JSONResponse(_repos_payload())

    @app.post("/api/repos/rescan")
    async def rescan_repos() -> JSONResponse:
        """Recompute the known-repos set and auto-resume any paused
        (``repo_unavailable``) task whose repo is now present, then re-warn from
        scratch on the next poll."""
        store = _store()
        resumed: list[dict[str, Any]] = []
        for row in await store.tasks_in_state(TaskHoldKind.REPO_UNAVAILABLE):
            repo = row.get("repo")
            if repo and repos.repo_available(workspace, repo):
                queue = _queue_from_label(row.get("queue"))
                await store.clear_task_state(queue, row["task"])
                resumed.append({"queue": queue_label(queue), "task": row["task"]})
                await _emit("queue_changed", queue=queue, task=row["task"])
        # Reset the per-queue warning dedupe so a still-missing repo re-warns.
        # Mutate in place: the reconciler captured this set at construction, so
        # rebinding would leave it deduping against a stale object forever.
        app.state.repo_warnings.clear()
        await _emit("repos_changed", payload={"resumed": resumed})
        return JSONResponse(_repos_payload())
