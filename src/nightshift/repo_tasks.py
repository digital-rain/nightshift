"""Repo task import — draining a target repo's ``.tasks/`` publishing inbox.

A target repo may carry a ``.tasks/`` directory that external tooling
publishes task briefs into, in one of two legacy layouts (or both at once):

* **flat** — ``.tasks/*.md`` briefs directly in the root;
* **queue dirs** — ``.tasks/<queue>/`` subdirectories, each with a local
  ``config.json`` (order) and ``*.md`` briefs.

Import is a *move* with git authority on both sides: the brief becomes
canonical in the content store (``nightshift-tasks/<queue>/``) and the source
file is removed from the repo's ``main`` by the manager (the sole writer to
``main``), so a brief exists in exactly one place, can never run twice, and
is never lost. See ``docs/spec/2026-07-04-repo-task-import.md``.

This module is shared-core: pure scan/copy plus the lock-held removal
orchestration; the HTTP surface lives in
:mod:`nightshift.manager.api_repo_tasks`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from nightshift.git import GitRunner
from nightshift.git.landing import (
    RepoContext,
    delete_produce,
    integrate_and_push_locked,
    push_main,
)
from nightshift.git.refs import main_sha
from nightshift.git.sync import sync_main_locked
from nightshift.lifecycle import LAND_SUCCESS_KINDS, LandingMode
from nightshift.queue_config import load_order, save_order
from nightshift.spawn_daily import is_disabled, split_frontmatter, task_priority
from nightshift.task_files import resolve_title


# The inbox directory external tooling publishes briefs into, at a target
# repo's root.
REPO_TASKS_DIR = ".tasks"


@dataclass(frozen=True)
class RepoTask:
    """One importable brief discovered in a repo's ``.tasks`` inbox."""

    name: str
    title: str
    # Repo-relative source path (``.tasks/….md``) — what the removal commit
    # deletes from ``main``.
    source: str
    priority: int
    disabled: bool
    # Exact text already present in the destination queue: import removes the
    # source without writing a second copy (the idempotent-replay/crash-
    # recovery path).
    duplicate: bool
    text: str


def _local_order(inbox_dir: Path) -> list[str]:
    """The inbox-local ``config.json`` ``order`` list (best-effort: ``[]`` on
    a missing/malformed file — callers fall back to filename order)."""
    path = inbox_dir / "config.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return []
    order = data.get("order") if isinstance(data, dict) else None
    return [str(name) for name in order] if isinstance(order, list) else []


def _scan_dir(inbox_dir: Path) -> list[Path]:
    """The ``*.md`` files of one inbox dir in its published order: stems listed
    in the dir's ``config.json`` ``order`` first, the rest by filename."""
    if not inbox_dir.is_dir():
        return []
    by_stem = {p.stem: p for p in inbox_dir.glob("*.md") if p.is_file()}
    rank = {name: i for i, name in enumerate(_local_order(inbox_dir))}
    listed = sorted((s for s in by_stem if s in rank), key=lambda s: rank[s])
    unlisted = sorted(s for s in by_stem if s not in rank)
    return [by_stem[s] for s in (*listed, *unlisted)]


def _parse_brief(path: Path) -> tuple[dict, str] | None:
    """Parse one inbox file into ``(frontmatter, text)`` — ``None`` when it is
    not an importable brief. Stems starting with ``_`` or ``.`` (templates,
    evergreen inboxes like ``_todo.md``) and ``autosplit: true`` sources
    (recurring, tooling appends to them in place) stay in the repo."""
    if path.stem.startswith(("_", ".")):
        return None
    text = path.read_text(errors="replace")
    meta = split_frontmatter(text)[0] if text.startswith("---") else {}
    if meta.get("autosplit"):
        return None
    return meta, text


def scan_repo_tasks(
    workspace: Path,
    repo: str,
    queue_name: str,
    tasks_root: Path,
    dest_rel: str,
) -> list[RepoTask]:
    """Scan ``<workspace>/<repo>/.tasks`` for briefs importable into a queue.

    Flat root files come first, then the subdir matching ``queue_name`` (the
    queue's label; other subdirs belong to other queues and stay untouched),
    each group in its published order (:func:`_scan_dir`). Read-only — the
    import itself is :func:`copy_repo_tasks` + :func:`remove_repo_tasks_locked`.
    """
    repo_root = workspace / repo
    home = repo_root / REPO_TASKS_DIR
    if not home.is_dir():
        return []

    dest_dir = tasks_root / dest_rel
    existing = (
        {p.read_text(errors="replace") for p in dest_dir.glob("*.md")}
        if dest_dir.is_dir()
        else set()
    )

    out: list[RepoTask] = []
    for path in (*_scan_dir(home), *_scan_dir(home / queue_name)):
        parsed = _parse_brief(path)
        if parsed is None:
            continue
        meta, text = parsed
        out.append(RepoTask(
            name=path.stem,
            title=resolve_title(path.stem, meta),
            source=str(path.relative_to(repo_root)),
            priority=task_priority(meta),
            disabled=is_disabled(meta),
            duplicate=text in existing,
            text=text,
        ))
    return out


def copy_repo_tasks(
    tasks_root: Path, dest_rel: str, entries: list[RepoTask]
) -> list[dict]:
    """Copy the non-duplicate scanned briefs into the destination queue dir,
    appending them to its execution order. A name collision with *different*
    content gets a ``-2`` suffix (the existing cross-queue copy policy).

    Returns ``{task, title}`` per brief written. This is the durable half of
    an import — the caller commits the content store, and only then removes
    the sources from the repo.
    """
    dest_dir = tasks_root / dest_rel
    dest_dir.mkdir(parents=True, exist_ok=True)
    imported: list[dict] = []
    for entry in entries:
        if entry.duplicate:
            continue
        name = entry.name
        n = 2
        while (dest_dir / f"{name}.md").exists():
            name = f"{entry.name}-{n}"
            n += 1
        (dest_dir / f"{name}.md").write_text(entry.text)
        imported.append({"task": name, "title": entry.title})
    if imported:
        save_order(
            tasks_root,
            [*load_order(tasks_root, dest_rel), *(t["task"] for t in imported)],
            dest_rel,
        )
    return imported


def remove_repo_tasks_locked(
    workspace: Path,
    repo: str,
    sources: list[str],
    message: str,
    *,
    remote: str = "origin",
) -> dict:
    """Remove drained inbox files from the repo's canonical ``main`` — one
    commit built and CAS'd through the landing pipeline. The caller must hold
    the RepoLock; in the manager this runs as a repo-executor job, so it can
    never interleave with a land or sync on the same repo.

    Posture around the commit: sync ``origin/main`` first (best-effort) so the
    removal lands on the fresh tip, and push ``main`` afterwards so the
    removal is never lost to a later divergence-rescuing sync. A failed
    removal or push is surfaced as ``warning`` and never unwound — the import
    already made the briefs durable in the content store, and a replayed
    import dedupes instead of duplicating.
    """
    repo_root = workspace / repo
    has_remote = GitRunner(repo_root).run("remote", "get-url", remote).ok
    if has_remote:
        sync_main_locked(workspace, repo, remote)
    outcome = integrate_and_push_locked(
        RepoContext(workspace=workspace, repo=repo),
        delete_produce(repo_root, sources, message),
        mode=LandingMode.NONE,
    )
    removed = outcome.kind in LAND_SUCCESS_KINDS
    warning: str | None = None
    if not removed:
        warning = f"could not remove imported briefs from {repo}: {outcome.detail}"
    elif has_remote:
        sha = main_sha(repo_root)
        push = push_main(workspace, repo, remote, sha) if sha else None
        if push is not None and not push.ok:
            warning = (
                f"imported-brief removal committed locally but the push to "
                f"{remote} failed: {push.detail}"
            )
    return {"removed": removed, "warning": warning}
