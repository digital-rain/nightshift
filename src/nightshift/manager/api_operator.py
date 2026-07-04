"""Operator-facing manager API — queue / tasks / playlists / repos / runs /
workers / stats / settings / transport plus the ``/api/events`` SSE stream.

Split out of ``manager/app.py`` in Phase 3 of the rebuild-in-place migration;
handler logic is unchanged. Endpoints are registered onto the shared FastAPI
app by :func:`register_operator_api`; the app wiring (store, registry, event
emitter, shared queue state) is injected by ``create_app``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from nightshift import playlists as playlists_mod
from nightshift import repos
from nightshift.config.validate import build_get_response, validate_delta, write_delta
from nightshift.enhance import EnhanceError, enhance_brief
from nightshift.git.executor import ExecutorPool
from nightshift.git.store import commit_tasks
from nightshift.lifecycle import AttemptRef, AttemptState, TaskHoldKind
from nightshift.manager import failure_policy
from nightshift.manager.api_playlists import register_playlist_api
from nightshift.manager.config import ManagerConfig
from nightshift.manager.registry import Registry
from nightshift.manager.scheduler import queue_label
from nightshift.manager.store import NightshiftStore
from nightshift.manager.views import analytics_run_view, lease_view, run_view
from nightshift.manager.wire import (
    EmitFn,
    StartResolveFn,
    jsonable,
    normalize_repo,
)
from nightshift.queue_config import (
    load_play_priorities,
    load_sort_mode,
    reorder_queue,
    save_play_priorities,
    save_queue_config_value,
    save_sort_mode,
)
from nightshift.spawn_daily import (
    MAX_PRIORITY,
    MIN_PRIORITY,
    load_queue_config,
    resolve_config,
    resolve_frontmatter,
)
from nightshift.task_files import (
    create_task,
    delete_task,
    frontmatter_held_tasks,
    list_queue,
    read_task,
    set_task_meta,
)
from nightshift.transitions import on_operator_stop


class QueueOrder(BaseModel):
    order: list[str]


class QueueSort(BaseModel):
    sort: str


class QueuePlayPriorities(BaseModel):
    priorities: list[int]


class QueueDedication(BaseModel):
    # Worker ids this queue is dedicated to (empty list clears dedication).
    worker_ids: list[str]


class QueueConfig(BaseModel):
    # Per-queue default target repo (workspace-relative child name). ``None``
    # clears the binding (a queue with no repo is an authoring error on dispatch).
    repo: str | None = None


class ActivePlaylistRequest(BaseModel):
    playlist: str | None = None


class TransportRequest(BaseModel):
    action: str
    mode: str | None = None
    task: str | None = None
    queue: str | None = None


class TaskCreate(BaseModel):
    title: str
    text: str
    quarantined: bool | None = None
    # Optional per-task repo override (defaults to the queue's repo). Written as
    # an editable frontmatter meta key on the new brief.
    repo: str | None = None
    loop: bool | None = None
    loop_max_iterations: int | None = None
    # Enhance-on-create: run the manager-side brief rewrite before writing the
    # file. ``text`` is then the ORIGINAL brief; the enhanced rewrite becomes
    # the effective body and the original is preserved below the marker.
    enhance: bool = False


class TaskUpdate(BaseModel):
    """Partial edit from the detail-view pane. Unset fields are left untouched;
    ``model`` set to "" or "default" clears the field so the task inherits the
    config default. ``title`` (headline) and ``body`` (spec prose) are content
    edits saved alongside the frontmatter toggles in one PATCH. The accepted keys
    mirror :data:`engine._EDITABLE_META_KEYS` ∪ :data:`engine._EDITABLE_CONTENT_KEYS`
    — any field declared here but absent from the model is silently dropped by
    ``model_dump(exclude_unset=True)`` and yields a spurious "no fields to update".
    """

    disabled: bool | None = None
    quarantined: bool | None = None
    failed: bool | None = None
    completed: bool | None = None
    evergreen: bool | None = None
    automerge: bool | None = None
    draft: bool | None = None
    model: str | None = None
    priority: int | None = None
    title: str | None = None
    body: str | None = None
    # Per-task target-repo override; "" / "default" clears it (inherit queue).
    repo: str | None = None
    loop: bool | None = None
    loop_max_iterations: int | None = None
    split: bool | None = None
    # The preserved pre-enhancement text ("" drops the marker section).
    original_brief: str | None = None


class RunRating(BaseModel):
    """The operator's thumbs verdict on a run: 'up', 'down', or null (clear)."""

    rating: str | None = None


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


def _workspace_tier(workspace: Path) -> dict:
    """Build a read-only 'Workspace' tier for the /api/settings response."""
    env_var = "NIGHTSHIFT_WORKSPACE"
    env_val = os.environ.get(env_var)
    return {
        "surface": "workspace",
        "categories": [
            {
                "name": "Workspace",
                "fields": [
                    {
                        "key": "location",
                        "label": "Location",
                        "desc": (
                            "The workspace directory Nightshift reads and writes repos "
                            "and config from. Set via --workspace flag or "
                            f"{env_var} env var; resolved at launch."
                        ),
                        "type": "readonly",
                        "apply": "restart",
                        "store": "—",
                        "default": None,
                        "secret": False,
                        "stored": str(workspace),
                        "effective": str(workspace),
                        "env": env_var,
                        "env_shadowed": bool(env_val),
                    }
                ],
            }
        ],
    }


def register_operator_api(
    app: FastAPI,
    *,
    cfg: ManagerConfig,
    workspace: Path,
    tasks_root: Path,
    tasks_repo: str,
    _store: Callable[[], NightshiftStore],
    _registry: Callable[[], Registry],
    _emit: EmitFn,
    _queue_from_label: Callable[[str | None], str | None],
    _all_queues: Callable[[], list[str | None]],
    # Per-queue-label failure-policy watch state; popped here when an operator
    # clears a quarantine/failure or presses play.
    _queue_failure_state: dict[str, failure_policy.QueueFailureState],
    _start_resolve: StartResolveFn,
    # Phase 7: the per-repo git executor pool (content-store commits are
    # tasks-repo jobs). Queue pause/mode state lives in the store now.
    _executors: ExecutorPool,
) -> None:
    """Register the operator endpoints. Shared wiring (store/registry accessors,
    the event emitter, queue-failure state, and the resolve spawner) is
    injected by ``create_app`` under the same names the handler bodies always
    used."""
    # UI focus: the playlist the operator is looking at. Declared ahead of
    # ``_resolve_queue`` (which closes over it); mutated only by ``set_active``.
    _active_playlist: str | None = None

    async def _commit(message: str) -> None:
        """Commit content-store churn as a tasks-repo executor job — the tasks
        repo is a repo like any other, so its commits serialize with every
        other git job targeting it (Phase 7)."""
        await asyncio.wrap_future(
            _executors.submit(tasks_repo, partial(commit_tasks, tasks_root, message))
        )

    def _resolve_queue(queue: str | None) -> str | None:
        """Operator API queue resolution: absent param (None) falls back to the
        focused queue so ``GET /api/queue`` returns the active playlist's tasks.
        An empty string targets main explicitly."""
        if queue is None:
            return _active_playlist
        if queue == "":
            return None
        return _queue_from_label(queue)

    def _queue_exists(queue: str | None) -> bool:
        return queue is None or playlists_mod.exists(tasks_root, queue)

    def _queue_repo(queue: str | None) -> str | None:
        """The queue's configured default target repo (or ``None`` when unset)."""
        return load_queue_config(
            tasks_root, playlists_mod.tasks_rel(queue)
        ).get("repo")

    # ===================================================================== #
    # Operator API
    # ===================================================================== #

    @app.post("/api/runs/{run_id}/{task}/resolve")
    @app.post("/api/runs/{run_id}/{task}/recover")
    async def resolve_run_task(
        run_id: str, task: str, queue: str | None = None
    ) -> JSONResponse:
        """Operator-triggered resolve of a conflicted task. Spawns the resolver
        out-of-process (non-blocking) so the manager stays responsive and other
        tasks keep dispatching while the agent works."""
        store = _store()
        origin = await store.get_attempt(run_id)
        if origin is None:
            raise HTTPException(status_code=404, detail="unknown run")
        target_queue = (
            _queue_from_label(queue) if queue is not None
            else _queue_from_label(origin.get("queue"))
        )
        repo = origin.get("repo") or _queue_repo(target_queue)
        if not repo:
            raise HTTPException(
                status_code=400, detail="no target repo for this task"
            )
        title = origin.get("title") or task
        started, child_run_id, error = await _start_resolve(
            run_id, task=task, queue=target_queue, repo=repo, title=title,
        )
        if not started:
            return JSONResponse({"ok": False, "error": error}, status_code=409)
        return JSONResponse(
            {"ok": True, "run_id": child_run_id, "task": task}, status_code=202
        )

    @app.get("/api/queue")
    def get_queue(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse(list_queue(tasks_root, playlists_mod.tasks_rel(target)))

    @app.get("/api/main/tasks")
    def get_main_tasks() -> JSONResponse:
        """The main queue's tasks, surfaced in the Playlists screen as the
        library row count and in the Add-from picker."""
        return JSONResponse(list_queue(tasks_root, playlists_mod.tasks_rel(None)))

    @app.get("/api/tasks/{task}")
    async def get_task(task: str, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        try:
            info = read_task(tasks_root, task, playlists_mod.tasks_rel(target))
        except FileNotFoundError:
            return JSONResponse({"error": "task not found"}, status_code=404)
        label = queue_label(target)
        info["model_options"] = await _registry().models_for_queue(label)
        return JSONResponse(info)

    @app.get("/api/task-defaults")
    async def get_task_defaults(queue: str | None = None) -> JSONResponse:
        """Brief-shaped defaults for a new task: effective model/draft/automerge
        for the target queue, plus live model choices from registered workers."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        config = resolve_config(workspace, tasks_root, playlists_mod.tasks_rel(target))
        resolved = resolve_frontmatter({}, config)
        label = queue_label(target)
        models = await _registry().models_for_queue(label)
        return JSONResponse(
            {
                "task": None,
                "title": "",
                "body": "",
                "original_brief": "",
                "frontmatter": {
                    "model": resolved["model"],
                    "draft": resolved["draft"],
                    "automerge": resolved["automerge"],
                    "priority": 3,
                },
                "frontmatter_raw": {},
                "evergreen": False,
                "disabled": False,
                "model_options": models,
            }
        )

    @app.post("/api/tasks")
    async def post_task(body: TaskCreate, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        target_rel = playlists_mod.tasks_rel(target)
        # Validate the optional repo override *before* writing the brief so a
        # malformed ref is a clean 400 that never orphans a file in the content
        # store (matches the legacy server and the contract's edit-time guard).
        try:
            repo_override = normalize_repo(body.repo)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        # Enhance-on-create: the rewrite happens BEFORE the file exists, so a
        # failed enhancement never creates a task (the operator's draft
        # survives client-side — they can retry or toggle enhancement off).
        # The blocking transport runs off the event loop; either outcome is
        # recorded in the enhancements telemetry table.
        text, original = body.text, None
        enhance_telemetry: dict[str, Any] | None = None
        if body.enhance:
            started = time.monotonic()
            try:
                result = await asyncio.to_thread(
                    enhance_brief,
                    body.title,
                    body.text,
                    model=cfg.enhance_brief_model,
                    env=dict(os.environ),
                )
            except EnhanceError as exc:
                await _store().record_enhancement(
                    uuid.uuid4().hex,
                    queue=target,
                    task=None,
                    model=cfg.enhance_brief_model,
                    input_tokens=None,
                    output_tokens=None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    ok=False,
                    error=str(exc),
                )
                return JSONResponse(
                    {"error": f"brief enhancement failed: {exc}"}, status_code=502
                )
            text, original = result.text, body.text
            usage = result.usage or {}
            enhance_telemetry = {
                "model": result.model,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        try:
            created = create_task(
                tasks_root, body.title, text, target_rel, original=original
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileExistsError as exc:
            return JSONResponse({"error": f"task already exists: {exc}"}, status_code=409)
        if enhance_telemetry is not None:
            await _store().record_enhancement(
                uuid.uuid4().hex,
                queue=target,
                task=created["task"],
                ok=True,
                **enhance_telemetry,
            )
        # Apply optional frontmatter fields from the create pane (repo
        # override, loop mode, enhancement attribution) to the freshly
        # created file.
        meta_changes: dict[str, object | None] = {}
        if repo_override is not None:
            meta_changes["repo"] = repo_override
        if body.loop is not None:
            meta_changes["loop"] = body.loop
        if body.loop_max_iterations is not None:
            meta_changes["loop_max_iterations"] = body.loop_max_iterations
        if body.enhance:
            meta_changes["enhanced"] = True
        if meta_changes:
            with contextlib.suppress(FileNotFoundError, ValueError):
                set_task_meta(
                    tasks_root, created["task"], meta_changes, target_rel
                )
        await _commit(f"nightshift: create task {created['task']}")
        await _emit("queue_changed", queue=target, task=created.get("task"))
        return JSONResponse(created)

    @app.patch("/api/tasks/{task}")
    async def patch_task(
        task: str, body: TaskUpdate, queue: str | None = None
    ) -> JSONResponse:
        target = _resolve_queue(queue)
        target_rel = playlists_mod.tasks_rel(target)
        # Partial edit: only the fields the detail pane actually sent reach the
        # frontmatter writer. ``model``/``priority``/``repo`` are normalised the
        # same way as the legacy server so the two backends behave identically.
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            return JSONResponse({"error": "no fields to update"}, status_code=400)
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
                    changes["repo"] = normalize_repo(value)
                else:
                    changes[key] = value
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            updated = set_task_meta(tasks_root, task, changes, target_rel)
        except FileNotFoundError:
            return JSONResponse({"error": "task not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        await _commit(f"nightshift: edit task {task}")
        # Quarantined/failed are frontmatter-authoritative: toggling them off
        # in the detail pane is all that's needed to release the task for
        # dispatch (live_ordered_queue re-scans frontmatter each cycle). No
        # DB overlay clearing is required for these two states.
        # The failure-policy watch is also reset when a quarantine/failure is
        # cleared so the queue can resume cleanly.
        label = queue_label(target)
        if "quarantined" in changes and not updated.get("quarantined"):
            _queue_failure_state.pop(label, None)
            # An operator release means "dispatchable now": drop any pending
            # retry backoff (the counter survives, as the streak history did).
            await _store().clear_task_backoff(target, task)
            await _emit(
                "task_released", queue=target, task=task,
                payload={"prior_state": "quarantined"},
            )
        if "failed" in changes and not updated.get("failed"):
            _queue_failure_state.pop(label, None)
            await _store().clear_task_backoff(target, task)
            await _emit(
                "task_released", queue=target, task=task,
                payload={"prior_state": "failed"},
            )
        await _emit("queue_changed", queue=target, task=task)
        return JSONResponse(updated)

    @app.post("/api/tasks/{task}/reset")
    async def reset_task(task: str, queue: str | None = None) -> JSONResponse:
        """Clear the DB ``blocked``/``repo_unavailable`` overlay for a task.

        This is the explicit release action for non-auto-clearing blocked
        states (validation-failed, unroutable, bad-repo-reference). It does
        NOT touch frontmatter-backed quarantined/failed flags — those are
        toggled via the normal PATCH/detail-pane save.
        """
        target = _resolve_queue(queue)
        store = _store()
        prior = await store.get_task_state(target, task)
        if not prior or prior.get("state") not in (
            TaskHoldKind.BLOCKED, TaskHoldKind.REPO_UNAVAILABLE,
        ):
            return JSONResponse(
                {"error": "task is not currently blocked"}, status_code=404,
            )
        await store.clear_task_state(target, task)
        await _emit(
            "task_released", queue=target, task=task,
            payload={"prior_state": prior.get("state")},
        )
        await _emit("queue_changed", queue=target, task=task)
        return JSONResponse({"released": True, "prior_state": prior.get("state")})

    @app.delete("/api/tasks/{task}")
    async def remove_task(task: str, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        result = delete_task(tasks_root, task, playlists_mod.tasks_rel(target))
        await _commit(f"nightshift: delete task {task}")
        await _emit("queue_changed", queue=target, task=task)
        return JSONResponse(result)

    @app.put("/api/queue/order")
    async def put_queue_order(req: QueueOrder, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        target_rel = playlists_mod.tasks_rel(target)
        order = reorder_queue(tasks_root, req.order, target_rel)
        await _commit(f"nightshift: reorder queue {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"order": order})
        return JSONResponse({"order": order})

    @app.get("/api/queue/sort")
    def get_queue_sort(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        return JSONResponse({"sort": load_sort_mode(tasks_root, playlists_mod.tasks_rel(target))})

    @app.put("/api/queue/sort")
    async def put_queue_sort(req: QueueSort, queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        target_rel = playlists_mod.tasks_rel(target)
        sort = save_sort_mode(tasks_root, req.sort, target_rel)
        await _commit(f"nightshift: set sort {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"sort": sort})
        return JSONResponse({"sort": sort})

    @app.get("/api/queue/play-priorities")
    def get_play_priorities(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        return JSONResponse(
            {"priorities": load_play_priorities(tasks_root, playlists_mod.tasks_rel(target))}
        )

    @app.put("/api/queue/play-priorities")
    async def put_play_priorities(
        req: QueuePlayPriorities, queue: str | None = None
    ) -> JSONResponse:
        target = _resolve_queue(queue)
        target_rel = playlists_mod.tasks_rel(target)
        priorities = save_play_priorities(tasks_root, req.priorities, target_rel)
        await _commit(f"nightshift: set play-priorities {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"priorities": priorities})
        return JSONResponse({"priorities": priorities})

    @app.get("/api/queue/config")
    def get_queue_config(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse({"repo": _queue_repo(target)})

    async def _set_queue_repo(target: str | None, req: QueueConfig) -> JSONResponse:
        """Persist a queue's default target repo into its ``config.json`` and
        commit the content store. A null/empty value clears the binding; a
        malformed name is rejected (the path-traversal guard — a per-task ``repo``
        override still wins at dispatch, but a bad queue default is an authoring
        error we surface here rather than at poll time)."""
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        repo_value = req.repo.strip() if isinstance(req.repo, str) else None
        repo_value = repo_value or None
        if repo_value is not None and not repos.is_valid_repo_ref(repo_value):
            return JSONResponse(
                {"error": (
                    f"invalid repo reference {repo_value!r}: a repo must be a bare "
                    "workspace child name (no paths, '..', '/', or absolute paths)"
                )},
                status_code=400,
            )
        target_rel = playlists_mod.tasks_rel(target)
        save_queue_config_value(tasks_root, "repo", repo_value, target_rel)
        await _commit(f"nightshift: set repo {queue_label(target)}")
        await _emit("queue_changed", queue=target, payload={"repo": repo_value})
        return JSONResponse({"repo": repo_value})

    @app.put("/api/queue/config")
    async def put_queue_config(req: QueueConfig, queue: str | None = None) -> JSONResponse:
        return await _set_queue_repo(_resolve_queue(queue), req)

    @app.get("/api/queue/repo")
    def get_queue_repo(queue: str | None = None) -> JSONResponse:
        """The target queue's default repo (mirrors the server's dedicated
        ``/api/queue/repo`` so the shared UI has one binding endpoint)."""
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        return JSONResponse({"repo": _queue_repo(target)})

    @app.put("/api/queue/repo")
    async def put_queue_repo(req: QueueConfig, queue: str | None = None) -> JSONResponse:
        return await _set_queue_repo(_resolve_queue(queue), req)

    @app.get("/api/queue/dedication")
    async def get_queue_dedication(queue: str | None = None) -> JSONResponse:
        dedication = await _store().queue_dedication()
        if queue is None:
            return JSONResponse({"dedication": dedication})
        label = queue_label(_queue_from_label(queue))
        return JSONResponse({"worker_ids": dedication.get(label, [])})

    @app.put("/api/queue/dedication")
    async def put_queue_dedication(
        req: QueueDedication, queue: str | None = None
    ) -> JSONResponse:
        target = _queue_from_label(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        label = queue_label(target)
        await _store().set_queue_dedication(label, req.worker_ids)
        await _emit(
            "queue_changed", queue=target, payload={"dedication": req.worker_ids}
        )
        return JSONResponse({"worker_ids": req.worker_ids})

    # ----- active playlist (UI focus) ----------------------------------- #
    # (``_active_playlist`` itself is declared at the top of this function.)

    @app.get("/api/active")
    def get_active() -> dict:
        return {"active_playlist": _active_playlist}

    @app.post("/api/active")
    def set_active(req: ActivePlaylistRequest) -> JSONResponse:
        nonlocal _active_playlist
        if req.playlist is not None and not playlists_mod.exists(tasks_root, req.playlist):
            return JSONResponse({"error": "playlist not found"}, status_code=404)
        _active_playlist = req.playlist
        return JSONResponse({"active_playlist": _active_playlist})

    # ----- transport (play/pause/stop/skip) -------------------------------- #

    _VALID_ACTIONS = {"play", "pause", "stop", "skip", "select"}
    _VALID_MODES = {"oneshot", "auto", "repeat"}

    # Pause/mode state lives in the store since Phase 7 (a manager restart no
    # longer silently unpauses queues); only the UI cursor stays in-memory.
    _queue_cursors: dict[str, str | None] = {}

    def _queue_state(
        queue: str | None,
        leases: list[dict[str, Any]],
        pauses: dict[str, str],
        modes: dict[str, str],
    ) -> dict[str, Any]:
        """Build a per-queue state dict in the shape the UI expects."""
        key = queue_label(queue)
        # Lease rows store the main queue as '' (see store._qkey); normalize to
        # the label before comparing.
        lease = next((le for le in leases if queue_label(le.get("queue")) == key), None)
        pause_reason = pauses.get(key)
        paused = pause_reason is not None
        if paused:
            st = "paused"
        elif lease:
            st = "playing"
        else:
            st = "idle"
        return {
            "state": st,
            "pause_reason": pause_reason,
            "mode": modes.get(key, "auto"),
            "now_playing": lease["task"] if lease else None,
            "cursor": _queue_cursors.get(key),
            "run_id": lease.get("run_id") if lease else None,
            "active_playlist": queue,
            "running_playlist": queue if lease else None,
        }

    async def _state_payload() -> dict[str, Any]:
        store = _store()
        # Live attempts projected to the historical lease dict shape (the
        # ``run_id`` key _queue_state serves on the wire).
        leases = [lease_view(a) for a in await store.live_attempts()]
        pauses = await store.queue_pauses()
        modes = await store.queue_modes()
        focused = _active_playlist
        focused_state = _queue_state(focused, leases, pauses, modes)
        queues: dict[str, dict[str, Any]] = {}
        queues[queue_label(focused)] = focused_state
        for q in _all_queues():
            key = queue_label(q)
            if key not in queues:
                queues[key] = _queue_state(q, leases, pauses, modes)
        return {**focused_state, "active_playlist": focused, "queues": queues}

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        return JSONResponse(await _state_payload())

    @app.post("/api/transport")
    async def post_transport(req: TransportRequest) -> JSONResponse:
        if req.action not in _VALID_ACTIONS:
            return JSONResponse(
                {"error": f"unknown action: {req.action}"}, status_code=400
            )
        if req.mode is not None and req.mode not in _VALID_MODES:
            return JSONResponse(
                {"error": f"unknown mode: {req.mode}"}, status_code=400
            )
        target = req.queue if req.queue not in (None, "") else _active_playlist
        key = queue_label(target)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        store = _store()
        if req.mode is not None:
            # "auto" is the default — persisting None lets the store prune the
            # row once neither a pause nor a non-default mode remains.
            await store.set_queue_mode(key, None if req.mode == "auto" else req.mode)
        async def _abort_live_attempts() -> None:
            """Operator stop/skip: abort every live attempt in the queue —
            CAS RUNNING first, then LANDING (a stop cancels mid-land too,
            exactly as before). Phase 8 behavior fix: the attempt is stored
            ABORTED with ``finished_at`` instead of the pre-phase
            cancelled-lease + forever-``running`` run row; the ``run_finished``
            emit is unchanged."""
            for att in await store.live_attempts():
                # Attempt rows store the main queue as '' (see store._qkey);
                # normalize to the label before comparing (main's fix, carried
                # into the Phase 8 rewrite of this block).
                if queue_label(att.get("queue")) != key:
                    continue
                t = on_operator_stop(AttemptRef(
                    id=att["id"],
                    queue=_queue_from_label(att["queue"]),
                    task=att["task"],
                ))
                applied = await store.apply_transition(
                    t, expected_status=AttemptState.RUNNING
                )
                if applied is None:
                    await store.apply_transition(
                        t, expected_status=AttemptState.LANDING
                    )
                await _emit("run_finished", run_id=att["id"], queue=target)

        if req.action == "play":
            await store.set_queue_pause(key, None)
            _queue_failure_state[key] = failure_policy.QueueFailureState()
        elif req.action == "pause":
            await store.set_queue_pause(key, "operator")
        elif req.action == "stop":
            await store.set_queue_pause(key, "operator")
            await _abort_live_attempts()
            await store.set_queue_pause(key, None)
        elif req.action == "skip":
            await _abort_live_attempts()
        elif req.action == "select":
            _queue_cursors[key] = req.task
        return JSONResponse(await _state_payload())

    register_playlist_api(
        app,
        workspace=workspace,
        tasks_root=tasks_root,
        tasks_repo=tasks_repo,
        _store=_store,
        _emit=_emit,
        _queue_from_label=_queue_from_label,
        _all_queues=_all_queues,
        _queue_repo=_queue_repo,
        _commit=_commit,
    )

    @app.get("/api/runs")
    async def get_runs(
        queue: str | None = None, limit: int = 200, since: str | None = None
    ) -> JSONResponse:
        target = _queue_from_label(queue) if queue is not None else None
        runs = await _store().list_attempts(
            limit=limit, queue=target if queue is not None else None, since=since
        )
        return JSONResponse([jsonable(run_view(r)) for r in runs])

    @app.get("/api/analytics/runs")
    async def get_analytics_runs(
        since: str | None = None, limit: int = 2000, queue: str | None = None
    ) -> JSONResponse:
        """Normalized run records for the shared analytics UI. Unlike
        ``/api/runs`` (a frozen shape), this exposes an explicit ``landed`` flag
        so the KPI can separate landed changes from no-change completions. A
        higher default ``limit`` lets a 30-day window aggregate client-side."""
        target = _queue_from_label(queue) if queue is not None else None
        runs = await _store().list_attempts(
            limit=limit, queue=target if queue is not None else None, since=since
        )
        return JSONResponse([jsonable(analytics_run_view(r)) for r in runs])

    @app.patch("/api/runs/{run_id}/rating")
    async def patch_run_rating(run_id: str, body: RunRating) -> JSONResponse:
        """Record the operator's thumbs verdict on a run ('up'/'down'; null
        clears it). The rating lives on the attempt row so the enhanced-vs-raw
        stats can aggregate satisfaction alongside outcome states."""
        if body.rating not in (None, "up", "down"):
            return JSONResponse(
                {"error": "rating must be 'up', 'down', or null"}, status_code=400
            )
        store = _store()
        attempt = await store.get_attempt(run_id)
        if attempt is None:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        await store.update_attempt(run_id, rating=body.rating)
        await _emit(
            "run_rated",
            run_id=run_id,
            queue=_queue_from_label(attempt.get("queue")),
            task=attempt.get("task"),
            payload={"rating": body.rating},
        )
        return JSONResponse({"id": run_id, "rating": body.rating})

    @app.get("/api/runs/{run_id}/events")
    async def get_run_events(run_id: str) -> JSONResponse:
        return JSONResponse([jsonable(e) for e in await _store().run_events(run_id)])

    @app.get("/api/workers")
    async def get_workers() -> JSONResponse:
        return JSONResponse([jsonable(w) for w in await _registry().snapshot()])

    @app.get("/api/models")
    async def get_models(queue: str | None = None) -> JSONResponse:
        """Models advertised by live workers that can serve the given queue.

        Used by the task detail dropdown so the operator sees only models
        actually routable to this queue's workers."""
        label = queue_label(_resolve_queue(queue) if queue else None)
        models = await _registry().models_for_queue(label)
        return JSONResponse({"models": models})

    @app.get("/api/stats")
    async def get_stats() -> JSONResponse:
        store = _store()
        return JSONResponse(
            {
                "overall": jsonable(await store.stats_overall()),
                "by_worker": [jsonable(r) for r in await store.stats_by_worker()],
                "by_backend": [jsonable(r) for r in await store.stats_by_backend()],
                "by_model": [jsonable(r) for r in await store.stats_by_model()],
                "by_queue": [jsonable(r) for r in await store.stats_by_queue()],
                "by_enhanced": [
                    jsonable(r) for r in await store.stats_by_enhanced()
                ],
                "enhancements": jsonable(await store.enhancements_summary()),
            }
        )

    @app.get("/api/leases")
    async def get_leases() -> JSONResponse:
        return JSONResponse(
            [jsonable(lease_view(a)) for a in await _store().live_attempts()]
        )

    @app.get("/api/blocked")
    async def get_blocked() -> JSONResponse:
        store = _store()
        # Quarantined/failed from frontmatter; blocked/repo_unavailable from DB.
        fm_rows: list[dict[str, Any]] = []
        for q in _all_queues():
            tasks_rel = playlists_mod.tasks_rel(q)
            fm_rows.extend(frontmatter_held_tasks(tasks_root, tasks_rel))
        rows = [
            *await store.list_blocked(),
            *fm_rows,
        ]
        return JSONResponse([jsonable(b) for b in rows])

    @app.get("/api/runs/{run_id}/{task}/log")
    async def get_run_log(
        run_id: str, task: str, offset: int = 0, queue: str | None = None
    ) -> JSONResponse:
        """Reconstruct a run's log from its persisted ``task_log`` events.

        The worker streams stdout to the manager as ``task_log`` events, which
        are stored durably (Postgres ``nightshift.events``). This re-assembles
        them into the plain-text payload the shared UI's log panel expects, so a
        finished run's output is viewable after the fact (parity with the
        single-process server's on-disk ``/log`` endpoint)."""
        events = await _store().run_events(run_id)
        text = "".join(
            str((ev.get("payload") or {}).get("line", ""))
            for ev in events
            if ev.get("kind") == "task_log"
        )
        return JSONResponse(
            {"text": text[offset:], "offset": len(text), "eof": True}
        )

    _MANAGER_SURFACES = ["manager", "player"]

    @app.get("/api/settings")
    def get_settings() -> JSONResponse:
        response = build_get_response(workspace, _MANAGER_SURFACES)
        response["tiers"].insert(0, _workspace_tier(workspace))
        return JSONResponse(response)

    @app.put("/api/settings")
    async def put_settings(body: dict[str, Any]) -> JSONResponse:
        allowed = set(_MANAGER_SURFACES)
        resolved, errors = validate_delta(body, allowed)
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=400)

        applied_live, restart_required = write_delta(workspace, resolved)
        response = build_get_response(workspace, _MANAGER_SURFACES)
        await _emit("settings_changed", payload={})

        return JSONResponse({
            "ok": True,
            "applied_live": applied_live,
            "restart_required": restart_required,
            **response,
        })

    # ----- SSE ------------------------------------------------------------- #

    async def _snapshot() -> dict[str, Any]:
        store = _store()
        fm_rows: list[dict[str, Any]] = []
        for q in _all_queues():
            tasks_rel = playlists_mod.tasks_rel(q)
            fm_rows.extend(frontmatter_held_tasks(tasks_root, tasks_rel))
        return {
            "cursor": await store.max_event_id(),
            "workers": [jsonable(w) for w in await store.list_workers()],
            "leases": [
                jsonable(lease_view(a)) for a in await store.live_attempts()
            ],
            "runs": [
                jsonable(run_view(r)) for r in await store.list_attempts(limit=50)
            ],
            "blocked": [
                jsonable(b) for b in [*await store.list_blocked(), *fm_rows]
            ],
        }

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        server = getattr(request.app.state, "uvicorn_server", None)

        def is_shutting_down() -> bool:
            # End the stream once the server starts shutting down (Ctrl-C) so this
            # long-lived connection doesn't block graceful shutdown. None under
            # the test client / uvicorn.run (no handle) → never shutting down.
            return server is not None and bool(server.should_exit)

        async def gen():
            async for frame in app.state.hub.stream(
                _snapshot,
                heartbeat_seconds=cfg.cadences.heartbeat_seconds,
                is_shutting_down=is_shutting_down,
            ):
                if await request.is_disconnected():
                    break
                yield frame

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ----- branding -------------------------------------------------------- #

    @app.get("/api/info")
    def info() -> JSONResponse:
        # The operator UI is shared with the single-process server; this lets the
        # frontend retitle itself "Nightshift Manager" when served by the manager
        # (the single-process server has no /api/info, so it keeps "Nightshift").
        return JSONResponse({"brand_name": "Nightshift Manager"})
