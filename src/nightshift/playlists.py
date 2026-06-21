"""Queues — directory-backed task queues in the ``nightshift-tasks`` store.

Every queue is a top-level directory of the ``nightshift-tasks`` content-store
repo (``tasks_root``)::

    nightshift-tasks/
        main/                 the default queue
            *.md              task briefs
            config.json       { "repo": "<default-target>", "order": [...], ... }
        <queue>/              additional queues (the former "playlists")
            *.md
            config.json

There is **no literal ``.tasks/``** and no main-vs-playlist asymmetry: the
default queue is just the ``main`` directory; alternate queues are sibling
directories. A directory is a queue iff it holds a ``config.json``.

This module owns queue discovery and lifecycle (create / delete) and the mapping
from a queue name to the relative path the engine threads through (``tasks_rel``)
and its inverse (``queue_from_tasks_rel``). Run history (``runs/``) and logs are
runtime state, not queue definition; they live under the queue dir but are
gitignored in the content store.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path


PLAYLIST_CONFIG = "config.json"

# The default queue's directory name (replaces the old root-level ``.tasks``).
DEFAULT_QUEUE = "main"

# A queue name is a slug: lowercase alphanumerics and dashes. This doubles as the
# on-disk directory name and is what blocks path traversal. It is the same guard
# reused for repo references (see :mod:`nightshift.repos`).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Top-level ``nightshift-tasks`` entries that are infrastructure, not queues.
_RESERVED = {"runs"}


def slugify_name(name: str) -> str:
    """Slugify a human queue name into a safe directory name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug


def is_valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and name not in _RESERVED


def tasks_rel(name: str | None) -> str:
    """Relative queue dir within ``tasks_root``: ``main`` (the default queue) or
    the named queue. The internal main queue is represented as ``None``."""
    return name or DEFAULT_QUEUE


def runs_rel(name: str | None) -> str:
    """Relative runs dir for a queue (runtime state, gitignored in the store)."""
    return f"{tasks_rel(name)}/runs"


def queue_from_tasks_rel(tasks_rel: str) -> str | None:
    """Inverse of :func:`tasks_rel`: recover a queue name from a tasks-relative
    queue dir. ``main`` (or empty) maps to the main queue (``None``); any other
    directory name maps to that named queue."""
    if not tasks_rel or tasks_rel == DEFAULT_QUEUE:
        return None
    return tasks_rel


def list_playlists(tasks_root: Path) -> list[dict]:
    """All alternate queues (top-level ``<queue>/`` dirs holding a ``config.json``,
    excluding the default ``main`` queue and reserved/hidden dirs).

    Returns ``{name, task_count}`` per queue, sorted by name.
    """
    if not tasks_root.exists():
        return []
    out: list[dict] = []
    for child in sorted(tasks_root.iterdir()):
        if not child.is_dir() or child.name in _RESERVED or child.name == DEFAULT_QUEUE:
            continue
        if child.name.startswith("."):
            continue
        if not (child / PLAYLIST_CONFIG).exists():
            continue
        task_count = len(list(child.glob("*.md")))
        out.append({"name": child.name, "task_count": task_count})
    return out


def exists(tasks_root: Path, name: str) -> bool:
    if not is_valid_name(name):
        return False
    return (tasks_root / name / PLAYLIST_CONFIG).exists()


def create_playlist(tasks_root: Path, name: str) -> dict:
    """Create ``<tasks_root>/<slug(name)>/config.json`` seeded with an empty queue.

    Everything else is inherited from the workspace-level/shipped config layers
    (see :func:`nightshift.spawn_daily.resolve_config`). Raises ``ValueError``
    for an empty/invalid name and ``FileExistsError`` if a queue with that name
    already exists.
    """
    slug = slugify_name(name)
    if not slug or not is_valid_name(slug):
        raise ValueError("queue name must contain letters or numbers")
    dest = tasks_root / slug
    config_path = dest / PLAYLIST_CONFIG
    if config_path.exists():
        raise FileExistsError(slug)
    dest.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"order": []}, indent=2) + "\n")
    return {"name": slug, "task_count": 0}


def delete_playlist(tasks_root: Path, name: str) -> bool:
    """Remove a queue directory and all its tasks/runs. Returns True if it
    existed. Guards against path traversal via :func:`is_valid_name`, and never
    deletes the default ``main`` queue."""
    if not is_valid_name(name) or name == DEFAULT_QUEUE:
        return False
    dest = (tasks_root / name).resolve()
    base = tasks_root.resolve()
    if dest.parent != base or not (dest / PLAYLIST_CONFIG).exists():
        return False
    shutil.rmtree(dest)
    return True


def rename_playlist(tasks_root: Path, old: str, new: str) -> str:
    """Rename the queue directory ``old`` to ``slug(new)`` and return the new
    slug. The directory move carries every task brief, ``config.json`` (so the
    repo binding survives), and on-disk run records with it.

    The caller is responsible for migrating any *external* state that keys on the
    queue name (the manager's Postgres run/lease/dedication rows; the server's
    in-memory runners/focus) — this only touches the content store.

    Raises ``ValueError`` for an empty/invalid new name or an attempt to rename
    the default ``main`` queue, ``FileNotFoundError`` when ``old`` is not an
    existing queue, and ``FileExistsError`` when a directory already occupies the
    target slug. Renaming to the same slug (a no-op, e.g. a case-only change) is
    allowed and simply returns the slug unchanged.
    """
    if not is_valid_name(old) or old == DEFAULT_QUEUE:
        raise ValueError("cannot rename this queue")
    base = tasks_root.resolve()
    src = (tasks_root / old).resolve()
    if src.parent != base or not (src / PLAYLIST_CONFIG).exists():
        raise FileNotFoundError(old)
    slug = slugify_name(new)
    if not slug or not is_valid_name(slug):
        raise ValueError("queue name must contain letters or numbers")
    if slug == old:
        return slug
    dest = tasks_root / slug
    if dest.exists():
        raise FileExistsError(slug)
    src.rename(dest)
    return slug


def set_playlist_repo(tasks_root: Path, name: str, repo: str | None) -> str | None:
    """Set (or clear, with ``None``) a queue's default target ``repo`` binding in
    its ``config.json``, preserving sibling keys. Returns the value written.

    Mirrors :func:`nightshift.engine.save_queue_config_value` for the ``repo``
    key but stays inside this low-level module (no engine import) so the rescan
    helper below can configure queues without a cross-layer dependency.
    """
    config_path = tasks_root / tasks_rel(name) / PLAYLIST_CONFIG
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except (ValueError, OSError):
            data = {}
    if repo is None:
        data.pop("repo", None)
    else:
        data["repo"] = repo
    config_path.write_text(json.dumps(data, indent=2) + "\n")
    return repo


def rescan_into_playlists(
    tasks_root: Path, repo_names: list[str], *, skip: set[str] | None = None
) -> dict:
    """Materialise one playlist per discovered workspace repo.

    For every name in ``repo_names`` (the workspace's git-repo children, already
    bare valid slugs), ensure a queue with that slug exists and bind its default
    ``repo`` to the discovered repo name (the alias the UI surfaces as
    "repository"). A name in ``skip`` (typically the content-store repo) is
    ignored. Returns ``{"created": [...], "configured": [...]}`` — the playlists
    newly created vs. the pre-existing ones whose repo binding was (re)set.
    """
    skip = skip or set()
    created: list[str] = []
    configured: list[str] = []
    for name in repo_names:
        slug = slugify_name(name)
        if not slug or not is_valid_name(slug) or slug in skip:
            continue
        config_path = tasks_root / slug / PLAYLIST_CONFIG
        if config_path.exists():
            configured.append(slug)
        else:
            create_playlist(tasks_root, slug)
            created.append(slug)
        set_playlist_repo(tasks_root, slug, name)
    return {"created": created, "configured": configured}
