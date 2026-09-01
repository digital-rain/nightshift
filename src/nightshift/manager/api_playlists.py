"""Operator playlist + repo endpoints — ``/api/playlists*`` and ``/api/repos*``.

Split out of ``manager/api_operator.py`` in Phase 9 purely for module size;
handler logic is unchanged. Endpoints are registered onto the shared FastAPI
app by :func:`register_playlist_api`; the shared wiring (store accessor, event
emitter, content-store committer, queue helpers) is injected by
``register_operator_api`` under the same names the handler bodies always used.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from nightshift import playlists as playlists_mod
from nightshift import repos
from nightshift.auto_import import (
    HOST_QUEUE_KEY,
    auto_import_repos,
    resolve_host_queue,
    set_auto_import,
)
from nightshift.lifecycle import TaskHoldKind
from nightshift.manager.scheduler import queue_label
from nightshift.manager.store import NightshiftStore
from nightshift.manager.wire import EmitFn, normalize_repo
from nightshift.queue_config import (
    ci_monitoring_enabled,
    normalize_validate_command,
    save_queue_config_value,
    set_ci_monitoring,
)
from nightshift.repo_tasks import list_repo_task_queues
from nightshift.spawn_daily import load_queue_config
from nightshift.task_files import list_queue


class PlaylistCreate(BaseModel):
    name: str


class QueueCiMonitoringBody(BaseModel):
    """Body for ``PUT /api/queue/ci-monitoring``. ``queue=None`` targets the
    main queue (mirrors every other queue-scoped body in this API)."""

    queue: str | None = None
    enabled: bool


class RepoAutoImportBody(BaseModel):
    """Body for ``PUT /api/repos/auto-import`` — the repo-level switch that
    turns a target repo's ``.tasks/`` inbox into queues Nightshift services."""

    repo: str
    enabled: bool


class QueueHostQueueBody(BaseModel):
    """Body for ``PUT /api/queue/host-queue``. ``host_queue`` is the
    ``.tasks/<name>`` subdir this queue drains; ``""``/``null`` clears the
    binding to an explicit "none" (which is *not* the same as leaving the key
    unset — see :func:`nightshift.auto_import.resolve_host_queue`)."""

    queue: str | None = None
    host_queue: str | None = None


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
    # A one-sentence summary and free-form (markdown) notes for the playlist.
    # A blank/whitespace-only value clears the stored key; ``None`` (field unset)
    # leaves it untouched.
    description: str | None = None
    notes: str | None = None


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
    async def get_playlists() -> JSONResponse:
        """The playlists list, each entry gaining ``ci_state``: the repo's
        latest CI state (``"green"|"red"|"pending"|"unknown"``) when the
        playlist's bound repo is monitored, else ``None`` -- covering both
        "not monitored" and "monitored but no CI row recorded yet", the same
        ambiguity ``/api/repos`` already accepts for its ``ci`` field.

        "Monitored" is repo-level (mirrors the reconciler's own gate): a
        playlist's repo counts as monitored when *any* queue bound to it --
        not necessarily this one -- has ``ci_monitoring`` on.

        Each entry also gains ``hold``: ``None``, or
        ``{"kind", "tasks", "detail"?, "url"?}`` describing the repo-level hold
        keeping this playlist's tasks out of dispatch. Without it a held
        playlist is indistinguishable from an idle one -- these holds never
        reach ``/api/blocked``, which filters on ``state = 'blocked'``.
        """
        store = _store()
        entries = playlists_mod.list_playlists(tasks_root)
        monitored = _monitored_repo_names()
        ci_rows = await store.repo_ci() if monitored else {}
        # A repo-level hold stops a playlist dispatching without changing
        # anything the row already shows, so the playlist just goes quiet.
        # Count the held tasks per queue and carry the reason, so the page can
        # say *why* nothing is running.
        held: dict[str | None, dict[str, Any]] = {}
        for kind in (TaskHoldKind.CI_RED, TaskHoldKind.REPO_UNAVAILABLE):
            for row in await store.tasks_in_state(kind):
                slot = held.setdefault(
                    row.get("queue"), {"kind": str(kind), "tasks": 0}
                )
                slot["tasks"] += 1
        for entry in entries:
            cfg = load_queue_config(tasks_root, playlists_mod.tasks_rel(entry["name"]))
            repo = cfg.get("repo")
            row = ci_rows.get(repo) if repo and repo in monitored else None
            entry["ci_state"] = row.get("state") if row else None
            hold = held.get(entry["name"])
            if hold and hold["kind"] == TaskHoldKind.CI_RED and row:
                hold = {**hold, "detail": row.get("detail"), "url": row.get("url")}
            entry["hold"] = hold
        return JSONResponse(entries)

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
            "description": cfg.get("description"),
            "notes": cfg.get("notes"),
            "ci_monitoring": ci_monitoring_enabled(cfg),
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
            "ci_monitoring": ci_monitoring_enabled(cfg),
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
        # ``description`` (one-sentence summary) and ``notes`` (free-form markdown)
        # are plain queue-config prose. A blank/whitespace-only value clears the
        # key so the config stays clean; any other value is stored verbatim (notes
        # keep their internal whitespace/newlines). An unset field is untouched.
        sent = req.model_dump(exclude_unset=True)
        for key in ("description", "notes"):
            if key not in sent:
                continue
            raw = getattr(req, key)
            value = raw if (raw and raw.strip()) else None
            save_queue_config_value(
                tasks_root, key, value, playlists_mod.tasks_rel(current)
            )
            await _commit(f"nightshift: set {key} {queue_label(current)}")
            await _emit("queue_changed", queue=current, payload={key: value})
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

    def _monitored_repo_names() -> set[str]:
        """Repos with at least one queue whose ``ci_monitoring`` switch is on.

        Mirrors the reconciler's own ``_monitored_repos`` (minus the per-queue
        grouping and the availability filter, neither of which this payload
        needs -- the caller only walks repos already known to the workspace)."""
        out: set[str] = set()
        for q in _all_queues():
            config = load_queue_config(tasks_root, playlists_mod.tasks_rel(q))
            repo = config.get("repo")
            if repo and ci_monitoring_enabled(config):
                out.add(repo)
        return out

    async def _repos_payload() -> dict[str, Any]:
        """The known-repos set, per-queue repo bindings, and warnings.

        The known set is the workspace's direct children with ``.git``; per-queue
        repo comes from each queue's ``config.json``. A queue whose configured
        repo is set but absent surfaces a single warning (matching the
        one-warning-per-queue pause rule). Each repo also carries ``monitored``
        (a queue bound to it has CI monitoring on) and, only when monitored,
        its latest ``ci`` state row (``None`` otherwise -- an unmonitored repo
        has no fresh row to trust).

        Auto-import rides along on both halves: each repo carries its switch
        (``auto_import``) plus, only when the switch is on, the host queues it
        publishes (``task_queues``, one ``ls-tree`` per switched-on repo); each
        queue carries the host queue it drains (``host_queue``, resolved -- so
        the UI shows the same default the importer will act on) and the
        options its bound repo offers.
        """
        known = repos.known_repos(workspace)
        enabled = set(auto_import_repos(tasks_root))
        # One tree read per switched-on, present repo, shared by the repo rows
        # and every queue bound to it. Repos with the switch off cost nothing.
        host_queues: dict[str, list[str]] = {
            name: await asyncio.to_thread(list_repo_task_queues, workspace, name)
            for name in known
            if name in enabled
        }
        queues_payload: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        for q in _all_queues():
            label = queue_label(q)
            repo = _queue_repo(q)
            available = bool(repo) and repos.repo_available(workspace, repo)
            config = load_queue_config(tasks_root, playlists_mod.tasks_rel(q))
            offered = host_queues.get(repo or "", [])
            auto_import = bool(repo) and repo in enabled
            queues_payload.append({
                "queue": label,
                "repo": repo,
                "available": available,
                "ci_monitoring": ci_monitoring_enabled(config),
                "auto_import": auto_import,
                "host_queue": (
                    resolve_host_queue(config, label, available=offered)
                    if auto_import else None
                ),
                "host_queues": offered,
            })
            if repo and not available:
                warnings.append({"queue": label, "repo": repo})
        monitored = _monitored_repo_names()
        ci_rows = await _store().repo_ci()
        repos_payload = []
        for name in known:
            is_monitored = name in monitored
            row = ci_rows.get(name) if is_monitored else None
            repos_payload.append({
                "name": name,
                "available": True,
                "monitored": is_monitored,
                "auto_import": name in enabled,
                "task_queues": host_queues.get(name, []),
                "ci": None if not row else {
                    "state": row.get("state"),
                    "head_sha": row.get("head_sha"),
                    "url": row.get("url"),
                    "detail": row.get("detail"),
                    "fix_task": row.get("fix_task"),
                },
            })
        return {
            "workspace": str(workspace),
            "tasks_repo": tasks_repo,
            "repos": repos_payload,
            "queues": queues_payload,
            "warnings": warnings,
        }

    @app.get("/api/repos")
    async def get_repos() -> JSONResponse:
        return JSONResponse(await _repos_payload())

    @app.put("/api/repos/auto-import")
    async def put_repo_auto_import(body: RepoAutoImportBody) -> JSONResponse:
        """Turn ``.tasks/`` auto-import on/off for one repo.

        Repo-level rather than per-queue: the inbox belongs to the repo, and
        the switch is what makes its host queues selectable at all. Turning it
        off stops the importer immediately but leaves each queue's
        ``host_queue`` binding in place, so switching back on resumes exactly
        where it left off. Briefs already pulled are ordinary tasks and are
        unaffected.
        """
        if not repos.is_valid_repo_ref(body.repo):
            return JSONResponse(
                {"error": (
                    f"invalid repo reference {body.repo!r}: a repo must be a bare "
                    "workspace child name (no paths, '..', '/', or absolute paths)"
                )},
                status_code=400,
            )
        set_auto_import(tasks_root, body.repo, body.enabled)
        verb = "enable" if body.enabled else "disable"
        await _commit(f"nightshift: {verb} auto-import {body.repo}")
        await _emit(
            "queue_changed",
            payload={"repo": body.repo, "auto_import": body.enabled},
        )
        return JSONResponse(await _repos_payload())

    @app.put("/api/queue/host-queue")
    async def put_queue_host_queue(body: QueueHostQueueBody) -> JSONResponse:
        """Bind one queue to a ``.tasks/<name>`` host queue on its repo.

        A blank value stores the explicit "none" that keeps this queue out of
        auto-import while its repo's switch stays on for other queues. The
        name is slug-guarded here rather than at read time: it is concatenated
        into an inbox path, and an authoring error belongs where it is
        authored.
        """
        target = _queue_from_label(body.queue)
        if target is not None and not playlists_mod.exists(tasks_root, target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        name = (body.host_queue or "").strip()
        if name and not playlists_mod.is_valid_name(name):
            return JSONResponse(
                {"error": (
                    f"invalid host queue {name!r}: a .tasks/ queue must be a bare "
                    "directory name matching [a-z0-9][a-z0-9-]*"
                )},
                status_code=400,
            )
        save_queue_config_value(
            tasks_root, HOST_QUEUE_KEY, name, playlists_mod.tasks_rel(target)
        )
        await _commit(f"nightshift: set host-queue {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"host_queue": name})
        return JSONResponse(await _repos_payload())

    @app.put("/api/queue/ci-monitoring")
    async def put_queue_ci_monitoring(body: QueueCiMonitoringBody) -> JSONResponse:
        """Turn CI monitoring on/off for one queue.

        Persists into that queue's own ``config.json`` beside ``repo`` and
        ``validate``. Turning it off leaves any ``ci_red`` hold on this queue's
        tasks for exactly one more tick -- the reconciler recomputes the
        monitored-repo set every refresh, so the clear loop drops the hold on
        the next pass once the repo is no longer in that set.
        """
        target = _queue_from_label(body.queue)
        if target is not None and not playlists_mod.exists(tasks_root, target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        set_ci_monitoring(tasks_root, playlists_mod.tasks_rel(target), body.enabled)
        await _commit(f"nightshift: set ci-monitoring {queue_label(target)}")
        await _emit(
            "queue_changed", queue=target, payload={"ci_monitoring": body.enabled}
        )
        return JSONResponse(await _repos_payload())

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
        return JSONResponse(await _repos_payload())
