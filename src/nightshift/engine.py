"""Shared nightshift orchestration core.

This module owns the logic for turning the `.tasks/` queue into work: building
the task list, running a worker in an isolated git worktree, validating, and
squash-committing to local ``main``. It is consumed by two front-ends:

- the CLI (:mod:`nightshift.run_local`), which prints events to stdout, and
- the server (:mod:`nightshift.server.app`), which drives runs with transport
  control and streams events to the browser.

The loop is both *observable* (it emits :class:`~nightshift.events.Event`
objects through a listener) and *controllable* (a :class:`Controller` can
pause, resume, stop, or skip between/within tasks).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from nightshift import playlists
from nightshift._paths import asset
from nightshift.events import (
    RUN_FINISHED,
    RUN_STARTED,
    TASK_LOG,
    TASK_RESULT,
    TASK_STARTED,
    TASK_STATUS,
    WORKER_STARTED,
    Event,
    Listener,
)
from nightshift.spawn_daily import (
    DEFAULT_PRIORITY,
    MAX_PRIORITY,
    MIN_PRIORITY,
    find_autosplit_sources,
    is_disabled,
    load_config,
    resolve_config,
    resolve_frontmatter,
    slugify,
    spawn_all,
    spawn_source,
    split_frontmatter,
    task_priority,
)


def _noop(_event: Event) -> None:
    return None


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


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Terminate a subprocess and its whole process group, escalating SIGTERM to
    SIGKILL after a short grace so a stubborn child (e.g. pytest workers) dies."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_interruptible(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    should_abort,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` capturing output, but kill it promptly when ``should_abort``
    (a zero-arg callable returning a reason or ``None``) fires.

    Runs the child in its own process group so the whole tree is killed on stop.
    On abort the returned ``CompletedProcess`` has a non-zero ``returncode`` so
    callers treat it as a failed/aborted phase.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            if callable(should_abort) and should_abort() is not None:
                _kill_process_group(proc)
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    out, err = "", ""
                return subprocess.CompletedProcess(cmd, proc.returncode or 130, out, err)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class TaskResult:
    task: str
    title: str
    success: bool
    commit_sha: str | None = None
    # Code lines churned by the landed commit (None when nothing landed).
    loc: int | None = None
    error: str | None = None
    status: str = ""
    result_line: str = ""
    # Classified failure category when ``success`` is False (see events.py).
    failure_kind: str | None = None

    def resolved_status(self) -> str:
        if self.status:
            return self.status
        return "completed" if self.success else "error"


@dataclass
class RunSummary:
    results: list[TaskResult] = field(default_factory=list)

    @property
    def landed(self) -> list[TaskResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[TaskResult]:
        return [r for r in self.results if not r.success and r.resolved_status() == "error"]


# --------------------------------------------------------------------------- #
# Preconditions
# --------------------------------------------------------------------------- #

MIN_FREE_PCT = 2.0


def enough_free_disk(root: Path, min_free_pct: float = MIN_FREE_PCT) -> bool:
    """Return True when the filesystem holding `root` has >= min_free_pct free."""
    usage = shutil.disk_usage(root)
    return (usage.free / usage.total) * 100.0 >= min_free_pct


def check_preconditions(root: Path) -> None:
    """Fail fast if prerequisites are missing."""
    if not enough_free_disk(root):
        usage = shutil.disk_usage(root)
        free_pct = (usage.free / usage.total) * 100.0
        sys.exit(
            f"error: only {free_pct:.1f}% disk free (need >= {MIN_FREE_PCT}%).\n"
            "Free space before running — e.g. 'just clean' to expunge the Bazel cache."
        )
    if not shutil.which("claude"):
        sys.exit(
            "error: 'claude' CLI not found on PATH.\n"
            "Install: https://docs.anthropic.com/en/docs/claude-code"
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "error: ANTHROPIC_API_KEY is not set.\n"
            "Add it to .env or export it in your shell."
        )
    # Queue state (.tasks/) and untracked files never block a run — they are
    # snapshotted / ignored at land time. Only genuine tracked *code* WIP matters,
    # and only when autostash is off (otherwise the land step sets it aside).
    blockers = _landing_blockers(root)
    if blockers:
        shown = "\n".join(f"  {line}" for line in blockers[:10])
        autostash = bool(resolve_config(root).get("autostash_operator_work", True))
        if autostash:
            print(
                "note: main has uncommitted code — it will be set aside "
                f"(git stash) during each land and restored after:\n{shown}"
            )
        else:
            sys.exit(
                "error: working tree has uncommitted code and "
                "autostash_operator_work is off — commit or stash before "
                f"running.\n{shown}"
            )
    print("Running just validate (pre-flight)...")
    result = subprocess.run(
        ["just", "validate"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip().splitlines()
        tail = "\n  ".join(output[-6:])
        sys.exit(
            "error: just validate is not clean — fix before running.\n"
            f"  {tail}"
        )


def acquire_lock(root: Path) -> int:
    """Acquire an exclusive lockfile; exit if another instance is running."""
    lock_path = root / ".worktrees" / ".nightshift-local.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(
            "error: another instance of run-tasks-local is already running.\n"
            "Only one instance may run at a time to prevent disk exhaustion."
        )
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


# --------------------------------------------------------------------------- #
# Task list / queue
# --------------------------------------------------------------------------- #


def resolve_title(task: str, meta: dict) -> str:
    """Resolve the display title from frontmatter or task name."""
    if "title" in meta:
        return meta["title"]
    return task


# --------------------------------------------------------------------------- #
# Execution order — `.tasks/config.json`
# --------------------------------------------------------------------------- #
#
# Task files no longer need a numeric `NN.` prefix to be ordered. Execution
# order is driven by an explicit list in `.tasks/config.json`:
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


def _order_config_path(root: Path, tasks_rel: str = ".tasks") -> Path:
    return root / tasks_rel / ORDER_CONFIG


def load_order(root: Path, tasks_rel: str = ".tasks") -> list[str]:
    """Return the configured task order from a queue's `config.json`.

    Returns an empty list when the file is absent or malformed — callers treat
    that as "no explicit order" and fall back to filename order.
    """
    path = _order_config_path(root, tasks_rel)
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


def save_order(root: Path, order: list[str], tasks_rel: str = ".tasks") -> list[str]:
    """Persist the task order to a queue's `config.json`, preserving other keys.

    Returns the written order. The on-disk JSON keeps any sibling keys so the
    file can hold other queue settings (e.g. ``validate``) alongside ``order``.
    """
    path = _order_config_path(root, tasks_rel)
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
    root: Path, key: str, value: object, tasks_rel: str = ".tasks"
) -> object:
    """Set a single key in a queue's ``config.json``, preserving sibling keys.

    Mirrors :func:`save_order` but for an arbitrary scalar setting (e.g.
    ``validate``). A ``None`` value removes the key so the queue falls back to
    the inherited default. Returns the value written (or ``None`` when removed).
    """
    path = _order_config_path(root, tasks_rel)
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


def load_sort_mode(root: Path, tasks_rel: str = ".tasks") -> str:
    """Return a queue's sort mode from its `config.json` (``manual`` default).

    Any value other than the known modes (incl. a missing/malformed file)
    degrades to ``manual`` so the queue keeps its drag-defined order.
    """
    path = _order_config_path(root, tasks_rel)
    if not path.exists():
        return SORT_MANUAL
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return SORT_MANUAL
    mode = data.get("sort") if isinstance(data, dict) else None
    return mode if mode in SORT_MODES else SORT_MANUAL


def save_sort_mode(root: Path, mode: str, tasks_rel: str = ".tasks") -> str:
    """Persist a queue's sort mode, preserving sibling keys. Unknown modes are
    coerced to ``manual``. Returns the mode written."""
    clean = mode if mode in SORT_MODES else SORT_MANUAL
    save_queue_config_value(root, "sort", clean, tasks_rel)
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


def load_play_priorities(root: Path, tasks_rel: str = ".tasks") -> list[int]:
    """Return a queue's play-priority filter from its `config.json`.

    The filter restricts which tasks *play*: only tasks whose priority is in the
    returned set run. An empty list means "all priorities" (no filter) — the
    default when the key is absent or malformed.
    """
    path = _order_config_path(root, tasks_rel)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return []
    raw = data.get("play_priorities") if isinstance(data, dict) else None
    return _clean_priority_list(raw)


def save_play_priorities(
    root: Path, priorities: list[int], tasks_rel: str = ".tasks"
) -> list[int]:
    """Persist a queue's play-priority filter (sorted/de-duped/validated),
    preserving sibling keys. An empty list clears the filter (play all). Returns
    the cleaned list written."""
    clean = _clean_priority_list(priorities)
    save_queue_config_value(root, "play_priorities", clean, tasks_rel)
    return clean


def _apply_play_filter(
    root: Path,
    stems: list[str],
    tasks_rel: str = ".tasks",
    *,
    priorities: dict[str, int] | None = None,
) -> list[str]:
    """Drop stems whose priority isn't in the queue's play-priority filter.

    A no-op when the filter is empty ("all priorities"). ``priorities`` lets a
    caller pass an already-parsed priority map to avoid a second read."""
    selected = load_play_priorities(root, tasks_rel)
    if not selected:
        return stems
    chosen = set(selected)
    if priorities is None:
        priorities = _read_priorities(root, stems, tasks_rel)
    return [s for s in stems if priorities.get(s, DEFAULT_PRIORITY) in chosen]


def _read_priorities(
    root: Path, stems: list[str], tasks_rel: str = ".tasks"
) -> dict[str, int]:
    """Read the 0-5 priority of each stem from its task file's frontmatter.

    Missing files / frontmatter fall back to the default (lowest) priority via
    :func:`task_priority`."""
    tasks_dir = root / tasks_rel
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
    root: Path,
    stems: list[str],
    tasks_rel: str = ".tasks",
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
    order = load_order(root, tasks_rel)
    rank = {name: i for i, name in enumerate(order)}
    listed = sorted((s for s in stems if s in rank), key=lambda s: rank[s])
    unlisted = sorted(s for s in stems if s not in rank)
    manual = listed + unlisted

    if sort is None:
        sort = load_sort_mode(root, tasks_rel)
    if sort != SORT_PRIORITY:
        return manual

    if priorities is None:
        priorities = _read_priorities(root, manual, tasks_rel)
    manual_index = {stem: i for i, stem in enumerate(manual)}
    return sorted(
        manual,
        key=lambda s: (priorities.get(s, DEFAULT_PRIORITY), manual_index[s]),
    )


def reorder_queue(root: Path, order: list[str], tasks_rel: str = ".tasks") -> list[str]:
    """Set the queue execution order from a UI drag.

    Accepts the desired ordering of task stems; only stems backed by an actual
    `<tasks_rel>/<stem>.md` file are kept (guards against stale/spoofed names),
    and any existing queue task omitted from ``order`` is appended in filename
    order so a partial payload never drops tasks. Returns the persisted order.
    """
    tasks_dir = root / tasks_rel
    existing = {p.stem for p in tasks_dir.glob("*.md")} if tasks_dir.exists() else set()
    seen: set[str] = set()
    cleaned: list[str] = []
    for name in order:
        if name in existing and name not in seen:
            cleaned.append(name)
            seen.add(name)
    for name in sorted(existing - seen):
        cleaned.append(name)
    return save_order(root, cleaned, tasks_rel)


def build_task_list(root: Path, task_arg: str, tasks_rel: str = ".tasks") -> list[str]:
    """Build the ordered list of tasks to run for a queue.

    Autosplit dispatch (spawning subtasks, committing the daily queue) applies
    only to the main `.tasks` queue; a playlist is a plain ordered set of its
    own `*.md` files.
    """
    tasks_dir = root / tasks_rel
    is_main = tasks_rel == ".tasks"

    if task_arg != "all":
        if is_main:
            autosplit_sources = set(find_autosplit_sources(root))
            if task_arg in autosplit_sources:
                result = spawn_source(root, task_arg, write=True)
                if result and result.spawned:
                    _commit_dispatch(root)
                    return [t.name for t in result.spawned]
                return []
        return [task_arg]

    results = []
    if is_main:
        results = spawn_all(root, write=True)
        if results:
            _commit_dispatch(root)

    queue_names = live_ordered_queue(root, tasks_rel)
    spawned_names = [t.name for r in results for t in r.spawned]
    ordered = order_stems(root, list(set(queue_names) | set(spawned_names)), tasks_rel)
    # Re-apply the play-priority filter so freshly-spawned autosplit subtasks
    # (folded in via the union above) also respect the active filter.
    return _apply_play_filter(root, ordered, tasks_rel)


def live_ordered_queue(root: Path, tasks_rel: str = ".tasks") -> list[str]:
    """Read-only ordered scan of a queue's runnable task stems.

    Globs ``<tasks_rel>/*.md``, skips autosplit-source and disabled files, and
    returns the stems in the queue's configured order. This is the side-effect-
    free core of :func:`build_task_list` ("all") — no spawning, no commits — and
    is reused by the live re-scan in :func:`run_queue` (which calls it every
    iteration, so it must stay quiet and cheap).
    """
    tasks_dir = root / tasks_rel
    if not tasks_dir.exists():
        return []
    autosplit = _find_autosplit_tasks(tasks_dir)
    queue_names: list[str] = []
    priorities: dict[str, int] = {}
    for p in tasks_dir.glob("*.md"):
        if p.stem in autosplit:
            continue
        text = p.read_text(errors="replace")
        meta = split_frontmatter(text)[0] if text.startswith("---") else {}
        if is_disabled(meta):
            continue
        queue_names.append(p.stem)
        priorities[p.stem] = task_priority(meta)
    ordered = order_stems(root, queue_names, tasks_rel, priorities=priorities)
    return _apply_play_filter(root, ordered, tasks_rel, priorities=priorities)


def _find_autosplit_tasks(tasks_dir: Path) -> set[str]:
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


def _commit_dispatch(root: Path) -> None:
    """Commit daily-queue dispatch (spawned files + evergreen reset) to main."""
    subprocess.run(
        ["git", "add", ".tasks/"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "status", "--porcelain", ".tasks/"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        subprocess.run(
            ["git", "commit", "-m", "nightshift-local: dispatch daily queues"],
            cwd=root,
            check=True,
            capture_output=True,
        )


# What a pre-run snapshot commits: the whole ``.tasks/`` tree (task briefs,
# queue ``config.json``, and any queue-adjacent files a worker might read) so the
# worktree mirrors the operator's working state — *minus* the runtime run/log
# records. Those records are live state written *during* a run; the main queue's
# ``runs/``/``logs/`` are gitignored, but a playlist's are tracked (the gitignore
# only covers ``.tasks/runs``), so they must be excluded explicitly or a snapshot
# would sweep in-flight run output. The leading ``.tasks`` always matches the
# (existing) dir, so unlike a ``*.md``-only pathspec this never aborts ``git add``
# when an optional file is absent. ``:(exclude,glob)`` magic spans every playlist.
QUEUE_SNAPSHOT_PATHSPECS = (
    ".tasks",
    ":(exclude,glob).tasks/**/runs/**",
    ":(exclude,glob).tasks/**/logs/**",
)


def _queue_snapshot_pathspecs(tasks_rel: str) -> tuple[str, ...]:
    """Pathspecs that scope a snapshot to a single queue's *definition* subtree.

    The main queue snapshots only its top-level ``.tasks/*.md`` + ``config.json``
    (excluding every playlist sub-dir), and a playlist snapshots only its own
    ``.tasks/<name>/**`` minus runtime ``runs/``/``logs/``. Scoping per queue
    means two concurrent runners' snapshots never sweep each other's files."""
    if tasks_rel == ".tasks":
        return (".tasks", ":(exclude,glob).tasks/*/**")
    return (
        tasks_rel,
        f":(exclude,glob){tasks_rel}/runs/**",
        f":(exclude,glob){tasks_rel}/logs/**",
    )


def commit_queue_state(root: Path, tasks_rel: str = ".tasks") -> str | None:
    """Commit any uncommitted queue changes so a run's worktree sees them.

    Worktrees are branched from committed ``HEAD`` (see :func:`setup_worktree`),
    so a task file added/edited/reordered through the UI but not yet committed is
    *absent* from the worker's checkout — the worker is handed a task path that
    doesn't exist, and the run fails. Snapshotting the queue just before a run
    closes that race.

    Only the landing queue's own definition subtree is committed (see
    :func:`_queue_snapshot_pathspecs`; runtime ``runs/``/``logs/`` and other
    queues' files are excluded), so a user's unrelated working-tree edits — and a
    concurrent queue's snapshot — are left untouched. Returns the new commit's
    short sha, or ``None`` when the queue was already clean (the common case once
    everything is committed). Callers that mutate the root index/HEAD must hold
    :func:`landing_lock`; this function does not take it itself (so it can run
    inside a squash's lock without deadlocking).
    """
    if not (root / ".tasks").exists():
        return None
    pathspecs = _queue_snapshot_pathspecs(tasks_rel)
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    # ``git add`` walks the positive pathspec's tree and exits 1 when it meets a
    # fully-ignored runtime dir (``runs/``/``logs/`` from .gitignore): the wanted
    # definition files are still staged and the ignored ones skipped — exactly the
    # intent — but the non-zero exit would otherwise abort the run. Neither the
    # negative pathspecs above nor ``--ignore-errors`` suppress it, so tolerate the
    # ignored-files case here while still surfacing any real failure (lock, etc.).
    add = subprocess.run(
        ["git", "-c", "advice.addIgnoredFile=false", "add", "--", *pathspecs],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0 and (
        add.returncode != 1
        or "ignored by one of your .gitignore" not in add.stderr
    ):
        raise subprocess.CalledProcessError(
            add.returncode, add.args, output=add.stdout, stderr=add.stderr
        )
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--", *pathspecs],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if not staged.stdout.strip():
        return None
    commit = subprocess.run(
        ["git", "commit", "-m", "nightshift: snapshot queue state before run",
         "--", *pathspecs],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        return None
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
    ).stdout.strip() or None


TASK_TEMPLATE = asset("templates", "task.md")


def create_task(root: Path, title: str, text: str, tasks_rel: str = ".tasks") -> dict:
    """Create a new task file `<tasks_rel>/<slug(title)>.md` from the template.

    Tasks are no longer numbered: the filename is the slugified title and the
    new task is appended to the queue's `config.json` execution order (so it
    lands at the end of the queue, where the operator can drag it into place).

    Raises ``ValueError`` for an empty title and ``FileExistsError`` if the
    target name is already taken.
    """
    title_clean = title.strip()
    if not title_clean:
        raise ValueError("title is required")

    tasks_dir = root / tasks_rel
    tasks_dir.mkdir(parents=True, exist_ok=True)
    name = slugify(title_clean)
    dest = tasks_dir / f"{name}.md"
    if dest.exists():
        raise FileExistsError(name)

    template = TASK_TEMPLATE.read_text()
    content = template.replace(
        "title: short descriptive title for the PR", f"title: {title_clean}", 1
    )
    content = content.replace("Task description goes here.", text.strip() or title_clean, 1)
    dest.write_text(content)
    save_order(root, [*load_order(root, tasks_rel), name], tasks_rel)
    return {"task": name, "title": title_clean}


def delete_task(root: Path, task: str, tasks_rel: str = ".tasks") -> dict:
    """Delete a queue task file ``<tasks_rel>/<task>.md``.

    Guards against path traversal: ``task`` must resolve to a direct child of
    the queue's tasks dir. Raises ``FileNotFoundError`` if there's no such task.
    """
    tasks_dir = (root / tasks_rel).resolve()
    dest = (tasks_dir / f"{task}.md").resolve()
    if dest.parent != tasks_dir or not dest.is_file():
        raise FileNotFoundError(task)
    dest.unlink()
    order = load_order(root, tasks_rel)
    if task in order:
        save_order(root, [name for name in order if name != task], tasks_rel)
    return {"task": task, "deleted": True}


def task_is_evergreen(meta: dict, task: str, config: dict) -> bool:
    """True when a task is evergreen — by its own frontmatter or by being listed
    in the queue config's ``evergreen_tasks``. Evergreen tasks reset and re-run,
    so they keep their file; regular tasks leave the queue once they complete."""
    return bool(meta.get("evergreen", False)) or task in set(
        config.get("evergreen_tasks", [])
    )


def drop_completed_task(
    root: Path, task: str, tasks_rel: str = ".tasks", *, queue: str | None = None
) -> bool:
    """Ensure a landed regular task's file is gone from the queue on ``main``.

    Queue removal of a completed task is the worker's job (it ``git rm``\\ s its
    own task file in the branch that lands). But a worker can finish successfully
    without removing it — a "no changes" completion never touches the file, and a
    worker may simply forget — leaving the task in ``<tasks_rel>/`` so the UI
    keeps listing a task that has already completed. This is the engine's
    backstop: after a successful land it deletes any lingering task file (and its
    execution-order entry) and commits that removal, so :func:`list_queue` and
    the dashboard drop the completed item.

    No-ops (returns ``False``) when the file is already gone — the common case
    when the worker did remove it. Returns ``True`` when it removed the file.
    Held under :func:`landing_lock` so the commit can't race a concurrent
    queue's snapshot/land on the shared index/HEAD.
    """
    task_file = (root / tasks_rel).resolve() / f"{task}.md"
    if not task_file.is_file():
        return False
    with landing_lock(root):
        # Re-check inside the lock: a concurrent land may have removed it.
        if not task_file.is_file():
            return False
        delete_task(root, task, tasks_rel)
        pathspecs = _queue_snapshot_pathspecs(tasks_rel)
        subprocess.run(
            ["git", "add", "--", *pathspecs],
            cwd=root,
            capture_output=True,
            text=True,
        )
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--", *pathspecs],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if staged.stdout.strip():
            subprocess.run(
                ["git", "commit", "-m", f"nightshift: drop completed task {task}",
                 "--", *pathspecs],
                cwd=root,
                capture_output=True,
                text=True,
            )
    return True


def import_task(root: Path, src_rel: str, task: str, dest_rel: str) -> dict:
    """Copy a task file from one queue into another, appending it to the
    destination's execution order.

    ``<src_rel>/<task>.md`` is copied verbatim (frontmatter and body) into
    ``<dest_rel>/``; if that name is already taken there, a numeric suffix is
    added so nothing is clobbered. Both paths are guarded against traversal the
    same way :func:`delete_task` is. Returns ``{task, title}`` for the new copy.
    """
    src_dir = (root / src_rel).resolve()
    src = (src_dir / f"{task}.md").resolve()
    if src.parent != src_dir or not src.is_file():
        raise FileNotFoundError(task)

    dest_dir = (root / dest_rel).resolve()
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
    save_order(root, [*load_order(root, dest_rel), name], dest_rel)

    meta = split_frontmatter(text)[0] if text.startswith("---") else {}
    return {"task": name, "title": resolve_title(name, meta)}


def read_task(root: Path, task: str, tasks_rel: str = ".tasks") -> dict:
    """Read a single queue task file ``.tasks/<task>.md`` for the detail view.

    Returns ``{task, title, body, frontmatter, evergreen, disabled}`` where
    ``frontmatter`` is the parsed YAML block merged with resolved defaults
    (model/draft/automerge) so the brief shows the effective values, and
    ``body`` is the spec prose with the frontmatter fence stripped. Read-only:
    it neither spawns subtasks nor mutates the queue.

    Guards against path traversal the same way :func:`delete_task` does: ``task``
    must resolve to a direct child of ``.tasks``. Raises ``FileNotFoundError``
    if there's no such task.
    """
    tasks_dir = (root / tasks_rel).resolve()
    dest = (tasks_dir / f"{task}.md").resolve()
    if dest.parent != tasks_dir or not dest.is_file():
        raise FileNotFoundError(task)

    text = dest.read_text(errors="replace")
    meta, body = split_frontmatter(text) if text.startswith("---") else ({}, text)
    config = resolve_config(root, tasks_rel)
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
        "frontmatter": frontmatter,
        # The raw, file-only frontmatter (before defaults are layered in) so the
        # editor can tell whether a field is explicitly set vs inherited — e.g.
        # "model" absent here means the task uses the config default.
        "frontmatter_raw": dict(meta),
        "evergreen": evergreen,
        "disabled": is_disabled(meta),
        # Curated model choices for the detail-view dropdown ("default" + these).
        "model_options": list(config.get("scheduled_models", [])),
    }


# Frontmatter keys the detail-view editor is allowed to set. ``model: None``
# clears the key so the task inherits the config default. ``title`` is a
# frontmatter key too, but is written via the dedicated ``title`` change so it
# always lands ahead of the other keys (it's the file's headline).
_EDITABLE_META_KEYS = {"disabled", "evergreen", "automerge", "draft", "model", "priority"}

# The detail-view editor may also rewrite the spec prose (``body``) and the
# headline (``title``); these aren't plain frontmatter scalars so they're
# handled separately from :data:`_EDITABLE_META_KEYS`.
_EDITABLE_CONTENT_KEYS = {"title", "body"}


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
    root: Path,
    task: str,
    changes: dict[str, object | None],
    tasks_rel: str = ".tasks",
) -> dict:
    """Update a queue task file in place from the detail-view editor.

    ``changes`` maps a key to its new value. Frontmatter scalars
    (:data:`_EDITABLE_META_KEYS`) are rewritten where they sit — preserving field
    order and any unrelated keys or comments — and missing keys are appended just
    before the closing fence; a value of ``None`` removes the key (so the task
    falls back to the config default). A file without a frontmatter fence gains
    one. Booleans serialise as ``true``/``false``.

    ``title`` (the headline, stored as a frontmatter key) and ``body`` (the spec
    prose below the fence) are content edits: ``title`` is written/updated as the
    leading frontmatter key, and ``body`` replaces the prose verbatim. Both are
    optional; omitting them leaves the existing content untouched.

    Only keys in :data:`_EDITABLE_META_KEYS` ∪ :data:`_EDITABLE_CONTENT_KEYS` are
    accepted. Guards against path traversal exactly like :func:`read_task`.
    Returns the refreshed brief.
    """
    bad = set(changes) - _EDITABLE_META_KEYS - _EDITABLE_CONTENT_KEYS
    if bad:
        raise ValueError(f"non-editable keys: {', '.join(sorted(bad))}")

    tasks_dir = (root / tasks_rel).resolve()
    dest = (tasks_dir / f"{task}.md").resolve()
    if dest.parent != tasks_dir or not dest.is_file():
        raise FileNotFoundError(task)

    new_title = changes.get("title") if "title" in changes else None
    new_body = changes.get("body") if "body" in changes else None
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

    if "body" in changes:
        body = _strip_leading_blanks(str(new_body or "").splitlines())
    else:
        body = _strip_leading_blanks(body_lines)
    if new_fence:
        out = ["---", *new_fence, "---", "", *body]
    else:
        out = body
    dest.write_text("\n".join(out).rstrip("\n") + "\n")

    return read_task(root, task, tasks_rel)


def list_queue(root: Path, tasks_rel: str = ".tasks") -> list[dict]:
    """List top-level `<tasks_rel>/*.md` (skips subdirs) for the UI queue.

    Returns ``{task, title, evergreen, disabled}`` in the configured execution
    order (the queue's `config.json` ``order``), falling back to filename order
    for unlisted tasks. Unlike :func:`build_task_list` this is read-only: it
    neither spawns autosplit subtasks nor commits.
    """
    tasks_dir = root / tasks_rel
    if not tasks_dir.exists():
        return []
    config = resolve_config(root, tasks_rel)
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
            "priority": priorities[p.stem],
        }
    ordered = order_stems(root, list(by_stem), tasks_rel, priorities=priorities)
    return [by_stem[s] for s in ordered]


# --------------------------------------------------------------------------- #
# Worker prompt / argv
# --------------------------------------------------------------------------- #


def build_prompt(root: Path, task: str, tasks_rel: str = ".tasks") -> str:
    """Build the worker prompt matching CI injection format.

    The validate command is resolved through the queue's layered config
    (:func:`resolve_config`) and injected as ``$VALIDATE`` so the worker runs
    the queue's own gate (e.g. a playlist's ``just validate-nightshift``) rather
    than the hardcoded default — matching the command the engine later enforces.

    ``$TASK_FILE`` carries the task file's *actual* queue-relative path
    (``{tasks_rel}/{task}.md``), which is ``.tasks/<task>.md`` for the main queue
    but ``.tasks/<playlist>/<task>.md`` for a playlist. The worker removes (and
    reads) this path rather than a hardcoded ``.tasks/<task>.md`` so a completed
    playlist task actually leaves its queue instead of lingering after it lands.
    """
    prompt_file = asset("prompts", "nightshift-local.md")
    prompt_body = prompt_file.read_text()
    validate_cmd = str(resolve_config(root, tasks_rel).get("validate") or DEFAULT_VALIDATE_CMD)
    task_file = f"{tasks_rel}/{task}.md"
    return (
        f"Your task file is: {task_file}\n"
        f"The TASK variable is: {task}\n"
        f"The TASK_FILE variable is: {task_file}\n"
        f"The VALIDATE command is: {validate_cmd}\n\n"
        f"{prompt_body}"
    )


def build_claude_argv(
    prompt: str,
    model: str,
    max_turns: int | None,
) -> list[str]:
    """Build the claude CLI argument vector.

    Uses ``--output-format stream-json --verbose`` so the run emits structured
    events: ``backends.AgentStreamParser`` renders the readable text for the live
    log and captures turn/token/cost telemetry from the final ``result`` event.
    The parser passes any non-JSON line through unchanged, so an older CLI that
    ignores these flags still streams output (just without telemetry).
    """
    argv = [
        "claude",
        "-p", prompt,
        "--model", model,
        "--allowedTools", "Bash,Edit,MultiEdit,Write,Read,Glob,Grep,LS",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
    ]
    if max_turns is not None:
        argv.extend(["--max-turns", str(max_turns)])
    return argv


# Bin dirs that interactive/login shells commonly add but a service started
# from a non-login shell (e.g. the UI server) may be missing.
_EXTRA_BIN_DIRS = (
    str(Path.home() / ".local/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def resolve_claude_bin(config: dict | None = None) -> str:
    """Resolve the ``claude`` executable robustly.

    Order: an explicit ``claude_bin`` in config, then ``$PATH`` (shutil.which),
    then common install dirs that non-login shells miss. Falls back to the bare
    name so the caller surfaces a clear "not found" error.
    """
    if config and config.get("claude_bin"):
        return os.path.expanduser(str(config["claude_bin"]))
    found = shutil.which("claude")
    if found:
        return found
    for d in _EXTRA_BIN_DIRS:
        cand = Path(d) / "claude"
        if cand.exists():
            return str(cand)
    return "claude"


def worker_env() -> dict[str, str]:
    """A child-process environment with the common bin dirs on PATH, so the
    worker (and the tools it shells out to) resolve even when the server was
    launched from a non-login shell."""
    env = os.environ.copy()
    parts = env.get("PATH", "").split(os.pathsep)
    for d in _EXTRA_BIN_DIRS:
        if d not in parts:
            parts.append(d)
    env["PATH"] = os.pathsep.join(p for p in parts if p)
    return env


# --------------------------------------------------------------------------- #
# Worktree lifecycle
# --------------------------------------------------------------------------- #

SYMLINK_TARGETS = [
    ".venv",
    "services/dashboard_ui/node_modules",
    "node_modules",
]


def _queue_slug(queue: str | None) -> str:
    """Path/branch-safe token for a queue: ``main`` for the main ``.tasks`` queue,
    otherwise the playlist name (already a slug)."""
    return queue or "main"


def worktree_branch(task: str, queue: str | None = None) -> str:
    """Branch name for a task's local worktree, namespaced by queue so two queues
    holding a same-named task cut distinct branches."""
    return f"task-local/{_queue_slug(queue)}/{task}"


def worktree_dir(root: Path, task: str, queue: str | None = None) -> Path:
    """Worktree directory for a task, namespaced by queue (see
    :func:`worktree_branch`)."""
    return root / ".worktrees" / f"task-local-{_queue_slug(queue)}-{task}"


# Serialize every mutation of the root index/HEAD/stash so concurrent queue
# runners (and a stray CLI process) can never interleave on the shared working
# tree. Two layers: a process-local lock across registry runner threads, and a
# cross-process file lock so a server land and a CLI land can't collide.
_LANDING_LOCK = threading.Lock()
_LANDING_LOCK_FILE = ".worktrees/.nightshift-landing.lock"


@contextlib.contextmanager
def landing_lock(root: Path):
    """Hold the in-process + cross-process landing lock for a short critical
    section (a queue snapshot, or a squash-merge + commit). Not reentrant — never
    nest ``landing_lock`` calls on one thread."""
    with _LANDING_LOCK:
        path = root / _LANDING_LOCK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def setup_worktree(root: Path, task: str, *, queue: str | None = None) -> Path:
    """Create a git worktree and symlink build artifacts."""
    wt_dir = worktree_dir(root, task, queue)
    branch = worktree_branch(task, queue)

    if wt_dir.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_dir)],
            cwd=root,
            capture_output=True,
        )
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=root,
        capture_output=True,
    )

    subprocess.run(
        ["git", "worktree", "add", str(wt_dir), "-b", branch, "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    for target in SYMLINK_TARGETS:
        src = root / target
        dst = wt_dir / target
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)

    return wt_dir


def teardown_worktree(root: Path, task: str, *, queue: str | None = None) -> None:
    """Remove the worktree and its branch unconditionally."""
    wt_dir = worktree_dir(root, task, queue)
    branch = worktree_branch(task, queue)

    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_dir)],
        cwd=root,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=root,
        capture_output=True,
    )


def cleanup_task_worktree(root: Path, task: str, *, queue: str | None = None) -> bool:
    """Remove a task's *preserved* worktree + branch when present (the artifacts a
    failed-to-land task leaves behind for a later Resolve). Returns True when
    something existed and was removed; a no-op (False) when neither exists — so a
    cleanly-landed task, whose worktree the engine already tore down, is safe to
    pass here. Callers are responsible for the orphan check (no active/other run
    still needs the branch)."""
    if not worktree_dir(root, task, queue).exists() and not _branch_exists(
        root, worktree_branch(task, queue)
    ):
        return False
    teardown_worktree(root, task, queue=queue)
    return True


def _worktree_has_commits(root: Path, task: str, *, queue: str | None = None) -> bool:
    """True if the task's worktree branch has commits beyond ``HEAD``.

    A worker that made no commit (a non-agentic API backend, or an agentic one
    that decided nothing was needed) leaves nothing to validate or squash. When
    we can't tell, err on the side of "yes" so the normal path still runs.
    """
    branch = worktree_branch(task, queue)
    result = subprocess.run(
        ["git", "rev-list", "--count", f"HEAD..{branch}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip() or "0") > 0
    except ValueError:
        return True


def _tracked_changes(root: Path) -> list[str]:
    """Porcelain status lines for *tracked* changes in ``root`` (ignores
    untracked ``??`` files, which only block a merge if they collide — and that
    case surfaces via git's own stderr instead)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return [
        line for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("??")
    ]


def _porcelain_path(line: str) -> str:
    """Extract the working path from a ``git status --porcelain`` line.

    Lines are ``XY <path>``; a rename is ``R  <old> -> <new>`` — we want the
    destination. Surrounding quotes (git quotes paths with special chars) are
    stripped so prefix matching against ``.tasks/`` is reliable."""
    path = line[3:] if len(line) > 3 else line.strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip().strip('"')


def _is_queue_path(path: str) -> bool:
    """True for paths under the ``.tasks/`` tree (queue definitions, run records,
    logs) — the operator's queue state, not code a worker squash can conflict
    with."""
    return path == ".tasks" or path.startswith(".tasks/")


def _landing_blockers(root: Path) -> list[str]:
    """Tracked changes that should block a squash-merge: everything from
    :func:`_tracked_changes` EXCEPT paths under ``.tasks/``.

    Queue definition edits, live run records, and logs are the operator's queue
    state, not code that can conflict with a worker's squash — the worker never
    edits the parent queue's ``.tasks/``, so leaving them dirty is safe for
    ``git merge --squash``."""
    return [
        line for line in _tracked_changes(root)
        if not _is_queue_path(_porcelain_path(line))
    ]


AUTOSTASH_MESSAGE = "nightshift-autostash"


def _stash_operator_work(root: Path, paths: list[str]) -> str | None:
    """Set aside the operator's tracked code WIP (the given non-``.tasks`` paths)
    for the land critical section. Returns the captured stash *commit sha*, or
    ``None`` when there was nothing to set aside.

    Stack-free by design: ``git stash create`` records the WIP as a commit object
    without touching the LIFO stash stack, so a human running ``git stash``
    mid-land can never perturb it. ``stash create`` does not revert the tree, so
    we then explicitly clean just the blocker ``paths`` (leaving live ``.tasks/``
    run records and any other queue state dirty in the working tree)."""
    if not paths:
        return None
    created = subprocess.run(
        ["git", "stash", "create", AUTOSTASH_MESSAGE],
        cwd=root,
        capture_output=True,
        text=True,
    )
    sha = created.stdout.strip()
    if created.returncode != 0 or not sha:
        return None
    # `stash create` captured the WIP but left the tree dirty; clean exactly the
    # blocker paths so the merge sees them at HEAD (queue runtime state untouched).
    subprocess.run(
        ["git", "checkout", "HEAD", "--", *paths],
        cwd=root,
        capture_output=True,
    )
    return sha


def _restore_operator_work(root: Path, sha: str, paths: list[str]) -> str | None:
    """Re-apply the set-aside WIP commit ``sha`` on top of the landed tree.
    Returns ``None`` on success, or a human-readable conflict detail when the
    apply conflicts with what the task just landed.

    On conflict the blocker ``paths`` are rolled back to the landed ``HEAD`` (so
    the tree is left clean, not littered with conflict markers) and the WIP is
    preserved on the stash stack under the ``nightshift-autostash`` message via
    ``git stash store`` so the operator can recover it by hand — never lost."""
    result = subprocess.run(
        ["git", "stash", "apply", sha],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return None
    # Conflict: clear the half-applied blocker paths (keep .tasks runtime state)
    # and stash-store the sha so the WIP is findable for a manual restore.
    if paths:
        subprocess.run(
            ["git", "checkout", "HEAD", "--", *paths],
            cwd=root,
            capture_output=True,
        )
    subprocess.run(
        ["git", "stash", "store", "-m", AUTOSTASH_MESSAGE, sha],
        cwd=root,
        capture_output=True,
    )
    detail = (result.stderr.strip() or result.stdout.strip()
              or "git stash apply failed")
    return detail


def _reset_to_head(root: Path) -> None:
    """Undo a half-applied squash so a failed merge never leaves ``root`` in a
    conflicted/partly-staged state. Safe only because :func:`squash_to_main`
    refuses to start when the tree already has tracked changes."""
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=root, capture_output=True)


def _conflicted_paths(root: Path) -> list[str]:
    """Files left with unmerged (conflicted) entries in the index after a failed
    ``git merge --squash``. Must be read *before* :func:`_reset_to_head`."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Lines-of-code accounting for the Stats page.
# --------------------------------------------------------------------------
# A landed task is a single squash commit; its "lines of code" figure is the
# churn (added + removed lines) of that commit, excluding noise the spec calls
# out: build files, docs, comments, and blank lines. Surfaced per-task so the
# Stats page can sum it across history.

# Path suffixes / basenames that are not "code": docs, build, lockfiles, data.
# Matched case-insensitively against the file's suffix and basename.
_NON_CODE_SUFFIXES = frozenset({
    ".md", ".markdown", ".rst", ".adoc", ".txt",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".lock",
    ".csv", ".tsv", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico",
})
_NON_CODE_BASENAMES = frozenset({
    "justfile", "makefile", "dockerfile", "build", "build.bazel",
    "workspace", "workspace.bazel", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "poetry.lock", "uv.lock", "requirements.txt",
    "go.sum", "cargo.lock",
})
_NON_CODE_BAZEL_SUFFIXES = frozenset({".bazel", ".bzl"})

# Directory prefixes whose contents are never code for LOC accounting: the task
# queue itself, build/dist output, vendored deps, and the git dir. Matched
# case-insensitively against the full (forward-slashed) path. Mirrors the
# pathspec excludes the spec references
# (``:(exclude).tasks/``, ``:(exclude)dist/*``, ``:(exclude)build/*``, …).
_NON_CODE_DIR_PREFIXES: tuple[str, ...] = (
    ".tasks/",
    ".git/",
    "dist/",
    "build/",
    "node_modules/",
    "vendor/",
    ".venv/",
)

# Per-language line-comment prefixes, keyed by code-file suffix. A diff line
# whose content (after stripping leading whitespace) starts with one of these
# is a comment and excluded from the count.
_LINE_COMMENT_PREFIXES: dict[str, tuple[str, ...]] = {
    ".py": ("#",),
    ".pyi": ("#",),
    ".sh": ("#",),
    ".bash": ("#",),
    ".rb": ("#",),
    ".js": ("//",),
    ".jsx": ("//",),
    ".ts": ("//",),
    ".tsx": ("//",),
    ".mjs": ("//",),
    ".cjs": ("//",),
    ".css": ("/*",),
    ".scss": ("//", "/*"),
    ".go": ("//",),
    ".rs": ("//",),
    ".c": ("//", "/*"),
    ".h": ("//", "/*"),
    ".cpp": ("//", "/*"),
    ".java": ("//", "/*"),
    ".sql": ("--",),
}


def _is_code_path(path: str) -> bool:
    """True if ``path`` counts as code for LOC accounting — i.e. not a doc,
    build file, lockfile, or data/asset file (the categories the spec excludes)."""
    lowered = path.lower()
    # A path under any excluded directory (anywhere in the tree) is not code:
    # ``.tasks/10.foo.md``, ``services/ui/dist/bundle.js``, ``a/build/x`` …
    for prefix in _NON_CODE_DIR_PREFIXES:
        if lowered.startswith(prefix) or ("/" + prefix) in lowered:
            return False
    name = path.rsplit("/", 1)[-1].lower()
    suffix = ""
    dot = name.rfind(".")
    if dot > 0:
        suffix = name[dot:]
    if name in _NON_CODE_BASENAMES:
        return False
    if suffix in _NON_CODE_SUFFIXES or suffix in _NON_CODE_BAZEL_SUFFIXES:
        return False
    return True


def _is_comment_line(content: str, suffix: str) -> bool:
    """True if a diff line's content is a blank line or a single-line comment for
    the file's language. Block-comment interiors are not tracked (a pragmatic
    line-prefix heuristic, not a parser), which is acceptable for a churn stat."""
    stripped = content.strip()
    if not stripped:
        return True
    prefixes = _LINE_COMMENT_PREFIXES.get(suffix)
    if not prefixes:
        return False
    return any(stripped.startswith(p) for p in prefixes)


def compute_code_loc(root: Path, sha: str) -> int:
    """Lines of code churned by a single commit ``sha`` (added + removed),
    excluding build files, docs, comments, and blank lines.

    Reads the commit's own diff (``git show <sha>``) so a squash commit is
    measured against its parent. Returns 0 on any git error or for the initial
    commit (no parent) so a missing figure never breaks a run record."""
    result = subprocess.run(
        ["git", "show", sha, "--format=", "--unified=0", "--no-color", "--no-renames"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0
    return _count_diff_code_lines(result.stdout)


def _count_diff_code_lines(diff: str) -> int:
    """Count added/removed code lines in a ``git show``/``git diff`` body,
    excluding non-code paths, comments, and blank lines."""
    total = 0
    suffix = ""
    counts = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            # `diff --git a/<path> b/<path>` — use the destination path to decide
            # whether this file's hunks count.
            parts = line.split(" b/", 1)
            path = parts[1].strip() if len(parts) == 2 else ""
            counts = bool(path) and _is_code_path(path)
            dot = path.rfind(".")
            suffix = path[dot:].lower() if dot > 0 else ""
            continue
        if not counts:
            continue
        # Skip file/hunk headers; +++/--- are metadata, not content.
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+") or line.startswith("-"):
            if not _is_comment_line(line[1:], suffix):
                total += 1
    return total


def _branch_exists(root: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=root,
        capture_output=True,
    ).returncode == 0


def _squash_failure_kind(recoverable: bool, detail: str) -> str:
    """Classify a :func:`squash_to_main` failure into a ``failure_kind``.

    A content conflict (overlapping edits) is ``merge_conflict``; everything else
    — a dirty ``main`` (transient), a failed commit, or a generic merge abort —
    is ``merge_rejected``.
    """
    if not recoverable and detail.startswith("merge conflict"):
        return "merge_conflict"
    return "merge_rejected"


def squash_to_main(
    root: Path, task: str, title: str, *, queue: str | None = None, autostash: bool = True,
) -> tuple[str | None, str, bool]:
    """Merge the task's worktree branch as a single squash commit on ``main``.

    Returns ``(sha, "", False)`` on success, or ``(None, detail, recoverable)``
    on failure where ``detail`` is a human-readable reason and ``recoverable``
    says whether re-attempting the *same* squash could succeed once the user
    clears a blocker.

    Queue state (``.tasks/``) never blocks a land: it is snapshotted up-front via
    :func:`commit_queue_state`, and the dirty-tree precheck (:func:`_landing_blockers`)
    ignores it, so adding/editing tasks or live playlist run records mid-run can
    never refuse a squash.

    Genuine operator *code* WIP in ``main`` is handled by ``autostash`` (default
    on, set per-playlist via ``autostash_operator_work``):

    * ``autostash=True`` — the operator's tracked code changes are set aside with
      ``git stash`` for the brief merge+commit, then restored. A success may
      return ``(sha, detail, False)`` with a non-empty ``detail`` when restoring
      the set-aside work hit a conflict (the land still happened; the stash entry
      is preserved). Callers that key off ``sha is None`` treat this as success.
    * ``autostash=False`` — preserves the old behavior: a code blocker returns
      ``recoverable=True`` ("main has uncommitted changes").

    Two failure shapes still matter and are NOT the same:

    * **Transient blocker** (``recoverable=True``) — ``main`` has uncommitted code
      and autostash is off. Re-running after committing/stashing will work.
    * **Content conflict** (``recoverable=False``) — the branch and ``main`` made
      overlapping edits. ``git merge --squash`` aborts; re-running fails
      identically. We list the conflicting files for a human 3-way resolution.

    On any merge/commit failure the working tree is reset back to ``HEAD`` (and
    any set-aside work restored) so it is never left half-merged.

    The whole critical section (snapshot → optional set-aside → merge → commit →
    reset-on-failure → restore) runs under :func:`landing_lock`, so concurrent
    queue runners (and a CLI process) serialize on the shared index/HEAD/stash
    instead of seeing a half-merged tree.
    """
    branch = worktree_branch(task, queue)

    if not _branch_exists(root, branch):
        return None, f"no task branch '{branch}' to merge (nothing to recover)", False

    with landing_lock(root):
        # Snapshot the queue so .tasks/ edits + live run records never block the
        # land (queue-scoped so a concurrent queue's snapshot can't collide).
        commit_queue_state(root, playlists.tasks_rel(queue))

        blockers = _landing_blockers(root)
        wip_sha: str | None = None
        blocker_paths: list[str] = []
        if blockers:
            blocker_paths = [_porcelain_path(line) for line in blockers]
            if autostash:
                wip_sha = _stash_operator_work(root, blocker_paths)
            if not autostash or wip_sha is None:
                shown = "\n".join(f"    {line}" for line in blockers[:20])
                extra = "" if len(blockers) <= 20 else f"\n    … and {len(blockers) - 20} more"
                return None, (
                    "main has uncommitted changes — commit or stash them before the "
                    f"squash-merge can run:\n{shown}{extra}"
                ), True

        restore_detail: str | None = None
        try:
            merge = subprocess.run(
                ["git", "merge", "--squash", branch],
                cwd=root,
                capture_output=True,
                text=True,
            )
            if merge.returncode != 0:
                conflicts = _conflicted_paths(root)
                _reset_to_head(root)
                if conflicts:
                    shown = "\n".join(f"    {p}" for p in conflicts)
                    return None, (
                        f"merge conflict — '{branch}' and main made overlapping edits to "
                        f"{len(conflicts)} file(s):\n{shown}\n"
                        "This cannot be auto-recovered; resolve the 3-way merge by hand "
                        "(see `recover_task` docs) or drop the stale branch."
                    ), False
                detail = (
                    merge.stderr.strip()
                    or merge.stdout.strip()
                    or f"git merge --squash {branch} exited {merge.returncode}"
                )
                return None, f"merge --squash failed:\n{detail}", False

            commit = subprocess.run(
                ["git", "commit", "-m", f"task: {title}"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            if commit.returncode != 0:
                detail = (
                    commit.stderr.strip()
                    or commit.stdout.strip()
                    or f"git commit exited {commit.returncode}"
                )
                _reset_to_head(root)
                return None, f"commit failed:\n{detail}", False

            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
            ).stdout.strip()
        finally:
            if wip_sha:
                restore_detail = _restore_operator_work(root, wip_sha, blocker_paths)

    if restore_detail:
        return sha, (
            f"landed ({sha}), but your set-aside working changes could not be "
            f"reapplied cleanly — they are kept in `git stash` for manual "
            f"restore:\n{restore_detail}"
        ), False
    return sha, "", False


def recover_task(
    root: Path, task: str, title: str, *, queue: str | None = None
) -> TaskResult:
    """Re-attempt the squash-merge for a task whose validate passed but whose
    merge to ``main`` failed (typically a dirty tree at the time).

    The worktree branch is preserved on such failures precisely so this cheap
    recovery is possible without re-running the worker. On success the branch
    and worktree are torn down; on failure they are left in place so the user
    can fix the blocker (e.g. commit their work) and retry again.
    """
    branch = worktree_branch(task, queue)
    if not _branch_exists(root, branch):
        return TaskResult(
            task=task, title=title, success=False,
            error=(
                "nothing to recover: the task branch no longer exists. "
                "Re-run the task instead."
            ),
        )

    autostash = bool(
        resolve_config(root, playlists.tasks_rel(queue)).get(
            "autostash_operator_work", True
        )
    )
    sha, detail, _ = squash_to_main(root, task, title, queue=queue, autostash=autostash)
    if sha is None:
        return TaskResult(
            task=task, title=title, success=False,
            error=detail or "squash-merge to main failed",
        )

    teardown_worktree(root, task, queue=queue)
    return TaskResult(
        task=task, title=title, success=True, commit_sha=sha,
        result_line=f"recovered: landed ({sha})",
    )


# --------------------------------------------------------------------------- #
# Resolve — diagnose + agentic conflict/validation resolution
# --------------------------------------------------------------------------- #

RESOLVE_PROMPT_FILE = asset("prompts", "nightshift-resolve.md")
DEFAULT_MAX_RESOLVE_ATTEMPTS = 2


def build_resolve_prompt(root: Path, task: str, *, context: str) -> str:
    """Build the prompt for the resolve agent — the resolution charter plus the
    concrete reason the squash-merge to main failed."""
    prompt_body = RESOLVE_PROMPT_FILE.read_text()
    return (
        f"Your task file is: .tasks/{task}.md\n"
        f"The TASK variable is: {task}\n\n"
        f"## Why the merge to main failed\n\n{context}\n\n"
        f"{prompt_body}"
    )


def _link_worktree_artifacts(root: Path, worktree_dir: Path) -> None:
    """Symlink build artifacts (`.venv`, node_modules) into ``worktree_dir`` so a
    re-attached worktree can run ``just validate``."""
    for target in SYMLINK_TARGETS:
        src = root / target
        dst = worktree_dir / target
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)


def _ensure_worktree_for_branch(
    root: Path, task: str, *, queue: str | None = None
) -> Path | None:
    """Ensure the task's worktree exists on its preserved branch (re-attaching it
    if its checkout was cleaned up). Returns the dir, or ``None`` if the branch is
    gone. Unlike :func:`setup_worktree` this never deletes the branch."""
    branch = worktree_branch(task, queue)
    if not _branch_exists(root, branch):
        return None
    wt_dir = worktree_dir(root, task, queue)
    if not wt_dir.exists():
        subprocess.run(
            ["git", "worktree", "add", str(wt_dir), branch],
            cwd=root,
            capture_output=True,
        )
    if not wt_dir.exists():
        return None
    _link_worktree_artifacts(root, wt_dir)
    return wt_dir


def _rebase_in_progress(worktree_dir: Path) -> bool:
    """True while a rebase is paused (e.g. on conflicts) in ``worktree_dir``."""
    return subprocess.run(
        ["git", "rebase", "--show-current-patch"],
        cwd=worktree_dir,
        capture_output=True,
    ).returncode == 0


def _abort_rebase(worktree_dir: Path) -> None:
    subprocess.run(
        ["git", "rebase", "--abort"], cwd=worktree_dir, capture_output=True
    )


def _rebase_onto_main(worktree_dir: Path) -> tuple[str, str]:
    """Rebase the worktree's branch onto ``main``.

    Returns ``("clean", "")`` when it applied with no conflicts, ``("conflict",
    detail)`` when it paused on conflicts (rebase left in progress for the agent
    to resolve), or ``("error", detail)`` for any other failure.
    """
    if _rebase_in_progress(worktree_dir):
        _abort_rebase(worktree_dir)
    result = subprocess.run(
        ["git", "rebase", "main"],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return "clean", ""
    detail = (result.stdout + "\n" + result.stderr).strip()
    if _rebase_in_progress(worktree_dir):
        return "conflict", detail
    return "error", detail


def resolve_task(
    root: Path,
    task: str,
    title: str,
    *,
    emit: Listener = _noop,
    config: dict | None = None,
    backend_name: str | None = None,
    abort_reason: object = None,
    queue: str | None = None,
) -> TaskResult:
    """Resolve a task whose validated work failed to land on ``main``.

    Diagnoses first, then acts:

    * If a plain re-squash now works (a transient blocker has cleared) it lands
      immediately — this is the cheap legacy :func:`recover_task` path.
    * If ``main`` is still dirty (``recoverable``) it reports that: an agent can't
      touch the operator's unrelated local edits.
    * Otherwise it's a content conflict: an agent rebases the branch onto ``main``,
      resolves the conflicts, re-validates, and squashes — bounded by
      ``max_resolve_attempts``.

    Emits ``TASK_STARTED``/``TASK_STATUS``/``TASK_RESULT`` so the caller can drive
    it as a tracked job (live log + ``resolve`` phase). The branch is preserved on
    failure so the operator can still resolve it by hand.
    """
    config = config or load_config(root)
    branch = worktree_branch(task, queue)

    task_file = root / playlists.tasks_rel(queue) / f"{task}.md"
    meta: dict = {}
    body = ""
    if task_file.exists():
        meta, body = split_frontmatter(task_file.read_text())
    emit(Event(TASK_STARTED, {
        "task": task, "title": title, "frontmatter": {**meta}, "body": body.strip(),
    }))

    if not _branch_exists(root, branch):
        error = (
            "nothing to resolve: the task branch no longer exists. "
            "Re-run the task instead."
        )
        emit(Event(TASK_RESULT, {
            "task": task, "status": "error", "error": error,
            "result_line": "branch gone — re-run the task",
            "failure_kind": "merge_rejected",
        }))
        return TaskResult(
            task=task, title=title, success=False, error=error,
            failure_kind="merge_rejected",
        )

    # 1. Cheap path: re-attempt the squash. Lands transient blockers that cleared.
    emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "commit"}))
    autostash = bool(config.get("autostash_operator_work", True))
    sha, detail, recoverable = squash_to_main(
        root, task, title, queue=queue, autostash=autostash
    )
    if sha is not None:
        loc = compute_code_loc(root, sha)
        teardown_worktree(root, task, queue=queue)
        # Backstop queue removal for a landed regular task (see drop_completed_task).
        if not task_is_evergreen(meta, task, config):
            drop_completed_task(root, task, playlists.tasks_rel(queue), queue=queue)
        result_line = f"resolved: landed ({sha})"
        emit(Event(TASK_RESULT, {
            "task": task, "status": "completed",
            "result_line": result_line, "commit_sha": sha, "loc": loc,
        }))
        return TaskResult(
            task=task, title=title, success=True, commit_sha=sha, loc=loc,
            result_line=result_line,
        )

    if recoverable:
        # Transient blocker (e.g. main has uncommitted edits): not an agent's job.
        emit(Event(TASK_RESULT, {
            "task": task, "status": "error", "error": detail,
            "result_line": "blocked — clear main, then resolve",
            "recoverable": True, "failure_kind": "merge_rejected",
        }))
        return TaskResult(
            task=task, title=title, success=False, error=detail,
            failure_kind="merge_rejected",
        )

    # 2. Content conflict (or generic merge failure): hand it to the agent.
    return _agent_resolve(
        root, task, title,
        conflict_detail=detail, emit=emit, config=config,
        backend_name=backend_name, abort_reason=abort_reason, queue=queue,
    )


def _agent_resolve(
    root: Path,
    task: str,
    title: str,
    *,
    conflict_detail: str,
    emit: Listener,
    config: dict,
    backend_name: str | None,
    abort_reason: object = None,
    queue: str | None = None,
) -> TaskResult:
    """Rebase the task branch onto ``main`` and drive an agent to resolve the
    conflicts / validation failures, then squash. Bounded by config
    ``max_resolve_attempts``."""
    from nightshift.backends import LAUNCH_FAILED, WorkerSpec, get_backend

    def _emit_log(line: str) -> None:
        emit(Event(TASK_LOG, {"task": task, "line": line}))

    def _on_worker_start(pid: int) -> None:
        emit(Event(WORKER_STARTED, {"task": task, "pid": pid}))

    def _should_abort() -> str | None:
        return abort_reason() if callable(abort_reason) else None

    worktree_dir = _ensure_worktree_for_branch(root, task, queue=queue)
    if worktree_dir is None:
        error = "could not prepare the task worktree for resolution"
        emit(Event(TASK_RESULT, {
            "task": task, "status": "error", "error": error,
            "result_line": "resolve setup failed", "failure_kind": "merge_conflict",
        }))
        return TaskResult(
            task=task, title=title, success=False, error=error,
            failure_kind="merge_conflict",
        )

    max_attempts = int(config.get("max_resolve_attempts", DEFAULT_MAX_RESOLVE_ATTEMPTS))
    validate_cmd = resolve_validate_cmd(config)
    env = worker_env()
    task_file = root / playlists.tasks_rel(queue) / f"{task}.md"
    meta: dict = {}
    if task_file.exists():
        meta, _ = split_frontmatter(task_file.read_text())
    resolved = resolve_frontmatter(meta, config)
    model = config.get("resolve_model") or resolved["model"]
    backend = get_backend(
        config.get("resolve_backend") or backend_name or config.get("worker_backend")
    )

    last_error = conflict_detail or "merge conflict"
    for attempt in range(1, max_attempts + 1):
        emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "resolve"}))
        _emit_log(f"  resolve attempt {attempt}/{max_attempts}: rebasing onto main...\n")
        outcome, rebase_detail = _rebase_onto_main(worktree_dir)
        if outcome == "error":
            _abort_rebase(worktree_dir)
            last_error = f"rebase onto main failed:\n{rebase_detail}"
            continue

        if outcome == "conflict":
            _emit_log("  conflicts detected — running resolver agent...\n")
            context = (
                f"{conflict_detail}\n\n"
                "A `git rebase main` is in progress in this worktree and has "
                f"paused on conflicts:\n{rebase_detail}"
            )
            spec = WorkerSpec(
                task=task,
                prompt=build_resolve_prompt(root, task, context=context),
                model=model,
                max_turns=resolved["max_turns"],
                cwd=worktree_dir,
                env=env,
                config=config,
            )
            worker = backend.run(
                spec, _emit_log, _should_abort, on_worker_start=_on_worker_start
            )
            if worker.aborted is not None:
                if _rebase_in_progress(worktree_dir):
                    _abort_rebase(worktree_dir)
                emit(Event(TASK_RESULT, {"task": task, "status": worker.aborted}))
                return TaskResult(
                    task=task, title=title, success=False, status=worker.aborted,
                )
            if worker.returncode == LAUNCH_FAILED:
                error = (
                    f"{worker.error}. Add the worker binary to PATH or set its "
                    "'*_bin' in config.json."
                )
                if _rebase_in_progress(worktree_dir):
                    _abort_rebase(worktree_dir)
                emit(Event(TASK_RESULT, {
                    "task": task, "status": "error", "error": error,
                    "result_line": "worker executable not found",
                    "failure_kind": "worker_launch",
                }))
                return TaskResult(
                    task=task, title=title, success=False, error=error,
                    failure_kind="worker_launch",
                )
            if _rebase_in_progress(worktree_dir):
                _abort_rebase(worktree_dir)
                last_error = "resolver did not finish the rebase (conflicts remain)"
                continue

        # Rebase complete (clean or resolved) — validate (unless the queue opted
        # out with an empty validate command), then squash.
        if validate_cmd is None:
            _emit_log("  validation disabled for this queue — skipping.\n")
        else:
            emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "validate"}))
            _emit_log(f"  running {' '.join(validate_cmd)}...\n")
            validate_result = subprocess.run(
                validate_cmd, cwd=worktree_dir, capture_output=True, text=True, env=env,
            )
            if validate_result.returncode != 0:
                _emit_log("  validate failed — attempting auto-repair...\n")
                validate_result = _attempt_repair(
                    worktree_dir, validate_result, validate_cmd=validate_cmd, env=env,
                )
            if validate_result.returncode != 0:
                last_error = (
                    "validate failed after resolution:\n"
                    f"{validate_result.stdout[-1500:]}\n{validate_result.stderr[-1500:]}"
                )
                continue

        emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "commit"}))
        sha, squash_detail, _recoverable = squash_to_main(
            root, task, title, queue=queue,
            autostash=bool(config.get("autostash_operator_work", True)),
        )
        if sha is not None:
            loc = compute_code_loc(root, sha)
            teardown_worktree(root, task, queue=queue)
            # Backstop queue removal for a landed regular task (see drop_completed_task).
            if not task_is_evergreen(meta, task, config):
                drop_completed_task(root, task, playlists.tasks_rel(queue), queue=queue)
            result_line = f"resolved: landed ({sha})"
            emit(Event(TASK_RESULT, {
                "task": task, "status": "completed",
                "result_line": result_line, "commit_sha": sha, "loc": loc,
            }))
            return TaskResult(
                task=task, title=title, success=True, commit_sha=sha, loc=loc,
                result_line=result_line,
            )
        last_error = squash_detail or "squash-merge still failed after resolution"

    error = f"auto-resolve failed after {max_attempts} attempt(s):\n{last_error}"
    _write_failure_log(root, worktree_dir, task, error)
    emit(Event(TASK_RESULT, {
        "task": task, "status": "error", "error": error,
        "result_line": "auto-resolve failed — manual resolution needed",
        "recoverable": False, "failure_kind": "merge_conflict",
    }))
    return TaskResult(
        task=task, title=title, success=False, error=error,
        failure_kind="merge_conflict",
    )


# --------------------------------------------------------------------------- #
# Failure logging / repair
# --------------------------------------------------------------------------- #

FAILURE_LOG_DIR = ".worktrees/failures"


def _write_failure_log(
    root: Path,
    worktree_dir: Path,
    task: str,
    error: str,
    *,
    validate_stdout: str = "",
    validate_stderr: str = "",
) -> Path:
    """Write a terse failure log so repeated failures are diagnosable."""
    log_dir = root / FAILURE_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task}.log"

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"{timestamp}  task={task}",
        f"error: {error.splitlines()[0][:200]}",
    ]

    if validate_stderr:
        last_lines = [l for l in validate_stderr.strip().splitlines() if l.strip()][-3:]
        lines.append("stderr: " + " | ".join(last_lines)[:300])
    elif validate_stdout:
        last_lines = [l for l in validate_stdout.strip().splitlines() if l.strip()][-3:]
        lines.append("stdout: " + " | ".join(last_lines)[:300])

    log_path.write_text("\n".join(lines) + "\n")
    print(f"  failure log: {log_path}")
    return log_path


def _attempt_repair(
    worktree_dir: Path,
    failed_result: subprocess.CompletedProcess[str],
    *,
    validate_cmd: list[str] | None = None,
    env: dict[str, str] | None = None,
    should_abort=None,
) -> subprocess.CompletedProcess[str]:
    """Run deterministic auto-fixes and retry validate once (interruptibly)."""
    subprocess.run(
        [".venv/bin/ruff", "check", "--fix", "--unsafe-fixes",
         "lib/python/long_*", "services/", "tools/long_cli"],
        cwd=worktree_dir,
        capture_output=True,
    )
    subprocess.run(
        [".venv/bin/ruff", "format",
         "lib/python/long_*", "services/", "tools/long_cli"],
        cwd=worktree_dir,
        capture_output=True,
    )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
    )
    if dirty.stdout.strip():
        subprocess.run(
            ["git", "add", "-A"],
            cwd=worktree_dir,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "autofix: ruff check --fix + format"],
            cwd=worktree_dir,
            capture_output=True,
        )
        print("  applied ruff auto-fixes, retrying validate...")
    else:
        print("  no auto-fixable issues; retrying validate...")

    return run_interruptible(
        validate_cmd or shlex.split(DEFAULT_VALIDATE_CMD),
        cwd=worktree_dir,
        env=env,
        should_abort=should_abort,
    )


_PYTEST_SUMMARY = re.compile(r"(\d+)\s+passed")


def extract_result_line(validate_stdout: str, validate_stderr: str = "") -> str:
    """Derive a one-line result (e.g. ``All 1291 tests pass``) from validate output."""
    text = validate_stdout or ""
    last_match = None
    for match in _PYTEST_SUMMARY.finditer(text):
        last_match = match
    if last_match is not None:
        return f"All {last_match.group(1)} tests pass"
    for line in reversed((text + "\n" + (validate_stderr or "")).splitlines()):
        if line.strip():
            return line.strip()[:120]
    return "validate passed"


# --------------------------------------------------------------------------- #
# Run control
# --------------------------------------------------------------------------- #


class Controller:
    """Thread-safe transport control for a play-through.

    The run loop consults this between tasks (pause/stop) and a running worker
    consults :meth:`abort_reason` to terminate early on skip/stop.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._skip = threading.Event()
        self._not_paused = threading.Event()
        self._not_paused.set()

    def pause(self) -> None:
        self._not_paused.clear()

    def resume(self) -> None:
        self._not_paused.set()

    @property
    def paused(self) -> bool:
        return not self._not_paused.is_set()

    def stop(self) -> None:
        self._stop.set()
        self._skip.set()
        self._not_paused.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def skip(self) -> None:
        self._skip.set()

    def begin_task(self) -> None:
        """Clear the per-task skip flag before starting a task."""
        self._skip.clear()

    def abort_reason(self) -> str | None:
        """Why the current worker should abort, if at all."""
        if self._stop.is_set():
            return "stopped"
        if self._skip.is_set():
            return "skipped"
        return None

    def wait_while_paused(self) -> None:
        while not self._not_paused.wait(timeout=0.2):
            if self._stop.is_set():
                return


def run_task(
    root: Path,
    task: str,
    *,
    emit: Listener = _noop,
    abort_reason: object = None,
    backend_name: str | None = None,
    tasks_rel: str = ".tasks",
) -> TaskResult:
    """Run a single task end-to-end. Always cleans up the worktree when done.

    ``abort_reason`` is an optional zero-arg callable returning ``None`` to
    continue or a status string (``"skipped"`` / ``"stopped"``) to terminate
    the worker early. ``backend_name`` selects the worker shim (claude / cursor
    / anthropic / ollama); when ``None`` it falls back to ``config`` then the
    default. ``tasks_rel`` selects the queue (``.tasks`` or a playlist sub-dir)
    the task file is read from and whose config (incl. the ``validate`` command)
    applies. The final ``task_result`` event carries a ``timings`` dict of
    per-phase seconds (``worker`` / ``validate`` / ``commit`` / ``total``).
    """
    tasks_dir = root / tasks_rel
    task_file = tasks_dir / f"{task}.md"
    config = resolve_config(root, tasks_rel)
    queue = playlists.queue_from_tasks_rel(tasks_rel)

    if not task_file.exists():
        result = TaskResult(task=task, title=task, success=False, error="task file not found")
        emit(Event(TASK_RESULT, {"task": task, "status": "error", "error": result.error}))
        return result

    text = task_file.read_text()
    meta, body = split_frontmatter(text)
    resolved = resolve_frontmatter(meta, config)
    title = resolve_title(task, meta)

    # Re-check the disabled flag at launch, not just when the queue was built.
    # The queue scan that produced this task list (build_task_list /
    # live_ordered_queue) filters disabled tasks, but a task can be disabled
    # through the UI after the list is built or between drain iterations, and a
    # single named task bypasses that scan entirely. Reading the flag here — the
    # one point every launch funnels through — guarantees a disabled task is
    # never handed to a worker. Skipped, not failed: a disabled task is a
    # deliberate operator choice, not an error.
    if is_disabled(meta):
        emit(Event(TASK_RESULT, {
            "task": task,
            "status": "skipped",
            "result_line": "skipped: task is disabled",
        }))
        return TaskResult(
            task=task, title=title, success=False, status="skipped",
            result_line="skipped: task is disabled",
        )

    frontmatter = {**meta}
    frontmatter.setdefault("model", resolved["model"])
    # Carry the brief prose into the run record so History can show the original
    # brief after the task file is removed (completed tasks leave the queue).
    emit(Event(TASK_STARTED, {
        "task": task, "title": title, "frontmatter": frontmatter, "body": body.strip(),
    }))
    emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "worker"}))

    def _should_abort() -> str | None:
        return abort_reason() if callable(abort_reason) else None

    t_task_start = time.monotonic()
    worktree_dir = setup_worktree(root, task, queue=queue)
    preserve_worktree = False

    # Imported here, not at module top, to avoid a backends<->engine import
    # cycle: backends reuses engine's claude argv/bin helpers.
    from nightshift.backends import LAUNCH_FAILED, WorkerSpec, get_backend

    try:
        prompt = build_prompt(root, task, tasks_rel)
        env = worker_env()
        validate_cmd = resolve_validate_cmd(config)
        backend = get_backend(backend_name or config.get("worker_backend"))
        spec = WorkerSpec(
            task=task,
            prompt=prompt,
            model=resolved["model"],
            max_turns=resolved["max_turns"],
            cwd=worktree_dir,
            env=env,
            config=config,
        )
        timings: dict[str, float] = {}

        def _with_total() -> dict[str, float]:
            timings["total"] = round(time.monotonic() - t_task_start, 1)
            return dict(timings)

        def _emit_log(line: str) -> None:
            emit(Event(TASK_LOG, {"task": task, "line": line}))

        def _on_worker_start(pid: int) -> None:
            # Record the live worker pid so stale-run reconciliation can tell a
            # busy (even orphaned) worker from an abandoned run.
            emit(Event(WORKER_STARTED, {"task": task, "pid": pid}))

        emit(Event(TASK_LOG, {
            "task": task,
            "line": f"  running worker [{backend.name}] ({resolved['model']})...\n",
        }))
        t_worker = time.monotonic()
        worker = backend.run(spec, _emit_log, _should_abort, on_worker_start=_on_worker_start)
        timings["worker"] = round(time.monotonic() - t_worker, 1)

        if worker.aborted is not None:
            emit(Event(TASK_RESULT, {
                "task": task, "status": worker.aborted, "timings": _with_total(),
            }))
            return TaskResult(task=task, title=title, success=False, status=worker.aborted)

        if worker.returncode == LAUNCH_FAILED:
            error = (
                f"{worker.error}. Add the worker binary to PATH or set its "
                "'*_bin' in config.json."
            )
            _write_failure_log(root, worktree_dir, task, error)
            emit(Event(TASK_RESULT, {
                "task": task, "status": "error", "error": error,
                "result_line": "worker executable not found",
                "failure_kind": "worker_launch", "timings": _with_total(),
            }))
            return TaskResult(
                task=task, title=title, success=False, error=error,
                result_line="worker executable not found",
                failure_kind="worker_launch",
            )

        if worker.returncode != 0:
            error = worker.error or f"worker [{backend.name}] exited with code {worker.returncode}"
            _write_failure_log(root, worktree_dir, task, error)
            line = error.splitlines()[0][:120]
            emit(Event(TASK_RESULT, {
                "task": task, "status": "error", "error": error,
                "result_line": line, "failure_kind": "worker_error",
                "timings": _with_total(),
            }))
            return TaskResult(
                task=task, title=title, success=False, error=error,
                result_line=line, failure_kind="worker_error",
            )

        # No commits → nothing to validate or squash. Finish cleanly instead of
        # tripping the squash step (e.g. non-agentic completion backends).
        if not _worktree_has_commits(root, task, queue=queue):
            result_line = "no changes produced (worker emitted output only)"
            # A no-changes completion never removed the task file (no branch to
            # land), so drop it here for regular tasks — a completed task must
            # leave the queue. Evergreen tasks keep their file and re-run.
            if not task_is_evergreen(meta, task, config):
                drop_completed_task(root, task, tasks_rel, queue=queue)
            emit(Event(TASK_RESULT, {
                "task": task, "status": "completed",
                "result_line": result_line, "timings": _with_total(),
            }))
            return TaskResult(task=task, title=title, success=True, result_line=result_line)

        # Honour a stop/skip requested while the worker was running, before we
        # sink time into validate.
        if _should_abort() is not None:
            reason = _should_abort()
            emit(Event(TASK_RESULT, {"task": task, "status": reason, "timings": _with_total()}))
            return TaskResult(task=task, title=title, success=False, status=reason)

        # A queue may opt out of validation by setting an empty validate command;
        # the worker's work then lands without a validate gate.
        if validate_cmd is None:
            emit(Event(TASK_LOG, {
                "task": task, "line": "  validation disabled for this queue — skipping.\n",
            }))
            result_line = "validation skipped (no validate command)"
        else:
            emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "validate"}))
            emit(Event(TASK_LOG, {"task": task, "line": f"  running {' '.join(validate_cmd)}...\n"}))
            t_validate = time.monotonic()
            validate_result = run_interruptible(
                validate_cmd, cwd=worktree_dir, env=env, should_abort=_should_abort,
            )

            if validate_result.returncode != 0 and _should_abort() is None:
                emit(Event(TASK_LOG, {"task": task, "line": "  validate failed — attempting auto-repair...\n"}))
                validate_result = _attempt_repair(
                    worktree_dir, validate_result,
                    validate_cmd=validate_cmd, env=env, should_abort=_should_abort,
                )
            timings["validate"] = round(time.monotonic() - t_validate, 1)

            # A stop during validate (the process was killed) ends the task now —
            # nothing is committed to main.
            if _should_abort() is not None:
                reason = _should_abort()
                emit(Event(TASK_RESULT, {"task": task, "status": reason, "timings": _with_total()}))
                return TaskResult(task=task, title=title, success=False, status=reason)

            if validate_result.returncode != 0:
                error = f"just validate failed:\n{validate_result.stdout[-2000:]}\n{validate_result.stderr[-2000:]}"
                _write_failure_log(
                    root, worktree_dir, task, error,
                    validate_stdout=validate_result.stdout,
                    validate_stderr=validate_result.stderr,
                )
                result_line = extract_result_line(validate_result.stdout, validate_result.stderr)
                emit(Event(TASK_RESULT, {
                    "task": task, "status": "error", "error": error,
                    "result_line": result_line, "failure_kind": "validation_error",
                    "timings": _with_total(),
                }))
                return TaskResult(
                    task=task, title=title, success=False, error=error,
                    result_line=result_line, failure_kind="validation_error",
                )

            result_line = extract_result_line(validate_result.stdout, validate_result.stderr)

        # Last chance to bail before mutating main — a stop here leaves the
        # validated work on the task branch (recoverable) rather than landing it.
        if _should_abort() is not None:
            reason = _should_abort()
            preserve_worktree = True
            emit(Event(TASK_RESULT, {"task": task, "status": reason, "timings": _with_total()}))
            return TaskResult(task=task, title=title, success=False, status=reason)

        emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "commit"}))
        t_commit = time.monotonic()
        sha, squash_error, recoverable = squash_to_main(
            root, task, title, queue=queue,
            autostash=bool(config.get("autostash_operator_work", True)),
        )
        timings["commit"] = round(time.monotonic() - t_commit, 1)
        # A successful land that couldn't reapply set-aside operator work surfaces
        # squash_error as a warning (the commit still landed; stash is preserved).
        if sha is not None and squash_error:
            emit(Event(TASK_LOG, {"task": task, "line": f"  warning: {squash_error}\n"}))
        if sha is None:
            error = squash_error or "squash-merge to main failed"
            _write_failure_log(root, worktree_dir, task, error)
            # Keep the worktree branch either way so the validated work is never
            # lost: a transient blocker can be re-squashed once cleared, and a
            # content conflict can be resolved by hand against the branch.
            preserve_worktree = True
            failure_kind = _squash_failure_kind(recoverable, error)

            # Gated auto-resolve: on a content conflict, hand straight to the
            # resolver agent instead of parking for a human. Off by default;
            # enabled per-repo (config ``auto_resolve``) or per-task
            # (frontmatter ``autoresolve``). A transient blocker (dirty main) is
            # never auto-resolved — an agent can't touch the operator's tree.
            auto_resolve = bool(meta.get("autoresolve", config.get("auto_resolve", False)))
            if auto_resolve and failure_kind == "merge_conflict":
                emit(Event(TASK_LOG, {
                    "task": task,
                    "line": "  auto-resolve enabled — launching resolver agent...\n",
                }))
                result = _agent_resolve(
                    root, task, title,
                    conflict_detail=error, emit=emit, config=config,
                    backend_name=backend_name, abort_reason=abort_reason, queue=queue,
                )
                preserve_worktree = not result.success
                return result

            result_line = (
                "squash-merge failed — recoverable"
                if recoverable
                else "squash-merge conflict — manual resolution needed"
            )
            emit(Event(TASK_RESULT, {
                "task": task, "status": "error", "error": error,
                "result_line": result_line,
                "recoverable": recoverable, "failure_kind": failure_kind,
                "timings": _with_total(),
            }))
            return TaskResult(
                task=task, title=title, success=False, error=error,
                failure_kind=failure_kind,
            )

        # Code lines churned by the squash commit this task landed on ``main``
        # (added + removed), excluding build files, docs, comments, and
        # queue/output dirs — summed on the Stats page. The landed commit is the
        # one metric the Stats backfill can also reconstruct from a record's
        # ``commit_sha`` after the task branch is torn down, so live capture and
        # backfill report the *same* figure for every task (a branch-history sum
        # would diverge from any later backfill and make the total inconsistent).
        loc = compute_code_loc(root, sha)
        # Backstop the worker's queue removal: a regular task that lands must
        # leave the queue. If the worker's branch didn't ``git rm`` its task
        # file, the squash kept it on ``main`` and the UI would keep listing a
        # completed task — drop it here. Evergreen tasks keep their file.
        if not task_is_evergreen(meta, task, config):
            drop_completed_task(root, task, tasks_rel, queue=queue)
        emit(Event(TASK_RESULT, {
            "task": task,
            "status": "completed",
            "result_line": result_line,
            "commit_sha": sha,
            "loc": loc,
            "timings": _with_total(),
        }))
        return TaskResult(
            task=task, title=title, success=True, commit_sha=sha, loc=loc,
            result_line=result_line,
        )

    finally:
        if not preserve_worktree:
            teardown_worktree(root, task, queue=queue)


def run_queue(
    root: Path,
    tasks: list[str],
    *,
    listeners: list[Listener] | None = None,
    controller: Controller | None = None,
    run_id: str | None = None,
    backend_name: str | None = None,
    tasks_rel: str = ".tasks",
    follow_queue: bool = False,
    task_slot: Callable[[], AbstractContextManager[object]] | None = None,
    admit_task: Callable[[], str | None] | None = None,
) -> RunSummary:
    """Run tasks from a queue, emitting events to ``listeners``.

    If a :class:`Controller` is supplied the loop honours pause/stop/skip; with
    no controller it runs straight through (the CLI path). ``backend_name``
    selects the worker shim for every task in the run. ``tasks_rel`` selects the
    queue (main ``.tasks`` or a playlist sub-dir) the tasks belong to.

    ``task_slot`` (server concurrency governor) is an optional context-manager
    factory held for the duration of *each* ``run_task`` (worker→validate→land),
    so a shared gate can cap simultaneous workers across queues. ``admit_task``
    (server disk admission) is an optional check run before each task; when it
    returns a message the task is failed (``failure_kind="disk"``) without
    cutting a worktree and the run stops rather than thrashing. Both default to
    ``None`` (the CLI path is unchanged — sequential, ungoverned).

    With ``follow_queue`` (queue/"all"/repeat runs) the loop drains the *live*
    queue: tasks added to the queue dir mid-run are folded in (in configured
    order) and executed in this same run, rather than waiting for the next cycle.
    An ``attempted`` set bounds each task to one attempt per run (so evergreen and
    failed tasks don't loop; completed regular tasks remove their own file and
    drop out of the scan). With ``follow_queue`` off the loop drains exactly the
    passed ``tasks`` (oneshot semantics, unchanged).

    The queue is snapshotted (:func:`commit_queue_state`) before each task cuts
    its worktree, so a task added/edited mid-run is committed to ``HEAD`` and is
    present in the worker's checkout instead of failing as a missing file.
    """
    emit: Listener = _noop
    if listeners:
        active = listeners

        def emit(event: Event) -> None:
            for listener in active:
                listener(event)

    emit(Event(RUN_STARTED, {"run_id": run_id, "tasks": list(tasks)}))
    summary = RunSummary()
    attempted: set[str] = set()
    # Seed with the passed list (carries autosplit-spawned subtasks + any
    # start_task slice); live additions are folded in when following the queue.
    order: list[str] = list(tasks)
    try:
        while True:
            if controller is not None and controller.stopped:
                break
            if follow_queue:
                # Re-read the live queue every iteration so mid-run edits — a
                # changed priority, a flipped sort mode, a dragged row, or a
                # freshly-added task — take effect at the next task boundary.
                # New stems are folded in; the not-yet-attempted tail is then
                # re-sorted by this fresh ordering so "Up Next" always reflects
                # the current on-disk state, never a list captured at play time.
                # The running task is already in ``attempted`` (added below
                # before its run_task call), so it's never reshuffled.
                live = live_ordered_queue(root, tasks_rel)
                for stem in live:
                    if stem not in order and stem not in attempted:
                        order.append(stem)
                live_rank = {stem: i for i, stem in enumerate(live)}
                pending = sorted(
                    (t for t in order if t not in attempted),
                    key=lambda s: live_rank.get(s, len(live_rank)),
                )
            else:
                pending = [t for t in order if t not in attempted]
            if not pending:
                break
            task = pending[0]
            attempted.add(task)

            if controller is not None:
                if controller.stopped:
                    break
                controller.wait_while_paused()
                if controller.stopped:
                    break
                controller.begin_task()

            # Disk admission: refuse to start a task when the tree is too full —
            # fail it cleanly (no worktree cut) and stop rather than thrash.
            if admit_task is not None:
                denial = admit_task()
                if denial is not None:
                    emit(Event(TASK_STARTED, {"task": task, "title": task}))
                    emit(Event(TASK_RESULT, {
                        "task": task, "status": "error", "error": denial,
                        "result_line": "insufficient disk — run paused",
                        "failure_kind": "disk",
                    }))
                    break

            # Per-task snapshot: commit any newly-added/edited queue files to HEAD
            # before run_task cuts the worktree from it. Under the landing lock
            # (queue-scoped) so a concurrent queue's snapshot/land can't race on
            # the shared index/HEAD.
            with landing_lock(root):
                commit_queue_state(root, tasks_rel)

            def _run() -> TaskResult:
                return run_task(
                    root,
                    task,
                    emit=emit,
                    abort_reason=(
                        controller.abort_reason if controller is not None else None
                    ),
                    backend_name=backend_name,
                    tasks_rel=tasks_rel,
                )

            # Hold a concurrency slot for the whole task when a gate is supplied,
            # so simultaneous workers across queues stay capped.
            if task_slot is not None:
                with task_slot():
                    result = _run()
            else:
                result = _run()
            summary.results.append(result)
            if controller is not None and controller.stopped:
                break
    finally:
        emit(Event(RUN_FINISHED, {"run_id": run_id}))
    return summary
