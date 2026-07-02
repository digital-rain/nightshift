"""Queue configuration — ``config.json`` execution order, sort modes,
play-priority filters, and the validate-command resolution.

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from nightshift.spawn_daily import (
    DEFAULT_PRIORITY,
    MAX_PRIORITY,
    MIN_PRIORITY,
    split_frontmatter,
    task_priority,
)


DEFAULT_VALIDATE_CMD = "just validate"


def normalize_validate_command(value: str) -> str:
    """Normalize a user-entered validate command for storage in a queue config.

    A queue opts out of validation by clearing the command. The UI treats any
    of the following as "cleared": a whitespace-only string, the empty-quote
    literals ``''`` and ``""``. Each of these normalizes to the empty string,
    which the engine reads as "validation disabled" (and which never falls back
    to the inherited default). Any other value is returned stripped of
    surrounding whitespace.
    """
    stripped = value.strip()
    if stripped in {"", "''", '""'}:
        return ""
    return stripped


def resolve_validate_cmd(config: dict) -> list[str] | None:
    """The validate command for a queue, or ``None`` when validation is disabled.

    Resolution distinguishes an *absent* ``validate`` key (the queue inherits the
    engine default, ``just validate``) from a key explicitly set to an empty or
    whitespace-only string (the queue opts out of validation entirely). An empty
    string is a deliberate signal — it never falls back to the inherited default.
    """
    if "validate" not in config:
        return shlex.split(DEFAULT_VALIDATE_CMD)
    raw = str(config.get("validate") or "").strip()
    if not raw:
        return None
    return shlex.split(raw)


def format_validate_cmd(argv: list[str] | None) -> str:
    """Shell command string for a work order, or ``""`` when validation is disabled."""
    if argv is None:
        return ""
    return shlex.join(argv)


def validate_cmd_from_blob(config: dict) -> tuple[list[str] | None, str | None]:
    """Resolve validate argv + display string from a work-order ``config`` blob.

    The manager sends an authoritative ``validate_cmd`` string (empty =
    disabled). Legacy blobs with only ``validate`` fall back to
    :func:`resolve_validate_cmd`. Returns ``(argv, display)`` where ``display``
    is the command string actually run, or ``None`` when validation is skipped.
    """
    if "validate_cmd" in config:
        raw = str(config.get("validate_cmd") or "").strip()
        if not raw:
            return None, None
        return shlex.split(raw), raw
    argv = resolve_validate_cmd(config)
    if argv is None:
        return None, None
    return argv, shlex.join(argv)


# --------------------------------------------------------------------------- #
# Execution order — `<tasks_root>/<queue>/config.json`
# --------------------------------------------------------------------------- #
#
# Task files no longer need a numeric `NN.` prefix to be ordered. Execution
# order is driven by an explicit list in a queue's `config.json` (e.g.
# `<tasks_root>/main/config.json`):
#
#     {"order": ["side-by-side", "detail-view", ...]}
#
# Tasks named in ``order`` run in that order; any task not listed falls back
# to lexicographic order *after* the listed ones (so a freshly-added file has a
# deterministic position and a missing/empty config degrades to the old
# filename ordering). Stale entries (files that no longer exist) are ignored.

ORDER_CONFIG = "config.json"

# Queue sort modes (persisted in a queue's config.json under ``sort``).
SORT_MANUAL = "manual"
SORT_PRIORITY = "priority"
SORT_MODES = (SORT_MANUAL, SORT_PRIORITY)


def _order_config_path(tasks_root: Path, tasks_rel: str = "main") -> Path:
    return tasks_root / tasks_rel / ORDER_CONFIG


def load_order(tasks_root: Path, tasks_rel: str = "main") -> list[str]:
    """Return the configured task order from a queue's `config.json`.

    Returns an empty list when the file is absent or malformed — callers treat
    that as "no explicit order" and fall back to filename order.
    """
    path = _order_config_path(tasks_root, tasks_rel)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return []
    order = data.get("order") if isinstance(data, dict) else None
    if not isinstance(order, list):
        return []
    return [str(name) for name in order]


def save_order(tasks_root: Path, order: list[str], tasks_rel: str = "main") -> list[str]:
    """Persist the task order to a queue's `config.json`, preserving other keys.

    Returns the written order. The on-disk JSON keeps any sibling keys so the
    file can hold other queue settings (e.g. ``validate``) alongside ``order``.
    """
    path = _order_config_path(tasks_root, tasks_rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except (ValueError, OSError):
            data = {}
    clean = [str(name) for name in order]
    data["order"] = clean
    path.write_text(json.dumps(data, indent=2) + "\n")
    return clean


def save_queue_config_value(
    tasks_root: Path, key: str, value: object, tasks_rel: str = "main"
) -> object:
    """Set a single key in a queue's ``config.json``, preserving sibling keys.

    Mirrors :func:`save_order` but for an arbitrary scalar setting (e.g.
    ``validate``). A ``None`` value removes the key so the queue falls back to
    the inherited default. Returns the value written (or ``None`` when removed).
    """
    path = _order_config_path(tasks_root, tasks_rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except (ValueError, OSError):
            data = {}
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    path.write_text(json.dumps(data, indent=2) + "\n")
    return value


def load_sort_mode(tasks_root: Path, tasks_rel: str = "main") -> str:
    """Return a queue's sort mode from its `config.json` (``manual`` default).

    Any value other than the known modes (incl. a missing/malformed file)
    degrades to ``manual`` so the queue keeps its drag-defined order.
    """
    path = _order_config_path(tasks_root, tasks_rel)
    if not path.exists():
        return SORT_MANUAL
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return SORT_MANUAL
    mode = data.get("sort") if isinstance(data, dict) else None
    return mode if mode in SORT_MODES else SORT_MANUAL


def save_sort_mode(tasks_root: Path, mode: str, tasks_rel: str = "main") -> str:
    """Persist a queue's sort mode, preserving sibling keys. Unknown modes are
    coerced to ``manual``. Returns the mode written."""
    clean = mode if mode in SORT_MODES else SORT_MANUAL
    save_queue_config_value(tasks_root, "sort", clean, tasks_rel)
    return clean


def _clean_priority_list(values: object) -> list[int]:
    """Coerce an arbitrary value into a sorted, de-duped list of valid 0-5
    priorities. Anything non-list / out-of-range is dropped."""
    if not isinstance(values, list):
        return []
    out: set[int] = set()
    for v in values:
        try:
            p = int(v)
        except (TypeError, ValueError):
            continue
        if MIN_PRIORITY <= p <= MAX_PRIORITY:
            out.add(p)
    return sorted(out)


def load_play_priorities(tasks_root: Path, tasks_rel: str = "main") -> list[int]:
    """Return a queue's play-priority filter from its `config.json`.

    The filter restricts which tasks *play*: only tasks whose priority is in the
    returned set run. An empty list means "all priorities" (no filter) — the
    default when the key is absent or malformed.
    """
    path = _order_config_path(tasks_root, tasks_rel)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return []
    raw = data.get("play_priorities") if isinstance(data, dict) else None
    return _clean_priority_list(raw)


def save_play_priorities(
    tasks_root: Path, priorities: list[int], tasks_rel: str = "main"
) -> list[int]:
    """Persist a queue's play-priority filter (sorted/de-duped/validated),
    preserving sibling keys. An empty list clears the filter (play all). Returns
    the cleaned list written."""
    clean = _clean_priority_list(priorities)
    save_queue_config_value(tasks_root, "play_priorities", clean, tasks_rel)
    return clean


def apply_play_filter(
    tasks_root: Path,
    stems: list[str],
    tasks_rel: str = "main",
    *,
    priorities: dict[str, int] | None = None,
) -> list[str]:
    """Drop stems whose priority isn't in the queue's play-priority filter.

    A no-op when the filter is empty ("all priorities"). ``priorities`` lets a
    caller pass an already-parsed priority map to avoid a second read."""
    selected = load_play_priorities(tasks_root, tasks_rel)
    if not selected:
        return stems
    chosen = set(selected)
    if priorities is None:
        priorities = _read_priorities(tasks_root, stems, tasks_rel)
    return [s for s in stems if priorities.get(s, DEFAULT_PRIORITY) in chosen]


def _read_priorities(
    tasks_root: Path, stems: list[str], tasks_rel: str = "main"
) -> dict[str, int]:
    """Read the 0-5 priority of each stem from its task file's frontmatter.

    Missing files / frontmatter fall back to the default (lowest) priority via
    :func:`task_priority`."""
    tasks_dir = tasks_root / tasks_rel
    out: dict[str, int] = {}
    for stem in stems:
        path = tasks_dir / f"{stem}.md"
        meta: dict = {}
        if path.exists():
            text = path.read_text(errors="replace")
            if text.startswith("---"):
                meta = split_frontmatter(text)[0]
        out[stem] = task_priority(meta)
    return out


def order_stems(
    tasks_root: Path,
    stems: list[str],
    tasks_rel: str = "main",
    *,
    priorities: dict[str, int] | None = None,
    sort: str | None = None,
) -> list[str]:
    """Sort task stems by the queue's active sort mode.

    Manual mode (default): listed stems come first in their configured order;
    the rest follow in filename order. Priority mode: stems sort by ascending
    priority (0 = highest), with the manual order as a stable tiebreak for equal
    priorities. Stable and total over ``stems`` regardless of the config.

    ``sort`` overrides the persisted mode (else it's read from config.json), and
    ``priorities`` lets a caller that already parsed frontmatter pass the map in
    to avoid a second read; when omitted it's read on demand in priority mode.
    """
    order = load_order(tasks_root, tasks_rel)
    rank = {name: i for i, name in enumerate(order)}
    listed = sorted((s for s in stems if s in rank), key=lambda s: rank[s])
    unlisted = sorted(s for s in stems if s not in rank)
    manual = listed + unlisted

    if sort is None:
        sort = load_sort_mode(tasks_root, tasks_rel)
    if sort != SORT_PRIORITY:
        return manual

    if priorities is None:
        priorities = _read_priorities(tasks_root, manual, tasks_rel)
    manual_index = {stem: i for i, stem in enumerate(manual)}
    return sorted(
        manual,
        key=lambda s: (priorities.get(s, DEFAULT_PRIORITY), manual_index[s]),
    )


def reorder_queue(tasks_root: Path, order: list[str], tasks_rel: str = "main") -> list[str]:
    """Set the queue execution order from a UI drag.

    Accepts the desired ordering of task stems; only stems backed by an actual
    `<tasks_rel>/<stem>.md` file are kept (guards against stale/spoofed names),
    and any existing queue task omitted from ``order`` is appended in filename
    order so a partial payload never drops tasks. Returns the persisted order.
    """
    tasks_dir = tasks_root / tasks_rel
    existing = {p.stem for p in tasks_dir.glob("*.md")} if tasks_dir.exists() else set()
    seen: set[str] = set()
    cleaned: list[str] = []
    for name in order:
        if name in existing and name not in seen:
            cleaned.append(name)
            seen.add(name)
    for name in sorted(existing - seen):
        cleaned.append(name)
    return save_order(tasks_root, cleaned, tasks_rel)
