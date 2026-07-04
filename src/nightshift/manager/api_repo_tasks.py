"""Operator repo-task import endpoints — ``/api/queue/repo-tasks*``.

The queue-page affordance that drains a target repo's ``.tasks/`` publishing
inbox into the queue bound to that repo (see
``docs/spec/2026-07-04-repo-task-import.md``). Endpoints are registered onto
the shared FastAPI app by :func:`register_repo_tasks_api`; the shared wiring
(queue resolution, repo binding, content-store committer, event emitter, git
executor pool) is injected by ``register_operator_api`` — the same split
pattern as ``manager/api_playlists.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from nightshift import playlists as playlists_mod
from nightshift import repos
from nightshift.git.executor import ExecutorPool
from nightshift.manager.scheduler import queue_label
from nightshift.manager.wire import EmitFn
from nightshift.repo_tasks import (
    RepoTask,
    copy_repo_tasks,
    remove_repo_tasks_locked,
    scan_repo_tasks,
)


def register_repo_tasks_api(
    app: FastAPI,
    *,
    workspace: Path,
    tasks_root: Path,
    _resolve_queue: Callable[[str | None], str | None],
    _queue_exists: Callable[[str | None], bool],
    _queue_repo: Callable[[str | None], str | None],
    _commit: Callable[[str], Awaitable[None]],
    _emit: EmitFn,
    _executors: ExecutorPool,
) -> None:
    """Register the repo-task import endpoints (see module docstring)."""
    # One import at a time: the copy step reads-then-writes the destination
    # queue dir and its execution order, so two concurrent drains must not
    # interleave (imports are rare, operator-initiated actions).
    import_lock = asyncio.Lock()

    def _scan(target: str | None, repo: str) -> list[RepoTask]:
        return scan_repo_tasks(
            workspace,
            repo,
            queue_label(target),
            tasks_root,
            playlists_mod.tasks_rel(target),
        )

    def _entry(e: RepoTask) -> dict:
        # The preview shape — everything the modal renders, minus the brief
        # text (it can be large and the preview doesn't need it).
        return {
            "task": e.name,
            "title": e.title,
            "source": e.source,
            "priority": e.priority,
            "disabled": e.disabled,
            "duplicate": e.duplicate,
        }

    @app.get("/api/queue/repo-tasks")
    def get_repo_tasks(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        repo = _queue_repo(target)
        available = bool(repo) and repos.repo_available(workspace, repo)
        entries = _scan(target, repo) if available and repo else []
        return JSONResponse({
            "queue": queue_label(target),
            "repo": repo,
            "available": available,
            "count": len(entries),
            "tasks": [_entry(e) for e in entries],
        })

    @app.post("/api/queue/repo-tasks/import")
    async def post_repo_tasks_import(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        repo = _queue_repo(target)
        if not repo or not repos.repo_available(workspace, repo):
            return JSONResponse(
                {"error": "queue has no available repo to import from"},
                status_code=409,
            )
        label = queue_label(target)
        async with import_lock:
            entries = _scan(target, repo)
            if not entries:
                return JSONResponse(
                    {"imported": [], "deduped": [], "removed": False, "warning": None}
                )
            # 1. Copy into the content store and commit — the briefs are
            #    durable from here; the removal below is cleanup.
            imported = copy_repo_tasks(
                tasks_root, playlists_mod.tasks_rel(target), entries
            )
            if imported:
                await _commit(
                    f"nightshift: import {len(imported)} task(s) "
                    f"from {repo}/.tasks"
                )
            # 2. Remove the drained sources from the repo's main as a
            #    repo-executor job (serialized with lands/syncs on that repo).
            removal = await asyncio.wrap_future(_executors.submit(repo, partial(
                remove_repo_tasks_locked,
                workspace,
                repo,
                [e.source for e in entries],
                f"nightshift: import {len(entries)} task(s) into queue {label}",
            )))
        await _emit(
            "queue_changed",
            queue=target,
            payload={"imported": [t["task"] for t in imported]},
        )
        return JSONResponse({
            "imported": imported,
            "deduped": [e.name for e in entries if e.duplicate],
            "removed": removal["removed"],
            "warning": removal["warning"],
        })
