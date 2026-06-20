"""Playlists — directory-backed alternate queues.

A *playlist* is a self-contained queue living in its own sub-directory of
``.tasks/``::

    .tasks/<name>/
        *.md          the playlist's own task files
        config.json   the playlist's queue order + any setting overrides
        runs/         this playlist's run history

A playlist is distinguished from an ordinary ``.tasks/`` sub-directory by the
presence of a ``config.json``. Its ``config.json`` inherits everything it does
not set from the system-wide ``.tasks/config.json`` (see
:func:`nightshift.spawn_daily.resolve_config`), so a freshly-created playlist
holds only ``{"order": []}``.

This module owns playlist discovery and lifecycle (create / delete) and the
mapping from a playlist name to the relative paths the engine threads through
(``tasks_rel`` and ``runs_rel``).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path


PLAYLIST_CONFIG = "config.json"

# A playlist name is a slug: lowercase alphanumerics and dashes. This doubles as
# the on-disk directory name and is what blocks path traversal.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# `.tasks/` sub-directories that are infrastructure, not playlists.
_RESERVED = {"runs"}


def slugify_name(name: str) -> str:
    """Slugify a human playlist name into a safe directory name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug


def is_valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and name not in _RESERVED


def tasks_rel(name: str | None) -> str:
    """Relative tasks dir for an active queue: ``.tasks`` (main) or a playlist."""
    if not name:
        return ".tasks"
    return f".tasks/{name}"


def runs_rel(name: str | None) -> str:
    """Relative runs dir for an active queue."""
    return f"{tasks_rel(name)}/runs"


def queue_from_tasks_rel(tasks_rel: str) -> str | None:
    """Inverse of :func:`tasks_rel`: recover a queue name from a tasks-relative
    path. ``.tasks`` (or empty) maps to the main queue (``None``); ``.tasks/foo``
    maps to the playlist ``foo``."""
    if not tasks_rel or tasks_rel == ".tasks":
        return None
    return tasks_rel.removeprefix(".tasks/") or None


def _playlists_root(root: Path) -> Path:
    return root / ".tasks"


def list_playlists(root: Path) -> list[dict]:
    """All playlists (``.tasks/<name>/`` dirs holding a ``config.json``).

    Returns ``{name, task_count}`` per playlist, sorted by name.
    """
    base = _playlists_root(root)
    if not base.exists():
        return []
    out: list[dict] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name in _RESERVED:
            continue
        if not (child / PLAYLIST_CONFIG).exists():
            continue
        task_count = len(list(child.glob("*.md")))
        out.append({"name": child.name, "task_count": task_count})
    return out


def exists(root: Path, name: str) -> bool:
    if not is_valid_name(name):
        return False
    return (_playlists_root(root) / name / PLAYLIST_CONFIG).exists()


def create_playlist(root: Path, name: str) -> dict:
    """Create ``.tasks/<slug(name)>/config.json`` seeded with just an empty queue.

    Everything else is inherited from the system-wide ``.tasks/config.json``.
    Raises ``ValueError`` for an empty/invalid name and ``FileExistsError`` if a
    playlist with that name already exists.
    """
    slug = slugify_name(name)
    if not slug or not is_valid_name(slug):
        raise ValueError("playlist name must contain letters or numbers")
    dest = _playlists_root(root) / slug
    config_path = dest / PLAYLIST_CONFIG
    if config_path.exists():
        raise FileExistsError(slug)
    dest.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"order": []}, indent=2) + "\n")
    return {"name": slug, "task_count": 0}


def delete_playlist(root: Path, name: str) -> bool:
    """Remove a playlist directory and all its tasks/runs. Returns True if it
    existed. Guards against path traversal via :func:`is_valid_name`."""
    if not is_valid_name(name):
        return False
    dest = (_playlists_root(root) / name).resolve()
    base = _playlists_root(root).resolve()
    if dest.parent != base or not (dest / PLAYLIST_CONFIG).exists():
        return False
    shutil.rmtree(dest)
    return True
