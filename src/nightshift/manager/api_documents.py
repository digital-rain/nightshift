"""Operator API for task documents (spec §6 / plan phase 3).

Endpoints
=========

* ``POST   /api/tasks/{task}/attachments``          — multipart upload
* ``PUT    /api/tasks/{task}/attachments/{name}``   — replace an attachment
* ``DELETE /api/tasks/{task}/attachments/{name}``   — delete file + drop from ``attachments:``
* ``GET    /api/tasks/{task}/docs/{name}``          — attachment bytes for preview
* ``GET    /api/tasks/{task}/documents``            — list docs + attachments + pin + drift
* ``POST   /api/tasks/{task}/docs/repin``           — refresh ``docs_pin`` (all or named)
* ``GET    /api/repos/{repo}/paths?prefix=&base_ref=`` — repo path autocomplete
* ``GET    /api/repos/{repo}/blob?sha=``            — raw blob bytes (path-doc preview)

Writes go through the tasks-repo executor lane (same pool as artifact writes).
``write_attachment`` already commits — do NOT double-commit in the wrapping job.

Attach guards → ``400`` JSON with ``{"error": <code>, "detail": <message>}``
where ``code`` is one of ``document_too_large``, ``unsupported_document_type``,
``document_budget_exceeded``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from nightshift import playlists as playlists_mod
from nightshift.docs_resolve import (
    DEFAULT_ALLOWED_DOC_MEDIA_TYPES,
    DEFAULT_DOCUMENT_BUDGET_BYTES,
    DEFAULT_DOCUMENT_CAP_BYTES,
    DocumentError,
    effective_document_cap,
    media_allowed,
    normalize_repo_path,
    parse_attachments_field,
    parse_docs_field,
    parse_docs_pin,
    read_fence_lines_for_key,
    render_docs_pin,
    resolve_task_docs,
    sniff_media_type,
)
from nightshift.git.executor import ExecutorPool
from nightshift.git.runner import GitRunner
from nightshift.manager.landing import canonical_head
from nightshift.spawn_daily import resolve_config, split_frontmatter
from nightshift.task_files import (
    attachments_dir,
    delete_attachment,
    list_attachments,
    read_attachment,
    read_task,
    set_engine_meta,
    set_task_meta,
    write_attachment,
)


class RepinBody(BaseModel):
    """Body of ``POST /api/tasks/{task}/docs/repin``.

    ``paths`` names the pin keys to clear (path strings for path docs, or
    ``attach:<name>`` for attachments).  ``None`` clears every entry.
    """

    paths: list[str] | None = None


def register_documents_api(
    app: FastAPI,
    *,
    workspace: Path,
    tasks_root: Path,
    tasks_repo: str,
    _resolve_queue: Callable[[str | None], str | None],
    _queue_repo: Callable[[str | None], str | None],
    _commit: Callable[[str], Awaitable[None]],
    _executors: ExecutorPool,
) -> None:
    """Register the Phase-3 document endpoints on ``app``.

    Wiring mirrors :func:`register_repo_tasks_api`: shared queue resolution,
    per-repo executor pool (tasks-repo jobs), and the async committer used
    for content-store mutations outside :func:`write_attachment`.
    """

    def _run_tasks_job(fn: Callable[[], Any]) -> Awaitable[Any]:
        return asyncio.wrap_future(_executors.submit(tasks_repo, fn))

    def _resolve_settings(target: str | None) -> dict[str, Any]:
        """Effective docs-related settings (queue-merged) for ``target``."""
        merged = resolve_config(
            workspace, tasks_root, playlists_mod.tasks_rel(target),
        )
        cap = int(merged.get("document_cap_bytes", DEFAULT_DOCUMENT_CAP_BYTES))
        allowed = merged.get("allowed_doc_media_types")
        if not isinstance(allowed, (list, tuple)):
            allowed = DEFAULT_ALLOWED_DOC_MEDIA_TYPES
        budget = int(merged.get(
            "document_budget_bytes", DEFAULT_DOCUMENT_BUDGET_BYTES,
        ))
        return {
            "effective_cap": effective_document_cap(cap),
            "allowed": tuple(allowed),
            "budget": budget,
        }

    def _task_meta(task: str, tasks_rel: str) -> tuple[dict[str, Any], str]:
        """Read a task's frontmatter + raw file text (empty dict when absent)."""
        path = tasks_root / tasks_rel / f"{task}.md"
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return {}, ""
        if not text.startswith("---"):
            return {}, text
        meta = split_frontmatter(text)[0]
        return meta, text

    def _attach_error(code: str, detail: str, *, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": code, "detail": detail}, status_code=status)

    def _sum_attachment_bytes(task: str, tasks_rel: str) -> int:
        adir = attachments_dir(tasks_root, task, tasks_rel)
        if not adir.is_dir():
            return 0
        total = 0
        for p in adir.iterdir():
            if p.is_file():
                total += p.stat().st_size
        return total

    def _parse_docs_from_meta(
        meta: dict[str, Any], text: str,
    ) -> list[Any]:
        docs_fence = (
            read_fence_lines_for_key(text, "docs")
            if meta.get("docs") in ("", None) and "docs" in meta else None
        )
        try:
            return parse_docs_field(meta.get("docs"), fence_lines=docs_fence)
        except DocumentError:
            return []

    def _parse_attachments_from_meta(
        meta: dict[str, Any], text: str,
    ) -> list[Any]:
        att_fence = (
            read_fence_lines_for_key(text, "attachments")
            if meta.get("attachments") in ("", None)
            and "attachments" in meta else None
        )
        try:
            return parse_attachments_field(
                meta.get("attachments"), fence_lines=att_fence,
            )
        except DocumentError:
            return []

    def _serialize_frontmatter_names(names: list[str]) -> object:
        """Render a list of attachment names as the ``attachments:`` value.

        A single bare string keeps the file idiomatic; multiple items use a
        JSON array (round-trips through the Phase-1 parse helpers).
        """
        if not names:
            return None
        if len(names) == 1:
            return names[0]
        return list(names)

    def _update_attachments_meta(task: str, tasks_rel: str, names: list[str]) -> None:
        """Write the ``attachments:`` frontmatter for ``task`` to ``names``."""
        value = _serialize_frontmatter_names(sorted(set(names)))
        set_task_meta(tasks_root, task, {"attachments": value}, tasks_rel)

    # -- attachments -------------------------------------------------------

    async def _attach_impl(
        task: str, name: str, data: bytes, target: str | None,
    ) -> JSONResponse:
        """Common attach/replace guardrail + write path."""
        tasks_rel = playlists_mod.tasks_rel(target)
        task_path = tasks_root / tasks_rel / f"{task}.md"
        if not task_path.is_file():
            return _attach_error("task_not_found", f"task not found: {task}",
                                 status=404)
        try:
            safe_name = normalize_repo_path(name)
        except DocumentError as exc:
            return _attach_error(exc.code, exc.message)
        if "/" in safe_name:
            return _attach_error(
                "unsupported_document_type",
                f"attachment name must not contain slashes: {name}",
            )

        settings = _resolve_settings(target)
        size = len(data)
        if size > settings["effective_cap"]:
            return _attach_error(
                "document_too_large",
                (f"attachment '{safe_name}' is {size // 1024} KB, over the "
                 f"document cap ({settings['effective_cap'] // 1024} KB)"),
            )
        try:
            media = sniff_media_type(safe_name, data[:64])
        except DocumentError as exc:
            return _attach_error(exc.code, exc.message)
        if not media_allowed(media, settings["allowed"]):
            return _attach_error(
                "unsupported_document_type",
                f"unsupported document type '{media}' for '{safe_name}'",
            )
        # Budget check: existing attachment bytes minus the outgoing name
        # (replace case) + the new payload.
        existing_bytes = _sum_attachment_bytes(task, tasks_rel)
        existing_path = attachments_dir(tasks_root, task, tasks_rel) / safe_name
        prior_size = (
            existing_path.stat().st_size if existing_path.is_file() else 0
        )
        projected = existing_bytes - prior_size + size
        if projected > settings["budget"]:
            return _attach_error(
                "document_budget_exceeded",
                (f"attachment '{safe_name}' would push total document bytes "
                 f"to {projected}, over the budget ({settings['budget']})"),
            )

        # Phase-1 write_attachment commits itself — do NOT double-commit.
        await _run_tasks_job(partial(
            write_attachment, tasks_root, task, safe_name, data, tasks_rel,
        ))

        # Refresh ``attachments:`` frontmatter so the new name is listed.
        meta, _ = _task_meta(task, tasks_rel)
        existing_specs = _parse_attachments_from_meta(meta, "")
        names = [spec.path.rsplit("/", 1)[-1] for spec in existing_specs]
        if safe_name not in names:
            names.append(safe_name)
            await _run_tasks_job(partial(
                _update_attachments_meta, task, tasks_rel, names,
            ))
            await _commit(f"nightshift: attach {task}/{safe_name}")

        return JSONResponse(
            {
                "task": task,
                "name": safe_name,
                "media": media,
                "bytes": size,
            },
            status_code=201,
        )

    @app.post("/api/tasks/{task}/attachments")
    async def post_attachment(
        task: str,
        request: Request,
        name: str = Query(..., description="Attachment filename with extension"),
        queue: str | None = None,
    ) -> JSONResponse:
        """Attach a task-local file.

        Upload is raw request body — no multipart, to keep the manager's
        dependency surface stdlib-only (plan Global constraints). Filename +
        extension travel via the ``name`` query param so the extension can
        drive media sniffing.
        """
        target = _resolve_queue(queue)
        data = await request.body()
        return await _attach_impl(task, name, data, target)

    @app.put("/api/tasks/{task}/attachments/{name}")
    async def put_attachment(
        task: str,
        name: str,
        request: Request,
        queue: str | None = None,
    ) -> JSONResponse:
        target = _resolve_queue(queue)
        data = await request.body()
        return await _attach_impl(task, name, data, target)

    @app.delete("/api/tasks/{task}/attachments/{name}")
    async def delete_attachment_route(
        task: str, name: str, queue: str | None = None,
    ) -> JSONResponse:
        target = _resolve_queue(queue)
        tasks_rel = playlists_mod.tasks_rel(target)
        try:
            safe_name = normalize_repo_path(name)
        except DocumentError as exc:
            return _attach_error(exc.code, exc.message)
        if "/" in safe_name:
            return _attach_error(
                "unsupported_document_type",
                f"attachment name must not contain slashes: {name}",
            )

        # Delete + commit — phase-1 helper commits itself.
        removed = await _run_tasks_job(partial(
            delete_attachment, tasks_root, task, safe_name, tasks_rel,
        ))
        if not removed:
            return _attach_error(
                "not_found", f"attachment not found: {safe_name}", status=404,
            )

        # Drop from ``attachments:`` frontmatter, and clear the pin entry
        # (spec §6 delete: also drops ``attach:<name>`` from ``docs_pin``).
        meta, _ = _task_meta(task, tasks_rel)
        existing_specs = _parse_attachments_from_meta(meta, "")
        remaining_names = [
            spec.path.rsplit("/", 1)[-1] for spec in existing_specs
            if spec.path.rsplit("/", 1)[-1] != safe_name
        ]
        meta_changes: dict[str, object | None] = {}
        if remaining_names:
            meta_changes["attachments"] = _serialize_frontmatter_names(
                sorted(set(remaining_names)),
            )
        else:
            meta_changes["attachments"] = None
        await _run_tasks_job(partial(
            set_task_meta, tasks_root, task, meta_changes, tasks_rel,
        ))
        # Prune the pin entry if present.
        pin = parse_docs_pin(meta.get("docs_pin"))
        pin_key = f"attach:{safe_name}"
        if pin_key in pin:
            new_pin = {k: v for k, v in pin.items() if k != pin_key}
            rendered = render_docs_pin(new_pin) if new_pin else None
            await _run_tasks_job(partial(
                set_engine_meta, tasks_root, task,
                {"docs_pin": rendered}, tasks_rel,
            ))

        await _commit(f"nightshift: detach {task}/{safe_name}")
        return JSONResponse({"task": task, "name": safe_name, "deleted": True})

    # -- attachment preview ------------------------------------------------

    @app.get("/api/tasks/{task}/docs/{name}")
    async def get_attachment_bytes(
        task: str, name: str, queue: str | None = None,
    ) -> Response:
        target = _resolve_queue(queue)
        tasks_rel = playlists_mod.tasks_rel(target)
        try:
            safe_name = normalize_repo_path(name)
        except DocumentError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from None
        if "/" in safe_name:
            raise HTTPException(status_code=400, detail="invalid attachment name")
        try:
            data = read_attachment(tasks_root, task, safe_name, tasks_rel)
        except (FileNotFoundError, OSError):
            raise HTTPException(status_code=404, detail="attachment not found") from None
        try:
            media = sniff_media_type(safe_name, data[:64])
        except DocumentError:
            media = "application/octet-stream"
        return Response(content=data, media_type=media)

    # -- documents summary -------------------------------------------------

    @app.get("/api/tasks/{task}/documents")
    async def get_task_documents(
        task: str, queue: str | None = None,
    ) -> JSONResponse:
        """Detail-pane payload: path docs + attachments + ``docs_pin`` + drift.

        Drift is computed per path doc as ``pin.sha != rev-parse(base_ref:path)``
        when the current base_ref is resolvable; otherwise drift is left ``null``
        (unknown).
        """
        target = _resolve_queue(queue)
        tasks_rel = playlists_mod.tasks_rel(target)
        task_path = tasks_root / tasks_rel / f"{task}.md"
        if not task_path.is_file():
            return JSONResponse({"error": "task not found"}, status_code=404)

        try:
            info = read_task(tasks_root, task, tasks_rel)
        except FileNotFoundError:
            return JSONResponse({"error": "task not found"}, status_code=404)
        meta = info.get("frontmatter_raw", {}) or {}
        text = task_path.read_text(errors="replace")
        pin = parse_docs_pin(meta.get("docs_pin"))

        # Effective repo for this task (per-task override wins over queue).
        repo = (
            str(meta.get("repo") or "").strip() or _queue_repo(target) or None
        )
        base_ref = None
        target_git: GitRunner | None = None
        if repo:
            repo_root = (workspace / repo).resolve()
            if repo_root.is_dir():
                base_ref = canonical_head(repo_root)
                target_git = GitRunner(repo_root)

        # Path docs — flatten specs into rows with pin/drift.
        path_specs = _parse_docs_from_meta(meta, text)
        docs_rows: list[dict[str, Any]] = []
        for spec in path_specs:
            try:
                norm_path = normalize_repo_path(spec.path)
            except DocumentError:
                norm_path = spec.path
            record = pin.get(norm_path)
            drift: bool | None = None
            live_sha: str | None = None
            if target_git is not None and base_ref is not None:
                live = target_git.out("rev-parse", f"{base_ref}:{norm_path}")
                if live:
                    live_sha = live
                    if record is not None:
                        drift = live_sha != record.sha
            docs_rows.append({
                "path": norm_path,
                "range": spec.range,
                "as": spec.as_,
                "steps": list(spec.steps) if spec.steps else None,
                "sha": record.sha if record else None,
                "media": record.media if record else None,
                "bytes": record.bytes if record else None,
                "drifted": drift,
                "live_sha": live_sha,
            })

        # Attachments — file listing + pin lookup.
        attach_specs = _parse_attachments_from_meta(meta, text)
        adir = attachments_dir(tasks_root, task, tasks_rel)
        on_disk = set(list_attachments(tasks_root, task, tasks_rel))
        attach_rows: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for spec in attach_specs:
            name = spec.path.rsplit("/", 1)[-1]
            seen_names.add(name)
            record = pin.get(f"attach:{name}")
            path = adir / name
            size = path.stat().st_size if path.is_file() else None
            attach_rows.append({
                "name": name,
                "range": spec.range,
                "as": spec.as_,
                "sha": record.sha if record else None,
                "media": record.media if record else None,
                "bytes": record.bytes if record else size,
                "present": path.is_file(),
            })
        # Orphan files on disk without a frontmatter entry (surfaced so the
        # operator can add them or clean them up).
        for name in sorted(on_disk - seen_names):
            path = adir / name
            attach_rows.append({
                "name": name,
                "range": None,
                "as": None,
                "sha": None,
                "media": None,
                "bytes": path.stat().st_size if path.is_file() else None,
                "present": True,
                "orphan": True,
            })

        pin_map = {
            k: {"sha": v.sha, "media": v.media, "bytes": v.bytes}
            for k, v in pin.items()
        }
        settings = _resolve_settings(target)
        return JSONResponse({
            "task": task,
            "repo": repo,
            "base_ref": base_ref,
            "docs": docs_rows,
            "attachments": attach_rows,
            "docs_pin": pin_map,
            "settings": {
                "document_cap_bytes": settings["effective_cap"],
                "document_budget_bytes": settings["budget"],
                "allowed_doc_media_types": list(settings["allowed"]),
            },
        })

    # -- repin -------------------------------------------------------------

    @app.post("/api/tasks/{task}/docs/repin")
    async def post_repin(
        task: str, body: RepinBody, queue: str | None = None,
    ) -> JSONResponse:
        """Refresh ``docs_pin`` — resolve now and persist, rather than clear.

        A UI-instant refresh: we re-resolve every entry (or just the named
        ones) against the current ``base_ref``, then rewrite ``docs_pin`` in
        one commit.  Named entries not currently pinned are simply added; a
        missing path/attachment surfaces the ordinary blocked message.
        """
        target = _resolve_queue(queue)
        tasks_rel = playlists_mod.tasks_rel(target)
        task_path = tasks_root / tasks_rel / f"{task}.md"
        if not task_path.is_file():
            return JSONResponse({"error": "task not found"}, status_code=404)
        text = task_path.read_text(errors="replace")
        meta = split_frontmatter(text)[0] if text.startswith("---") else {}

        repo = (
            str(meta.get("repo") or "").strip() or _queue_repo(target) or None
        )
        base_ref = None
        if repo:
            repo_root = (workspace / repo).resolve()
            if repo_root.is_dir():
                base_ref = canonical_head(repo_root)

        # If specific paths were named, drop them from the existing pin so
        # ``resolve_task_docs`` re-resolves them from scratch; unnamed entries
        # keep their cached pin as before.
        if body.paths:
            pin = parse_docs_pin(meta.get("docs_pin"))
            filtered = {k: v for k, v in pin.items() if k not in body.paths}
            meta = {**meta, "docs_pin": render_docs_pin(filtered) if filtered else None}
        else:
            # Full refresh: drop the pin entirely so every entry re-resolves.
            meta = {**meta, "docs_pin": None}

        merged = resolve_config(workspace, tasks_root, tasks_rel)
        result = resolve_task_docs(
            workspace=workspace,
            tasks_root=tasks_root,
            task=task,
            queue=target,
            repo=repo,
            base_ref=base_ref,
            meta=meta,
            merged_config=merged,
            workflow_step_id=None,
            task_file_text=text,
        )
        if result.blocked_reason is not None:
            return JSONResponse(
                {"error": "blocked", "detail": result.blocked_reason},
                status_code=409,
            )
        rendered = render_docs_pin(result.pin) if result.pin else None
        await _run_tasks_job(partial(
            set_engine_meta, tasks_root, task, {"docs_pin": rendered}, tasks_rel,
        ))
        await _commit(f"nightshift: repin docs for {task}")
        return JSONResponse({
            "task": task,
            "docs_pin": {
                k: {"sha": v.sha, "media": v.media, "bytes": v.bytes}
                for k, v in result.pin.items()
            },
        })

    # -- repo path autocomplete + blob preview -----------------------------

    def _repo_git(repo: str) -> GitRunner | None:
        repo_root = (workspace / repo).resolve()
        if not repo_root.is_dir():
            return None
        return GitRunner(repo_root)

    @app.get("/api/repos/{repo}/paths")
    async def get_repo_paths(
        repo: str,
        prefix: str = "",
        base_ref: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> JSONResponse:
        """List blob entries under ``prefix`` at ``base_ref`` (default HEAD).

        Each row: ``{path, media, sha}`` — sha lets the UI request a
        thumbnail without a second round-trip.  Filtered to the current
        ``allowed_doc_media_types`` allow-list; over-cap rows are still
        returned (the operator sees them but the attach flow rejects).
        """
        git = _repo_git(repo)
        if git is None:
            return JSONResponse(
                {"error": f"repo not available: {repo}"}, status_code=404,
            )
        ref = base_ref or canonical_head((workspace / repo).resolve()) or "HEAD"

        # ``git ls-tree -r <ref> [<prefix>]`` — one line per blob:
        # ``<mode> blob <sha>\t<path>``.
        args = ["ls-tree", "-r", ref]
        if prefix:
            args.append(prefix)
        result = git.run(*args)
        if not result.ok:
            return JSONResponse(
                {"error": "could not list repo paths", "detail": result.detail},
                status_code=500,
            )
        allowed = _resolve_settings(None)["allowed"]
        rows: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            head, _, path = line.partition("\t")
            parts = head.split()
            if len(parts) < 3 or parts[1] != "blob":
                continue
            sha = parts[2]
            if prefix and not path.startswith(prefix):
                continue
            # Sniff by extension only (avoid a per-file blob read).
            try:
                media = sniff_media_type(path, b"")
            except DocumentError:
                continue
            if not media_allowed(media, allowed):
                continue
            rows.append({"path": path, "media": media, "sha": sha})
            if len(rows) >= limit:
                break
        rows.sort(key=lambda r: r["path"])
        return JSONResponse({
            "repo": repo,
            "base_ref": ref,
            "paths": rows,
        })

    @app.get("/api/repos/{repo}/blob")
    async def get_repo_blob(repo: str, sha: str = Query(...)) -> Response:
        """Raw blob bytes for path-doc previews.  Capped to the effective cap."""
        if not sha or not sha.isalnum() or len(sha) < 4 or len(sha) > 64:
            raise HTTPException(status_code=400, detail="invalid sha")
        git = _repo_git(repo)
        if git is None:
            raise HTTPException(status_code=404, detail="repo not available")
        settings = _resolve_settings(None)
        size_out = git.out("cat-file", "-s", sha)
        try:
            size = int(size_out) if size_out else 0
        except ValueError:
            size = 0
        if size > settings["effective_cap"]:
            raise HTTPException(
                status_code=413,
                detail=f"blob {size} bytes exceeds cap {settings['effective_cap']}",
            )
        rc, data = git.run_bytes("cat-file", "blob", sha)
        if rc != 0:
            raise HTTPException(status_code=404, detail="blob not found")
        try:
            media = sniff_media_type(f"blob.{sha[:6]}", data[:64])
        except DocumentError:
            media = "application/octet-stream"
        return Response(content=data, media_type=media)


__all__ = ["register_documents_api", "RepinBody"]
