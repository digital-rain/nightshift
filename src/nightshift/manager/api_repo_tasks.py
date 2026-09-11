"""Operator repo-task import endpoints — ``/api/queue/repo-tasks*``.

The queue-page affordance that drains a target repo's publishing inboxes
(``.tasks/`` and ``docs/tasks/``) into the queue bound to that repo (see
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
from pydantic import BaseModel

from nightshift import playlists as playlists_mod
from nightshift import repos
from nightshift.git.executor import ExecutorPool
from nightshift.manager.scheduler import queue_label
from nightshift.manager.store import NightshiftStore
from nightshift.manager.wire import EmitFn
from nightshift.repo_tasks import (
    RepoTask,
    copy_repo_tasks,
    remove_repo_tasks_locked,
    scan_repo_tasks,
    select_repo_tasks,
)


class RepoTaskImport(BaseModel):
    # The briefs to drain, as repo-relative source paths (null = the whole
    # scanned set; ``[]`` = nothing). Selection keys on ``source`` rather than
    # the task stem because the stem is ambiguous — the same name can be
    # published under either inbox root (``.tasks/x.md``, ``docs/tasks/x.md``)
    # and both flat and under the queue's subdir — those are distinct briefs.
    sources: list[str] | None = None


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
    _store: Callable[[], NightshiftStore],
) -> None:
    """Register the repo-task import endpoints (see module docstring)."""
    # One import at a time: the copy step reads-then-writes the destination
    # queue dir and its execution order, so two concurrent drains must not
    # interleave (imports are rare, operator-initiated actions).
    import_lock = asyncio.Lock()

    async def _scan(target: str | None, repo: str) -> list[RepoTask]:
        # The started set turns a name collision into an update-or-refuse
        # decision, so it is read fresh with every scan rather than cached.
        started = await _store().started_tasks(target)
        return await asyncio.to_thread(
            scan_repo_tasks,
            workspace,
            repo,
            queue_label(target),
            tasks_root,
            playlists_mod.tasks_rel(target),
            started,
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
            "quarantined": e.quarantined,
            "duplicate": e.duplicate,
            "replaces": e.replaces,
            "started": e.started,
        }

    @app.get("/api/queue/repo-tasks")
    async def get_repo_tasks(queue: str | None = None) -> JSONResponse:
        target = _resolve_queue(queue)
        if not _queue_exists(target):
            return JSONResponse({"error": "queue not found"}, status_code=404)
        repo = _queue_repo(target)
        available = bool(repo) and repos.repo_available(workspace, repo)
        entries = await _scan(target, repo) if available and repo else []
        return JSONResponse({
            "queue": queue_label(target),
            "repo": repo,
            "available": available,
            "count": len(entries),
            "tasks": [_entry(e) for e in entries],
        })

    @app.post("/api/queue/repo-tasks/import")
    async def post_repo_tasks_import(
        req: RepoTaskImport | None = None, queue: str | None = None
    ) -> JSONResponse:
        """Drain the selected briefs (``sources``; absent = the whole scanned
        set) into the queue and remove them from the repo's ``main``. Briefs the
        operator left out stay published in the inbox and are offered again by
        the next preview.

        A brief whose name is already a task here updates that task in place
        instead of arriving beside it — unless the task has begun, in which
        case the brief is refused: not imported, and held (``disabled: true``)
        where it was published instead of drained."""
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
            entries, missing = select_repo_tasks(
                await _scan(target, repo), req.sources if req else None
            )
            if not entries:
                return JSONResponse({
                    "imported": [],
                    "deduped": [],
                    "refused": [],
                    "removed": False,
                    "warning": None,
                    "missing": missing,
                })
            # 1. Copy into the content store and commit — the briefs are
            #    durable from here; the removal below is cleanup.
            imported = copy_repo_tasks(
                tasks_root, playlists_mod.tasks_rel(target), entries
            )
            if imported:
                await _commit(
                    f"nightshift: import {len(imported)} task(s) from {repo}"
                )
            # 2. Remove the drained sources from the repo's main as a
            #    repo-executor job (serialized with lands/syncs on that repo).
            #    A refused brief is held at the source instead of removed.
            drained = [e.source for e in entries if not e.started]
            refused = [e for e in entries if e.started]
            removal = await asyncio.wrap_future(_executors.submit(repo, partial(
                remove_repo_tasks_locked,
                workspace,
                repo,
                drained,
                f"nightshift: import {len(drained)} task(s) into queue {label}",
                disable=[e.source for e in refused],
            )))
        await _emit(
            "queue_changed",
            queue=target,
            payload={"imported": [t["task"] for t in imported]},
        )
        return JSONResponse({
            "imported": imported,
            "deduped": [e.name for e in entries if e.duplicate],
            "refused": [e.name for e in refused],
            "removed": removal["removed"],
            "warning": removal["warning"],
            "missing": missing,
        })
