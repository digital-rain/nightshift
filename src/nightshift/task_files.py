"""Task brief files — queue scans, CRUD on ``<tasks_root>/<queue>/*.md``,
run-scratch brief materialisation, and split (decomposition) harvest.

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration.
"""

from __future__ import annotations

from pathlib import Path

from nightshift import playlists
from nightshift._paths import asset
from nightshift.git.store import commit_dispatch, commit_tasks
from nightshift.git.worktrees import queue_slug
from nightshift.queue_config import (
    apply_play_filter,
    load_order,
    order_stems,
    save_order,
)
from nightshift.spawn_daily import (
    find_autosplit_sources,
    is_completed,
    is_disabled,
    is_failed,
    is_quarantined,
    resolve_config,
    resolve_frontmatter,
    slugify,
    spawn_all,
    spawn_source,
    split_frontmatter,
    task_priority,
)


def resolve_title(task: str, meta: dict) -> str:
    """Resolve the display title from frontmatter or task name."""
    if "title" in meta:
        return meta["title"]
    return task


# Delimits the operator's original (pre-enhancement) brief at the END of a
# task file's body. Everything after this line, to EOF, is the original text
# as typed; everything before it is the effective brief the worker runs.
# Workers never see the original: build_work_order strips it from the body.
ORIGINAL_BRIEF_MARKER = "<!-- nightshift:original-brief -->"


def split_original(body: str) -> tuple[str, str]:
    """Split a brief body into ``(effective_brief, original_brief)``.

    The original-brief section is the tail of the body after
    :data:`ORIGINAL_BRIEF_MARKER`; a body without the marker has no original
    (``("", ...)`` empty second half). Both halves come back stripped.
    """
    if ORIGINAL_BRIEF_MARKER not in body:
        return body.strip(), ""
    brief, original = body.split(ORIGINAL_BRIEF_MARKER, 1)
    return brief.strip(), original.strip()


def join_original(body: str, original: str) -> str:
    """Reassemble a brief body with its original-brief tail section.

    An empty ``original`` yields just the body (no marker is written).
    """
    body = body.strip()
    original = original.strip()
    if not original:
        return body
    return f"{body}\n\n{ORIGINAL_BRIEF_MARKER}\n{original}"


def build_task_list(tasks_root: Path, task_arg: str, tasks_rel: str = "main") -> list[str]:
    """Build the ordered list of tasks to run for a queue.

    Autosplit dispatch (spawning subtasks, committing the daily queue to the
    content store) applies only to the default ``main`` queue; an alternate
    queue is a plain ordered set of its own `*.md` files.
    """
    is_main = tasks_rel == playlists.DEFAULT_QUEUE

    if task_arg != "all":
        if is_main:
            autosplit_sources = set(find_autosplit_sources(tasks_root, tasks_rel))
            if task_arg in autosplit_sources:
                result = spawn_source(tasks_root, task_arg, write=True, tasks_rel=tasks_rel)
                if result and result.spawned:
                    commit_dispatch(tasks_root, tasks_rel)
                    return [t.name for t in result.spawned]
                return []
        return [task_arg]

    results = []
    if is_main:
        results = spawn_all(tasks_root, write=True, tasks_rel=tasks_rel)
        if results:
            commit_dispatch(tasks_root, tasks_rel)

    queue_names = live_ordered_queue(tasks_root, tasks_rel)
    spawned_names = [t.name for r in results for t in r.spawned]
    ordered = order_stems(tasks_root, list(set(queue_names) | set(spawned_names)), tasks_rel)
    # Re-apply the play-priority filter so freshly-spawned autosplit subtasks
    # (folded in via the union above) also respect the active filter.
    return apply_play_filter(tasks_root, ordered, tasks_rel)


def live_ordered_queue(tasks_root: Path, tasks_rel: str = "main") -> list[str]:
    """Read-only ordered scan of a queue's runnable task stems.

    Globs ``<tasks_root>/<tasks_rel>/*.md``, skips autosplit-source and disabled
    files, and returns the stems in the queue's configured order. This is the
    side-effect-free core of :func:`build_task_list` ("all") — no spawning, no
    commits — and is reused by the live re-scan in :func:`run_queue` (which calls
    it every iteration, so it must stay quiet and cheap).
    """
    tasks_dir = tasks_root / tasks_rel
    if not tasks_dir.exists():
        return []
    autosplit = find_autosplit_tasks(tasks_dir)
    queue_names: list[str] = []
    priorities: dict[str, int] = {}
    for p in tasks_dir.glob("*.md"):
        if p.stem in autosplit:
            continue
        text = p.read_text(errors="replace")
        meta = split_frontmatter(text)[0] if text.startswith("---") else {}
        if is_disabled(meta) or is_quarantined(meta) or is_completed(meta):
            continue
        queue_names.append(p.stem)
        priorities[p.stem] = task_priority(meta)
    ordered = order_stems(tasks_root, queue_names, tasks_rel, priorities=priorities)
    return apply_play_filter(tasks_root, ordered, tasks_rel, priorities=priorities)


def frontmatter_held_tasks(
    tasks_root: Path, tasks_rel: str = "main",
) -> list[dict[str, str]]:
    """Return quarantined and failed tasks from frontmatter for a queue.

    Used by ``/api/blocked`` to surface these tasks in the "needs attention"
    list without relying on the DB overlay.
    """
    tasks_dir = tasks_root / tasks_rel
    if not tasks_dir.exists():
        return []
    from nightshift import playlists
    queue = playlists.queue_from_tasks_rel(tasks_rel)
    queue_key = queue or ""
    out: list[dict[str, str]] = []
    for p in tasks_dir.glob("*.md"):
        text = p.read_text(errors="replace")
        meta = split_frontmatter(text)[0] if text.startswith("---") else {}
        if is_quarantined(meta):
            out.append({
                "queue": queue_key, "task": p.stem, "state": "quarantined",
                "blocked_reason": meta.get("quarantine_reason", ""),
            })
        elif is_failed(meta):
            out.append({
                "queue": queue_key, "task": p.stem, "state": "failed",
                "blocked_reason": meta.get("failed_reason", ""),
            })
    return out


def failed_tasks(tasks_root: Path, tasks_rel: str = "main") -> list[dict[str, str]]:
    """Return frontmatter-failed tasks for a queue as dicts with queue/task keys.

    Used by the Phase B retry logic to find tasks eligible for retry without
    relying on the DB overlay (frontmatter is the source of truth for failed).
    Sorted by stem so pick_retry gets a deterministic tiebreaker.
    """
    tasks_dir = tasks_root / tasks_rel
    if not tasks_dir.exists():
        return []
    from nightshift import playlists
    queue = playlists.queue_from_tasks_rel(tasks_rel)
    queue_key = queue or ""
    out: list[dict[str, str]] = []
    for p in sorted(tasks_dir.glob("*.md"), key=lambda p: p.stem):
        text = p.read_text(errors="replace")
        meta = split_frontmatter(text)[0] if text.startswith("---") else {}
        if is_failed(meta) and not is_quarantined(meta) and not is_completed(meta):
            out.append({"queue": queue_key, "task": p.stem, "state": "failed"})
    return out


def find_autosplit_tasks(tasks_dir: Path) -> set[str]:
    """Return stems of task files that have autosplit: true in frontmatter."""
    result: set[str] = set()
    for p in tasks_dir.glob("*.md"):
        text = p.read_text(errors="replace")
        if not text.startswith("---"):
            continue
        meta, _ = split_frontmatter(text)
        if meta.get("autosplit"):
            result.add(p.stem)
    return result


TASK_TEMPLATE = asset("templates", "task.md")


def create_task(
    tasks_root: Path,
    title: str,
    text: str,
    tasks_rel: str = "main",
    original: str | None = None,
) -> dict:
    """Create a new task file `<tasks_rel>/<slug(title)>.md` from the template.

    Tasks are no longer numbered: the filename is the slugified title and the
    new task is appended to the queue's `config.json` execution order (so it
    lands at the end of the queue, where the operator can drag it into place).

    ``original`` (the operator's pre-enhancement brief) is preserved verbatim
    below :data:`ORIGINAL_BRIEF_MARKER` at the end of the body; ``None``/empty
    writes no marker section (the pre-enhancement behavior, byte-for-byte).

    Raises ``ValueError`` for an empty title and ``FileExistsError`` if the
    target name is already taken.
    """
    title_clean = title.strip()
    if not title_clean:
        raise ValueError("title is required")

    tasks_dir = tasks_root / tasks_rel
    tasks_dir.mkdir(parents=True, exist_ok=True)
    name = slugify(title_clean)
    dest = tasks_dir / f"{name}.md"
    if dest.exists():
        raise FileExistsError(name)

    body = join_original(text.strip() or title_clean, original or "")
    template = TASK_TEMPLATE.read_text()
    content = template.replace(
        "title: short descriptive title for the PR", f"title: {title_clean}", 1
    )
    content = content.replace("Task description goes here.", body, 1)
    dest.write_text(content)
    save_order(tasks_root, [*load_order(tasks_root, tasks_rel), name], tasks_rel)
    return {"task": name, "title": title_clean}


def delete_task(tasks_root: Path, task: str, tasks_rel: str = "main") -> dict:
    """Delete a queue task file ``<tasks_rel>/<task>.md``.

    Guards against path traversal: ``task`` must resolve to a direct child of
    the queue's tasks dir. Raises ``FileNotFoundError`` if there's no such task.
    """
    tasks_dir = (tasks_root / tasks_rel).resolve()
    dest = (tasks_dir / f"{task}.md").resolve()
    if dest.parent != tasks_dir or not dest.is_file():
        raise FileNotFoundError(task)
    dest.unlink()
    order = load_order(tasks_root, tasks_rel)
    if task in order:
        save_order(tasks_root, [name for name in order if name != task], tasks_rel)
    return {"task": task, "deleted": True}


def task_is_evergreen(meta: dict, task: str, config: dict) -> bool:
    """True when a task is evergreen — by its own frontmatter or by being listed
    in the queue config's ``evergreen_tasks``. Evergreen tasks reset and re-run,
    so they keep their file; regular tasks leave the queue once they complete."""
    return bool(meta.get("evergreen", False)) or task in set(
        config.get("evergreen_tasks", [])
    )


def drop_completed_task(
    tasks_root: Path, task: str, tasks_rel: str = "main", *, queue: str | None = None
) -> bool:
    """Ensure a landed regular task's brief is gone from the content store.

    Completed regular tasks must leave the queue. After a successful land the
    engine deletes the brief from ``tasks_root`` (and its execution-order entry)
    and commits that removal in the content store via :func:`commit_tasks`, so
    :func:`list_queue` and the dashboard drop the completed item. The brief never
    lived in the target repo, so this is purely a content-store operation.

    No-ops (returns ``False``) when the brief is already gone. Returns ``True``
    when it removed the brief. ``queue`` is accepted for caller symmetry but the
    brief path is derived from ``tasks_rel``.
    """
    task_file = (tasks_root / tasks_rel).resolve() / f"{task}.md"
    if not task_file.is_file():
        return False
    delete_task(tasks_root, task, tasks_rel)
    commit_tasks(
        tasks_root,
        f"nightshift: drop completed task {task}",
        pathspecs=(tasks_rel,),
    )
    return True


def import_task(tasks_root: Path, src_rel: str, task: str, dest_rel: str) -> dict:
    """Copy a task file from one queue into another, appending it to the
    destination's execution order.

    ``<src_rel>/<task>.md`` is copied verbatim (frontmatter and body) into
    ``<dest_rel>/``; if that name is already taken there, a numeric suffix is
    added so nothing is clobbered. Both paths are guarded against traversal the
    same way :func:`delete_task` is. Returns ``{task, title}`` for the new copy.
    """
    src_dir = (tasks_root / src_rel).resolve()
    src = (src_dir / f"{task}.md").resolve()
    if src.parent != src_dir or not src.is_file():
        raise FileNotFoundError(task)

    dest_dir = (tasks_root / dest_rel).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = task
    dest = dest_dir / f"{name}.md"
    n = 2
    while dest.exists():
        name = f"{task}-{n}"
        dest = dest_dir / f"{name}.md"
        n += 1

    text = src.read_text(errors="replace")
    dest.write_text(text)
    save_order(tasks_root, [*load_order(tasks_root, dest_rel), name], dest_rel)

    meta = split_frontmatter(text)[0] if text.startswith("---") else {}
    return {"task": name, "title": resolve_title(name, meta)}


def read_task(tasks_root: Path, task: str, tasks_rel: str = "main") -> dict:
    """Read a single queue brief ``<tasks_root>/<tasks_rel>/<task>.md`` for the
    detail view.

    Returns ``{task, title, body, original_brief, frontmatter, evergreen,
    disabled}`` where ``frontmatter`` is the parsed YAML block merged with
    resolved defaults (model/draft/automerge) so the brief shows the effective
    values, ``body`` is the spec prose with the frontmatter fence and any
    original-brief tail stripped, and ``original_brief`` is the preserved
    pre-enhancement text ("" when the file has none). Read-only: it neither
    spawns subtasks nor mutates the queue.

    Guards against path traversal the same way :func:`delete_task` does: ``task``
    must resolve to a direct child of the queue dir. Raises ``FileNotFoundError``
    if there's no such task.
    """
    tasks_dir = (tasks_root / tasks_rel).resolve()
    dest = (tasks_dir / f"{task}.md").resolve()
    if dest.parent != tasks_dir or not dest.is_file():
        raise FileNotFoundError(task)

    text = dest.read_text(errors="replace")
    meta, body = split_frontmatter(text) if text.startswith("---") else ({}, text)
    body, original = split_original(body)
    # ``tasks_root`` is ``<workspace>/<tasks_repo>`` by construction, so its
    # parent is the workspace — resolve the layered queue config from both roots.
    config = resolve_config(tasks_root.parent, tasks_root, tasks_rel)
    resolved = resolve_frontmatter(meta, config)
    evergreen = bool(meta.get("evergreen", False)) or task in set(
        config.get("evergreen_tasks", [])
    )

    # Merge the raw frontmatter with resolved defaults so the brief reflects the
    # effective model/draft/automerge even when the file omits them.
    frontmatter = {**meta}
    frontmatter.setdefault("model", resolved["model"])
    frontmatter.setdefault("draft", resolved["draft"])
    frontmatter.setdefault("automerge", resolved["automerge"])
    # Always surface the effective 0-5 priority (clamped, default lowest) so the
    # detail editor's segmented control has a value even when the file omits it.
    frontmatter["priority"] = task_priority(meta)

    return {
        "task": task,
        "title": resolve_title(task, meta),
        "body": body.strip(),
        "original_brief": original,
        "frontmatter": frontmatter,
        # The raw, file-only frontmatter (before defaults are layered in) so the
        # editor can tell whether a field is explicitly set vs inherited — e.g.
        # "model" absent here means the task uses the config default.
        "frontmatter_raw": dict(meta),
        "evergreen": evergreen,
        "disabled": is_disabled(meta),
        "quarantined": is_quarantined(meta),
        "quarantine_reason": meta.get("quarantine_reason", ""),
        "failed": is_failed(meta),
        "failed_reason": meta.get("failed_reason", ""),
        "completed": is_completed(meta),
    }


# Frontmatter keys the detail-view editor is allowed to set. ``model: None``
# clears the key so the task inherits the config default. ``title`` is a
# frontmatter key too, but is written via the dedicated ``title`` change so it
# always lands ahead of the other keys (it's the file's headline). ``repo`` is
# the per-task target-repo override (a bare workspace-child name); clearing it
# (``repo: None``) falls the task back to the queue's default ``repo``.
_EDITABLE_META_KEYS = {
    "disabled", "quarantined", "quarantine_reason", "failed", "failed_reason",
    "completed", "evergreen", "automerge", "draft", "model", "priority", "repo",
    "loop", "loop_max_iterations", "split", "enhanced",
}

# The detail-view editor may also rewrite the spec prose (``body``), the
# headline (``title``), and the preserved pre-enhancement text
# (``original_brief``); these aren't plain frontmatter scalars so they're
# handled separately from :data:`_EDITABLE_META_KEYS`.
_EDITABLE_CONTENT_KEYS = {"title", "body", "original_brief"}


def _render_meta_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _strip_leading_blanks(lines: list[str]) -> list[str]:
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    return lines[idx:]


def set_task_meta(
    tasks_root: Path,
    task: str,
    changes: dict[str, object | None],
    tasks_rel: str = "main",
) -> dict:
    """Update a queue task file in place from the detail-view editor.

    ``changes`` maps a key to its new value. Frontmatter scalars
    (:data:`_EDITABLE_META_KEYS`) are rewritten where they sit — preserving field
    order and any unrelated keys or comments — and missing keys are appended just
    before the closing fence; a value of ``None`` removes the key (so the task
    falls back to the config default). A file without a frontmatter fence gains
    one. Booleans serialise as ``true``/``false``.

    ``title`` (the headline, stored as a frontmatter key), ``body`` (the spec
    prose below the fence), and ``original_brief`` (the preserved
    pre-enhancement text after :data:`ORIGINAL_BRIEF_MARKER`) are content
    edits: ``title`` is written/updated as the leading frontmatter key, and
    ``body``/``original_brief`` each replace their half of the prose while
    leaving the other half untouched (an empty ``original_brief`` drops the
    marker section). All are optional; omitting them leaves the existing
    content untouched.

    Only keys in :data:`_EDITABLE_META_KEYS` ∪ :data:`_EDITABLE_CONTENT_KEYS` are
    accepted. Guards against path traversal exactly like :func:`read_task`.
    Returns the refreshed brief.
    """
    bad = set(changes) - _EDITABLE_META_KEYS - _EDITABLE_CONTENT_KEYS
    if bad:
        raise ValueError(f"non-editable keys: {', '.join(sorted(bad))}")

    tasks_dir = (tasks_root / tasks_rel).resolve()
    dest = (tasks_dir / f"{task}.md").resolve()
    if dest.parent != tasks_dir or not dest.is_file():
        raise FileNotFoundError(task)

    new_title = changes.get("title") if "title" in changes else None
    if new_title is not None and not str(new_title).strip():
        raise ValueError("title is required")
    meta_changes = {k: v for k, v in changes.items() if k in _EDITABLE_META_KEYS}

    lines = dest.read_text(errors="replace").splitlines()
    close: int | None = None
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                close = i
                break

    if close is not None:
        fence_lines = lines[1:close]
        body_lines = lines[close + 1:]
    else:
        fence_lines = []
        body_lines = lines

    # ``title`` rides through the same fence-rewrite machinery as the scalars so
    # an existing ``title:`` line is updated in place rather than duplicated.
    fence_changes: dict[str, object | None] = dict(meta_changes)
    if "title" in changes:
        fence_changes["title"] = str(new_title).strip()

    remaining = dict(fence_changes)
    new_fence: list[str] = []
    for line in fence_lines:
        stripped = line.strip()
        key = (
            line.split(":", 1)[0].strip()
            if stripped and not stripped.startswith("#") and ":" in line
            else None
        )
        if key is not None and key in remaining:
            value = remaining.pop(key)
            if value is not None:
                new_fence.append(f"{key}: {_render_meta_value(value)}")
        else:
            new_fence.append(line)
    for key, value in remaining.items():
        if value is not None:
            new_fence.append(f"{key}: {_render_meta_value(value)}")

    # Content edits operate on the two body halves independently: the spec
    # prose and the original-brief tail each replace their own half, so an
    # edit to one never clobbers the other.
    brief, original = split_original("\n".join(body_lines))
    if "body" in changes:
        brief = str(changes.get("body") or "").strip()
    if "original_brief" in changes:
        original = str(changes.get("original_brief") or "").strip()
    body = _strip_leading_blanks(join_original(brief, original).splitlines())
    if new_fence:
        out = ["---", *new_fence, "---", "", *body]
    else:
        out = body
    dest.write_text("\n".join(out).rstrip("\n") + "\n")

    return read_task(tasks_root, task, tasks_rel)


def list_queue(tasks_root: Path, tasks_rel: str = "main") -> list[dict]:
    """List top-level `<tasks_rel>/*.md` (skips subdirs) for the UI queue.

    Returns ``{task, title, evergreen, disabled}`` in the configured execution
    order (the queue's `config.json` ``order``), falling back to filename order
    for unlisted tasks. Unlike :func:`build_task_list` this is read-only: it
    neither spawns autosplit subtasks nor commits.
    """
    tasks_dir = tasks_root / tasks_rel
    if not tasks_dir.exists():
        return []
    config = resolve_config(tasks_root.parent, tasks_root, tasks_rel)
    evergreen_tasks = set(config.get("evergreen_tasks", []))

    by_stem: dict[str, dict] = {}
    priorities: dict[str, int] = {}
    for p in tasks_dir.glob("*.md"):
        text = p.read_text(errors="replace")
        meta = split_frontmatter(text)[0] if text.startswith("---") else {}
        evergreen = bool(meta.get("evergreen", False)) or p.stem in evergreen_tasks
        priorities[p.stem] = task_priority(meta)
        by_stem[p.stem] = {
            "task": p.stem,
            "title": resolve_title(p.stem, meta),
            "evergreen": evergreen,
            "disabled": is_disabled(meta),
            "quarantined": is_quarantined(meta),
            "failed": is_failed(meta),
            "completed": is_completed(meta),
            "priority": priorities[p.stem],
        }
    ordered = order_stems(tasks_root, list(by_stem), tasks_rel, priorities=priorities)
    return [by_stem[s] for s in ordered]


def materialize_brief(
    workspace: Path, repo: str, task: str, body: str, *, queue: str | None = None
) -> Path:
    """Write a task's brief ``body`` to a run-scratch file **outside** the
    target worktree and return its path.

    The scratch file is a sibling of the worktree dir
    (``<workspace>/.worktrees/<repo>/task-local-<queue>-<task>.taskfile.md``), so
    the brief is delivered to the worker (as ``$TASK_FILE``) without ever entering
    the target repo's tracked tree — the agent cannot accidentally commit it, and
    only the implementation squash lands. The body is the frontmatter-stripped
    brief markdown (as carried in the work order).
    """
    scratch = (
        workspace / ".worktrees" / repo
        / f"task-local-{queue_slug(queue)}-{task}.taskfile.md"
    )
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(body if body.endswith("\n") else f"{body}\n")
    return scratch


def split_output_dir(
    workspace: Path, repo: str, task: str, *, queue: str | None = None
) -> Path:
    """Return the directory where a ``split: true`` worker writes subtask briefs.

    A dedicated sibling of the worktree dir
    (``<workspace>/.worktrees/<repo>/task-local-<queue>-<task>.split/``) so
    generated briefs never collide with other tasks' scratch files or worktrees.
    The caller creates the directory before the worker runs; after the worker
    finishes, :func:`harvest_split_output` scans it for ``*.md`` files.
    """
    return (
        workspace / ".worktrees" / repo
        / f"task-local-{queue_slug(queue)}-{task}.split"
    )


def harvest_split_output(
    workspace: Path,
    tasks_root: Path,
    repo: str,
    task: str,
    meta: dict,
    *,
    queue: str | None = None,
    tasks_rel: str = "main",
) -> list[str]:
    """Collect subtask briefs from a decomposition run and enqueue them.

    Scans ``split_output_dir(...)`` for ``*.md`` files the worker wrote,
    copies each into the content store (``tasks_root/tasks_rel``) with
    collision-safe naming, commits via :func:`commit_tasks`, retires the
    parent brief via :func:`drop_completed_task`, and returns the list of
    created subtask stems (for the result line / event payload).

    If the split dir is empty or missing, returns an empty list (the caller
    treats this as "no subtasks produced" — an honest-failure path).
    """
    from nightshift.spawn_daily import next_sub_id, unique_spawn_name

    sdir = split_output_dir(workspace, repo, task, queue=queue)
    if not sdir.is_dir():
        return []

    briefs = sorted(sdir.glob("*.md"))
    if not briefs:
        return []

    tasks_dir = tasks_root / tasks_rel
    tasks_dir.mkdir(parents=True, exist_ok=True)
    parent_num = task.split(".", 1)[0] if "." in task else task
    sub_id = next_sub_id(tasks_dir, parent_num) - 1

    created: list[str] = []
    for brief_path in briefs:
        sub_id += 1
        stem = brief_path.stem
        slug = slugify(stem) if stem else f"subtask-{sub_id}"
        name = unique_spawn_name(tasks_dir, f"{parent_num}.{sub_id}.{slug}")
        dest = tasks_dir / f"{name}.md"
        dest.write_text(brief_path.read_text())
        created.append(name)

    if created:
        commit_tasks(
            tasks_root,
            f"nightshift: decompose {task} into {len(created)} subtask(s)",
            pathspecs=(tasks_rel,),
        )
        drop_completed_task(tasks_root, task, tasks_rel, queue=queue)

    import shutil
    shutil.rmtree(sdir, ignore_errors=True)

    return created
