"""FastAPI app for the Nightshift UI.

Serves the static single-page UI and a small JSON/SSE API over the shared
engine. Runs play-throughs in a background thread via :class:`Player` and
streams live state by tailing the on-disk run records, so runs launched from
the CLI appear here too.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from nightshift import playlists as playlists_mod
from nightshift import repos as repos_mod
from nightshift._paths import UI_DIR
from nightshift.backends import list_backends
from nightshift.config.io import load_json, worker_json_path
from nightshift.config.validate import build_get_response, validate_delta, write_delta
from nightshift.engine import (
    DEFAULT_VALIDATE_CMD,
    cleanup_task_worktree,
    create_task,
    delete_task,
    import_task,
    list_queue,
    load_play_priorities,
    load_sort_mode,
    normalize_validate_command,
    read_task,
    reorder_queue,
    save_play_priorities,
    save_queue_config_value,
    save_sort_mode,
    set_task_meta,
)
from nightshift.events import RunStore
from nightshift.server.player import Player, resolve_tasks_repo
from nightshift.spawn_daily import (
    MAX_PRIORITY,
    MIN_PRIORITY,
    load_config,
    load_queue_config,
    resolve_config,
    resolve_frontmatter,
)


_VALID_ACTIONS = {"play", "pause", "stop", "skip", "select"}
_VALID_MODES = {"oneshot", "auto", "repeat"}


def _validate_priority(value: object) -> int:
    """Coerce a request's priority to an int in ``[MIN_PRIORITY, MAX_PRIORITY]``,
    raising ``ValueError`` (surfaced as a 400) for out-of-range/non-int input."""
    try:
        priority = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("priority must be an integer 0-5")
    if not (MIN_PRIORITY <= priority <= MAX_PRIORITY):
        raise ValueError(f"priority must be between {MIN_PRIORITY} and {MAX_PRIORITY}")
    return priority


def _normalize_repo(value: object) -> str | None:
    """Normalize a repo override from a request payload. ``None``, "", and
    "default" clear it (the task/queue then inherits its default); any other
    value must be a valid bare workspace-child slug — a malformed reference is an
    authoring error surfaced as a 400 at edit time (the path-traversal guard).
    Returns the cleaned name or ``None`` (clear)."""
    if value in (None, "", "default"):
        return None
    repo = str(value).strip()
    if not repo:
        return None
    if not repos_mod.is_valid_repo_ref(repo):
        raise ValueError(
            f"invalid repo {repo!r}: must be a bare workspace-child name "
            "matching [a-z0-9][a-z0-9-]* (no paths, '..', '/', or absolute paths)"
        )
    return repo


class NoCacheStaticFiles(StaticFiles):
    """Serve UI assets with revalidation so swapped files (e.g. logo.png) show up
    immediately instead of being served stale from the browser cache."""

    async def get_response(self, path: str, scope: Any):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class TransportRequest(BaseModel):
    action: str
    mode: str | None = None
    task: str | None = None
    # Target queue: None falls back to the focused queue, "" is the main queue,
    # any other value is a playlist. Lets the UI drive any queue's transport.
    queue: str | None = None


class TaskCreate(BaseModel):
    """Create a task from the detail-view pane. ``title`` and ``text`` (the spec
    prose) are required-ish (title must be non-empty); the frontmatter toggles
    and ``model`` are optional and applied to the new file when present, so the
    same detail surface that edits a task also creates one. ``model`` set to ""
    or "default" leaves the field unset so the task inherits the config default."""

    title: str
    text: str = ""
    disabled: bool | None = None
    evergreen: bool | None = None
    automerge: bool | None = None
    draft: bool | None = None
    model: str | None = None
    priority: int | None = None
    # Optional per-task target-repo override (a bare workspace-child name);
    # defaults to the queue's ``repo`` when "", "default", or unset.
    repo: str | None = None


class TaskUpdate(BaseModel):
    """Partial edit from the detail-view pane. Unset fields are left untouched;
    ``model`` set to "" or "default" clears the field so the task inherits the
    config default. ``title`` (headline) and ``body`` (spec prose) are content
    edits saved alongside the frontmatter toggles in one PATCH."""

    disabled: bool | None = None
    evergreen: bool | None = None
    automerge: bool | None = None
    draft: bool | None = None
    model: str | None = None
    priority: int | None = None
    title: str | None = None
    body: str | None = None
    # Per-task target-repo override; "" / "default" clears it (inherit queue).
    repo: str | None = None


class QueueOrder(BaseModel):
    order: list[str]


class QueueSort(BaseModel):
    # Queue sort mode: "manual" (drag order) or "priority".
    sort: str


class QueueRepo(BaseModel):
    # The queue's default target repo (a bare workspace-child name), or null to
    # clear it so the queue has no default and tasks must set their own.
    repo: str | None = None


class QueueConfigUpdate(BaseModel):
    """Several per-queue config values in one request — used by the Add-queue
    screen to configure a new queue on creation. Every field is optional; only
    the fields actually present are written. ``repo`` set to "" or "default"
    clears that key (the queue inherits instead)."""

    # ``validate`` on the wire; the field is named ``validate_cmd`` to avoid
    # shadowing ``BaseModel.validate``.
    model_config = ConfigDict(populate_by_name=True)

    repo: str | None = None
    validate_cmd: str | None = Field(default=None, alias="validate")
    # "on"/"off" (also accepts true/false/1/0); conflict-handling policy.
    auto_resolve: str | None = None


class QueuePlayPriorities(BaseModel):
    # Play-priority filter: the 0-5 levels allowed to play. Empty = all.
    priorities: list[int]


class PlaylistCreate(BaseModel):
    name: str


class PlaylistUpdate(BaseModel):
    """Edit a playlist from its info page. ``name`` renames the queue (its
    on-disk dir, which carries its tasks + run records); ``repository`` is the
    alias the UI shows for the queue's default ``repo`` binding. Both optional;
    only fields present in the request are applied."""

    name: str | None = None
    repository: str | None = None
    validate: str | None = None
    # Hide the playlist from the default Playlists view and exclude it from
    # scheduling; ``False`` re-enables it. ``None`` leaves the flag untouched.
    disabled: bool | None = None


class QueueImport(BaseModel):
    """Copy tasks from another queue into the active one. ``source`` is a
    playlist name, or null for the main ``.tasks`` queue; ``tasks`` names the
    specific task ids to copy, or is null/empty to copy the whole source queue."""

    source: str | None = None
    tasks: list[str] | None = None


class ActiveRequest(BaseModel):
    # The playlist to make active, or None to return to the main `.tasks` queue.
    playlist: str | None = None


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def create_app(workspace: Path) -> FastAPI:
    workspace = workspace.resolve()
    tasks_repo = resolve_tasks_repo(workspace)
    # Two roots: ``tasks_root`` (the content store ``<workspace>/<tasks_repo>``)
    # holds briefs + queue config and the gitignored per-queue ``runs/``; git
    # ops + worktrees run under ``workspace`` (resolved per task inside the
    # engine). Each engine call below is threaded with whichever root it needs.
    tasks_root = workspace / tasks_repo
    app = FastAPI(title="Nightshift UI")
    player = Player(workspace)

    def _resolve_queue(queue: str | None) -> str | None:
        """Map a request's ``queue`` param to a playlist name (None = main).

        An absent param (None) falls back to the *focused* queue for back-compat;
        an empty string targets the main ``.tasks`` queue explicitly; any other
        value is a playlist name. This is what lets the UI read and edit any
        queue — even one that isn't focused — while another queue is running."""
        if queue is None:
            return player.active_playlist()
        if queue == "":
            return None
        return queue

    def _queue_exists(queue: str | None) -> bool:
        return queue is None or playlists_mod.exists(tasks_root, queue)

    def _repo_for_queue(queue: str | None) -> str | None:
        """The queue's resolved default target repo (or ``None`` when unset or
        malformed). Used for best-effort worktree cleanup, whose artifacts live
        under ``workspace/.worktrees/<repo>/``."""
        rel = playlists_mod.tasks_rel(queue)
        try:
            return repos_mod.resolve_repo(
                None, load_queue_config(tasks_root, rel).get("repo")
            )
        except repos_mod.RepoConfigError:
            return None

    @app.get("/api/queue")
    def get_queue(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse(list_queue(tasks_root, playlists_mod.tasks_rel(target)))

    @app.get("/api/tasks/{task}")
    def get_task(task: str, queue: str | None = None) -> JSONResponse:
        """Read one queue task's brief (title, body, frontmatter) for the detail
        view. Live run status/log is layered on the client from `/api/runs`."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        try:
            return JSONResponse(
                read_task(tasks_root, task, playlists_mod.tasks_rel(target))
            )
        except FileNotFoundError:
            return JSONResponse({"error": "task not found"}, status_code=404)

    @app.put("/api/queue/order")
    def put_queue_order(req: QueueOrder, queue: str | None = None) -> JSONResponse:
        """Persist a drag-reordered queue to the target queue's `config.json`,
        returning the cleaned execution order so the client can confirm it."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse(
            {
                "order": reorder_queue(
                    tasks_root, req.order, playlists_mod.tasks_rel(target)
                )
            }
        )

    @app.get("/api/queue/sort")
    def get_queue_sort(queue: str | None = None) -> JSONResponse:
        """The target queue's sort mode ("manual" drag order or "priority")."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse(
            {"sort": load_sort_mode(tasks_root, playlists_mod.tasks_rel(target))}
        )

    @app.put("/api/queue/sort")
    def put_queue_sort(req: QueueSort, queue: str | None = None) -> JSONResponse:
        """Persist the target queue's sort mode, echoing the saved value. Unknown
        modes degrade to "manual". This drives both the UI display and the
        engine's play/execute order (both route through ``order_stems``)."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse(
            {"sort": save_sort_mode(tasks_root, req.sort, playlists_mod.tasks_rel(target))}
        )

    @app.get("/api/queue/play-priorities")
    def get_queue_play_priorities(queue: str | None = None) -> JSONResponse:
        """The target queue's play-priority filter (the 0-5 levels allowed to
        play). An empty list means "all priorities"."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse(
            {"priorities": load_play_priorities(tasks_root, playlists_mod.tasks_rel(target))}
        )

    @app.put("/api/queue/play-priorities")
    def put_queue_play_priorities(
        req: QueuePlayPriorities, queue: str | None = None
    ) -> JSONResponse:
        """Persist the target queue's play-priority filter, echoing the cleaned
        list (sorted, de-duped, 0-5 only). An empty list clears the filter so all
        priorities play. This restricts the engine's play/execute set."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse(
            {
                "priorities": save_play_priorities(
                    tasks_root, req.priorities, playlists_mod.tasks_rel(target)
                )
            }
        )

    @app.get("/api/queue/repo")
    def get_queue_repo(queue: str | None = None) -> JSONResponse:
        """The target queue's default target repo (the bare workspace-child name
        bound in its ``config.json``), or null when unset."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        repo = load_queue_config(
            tasks_root, playlists_mod.tasks_rel(target)
        ).get("repo")
        return JSONResponse({"repo": repo})

    @app.put("/api/queue/repo")
    def put_queue_repo(req: QueueRepo, queue: str | None = None) -> JSONResponse:
        """Persist the target queue's default repo into its ``config.json``. A
        null/empty value clears it; a malformed name is rejected (the
        path-traversal guard). A per-task ``repo`` override still wins at
        dispatch; availability is resolved inside the engine (a missing repo
        pauses the task rather than failing the queue)."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        try:
            repo = _normalize_repo(req.repo)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        save_queue_config_value(
            tasks_root, "repo", repo, playlists_mod.tasks_rel(target)
        )
        return JSONResponse({"repo": repo})

    @app.get("/api/queue/config")
    def get_queue_config(queue: str | None = None) -> JSONResponse:
        """The target queue's editable per-queue options (repo, validate
        command, conflict policy) read straight from its ``config.json`` — the
        surface the Add-queue screen seeds and edits."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        cfg = load_queue_config(tasks_root, playlists_mod.tasks_rel(target))
        return JSONResponse(
            {
                "repo": cfg.get("repo"),
                "validate": cfg.get("validate"),
                "auto_resolve": bool(cfg.get("auto_resolve", False)),
            }
        )

    @app.put("/api/queue/config")
    def put_queue_config(
        req: QueueConfigUpdate, queue: str | None = None
    ) -> JSONResponse:
        """Persist several per-queue options at once into the queue's
        ``config.json``. Only the fields present in the request are written, so
        an Add-queue form leaves unset options inheriting the main queue. Each
        value is validated/normalized the same way its dedicated endpoint does;
        a malformed repo is a 400 that writes nothing."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        rel = playlists_mod.tasks_rel(target)
        fields = req.model_dump(exclude_unset=True)
        try:
            if "repo" in fields:
                save_queue_config_value(
                    tasks_root, "repo", _normalize_repo(fields["repo"]), rel
                )
            if "validate_cmd" in fields:
                cmd = normalize_validate_command(str(fields["validate_cmd"]))
                save_queue_config_value(tasks_root, "validate", cmd, rel)
            if "auto_resolve" in fields:
                on = str(fields["auto_resolve"]).strip().lower() in ("on", "true", "1")
                save_queue_config_value(tasks_root, "auto_resolve", on, rel)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        cfg = load_queue_config(tasks_root, rel)
        return JSONResponse(
            {
                "repo": cfg.get("repo"),
                "validate": cfg.get("validate"),
                "auto_resolve": bool(cfg.get("auto_resolve", False)),
            }
        )

    def _queue_live_ids(queue: str | None) -> set[str]:
        # The live run id(s) of one queue, for reconciling *that* queue's store
        # without touching another queue's healthy run.
        return player.active_run_ids_by_queue().get(queue, set())

    @app.get("/api/runs")
    def get_runs(queue: str | None = None) -> list[dict]:
        # Self-heal: any run that looks "running" but isn't this queue's live run
        # is marked aborted (persisted) so the pane never shows a phantom run.
        target = _resolve_queue(queue)
        store = player.store_for(target)
        store.reconcile_stale(_queue_live_ids(target))
        return store.list_runs()

    @app.delete("/api/runs")
    def clear_runs(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        store = player.store_for(target)
        keep = _queue_live_ids(target)
        runs = store.list_runs()
        removed = store.clear_runs(keep=keep)
        # Reap the preserved worktree/branch of every cleared task, with the same
        # orphan guard as delete_run: skip the live track and any task a surviving
        # (kept/active) run still references. Scoped to this queue so a same-named
        # task in another queue keeps its namespaced worktree.
        now_playing = player.live_task(target)
        still_referenced = {
            t.get("task")
            for r in runs
            if r.get("id") in keep
            for t in r.get("tasks", [])
        }
        cleared_tasks = {
            t.get("task")
            for r in runs
            if r.get("id") not in keep
            for t in r.get("tasks", [])
        }
        cleaned: list[str] = []
        for name in cleared_tasks:
            if not name or name == now_playing or name in still_referenced:
                continue
            try:
                repo = _repo_for_queue(target)
                if repo and cleanup_task_worktree(
                    workspace, repo, name, queue=target
                ):
                    cleaned.append(name)
            except Exception:
                # Best-effort; the records are already cleared.
                pass
        return JSONResponse({"cleared": removed, "cleaned": cleaned})

    @app.get("/api/runs/{run_id}/{task}/log")
    def get_log(
        run_id: str, task: str, offset: int = 0, queue: str | None = None
    ) -> dict:
        return player.store_for(_resolve_queue(queue)).read_log(run_id, task, offset)

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str, queue: str | None = None) -> JSONResponse:
        if run_id in player.active_run_ids():
            return JSONResponse(
                {"error": "cannot delete the active run; stop it first"},
                status_code=409,
            )
        target = _resolve_queue(queue)
        store = player.store_for(target)
        runs = store.list_runs()
        run = next((r for r in runs if r.get("id") == run_id), None)
        if run is None or not store.delete_run(run_id):
            return JSONResponse({"error": "run not found"}, status_code=404)
        # Also remove the preserved worktree/branch a failed-to-land task leaves
        # behind — but only when truly orphaned: not the live track, and not
        # referenced by any *surviving* run (which might still want to Resolve
        # it). The active run is among `runs` and != run_id, so its tasks are
        # protected by `still_referenced` automatically.
        now_playing = player.live_task(target)
        still_referenced = {
            t.get("task")
            for r in runs
            if r.get("id") != run_id
            for t in r.get("tasks", [])
        }
        cleaned: list[str] = []
        for t in run.get("tasks", []):
            name = t.get("task")
            if not name or name == now_playing or name in still_referenced:
                continue
            try:
                repo = _repo_for_queue(target)
                if repo and cleanup_task_worktree(
                    workspace, repo, name, queue=target
                ):
                    cleaned.append(name)
            except Exception:
                # Cleanup is best-effort; the run record is already gone.
                pass
        return JSONResponse({"deleted": run_id, "cleaned": cleaned})

    @app.post("/api/runs/{run_id}/{task}/resolve")
    @app.post("/api/runs/{run_id}/{task}/recover")
    def resolve_run_task(
        run_id: str, task: str, queue: str | None = None
    ) -> JSONResponse:
        """Resolve a validated-but-unlanded task (its worktree branch was
        preserved on the squash failure): the engine re-squashes a transient
        blocker, or runs an agent to rebase + resolve a content conflict. Returns
        immediately; progress streams over SSE. ``/recover`` is a back-compat
        alias for the same handler. The task is resolved on its own queue's
        runner (``queue`` defaults to the focused queue)."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        result = player.runner(target).resolve(run_id, task)
        if not result.get("ok"):
            return JSONResponse(result, status_code=409)
        return JSONResponse(result)

    def _state_payload() -> dict[str, Any]:
        """The state envelope: a per-queue ``queues`` map (the multi-queue
        surface the UI consumes) plus a flat focused-queue state for back-compat
        with any caller still reading the single-context shape."""
        return {**player.state(), "queues": player.states()}

    @app.get("/api/state")
    def get_state() -> dict:
        return _state_payload()

    @app.post("/api/transport")
    def post_transport(req: TransportRequest) -> JSONResponse:
        if req.action not in _VALID_ACTIONS:
            return JSONResponse(
                {"error": f"unknown action: {req.action}"}, status_code=400
            )
        if req.mode is not None and req.mode not in _VALID_MODES:
            return JSONResponse(
                {"error": f"unknown mode: {req.mode}"}, status_code=400
            )
        target = _resolve_queue(req.queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        runner = player.runner(target)
        if req.action == "play":
            runner.play(mode=req.mode, task=req.task)
        elif req.action == "pause":
            runner.pause()
        elif req.action == "stop":
            runner.stop()
        elif req.action == "skip":
            runner.skip()
        elif req.action == "select":
            runner.select(req.task)
        return JSONResponse(_state_payload())

    @app.get("/api/task-defaults")
    def get_task_defaults(queue: str | None = None) -> JSONResponse:
        """Brief-shaped defaults for a brand-new (not-yet-created) task, so the
        detail-view pane can seed its create form the same way it does an edit:
        effective model/draft/automerge for the target queue, plus the curated
        model choices for the dropdown. No file is read or written."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        config = resolve_config(workspace, tasks_root, playlists_mod.tasks_rel(target))
        resolved = resolve_frontmatter({}, config)
        return JSONResponse(
            {
                "task": None,
                "title": "",
                "body": "",
                "frontmatter": {
                    "model": resolved["model"],
                    "draft": resolved["draft"],
                    "automerge": resolved["automerge"],
                },
                "frontmatter_raw": {},
                "evergreen": False,
                "disabled": False,
                "model_options": list(config.get("scheduled_models", [])),
            }
        )

    @app.post("/api/tasks")
    def post_task(req: TaskCreate, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        target_rel = playlists_mod.tasks_rel(target)
        # Validate the optional repo override *before* writing the brief so a
        # malformed ref is a clean 400 that never orphans a file in the content
        # store. (The post-create pass re-normalizes the same value.)
        try:
            _normalize_repo(req.repo)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            created = create_task(tasks_root, req.title, req.text, target_rel)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileExistsError as exc:
            return JSONResponse(
                {"error": f"task already exists: {exc}"}, status_code=409
            )
        # Apply the detail pane's optional frontmatter (toggles + model) to the
        # freshly created file so create and edit share one surface. A "" /
        # "default" model leaves the key unset so the task inherits the default.
        fields = req.model_dump(
            exclude_unset=True,
            include={
                "disabled", "evergreen", "automerge", "draft", "model",
                "priority", "repo",
            },
        )
        changes: dict[str, object | None] = {}
        try:
            for key, value in fields.items():
                if key == "model":
                    changes["model"] = None if value in (None, "", "default") else value
                elif key == "priority":
                    if value is not None:
                        changes["priority"] = _validate_priority(value)
                elif key == "repo":
                    changes["repo"] = _normalize_repo(value)
                else:
                    changes[key] = value
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if changes:
            try:
                set_task_meta(tasks_root, created["task"], changes, target_rel)
            except (FileNotFoundError, ValueError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(created, status_code=201)

    def _is_live_task(target: str | None, task: str) -> bool:
        """True only when ``task`` is the live track of the *running* queue, so
        edits to any other queue (or any non-running task) are always allowed."""
        st = player.state()
        return st.get("running_playlist") == target and st.get("now_playing") == task

    @app.patch("/api/tasks/{task}")
    def patch_task(task: str, req: TaskUpdate, queue: str | None = None) -> JSONResponse:
        """Save a queue task's edits (title, brief body, and the frontmatter
        toggles: enable/disable, evergreen, automerge, draft, model) from the
        detail-view pane. Refused only while the task is the live track of the
        running queue so a running worker's spec can't change under it — any
        other queue stays fully editable."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        if _is_live_task(target, task):
            return JSONResponse(
                {"error": "task is currently running; stop it first"},
                status_code=409,
            )
        fields = req.model_dump(exclude_unset=True)
        changes: dict[str, object | None] = {}
        try:
            for key, value in fields.items():
                if key == "model":
                    # "" / "default" clears the key so the task inherits the default.
                    changes["model"] = None if value in (None, "", "default") else value
                elif key == "priority":
                    if value is not None:
                        changes["priority"] = _validate_priority(value)
                elif key == "repo":
                    # "" / "default" clears the override → inherit the queue repo.
                    changes["repo"] = _normalize_repo(value)
                else:
                    changes[key] = value
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not changes:
            return JSONResponse({"error": "no fields to update"}, status_code=400)
        try:
            updated = set_task_meta(
                tasks_root, task, changes, playlists_mod.tasks_rel(target)
            )
        except FileNotFoundError:
            return JSONResponse({"error": "task not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(updated)

    @app.delete("/api/tasks/{task}")
    def remove_task(task: str, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        if _is_live_task(target, task):
            return JSONResponse(
                {"error": "task is currently running; stop it first"},
                status_code=409,
            )
        try:
            deleted = delete_task(tasks_root, task, playlists_mod.tasks_rel(target))
        except FileNotFoundError:
            return JSONResponse({"error": "task not found"}, status_code=404)
        return JSONResponse(deleted)

    # ----- playlists (alternate queues) -------------------------------- #

    @app.get("/api/active")
    def get_active() -> dict:
        return {"active_playlist": player.active_playlist()}

    @app.post("/api/active")
    def set_active(req: ActiveRequest) -> JSONResponse:
        """Switch the *focused* queue to a playlist (or back to main when null).

        Always allowed — it only moves the UI's view/edit focus. A run on another
        queue keeps running and is unaffected, so you can browse and edit any
        queue while one is playing."""
        if req.playlist is not None and not playlists_mod.exists(tasks_root, req.playlist):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        return JSONResponse(player.set_active(req.playlist))

    @app.get("/api/playlists")
    def get_playlists() -> list[dict]:
        return playlists_mod.list_playlists(tasks_root)

    @app.get("/api/main/tasks")
    def get_main_tasks() -> JSONResponse:
        """The main ``.tasks/`` queue's tasks, surfaced in the Add-from picker
        as the special ‘library’ playlist (so a playlist can pull from main)."""
        return JSONResponse(list_queue(tasks_root, playlists_mod.tasks_rel(None)))

    @app.get("/api/playlists/{name}/tasks")
    def get_playlist_tasks(name: str) -> JSONResponse:
        """List a playlist's tasks without making it active, so the Add-from
        picker can preview and copy individual tasks."""
        if not playlists_mod.exists(tasks_root, name):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        return JSONResponse(list_queue(tasks_root, playlists_mod.tasks_rel(name)))

    @app.post("/api/queue/import")
    def import_into_queue(req: QueueImport, queue: str | None = None) -> JSONResponse:
        """Copy task(s) from another queue (a playlist, or the main queue when
        ``source`` is null) into the target queue, appending them to its order."""
        if req.source is not None and not playlists_mod.exists(tasks_root, req.source):
            return JSONResponse({"error": "source playlist not found"}, status_code=404)
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        src_rel = playlists_mod.tasks_rel(req.source)
        dest_rel = playlists_mod.tasks_rel(target)
        if src_rel == dest_rel:
            return JSONResponse(
                {"error": "source and destination are the same queue"}, status_code=400
            )
        tasks = req.tasks or [t["task"] for t in list_queue(tasks_root, src_rel)]
        imported: list[dict] = []
        for task in tasks:
            try:
                imported.append(import_task(tasks_root, src_rel, task, dest_rel))
            except FileNotFoundError:
                return JSONResponse(
                    {"error": f"task not found in source: {task}"}, status_code=404
                )
        return JSONResponse({"imported": imported}, status_code=201)

    @app.post("/api/playlists")
    def post_playlist(req: PlaylistCreate) -> JSONResponse:
        try:
            created = playlists_mod.create_playlist(tasks_root, req.name)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileExistsError as exc:
            return JSONResponse(
                {"error": f"playlist already exists: {exc}"}, status_code=409
            )
        return JSONResponse(created, status_code=201)

    def _playlist_info(name: str) -> dict[str, Any]:
        """The playlist-info payload: name, task count, and the queue's ``repo``
        binding aliased to ``repository`` for the info page."""
        count = len(list_queue(tasks_root, playlists_mod.tasks_rel(name)))
        cfg = load_queue_config(tasks_root, playlists_mod.tasks_rel(name))
        disabled = playlists_mod.is_disabled(tasks_root, name)
        return {
            "name": name,
            "task_count": count,
            "repository": cfg.get("repo"),
            "validate": cfg.get("validate"),
            "disabled": disabled,
        }

    @app.get("/api/playlists/{name}")
    def get_playlist(name: str) -> JSONResponse:
        if not playlists_mod.exists(tasks_root, name):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        return JSONResponse(_playlist_info(name))

    @app.put("/api/playlists/{name}")
    def put_playlist(name: str, req: PlaylistUpdate) -> JSONResponse:
        if not playlists_mod.exists(tasks_root, name):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        current = name
        if req.name is not None and playlists_mod.slugify_name(req.name) != name:
            if player.is_running(name):
                return JSONResponse(
                    {"error": "playlist is currently running; stop it first"},
                    status_code=409,
                )
            try:
                current = playlists_mod.rename_playlist(tasks_root, name, req.name)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            except FileExistsError as exc:
                return JSONResponse(
                    {"error": f"playlist already exists: {exc}"}, status_code=409
                )
            except FileNotFoundError:
                return JSONResponse({"error": "playlist not found"}, status_code=404)
            player.rename_queue(name, current)
        if "repository" in req.model_dump(exclude_unset=True):
            try:
                repo = _normalize_repo(req.repository)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            save_queue_config_value(
                tasks_root, "repo", repo, playlists_mod.tasks_rel(current)
            )
        if "validate" in req.model_dump(exclude_unset=True):
            cmd = normalize_validate_command(str(req.validate or ""))
            save_queue_config_value(
                tasks_root, "validate", cmd, playlists_mod.tasks_rel(current)
            )
        if req.disabled is not None:
            playlists_mod.set_playlist_disabled(tasks_root, current, req.disabled)
        return JSONResponse(_playlist_info(current))

    @app.delete("/api/playlists/{name}")
    def remove_playlist(name: str) -> JSONResponse:
        # Only refuse when the playlist is the queue a run is actively draining;
        # an idle playlist can be deleted even while another queue runs.
        if player.is_running(name):
            return JSONResponse(
                {"error": "playlist is currently running; stop it first"},
                status_code=409,
            )
        # Deleting the focused playlist drops your view back to the main queue.
        if player.active_playlist() == name:
            player.set_active(None)
        if not playlists_mod.delete_playlist(tasks_root, name):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        return JSONResponse({"name": name, "deleted": True})

    @app.post("/api/playlists/rescan")
    def rescan_playlists() -> JSONResponse:
        """Scan the workspace's immediate children for git repos and materialise
        one playlist per repo (name = repo dir name), binding each playlist's
        default repo to the discovered repo. The content-store repo is skipped."""
        repo_names = repos_mod.known_repos(workspace)
        result = playlists_mod.rescan_into_playlists(
            tasks_root, repo_names, skip={tasks_repo}
        )
        return JSONResponse(
            {**result, "playlists": playlists_mod.list_playlists(tasks_root)}
        )

    @app.get("/api/backends")
    def get_backends() -> dict:
        """The shim surface: which worker backends exist, whether each is usable
        here, and which one new runs will use."""
        try:
            config = load_config(workspace)
        except (FileNotFoundError, ValueError):
            config = {}
        worker_data = load_json(worker_json_path(workspace))
        current = worker_data.get("backend", "claude-code")
        return {"backends": list_backends(config), "current": current}

    def _queue_validate() -> str:
        """The active queue's effective validate command (resolved through the
        config layering).

        An *absent* ``validate`` key inherits the engine default; a key set to an
        empty string is a deliberate opt-out (no validation) and is surfaced as
        the empty string so the field reads back as cleared rather than the
        default.
        """
        tasks_rel = player.tasks_rel()
        cfg = resolve_config(workspace, tasks_root, tasks_rel)
        if "validate" not in cfg:
            return DEFAULT_VALIDATE_CMD
        return str(cfg.get("validate") or "").strip()

    # Single-host/server mode exposes all three surfaces.
    _SERVER_SURFACES = ["manager", "worker", "player"]

    @app.get("/api/settings")
    def get_settings() -> JSONResponse:
        return JSONResponse(build_get_response(workspace, _SERVER_SURFACES))

    @app.put("/api/settings")
    def put_settings(body: dict[str, Any]) -> JSONResponse:
        allowed = set(_SERVER_SURFACES)
        resolved, errors = validate_delta(body, allowed)
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=400)

        applied_live, restart_required = write_delta(workspace, resolved)

        return JSONResponse({
            "ok": True,
            "applied_live": applied_live,
            "restart_required": restart_required,
            **build_get_response(workspace, _SERVER_SURFACES),
        })


    # ----- repos (workspace target repos) ------------------------------ #

    def _repos_payload() -> dict[str, Any]:
        """Mirror the manager's ``/api/repos`` shape so the shared UI works under
        the server: the known-repos set (direct workspace children with a
        ``.git``), each queue's bound repo + availability, and one warning per
        queue whose configured repo is currently absent. The server recomputes
        live from the filesystem and keeps no DB-backed paused table, so
        ``warnings`` is derived purely from queue config vs the known set."""
        known = repos_mod.known_repos(workspace)
        known_set = set(known)
        queues: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        # The main queue first, then every alternate queue.
        alt = [p["name"] for p in playlists_mod.list_playlists(tasks_root)]
        for queue in [None, *alt]:
            rel = playlists_mod.tasks_rel(queue)
            qrepo = load_queue_config(tasks_root, rel).get("repo")
            label = queue or "main"
            available = bool(qrepo) and qrepo in known_set
            queues.append({"queue": label, "repo": qrepo, "available": available})
            if qrepo and not available:
                warnings.append({"queue": label, "repo": qrepo})
        return {
            "workspace": str(workspace),
            "tasks_repo": tasks_repo,
            "repos": [{"name": name, "available": True} for name in known],
            "queues": queues,
            "warnings": warnings,
        }

    @app.get("/api/repos")
    def get_repos() -> dict:
        """Known workspace repos + per-queue bindings (mirrors the manager)."""
        return _repos_payload()

    @app.post("/api/repos/rescan")
    def rescan_repos() -> dict:
        """Recompute the known-repos set and return the fresh ``/api/repos``
        payload. The known set is read live from the filesystem on every request
        and the server has no DB-backed paused-state table, so a paused
        (``repo_unavailable``) task auto-resumes on its next play once its repo is
        present — a rescan here is simply a recompute + refresh."""
        return _repos_payload()

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        server = getattr(request.app.state, "uvicorn_server", None)

        def is_shutting_down() -> bool:
            # End the stream when the server starts shutting down (Ctrl-C), so
            # this long-lived connection doesn't block graceful shutdown.
            return server is not None and bool(server.should_exit)

        return StreamingResponse(
            sse_stream(player, request.is_disconnected, is_shutting_down),
            media_type="text/event-stream",
        )

    if UI_DIR.exists():
        app.mount("/", NoCacheStaticFiles(directory=str(UI_DIR), html=True), name="ui")

    return app


async def sse_stream(
    player: Player,
    is_disconnected: Any,
    is_shutting_down: Any = None,
    poll: float = 0.5,
) -> Any:
    """Yield SSE frames multiplexed across every queue: per-queue state changes
    and new engine events tailed from each watched queue's run record on disk.

    Every frame carries a ``queue`` key (``main`` or a playlist name) so the UI
    can route it to the right queue's card/log; the flat single-context fields
    are kept alongside for back-compat. We watch the *focused* queue (so the
    viewed queue streams even when idle) plus every *running* queue (so two
    concurrent runs both stream), each with its own tail cursor.

    ``is_disconnected`` is an async callable (``request.is_disconnected``) used
    to end the stream when the client goes away. ``is_shutting_down`` is an
    optional sync callable that ends the stream when the server is stopping
    (Ctrl-C), so this long-lived connection releases the graceful shutdown.
    """

    def _key(queue: str | None) -> str:
        return queue or "main"

    def _watched() -> dict[str | None, Any]:
        # Focused queue (always) + every running queue, deduped by queue.
        watch: dict[str | None, Any] = dict(player.active_runners())
        focused = player.active_playlist()
        watch.setdefault(focused, player.runner(focused))
        return watch

    last_states: dict[str, dict[str, Any]] = {}
    # Per-queue tail cursor: key -> (run_id, consumed-bytes).
    cursors: dict[str, tuple[str | None, int]] = {}

    for key, st in player.states().items():
        last_states[key] = st
        yield _sse({"kind": "state", "queue": key, **st})

    while True:
        if await is_disconnected():
            break
        if is_shutting_down is not None and is_shutting_down():
            break
        for key, st in player.states().items():
            if last_states.get(key) != st:
                yield _sse({"kind": "state", "queue": key, **st})
                last_states[key] = st
        for queue, runner in _watched().items():
            key = _key(queue)
            store = runner.store
            run_id, consumed = cursors.get(key, (None, 0))
            latest = store.latest_run_dir()
            if latest is not None and latest.name != run_id:
                run_id, consumed = latest.name, 0
            if run_id is not None:
                consumed, payloads = _read_new_events(store, run_id, consumed)
                for payload in payloads:
                    yield _sse({"kind": "event", "queue": key, **payload})
            cursors[key] = (run_id, consumed)
        await asyncio.sleep(poll)


def _read_new_events(
    store: RunStore, run_id: str, consumed: int
) -> tuple[int, list[dict[str, Any]]]:
    """Read complete new JSONL events since ``consumed`` bytes; return new offset."""
    path = store.base / run_id / "events.jsonl"
    if not path.exists():
        return consumed, []
    data = path.read_text(errors="replace")
    if consumed >= len(data):
        return consumed, []
    chunk = data[consumed:]
    last_nl = chunk.rfind("\n")
    if last_nl == -1:
        return consumed, []
    payloads: list[dict[str, Any]] = []
    for line in chunk[:last_nl].splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payloads.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return consumed + last_nl + 1, payloads
