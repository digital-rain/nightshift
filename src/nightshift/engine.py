"""Shared nightshift orchestration core.

This module owns the logic for turning a queue into work across a *workspace* of
many git repos. It threads **two roots**: ``tasks_root`` (the ``nightshift-tasks``
content store, ``<workspace>/<tasks_repo>``) holds briefs + queue config, while
each task's git ops run in ``repo_root`` (``<workspace>/<repo>``, resolved per
task). It builds the task list, runs a worker in an isolated git worktree (placed
outside the target repo under ``<workspace>/.worktrees/<repo>/``), validates, and
squash-commits to the target repo's local ``main``. It is consumed by two
front-ends:

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
import hashlib
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
from typing import Any

from nightshift import playlists, repos
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
from nightshift.model_id import split_model
from nightshift.spawn_daily import (
    DEFAULT_PRIORITY,
    MAX_PRIORITY,
    MIN_PRIORITY,
    find_autosplit_sources,
    is_completed,
    is_disabled,
    is_quarantined,
    load_config,
    load_queue_config,
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


def select_run_backend(model: str, fallback_backend: str | None) -> tuple[Any, str]:
    """Pick the backend for a (possibly qualified) model in the legacy run path.

    A ``provider/model`` id dispatches to that provider's backend and the bare
    model is what reaches the CLI. Agnostic keywords (``auto``/``max``) and bare
    or unrecognized ids fall back to ``fallback_backend`` (the default backend
    when ``None``) with the id passed through unchanged.

    Imported lazily to avoid the backends<->engine import cycle (backends reuses
    engine's claude argv/bin helpers).
    """
    from nightshift.backends import get_backend, require_backend

    provider, bare = split_model(model)
    if provider is not None:
        try:
            return require_backend(provider), bare
        except KeyError:
            pass
    return get_backend(fallback_backend), model


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


# Environment preflight: keep the worker's shared ``.venv`` in step with the
# committed lockfile *before* spending model budget. A cross-machine worker can
# be handed a task whose target-repo commit references a dependency that landed
# on another box's venv (e.g. a transitive ``cvxpy`` behind an optional extra)
# but was never installed here. Left unchecked, the miss only surfaces after the
# agent has run, at ``validate`` time, as an import error — the whole point is to
# catch it up front and self-heal cheaply. ``uv sync --frozen`` is lockfile-exact
# (never re-resolves, so it can't silently bump a pinned dep).
DEFAULT_PREFLIGHT_CMD = "uv sync --frozen"

# Marker file (next to the shared venv) recording the lockfile fingerprint the
# venv was last successfully synced against, so the common case is a one-line
# hash compare and ``uv sync`` runs only on a real change/miss.
LOCK_MARKER_NAME = ".nightshift-lock-hash"
# Lockfiles a preflight watches, in priority order (first present wins).
_LOCK_FILENAMES = ("uv.lock", "requirements.lock")


def resolve_preflight_cmd(config: dict) -> list[str] | None:
    """The environment-preflight command for a queue, or ``None`` when disabled.

    Mirrors :func:`resolve_validate_cmd`: an *absent* ``preflight`` key inherits
    the engine default (``uv sync --frozen``); a key set to an empty/whitespace
    string opts out. The command only runs when the lockfile fingerprint has
    changed (see :func:`ensure_env_synced`), so the default is safe to leave on.
    """
    if "preflight" not in config:
        return shlex.split(DEFAULT_PREFLIGHT_CMD)
    raw = str(config.get("preflight") or "").strip()
    if not raw:
        return None
    return shlex.split(raw)


def preflight_cmd_from_blob(config: dict) -> tuple[list[str] | None, str | None]:
    """Resolve preflight argv + display string from a work-order ``config`` blob.

    Returns ``(argv, display)`` where ``display`` is the command string that
    would run, or ``(None, None)`` when the preflight is disabled.
    """
    if "preflight_cmd" in config:
        raw = str(config.get("preflight_cmd") or "").strip()
        if not raw:
            return None, None
        return shlex.split(raw), raw
    argv = resolve_preflight_cmd(config)
    if argv is None:
        return None, None
    return argv, shlex.join(argv)


def _lockfile_for(repo_root: Path) -> Path | None:
    """The lockfile a preflight tracks in ``repo_root`` (first present), or None
    when the repo has no recognized lockfile (a non-uv repo — preflight no-ops)."""
    for name in _LOCK_FILENAMES:
        candidate = repo_root / name
        if candidate.is_file():
            return candidate
    return None


def lock_fingerprint(repo_root: Path) -> str | None:
    """SHA-256 of the repo's lockfile contents, or ``None`` when absent.

    Keyed on file *content* (not a git range) so it is correct regardless of how
    the checkout got here — fresh clone, fast-forward, or a hand-rebuilt venv —
    which a "did the last fetch change uv.lock?" diff cannot guarantee.
    """
    lockfile = _lockfile_for(repo_root)
    if lockfile is None:
        return None
    return hashlib.sha256(lockfile.read_bytes()).hexdigest()


def _venv_dir(repo_root: Path) -> Path:
    return repo_root / ".venv"


def _lock_marker_path(repo_root: Path) -> Path:
    return _venv_dir(repo_root) / LOCK_MARKER_NAME


def _read_lock_marker(repo_root: Path) -> str | None:
    marker = _lock_marker_path(repo_root)
    try:
        return marker.read_text().strip() or None
    except OSError:
        return None


def _write_lock_marker(repo_root: Path, fingerprint: str) -> None:
    """Record the synced-against fingerprint next to the venv (best-effort)."""
    marker = _lock_marker_path(repo_root)
    with contextlib.suppress(OSError):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(fingerprint + "\n")


def invalidate_lock_marker(repo_root: Path) -> None:
    """Drop the lock marker so the next :func:`ensure_env_synced` re-syncs.

    Called when a fast-forward pulls in a new lockfile (an eager signal on
    whichever clone observed the change); the marker gate remains the
    authoritative check for clones that never fast-forward (e.g. a worker box).
    """
    with contextlib.suppress(OSError):
        _lock_marker_path(repo_root).unlink()


def lock_changed_between(repo_root: Path, old: str, new: str) -> bool:
    """True if any tracked lockfile differs between two commit-ishes.

    Used as an eager fast-forward signal: when local ``main`` advances and a
    lockfile changed in that range, the venv is (probably) stale and the marker
    is invalidated so the next preflight re-syncs. Best-effort — a git error
    returns False and the marker fingerprint remains the authoritative gate.
    """
    res = subprocess.run(
        ["git", "diff", "--name-only", old, new, "--", *_LOCK_FILENAMES],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return res.returncode == 0 and bool(res.stdout.strip())


@dataclass
class PreflightResult:
    """Outcome of :func:`ensure_env_synced`.

    ``ok`` is False only when a needed sync actually failed. ``synced`` is True
    when the sync command ran (lock changed/missing or marker absent); False
    means the fast path (fingerprint already recorded) or a no-op (no lockfile /
    preflight disabled). ``detail`` carries the sync output tail on failure.
    """

    ok: bool
    synced: bool
    detail: str = ""
    display: str = ""


def ensure_env_synced(
    repo_root: Path,
    *,
    preflight_argv: list[str] | None,
    preflight_display: str | None = None,
    env: dict[str, str] | None = None,
    should_abort: Callable[[], Any] | None = None,
) -> PreflightResult:
    """Make the shared venv match the committed lockfile before a task runs.

    Fast path (the overwhelming common case): the lockfile fingerprint already
    matches the marker → return immediately, nothing runs. On a change/miss (a
    new dependency landed, a fresh box, or an invalidated marker) run
    ``preflight_argv`` (default ``uv sync --frozen``) in ``repo_root`` and, on
    success, record the new fingerprint so the next task takes the fast path.

    No-ops (``ok=True, synced=False``) when the preflight is disabled or the repo
    has no recognized lockfile, so non-uv repos are unaffected.
    """
    if preflight_argv is None:
        return PreflightResult(ok=True, synced=False)

    fingerprint = lock_fingerprint(repo_root)
    if fingerprint is None:
        # No lockfile to track — nothing to sync against.
        return PreflightResult(ok=True, synced=False)

    if _read_lock_marker(repo_root) == fingerprint:
        return PreflightResult(ok=True, synced=False)

    display = preflight_display or shlex.join(preflight_argv)
    proc = run_interruptible(
        preflight_argv,
        cwd=repo_root,
        env=env,
        should_abort=should_abort or (lambda: None),
    )
    if proc.returncode != 0:
        tail = (proc.stdout[-1500:] + "\n" + proc.stderr[-500:]).strip()
        return PreflightResult(ok=False, synced=True, detail=tail, display=display)

    # Re-read the fingerprint post-sync: uv may rewrite uv.lock if it was stale,
    # and we want the marker to reflect the state we actually synced to.
    _write_lock_marker(repo_root, lock_fingerprint(repo_root) or fingerprint)
    return PreflightResult(ok=True, synced=True, display=display)


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


def enough_free_disk(workspace: Path, min_free_pct: float = MIN_FREE_PCT) -> bool:
    """Return True when the filesystem holding ``workspace`` has >= min_free_pct free."""
    usage = shutil.disk_usage(workspace)
    return (usage.free / usage.total) * 100.0 >= min_free_pct


def check_preconditions(workspace: Path, repo: str) -> None:
    """Fail fast if prerequisites are missing.

    Disk headroom is checked on the ``workspace`` (which parents every worktree);
    tracked-code WIP and the pre-flight ``just validate`` are checked in the
    target ``repo_root = workspace / repo``.
    """
    repo_root = workspace / repo
    if not enough_free_disk(workspace):
        usage = shutil.disk_usage(workspace)
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
    # Untracked files never block a run. Only genuine tracked *code* WIP in the
    # target repo matters, and only when autostash is off (otherwise the land
    # step sets it aside). Briefs/queue config live in the separate content
    # store, never in the target repo, so they can't be a blocker here.
    blockers = _landing_blockers(repo_root)
    if blockers:
        shown = "\n".join(f"  {line}" for line in blockers[:10])
        try:
            host_config = load_config(workspace)
        except (FileNotFoundError, ValueError):
            host_config = {}
        autostash = bool(host_config.get("autostash_operator_work", True))
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
        cwd=repo_root,
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


def acquire_lock(workspace: Path) -> int:
    """Acquire an exclusive lockfile; exit if another instance is running.

    The lock is workspace-level (``<workspace>/.worktrees/.nightshift-local.lock``)
    so a single local runner owns the whole workspace at a time.
    """
    lock_path = workspace / ".worktrees" / ".nightshift-local.lock"
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


def _apply_play_filter(
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
                    _commit_dispatch(tasks_root, tasks_rel)
                    return [t.name for t in result.spawned]
                return []
        return [task_arg]

    results = []
    if is_main:
        results = spawn_all(tasks_root, write=True, tasks_rel=tasks_rel)
        if results:
            _commit_dispatch(tasks_root, tasks_rel)

    queue_names = live_ordered_queue(tasks_root, tasks_rel)
    spawned_names = [t.name for r in results for t in r.spawned]
    ordered = order_stems(tasks_root, list(set(queue_names) | set(spawned_names)), tasks_rel)
    # Re-apply the play-priority filter so freshly-spawned autosplit subtasks
    # (folded in via the union above) also respect the active filter.
    return _apply_play_filter(tasks_root, ordered, tasks_rel)


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
    autosplit = _find_autosplit_tasks(tasks_dir)
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
    return _apply_play_filter(tasks_root, ordered, tasks_rel, priorities=priorities)


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


def commit_tasks(
    tasks_root: Path, message: str, *, pathspecs: tuple[str, ...] = (".",)
) -> str | None:
    """Stage ``pathspecs`` in the content store (``tasks_root``) and commit them.

    The generic content-store commit helper for the ``nightshift-tasks`` git
    lifecycle (briefs created/removed, queue config edited). Local commit only —
    no remote required, no push. Returns the new commit's short sha, or ``None``
    when ``tasks_root`` is not a git repo *or* nothing was staged (a no-op).

    ``git add`` exits 1 when a pathspec matches only ``.gitignore``-ignored files
    (a queue's runtime ``runs/``/``logs/`` are gitignored in the store); that case
    is tolerated — the wanted files are still staged — while any other failure is
    treated as "nothing to commit" so a lifecycle event can never crash a run.
    """
    if not (tasks_root / ".git").exists():
        return None
    add = subprocess.run(
        ["git", "-c", "advice.addIgnoredFile=false", "add", "--", *pathspecs],
        cwd=tasks_root,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0 and (
        add.returncode != 1
        or "ignored by one of your .gitignore" not in add.stderr
    ):
        return None
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--", *pathspecs],
        cwd=tasks_root,
        capture_output=True,
        text=True,
    )
    if not staged.stdout.strip():
        return None
    commit = subprocess.run(
        ["git", "commit", "-m", message, "--", *pathspecs],
        cwd=tasks_root,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        return None
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=tasks_root,
        capture_output=True,
        text=True,
    ).stdout.strip() or None


def _commit_dispatch(tasks_root: Path, tasks_rel: str = "main") -> None:
    """Commit autosplit dispatch (spawned files + evergreen reset) in the content
    store, scoped to the dispatched queue dir."""
    commit_tasks(
        tasks_root,
        "nightshift-local: dispatch daily queues",
        pathspecs=(tasks_rel,),
    )


def commit_queue_state(tasks_root: Path, tasks_rel: str = "main") -> str | None:
    """Commit a queue's brief/config churn in the content store (``tasks_root``).

    The pre-run *target-repo* snapshot is gone: briefs are read live from
    ``tasks_root`` and delivered to the worker via a run-scratch file, so a run
    never needs to snapshot anything into the repo it lands in. This helper is
    retained for the create/edit lifecycle — it commits the queue dir
    (``<tasks_root>/<tasks_rel>``) churn locally, returning the new short sha or
    ``None`` when the store was already clean / not a git repo.
    """
    return commit_tasks(
        tasks_root,
        "nightshift: commit queue state",
        pathspecs=(tasks_rel,),
    )


TASK_TEMPLATE = asset("templates", "task.md")


def create_task(tasks_root: Path, title: str, text: str, tasks_rel: str = "main") -> dict:
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

    tasks_dir = tasks_root / tasks_rel
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

    Returns ``{task, title, body, frontmatter, evergreen, disabled}`` where
    ``frontmatter`` is the parsed YAML block merged with resolved defaults
    (model/draft/automerge) so the brief shows the effective values, and
    ``body`` is the spec prose with the frontmatter fence stripped. Read-only:
    it neither spawns subtasks nor mutates the queue.

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
        "frontmatter": frontmatter,
        # The raw, file-only frontmatter (before defaults are layered in) so the
        # editor can tell whether a field is explicitly set vs inherited — e.g.
        # "model" absent here means the task uses the config default.
        "frontmatter_raw": dict(meta),
        "evergreen": evergreen,
        "disabled": is_disabled(meta),
        "quarantined": is_quarantined(meta),
        "completed": is_completed(meta),
    }


# Frontmatter keys the detail-view editor is allowed to set. ``model: None``
# clears the key so the task inherits the config default. ``title`` is a
# frontmatter key too, but is written via the dedicated ``title`` change so it
# always lands ahead of the other keys (it's the file's headline). ``repo`` is
# the per-task target-repo override (a bare workspace-child name); clearing it
# (``repo: None``) falls the task back to the queue's default ``repo``.
_EDITABLE_META_KEYS = {
    "disabled", "quarantined", "completed", "evergreen", "automerge", "draft",
    "model", "priority", "repo", "loop", "loop_max_iterations",
}

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

    tasks_dir = (tasks_root / tasks_rel).resolve()
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
            "completed": is_completed(meta),
            "priority": priorities[p.stem],
        }
    ordered = order_stems(tasks_root, list(by_stem), tasks_rel, priorities=priorities)
    return [by_stem[s] for s in ordered]


# --------------------------------------------------------------------------- #
# Worker prompt / argv
# --------------------------------------------------------------------------- #


def build_prompt(
    task: str,
    *,
    task_file: str,
    validate_cmd: str,
    loop: bool = False,
    loop_max_iterations: int = 0,
) -> str:
    """Build the worker prompt matching CI injection format.

    ``task_file`` is the path to the **already-materialised** run-scratch brief
    (see :func:`materialize_brief`) — a read-only file *outside* the target
    worktree, so the brief never enters the repo the agent lands in.
    ``validate_cmd`` is the queue's resolved validate command, injected as
    ``$VALIDATE`` so the worker runs the queue's own gate — matching the command
    the engine later enforces. The caller (engine or worker) resolves both from
    the queue config before calling, so this helper needs neither root.

    When ``loop`` is True, use the ralph-loop prompt instead of the standard
    nightshift-local prompt. ``loop_max_iterations`` (0 = unlimited) is
    injected as ``$MAX_ITERATIONS``.
    """
    if loop:
        prompt_file = asset("prompts", "nightshift-ralph-loop.md")
        prompt_body = prompt_file.read_text()
        return (
            f"Your task file is: {task_file}\n"
            f"The TASK variable is: {task}\n"
            f"The TASK_FILE variable is: {task_file}\n"
            f"The VALIDATE command is: {validate_cmd}\n"
            f"The MAX_ITERATIONS variable is: {loop_max_iterations}\n\n"
            f"{prompt_body}"
        )
    prompt_file = asset("prompts", "nightshift-local.md")
    prompt_body = prompt_file.read_text()
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


def worker_env(worktree: Path | str | None = None) -> dict[str, str]:
    """A child-process environment with the common bin dirs on PATH, so the
    worker (and the tools it shells out to) resolve even when the server was
    launched from a non-login shell.

    ``worktree`` (a task worktree dir): when given, ``<worktree>/src`` is
    prepended to ``PYTHONPATH`` so ``import nightshift`` resolves to the
    worktree's own source. Worktrees symlink the target repo's ``.venv``
    (:data:`SYMLINK_TARGETS`), whose editable install points at the *main*
    checkout's ``src`` — without this override, ``just validate`` (and the
    agent's own ``python`` runs) would exercise main's code instead of the
    branch under test, failing any task that adds code + a test for it.
    """
    env = os.environ.copy()
    parts = env.get("PATH", "").split(os.pathsep)
    for d in _EXTRA_BIN_DIRS:
        if d not in parts:
            parts.append(d)
    env["PATH"] = os.pathsep.join(p for p in parts if p)
    if worktree is not None:
        wt_src = str(Path(worktree) / "src")
        if Path(wt_src).is_dir():
            existing = [
                p
                for p in env.get("PYTHONPATH", "").split(os.pathsep)
                if p and p != wt_src
            ]
            env["PYTHONPATH"] = os.pathsep.join([wt_src, *existing])
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
    """Path/branch-safe token for a queue: ``main`` for the default queue,
    otherwise the queue name (already a slug)."""
    return queue or "main"


def worktree_branch(task: str, queue: str | None = None) -> str:
    """Branch name for a task's local worktree, namespaced by queue so two queues
    holding a same-named task cut distinct branches."""
    return f"task-local/{_queue_slug(queue)}/{task}"


def worktree_dir(workspace: Path, repo: str, task: str, queue: str | None = None) -> Path:
    """Worktree directory for a task, placed **outside** the target repo under a
    workspace-level ``<workspace>/.worktrees/<repo>/`` so the target repo stays
    pristine; namespaced by queue (see :func:`worktree_branch`)."""
    return (
        workspace / ".worktrees" / repo / f"task-local-{_queue_slug(queue)}-{task}"
    )


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
        / f"task-local-{_queue_slug(queue)}-{task}.taskfile.md"
    )
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(body if body.endswith("\n") else f"{body}\n")
    return scratch


# Serialize every mutation of a target repo's index/HEAD/stash so concurrent
# queue runners (and a stray CLI process) can never interleave on the shared
# working tree. Two layers: a process-local lock across registry runner threads,
# and a cross-process file lock (per workspace+repo) so a server land and a CLI
# land can't collide.
_LANDING_LOCK = threading.Lock()
_INTEGRATE_LOCK = threading.Lock()


@contextlib.contextmanager
def landing_lock(workspace: Path, repo: str):
    """Hold the in-process + cross-process landing lock for a short critical
    section (a squash-merge + commit) on a target repo. The cross-process lock
    file lives at ``<workspace>/.worktrees/<repo>/.nightshift-landing.lock``. Not
    reentrant — never nest ``landing_lock`` calls on one thread."""
    with _LANDING_LOCK:
        path = workspace / ".worktrees" / repo / ".nightshift-landing.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


@contextlib.contextmanager
def integrate_lock(workspace: Path, repo: str):
    """Serialize a whole *integrate-and-push* section (sync origin -> preview ->
    squash -> push, with retries) across every land path and the out-of-process
    resolve runner, so concurrent merges to a target repo are strictly
    serialized while the long-running work that *precedes* the merge stays
    unlocked.

    This is a DIFFERENT lock from :func:`landing_lock`: the inner git primitives
    (``sync_main_to_origin``, ``squash_to_main``, the push) each still take
    ``landing_lock`` for their own critical section, so this outer lock must
    never be the landing lock (that would self-deadlock on the non-reentrant
    flock). The cross-process lock file lives at
    ``<workspace>/.worktrees/<repo>/.nightshift-integrate.lock``. Not reentrant.
    """
    with _INTEGRATE_LOCK:
        path = workspace / ".worktrees" / repo / ".nightshift-integrate.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def setup_worktree(
    workspace: Path, repo: str, task: str, *, queue: str | None = None, base: str = "HEAD"
) -> Path:
    """Create a git worktree (checked out from the target ``repo_root`` but
    placed outside it under ``<workspace>/.worktrees/<repo>/``) and symlink build
    artifacts from the target repo into it.

    ``base`` is the commit-ish the worktree branch is cut from (default the target
    repo's ``HEAD``). A cross-machine worker passes the work order's ``base_ref``
    so its branch is anchored to the same commit the manager will squash onto;
    the caller must have made ``base`` reachable in ``repo_root`` first (e.g. a
    fetch of the rendezvous remote)."""
    repo_root = workspace / repo
    wt_dir = worktree_dir(workspace, repo, task, queue)
    branch = worktree_branch(task, queue)

    if wt_dir.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_dir)],
            cwd=repo_root,
            capture_output=True,
        )
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=repo_root,
        capture_output=True,
    )

    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", str(wt_dir), "-b", branch, base],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

    for target in SYMLINK_TARGETS:
        src = repo_root / target
        dst = wt_dir / target
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)

    return wt_dir


def teardown_worktree(
    workspace: Path, repo: str, task: str, *, queue: str | None = None
) -> None:
    """Remove the worktree and its branch unconditionally."""
    repo_root = workspace / repo
    wt_dir = worktree_dir(workspace, repo, task, queue)
    branch = worktree_branch(task, queue)

    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_dir)],
        cwd=repo_root,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=repo_root,
        capture_output=True,
    )


def cleanup_task_worktree(
    workspace: Path, repo: str, task: str, *, queue: str | None = None
) -> bool:
    """Remove a task's *preserved* worktree + branch when present (the artifacts a
    failed-to-land task leaves behind for a later Resolve). Returns True when
    something existed and was removed; a no-op (False) when neither exists — so a
    cleanly-landed task, whose worktree the engine already tore down, is safe to
    pass here. Callers are responsible for the orphan check (no active/other run
    still needs the branch)."""
    repo_root = workspace / repo
    if not worktree_dir(workspace, repo, task, queue).exists() and not _branch_exists(
        repo_root, worktree_branch(task, queue)
    ):
        return False
    teardown_worktree(workspace, repo, task, queue=queue)
    return True


def _worktree_has_commits(
    workspace: Path, repo: str, task: str, *, queue: str | None = None
) -> bool:
    """True if the task's worktree branch has commits beyond ``HEAD``.

    A worker that made no commit (a non-agentic API backend, or an agentic one
    that decided nothing was needed) leaves nothing to validate or squash. When
    we can't tell, err on the side of "yes" so the normal path still runs.
    """
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)
    result = subprocess.run(
        ["git", "rev-list", "--count", f"HEAD..{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip() or "0") > 0
    except ValueError:
        return True


def _tracked_changes(repo_root: Path) -> list[str]:
    """Porcelain status lines for *tracked* changes in ``repo_root`` (ignores
    untracked ``??`` files, which only block a merge if they collide — and that
    case surfaces via git's own stderr instead)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
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
    stripped so callers see a clean repo-relative path."""
    path = line[3:] if len(line) > 3 else line.strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip().strip('"')


def _landing_blockers(repo_root: Path) -> list[str]:
    """Tracked changes in the target repo that should block a squash-merge.

    The content store is a *separate* repo, so briefs/queue config never live in
    ``repo_root`` and can't be a blocker. Every tracked change here is therefore
    genuine operator *code* WIP — return all of it (the land still stash/restores
    it when ``autostash`` is on)."""
    return _tracked_changes(repo_root)


AUTOSTASH_MESSAGE = "nightshift-autostash"


def _stash_operator_work(repo_root: Path, paths: list[str]) -> str | None:
    """Set aside the operator's tracked code WIP (the given ``paths``) in the
    target repo for the land critical section. Returns the captured stash
    *commit sha*, or ``None`` when there was nothing to set aside.

    Stack-free by design: ``git stash create`` records the WIP as a commit object
    without touching the LIFO stash stack, so a human running ``git stash``
    mid-land can never perturb it. ``stash create`` does not revert the tree, so
    we then explicitly clean just the blocker ``paths``."""
    if not paths:
        return None
    created = subprocess.run(
        ["git", "stash", "create", AUTOSTASH_MESSAGE],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    sha = created.stdout.strip()
    if created.returncode != 0 or not sha:
        return None
    # `stash create` captured the WIP but left the tree dirty; clean exactly the
    # blocker paths so the merge sees them at HEAD.
    subprocess.run(
        ["git", "checkout", "HEAD", "--", *paths],
        cwd=repo_root,
        capture_output=True,
    )
    return sha


def _restore_operator_work(repo_root: Path, sha: str, paths: list[str]) -> str | None:
    """Re-apply the set-aside WIP commit ``sha`` on top of the landed tree.
    Returns ``None`` on success, or a human-readable conflict detail when the
    apply conflicts with what the task just landed.

    On conflict the blocker ``paths`` are rolled back to the landed ``HEAD`` (so
    the tree is left clean, not littered with conflict markers) and the WIP is
    preserved on the stash stack under the ``nightshift-autostash`` message via
    ``git stash store`` so the operator can recover it by hand — never lost."""
    result = subprocess.run(
        ["git", "stash", "apply", sha],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return None
    # Conflict: clear the half-applied blocker paths and stash-store the sha so
    # the WIP is findable for a manual restore.
    if paths:
        subprocess.run(
            ["git", "checkout", "HEAD", "--", *paths],
            cwd=repo_root,
            capture_output=True,
        )
    subprocess.run(
        ["git", "stash", "store", "-m", AUTOSTASH_MESSAGE, sha],
        cwd=repo_root,
        capture_output=True,
    )
    detail = (result.stderr.strip() or result.stdout.strip()
              or "git stash apply failed")
    return detail


def _reset_to_head(repo_root: Path) -> None:
    """Undo a half-applied squash so a failed merge never leaves ``repo_root`` in
    a conflicted/partly-staged state. Safe only because :func:`squash_to_main`
    refuses to start when the tree already has tracked changes."""
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_root, capture_output=True)


def _conflicted_paths(repo_root: Path) -> list[str]:
    """Files left with unmerged (conflicted) entries in the index after a failed
    ``git merge --squash``. Must be read *before* :func:`_reset_to_head`."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=repo_root,
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

# Directory prefixes whose contents are never code for LOC accounting:
# build/dist output, vendored deps, and the git dir. Matched case-insensitively
# against the full (forward-slashed) path. ``.worktrees/`` is excluded
# defensively (worktrees live under the workspace, not a landed commit, but a
# repo that nests one should never have it counted).
_NON_CODE_DIR_PREFIXES: tuple[str, ...] = (
    ".worktrees/",
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
    # ``services/ui/dist/bundle.js``, ``a/build/x`` …
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


def compute_code_loc(repo_root: Path, sha: str) -> int:
    """Lines of code churned by a single commit ``sha`` in ``repo_root`` (added +
    removed), excluding build files, docs, comments, and blank lines.

    Reads the commit's own diff (``git show <sha>``) so a squash commit is
    measured against its parent. Returns 0 on any git error or for the initial
    commit (no parent) so a missing figure never breaks a run record."""
    result = subprocess.run(
        ["git", "show", sha, "--format=", "--unified=0", "--no-color", "--no-renames"],
        cwd=repo_root,
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


def _branch_exists(repo_root: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_root,
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
    workspace: Path,
    repo: str,
    task: str,
    title: str,
    *,
    queue: str | None = None,
    autostash: bool = True,
) -> tuple[str | None, str, bool]:
    """Merge the task's worktree branch as a single squash commit on the target
    repo's ``main`` (``repo_root = workspace / repo``).

    Returns ``(sha, "", False)`` on success, or ``(None, detail, recoverable)``
    on failure where ``detail`` is a human-readable reason and ``recoverable``
    says whether re-attempting the *same* squash could succeed once the user
    clears a blocker.

    Briefs/queue config live in the separate content store and are delivered via
    a run-scratch file, so the target repo only ever receives the implementation
    squash — there is nothing to snapshot up-front, and every tracked change in
    ``repo_root`` is genuine operator *code* WIP. That WIP is handled by
    ``autostash`` (default on, set per-queue via ``autostash_operator_work``):

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

    The whole critical section (optional set-aside → merge → commit →
    reset-on-failure → restore) runs under :func:`landing_lock` (per
    workspace+repo), so concurrent queue runners (and a CLI process) serialize on
    the repo's index/HEAD/stash instead of seeing a half-merged tree.
    """
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)

    if not _branch_exists(repo_root, branch):
        return None, f"no task branch '{branch}' to merge (nothing to recover)", False

    with landing_lock(workspace, repo):
        blockers = _landing_blockers(repo_root)
        wip_sha: str | None = None
        blocker_paths: list[str] = []
        if blockers:
            blocker_paths = [_porcelain_path(line) for line in blockers]
            if autostash:
                wip_sha = _stash_operator_work(repo_root, blocker_paths)
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
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if merge.returncode != 0:
                conflicts = _conflicted_paths(repo_root)
                _reset_to_head(repo_root)
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
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if commit.returncode != 0:
                detail = (
                    commit.stderr.strip()
                    or commit.stdout.strip()
                    or f"git commit exited {commit.returncode}"
                )
                _reset_to_head(repo_root)
                return None, f"commit failed:\n{detail}", False

            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
            ).stdout.strip()
        finally:
            if wip_sha:
                restore_detail = _restore_operator_work(repo_root, wip_sha, blocker_paths)

    if restore_detail:
        return sha, (
            f"landed ({sha}), but your set-aside working changes could not be "
            f"reapplied cleanly — they are kept in `git stash` for manual "
            f"restore:\n{restore_detail}"
        ), False
    return sha, "", False


# --------------------------------------------------------------------------
# Cross-machine landing transport (transport B — git rendezvous remote).
#
# A worker on a different box publishes its validated task branch to a git
# remote it has scoped access to; the manager fetches it into its own clone and
# runs the existing land() path. The WIP namespace (``nightshift-wip/*``) is
# kept distinct from the manager's PR branch (``task/*``) so a worker credential
# can be restricted to it. See docs/spec/remote-landing.md.
# --------------------------------------------------------------------------

WIP_REF_PREFIX = "nightshift-wip"


def normalize_wip_prefix(value: object) -> str:
    """Normalize the WIP-namespace prefix — the ``<prefix>`` segment of the
    rendezvous ref ``refs/heads/<prefix>/<queue>/<task>``.

    Returns a git-ref-safe namespace (one or more ``/``-joined segments).
    Raises ``ValueError`` on an unsafe value (empty, a leading ``-``, ``..``,
    ``//``, or characters outside ``[A-Za-z0-9._/-]``) so a bad operator value
    is surfaced at edit time rather than corrupting a push refspec.
    """
    text = str(value or "").strip().strip("/")
    if not text:
        raise ValueError("branch prefix must not be empty")
    if text.startswith("-") or ".." in text or "//" in text:
        raise ValueError(f"invalid branch prefix {text!r}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", text):
        raise ValueError(
            f"invalid branch prefix {text!r}: use letters, digits, '.', '_', "
            "'-', or '/' (no spaces, leading '-', '..', or '//')"
        )
    return text


def _wip_ref(task: str, queue: str | None, prefix: str = WIP_REF_PREFIX) -> str:
    """Remote ref a worker publishes its task branch to (transport-only). The
    ``prefix`` (the WIP namespace) defaults to :data:`WIP_REF_PREFIX` and is
    operator-configurable (manager ``wip_ref_prefix``, threaded via the work
    order)."""
    return f"refs/heads/{prefix or WIP_REF_PREFIX}/{_queue_slug(queue)}/{task}"


def _rev_parse(repo_root: Path, ref: str) -> str | None:
    res = subprocess.run(
        ["git", "rev-parse", ref], cwd=repo_root, capture_output=True, text=True
    )
    return res.stdout.strip() if res.returncode == 0 else None


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant`` (inclusive)."""
    res = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return res.returncode == 0


def publish_task_branch(
    workspace: Path,
    repo: str,
    task: str,
    remote: str,
    *,
    queue: str | None = None,
    prefix: str | None = None,
) -> tuple[str, str]:
    """Force-push the task's local worktree branch to ``<remote>`` as its WIP ref.

    ``prefix`` is the WIP namespace (the manager's ``wip_ref_prefix``, delivered
    in the work order); ``None`` falls back to :data:`WIP_REF_PREFIX`.

    Returns ``(wip_ref, head_sha)`` where ``head_sha`` is the full SHA of the
    pushed branch tip (the manager re-verifies it after fetching). Raises
    ``RuntimeError`` on a push failure or a missing branch so the worker can
    surface ``publish_failed`` and land nothing.
    """
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)
    wip_ref = _wip_ref(task, queue, prefix or WIP_REF_PREFIX)

    head_sha = _rev_parse(repo_root, branch)
    if head_sha is None:
        raise RuntimeError(f"no task branch '{branch}' to publish")

    push = subprocess.run(
        ["git", "push", "-f", remote, f"{branch}:{wip_ref}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        raise RuntimeError(
            f"publish of '{branch}' to {remote} {wip_ref} failed: "
            f"{(push.stderr or push.stdout).strip()[:300]}"
        )
    return wip_ref, head_sha


def fetch_rendezvous_branch(
    workspace: Path,
    repo: str,
    remote: str,
    wip_ref: str,
    task: str,
    *,
    queue: str | None = None,
) -> str | None:
    """Force-fetch a worker's published WIP ref into ``repo_root`` as the local
    ``worktree_branch``. Returns the fetched tip SHA, or ``None`` on a fetch
    error (the caller maps that to a recoverable land failure)."""
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)
    fetch = subprocess.run(
        ["git", "fetch", "-f", remote, f"{wip_ref}:refs/heads/{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        return None
    return _rev_parse(repo_root, branch)


def prune_rendezvous_branch(
    workspace: Path, repo: str, remote: str, wip_ref: str
) -> None:
    """Best-effort delete of a consumed WIP ref on the rendezvous remote. A
    failure is swallowed: the ref is transport-only and a leftover is harmless
    (and eligible for a future scheduled GC)."""
    repo_root = workspace / repo
    subprocess.run(
        ["git", "push", remote, "--delete", wip_ref],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )


def prepare_worktree_base(
    workspace: Path, repo: str, remote: str, base_ref: str | None
) -> str:
    """Cross-machine: make the manager's pinned ``base_ref`` reachable in the
    worker's clone, then return the commit-ish ``setup_worktree`` should cut from.

    Fetches ``<remote> main`` (``base_ref`` is the manager's ``origin/main`` HEAD,
    so it is reachable as an ancestor) and returns ``base_ref`` when it is now
    present, else falls back to ``HEAD`` (best-effort; a stale clone still lands
    co-located-style, and any real divergence surfaces as a land-time conflict).
    """
    if not base_ref:
        return "HEAD"
    repo_root = workspace / repo
    subprocess.run(
        ["git", "fetch", remote, "main"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return base_ref if _rev_parse(repo_root, base_ref) is not None else "HEAD"


# Per-target-repo throttle for origin/main refresh (monotonic timestamps).
_LAST_ORIGIN_SYNC_CHECK: dict[tuple[str, str], float] = {}


def _origin_sync_key(workspace: Path, repo: str) -> tuple[str, str]:
    return (str(workspace.resolve()), repo)


def reset_origin_sync_throttle(workspace: Path | None = None, repo: str | None = None) -> None:
    """Clear origin-sync throttle state (tests only)."""
    if workspace is None and repo is None:
        _LAST_ORIGIN_SYNC_CHECK.clear()
        return
    if workspace is None or repo is None:
        raise ValueError("workspace and repo must both be set or both omitted")
    _LAST_ORIGIN_SYNC_CHECK.pop(_origin_sync_key(workspace, repo), None)


def maybe_sync_main_to_origin(
    workspace: Path,
    repo: str,
    remote: str,
    *,
    min_interval_seconds: float = 15.0,
    autostash: bool = True,
    force: bool = False,
    reset_divergence: bool = False,
    drop_shas: frozenset[str] | None = None,
) -> str | None:
    """Refresh local ``main`` from ``<remote>/main`` when due.

    When ``force`` is false and the last fetch for this repo was less than
    ``min_interval_seconds`` ago, skip the network round-trip and return the
    current local ``HEAD``. Otherwise:

    1. ``git fetch <remote> main``
    2. Compare local ``HEAD`` to ``FETCH_HEAD`` (no work when already current)
    3. When local ``main`` is **strictly behind** ``origin/main``, fast-forward
       with ``reset --hard`` (operator dirty-tree WIP is autostashed when enabled)
    4. When local ``main`` is **ahead of or diverged from** ``origin/main``,
       leave it alone unless ``reset_divergence=True`` (land retries / orphan
       pr-mode cleanup). Even then the reset is surgical: unpushed commits are
       replayed onto the fresh ``origin/main`` afterwards, so operator
       cherry-picks survive. ``drop_shas`` names commits the caller deliberately
       discards (its own orphan squash) so they are *not* replayed.

    Returns the local ``HEAD`` after the check, or ``None`` when the remote has
    no ``main`` yet (callers fall back to local ``HEAD``). See
    ``docs/spec/remote-landing.md``, Proposal 1.
    """
    repo_root = workspace / repo
    key = _origin_sync_key(workspace, repo)
    now = time.monotonic()
    if (
        not force
        and min_interval_seconds > 0
        and key in _LAST_ORIGIN_SYNC_CHECK
        and (now - _LAST_ORIGIN_SYNC_CHECK[key]) < min_interval_seconds
    ):
        return _rev_parse(repo_root, "HEAD")

    result = _sync_main_to_origin_impl(
        workspace,
        repo,
        remote,
        autostash=autostash,
        reset_divergence=reset_divergence,
        drop_shas=drop_shas,
    )
    _LAST_ORIGIN_SYNC_CHECK[key] = time.monotonic()
    return result


def sync_main_to_origin(
    workspace: Path,
    repo: str,
    remote: str,
    *,
    autostash: bool = True,
    reset_divergence: bool = True,
    drop_shas: frozenset[str] | None = None,
) -> str | None:
    """Force an immediate origin/main refresh (bypasses the git-refresh throttle).

    Used when correctness matters more than pacing — e.g. a push-rejected land
    retry or an out-of-process resolve about to rebase. By default
    ``reset_divergence=True`` so an orphaned pr-mode squash can be dropped; pass
    ``reset_divergence=False`` for a fetch + fast-forward-only check.

    ``drop_shas`` names commits the caller deliberately discards (its own orphan
    squash). Any *other* unpushed commit on local ``main`` (e.g. an operator
    cherry-pick) is replayed onto the fresh ``origin/main`` and preserved.
    """
    return maybe_sync_main_to_origin(
        workspace,
        repo,
        remote,
        min_interval_seconds=0,
        autostash=autostash,
        force=True,
        reset_divergence=reset_divergence,
        drop_shas=drop_shas,
    )


def _unpushed_commits(repo_root: Path, target: str, head: str) -> list[str]:
    """Commits on local ``head`` not reachable from ``target``, oldest-first.

    These are the commits a ``reset --hard target`` over a divergence would drop
    (e.g. an operator cherry-pick on ``main`` plus the manager's own orphan
    squash). Returned oldest-first so a replay re-applies them in order."""
    res = subprocess.run(
        ["git", "rev-list", "--reverse", f"{target}..{head}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def _replay_commits(repo_root: Path, shas: list[str]) -> None:
    """Cherry-pick ``shas`` (oldest-first) onto the current ``HEAD``.

    Each commit is replayed individually so a redundant one (its content already
    present on the new base — e.g. the manager's own squash that origin already
    carries) collapses to an empty cherry-pick and is skipped rather than
    re-introduced. A commit that genuinely conflicts with the new base is skipped
    too (best-effort rescue): its content is preserved unreachable in the reflog
    for manual recovery rather than left as a half-applied conflict on ``main``.
    """
    for sha in shas:
        cp = subprocess.run(
            ["git", "cherry-pick", sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if cp.returncode == 0:
            continue
        # Empty (already-applied) or conflicted: abort this pick and move on.
        # ``--skip`` advances past an empty pick; ``--abort`` unwinds a conflict.
        # Try skip first (the common, redundant-squash case), then abort.
        if subprocess.run(
            ["git", "cherry-pick", "--skip"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        ).returncode != 0:
            subprocess.run(
                ["git", "cherry-pick", "--abort"],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )


def _sync_main_to_origin_impl(
    workspace: Path,
    repo: str,
    remote: str,
    *,
    autostash: bool = True,
    reset_divergence: bool = False,
    drop_shas: frozenset[str] | None = None,
) -> str | None:
    """Fetch ``<remote> main`` and move local ``main`` when safe.

    When ``reset_divergence`` is set and local ``main`` has diverged (carries
    unpushed commits), the reset to ``origin/main`` is *surgical*: any unpushed
    commit is rescued and replayed on top of the fresh tip afterwards, so an
    operator cherry-pick sitting on ``main`` survives a land retry instead of
    being silently dropped. ``drop_shas`` names commits the caller intends to
    discard (the manager's own orphan squash from a rejected push), which are
    excluded from the replay; redundant picks (content already on the new base)
    collapse to empty and are skipped automatically.
    """
    drop = drop_shas or frozenset()
    repo_root = workspace / repo
    with landing_lock(workspace, repo):
        fetch = subprocess.run(
            ["git", "fetch", remote, "main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if fetch.returncode != 0:
            return None
        target = _rev_parse(repo_root, "FETCH_HEAD")
        if target is None:
            return None
        head = _rev_parse(repo_root, "HEAD")
        if head is None:
            return None
        if head == target:
            return head

        behind = _is_ancestor(repo_root, head, target)
        if not behind and not reset_divergence:
            # Local main carries unpushed or divergent commits (e.g. a direct
            # cherry-pick) — periodic/poll sync must not clobber them.
            return head

        # Over a divergence, capture the unpushed commits so we can replay the
        # ones the caller did not ask to drop after fast-forwarding to origin.
        rescue: list[str] = []
        if not behind:
            rescue = [
                sha
                for sha in _unpushed_commits(repo_root, target, head)
                if sha not in drop
            ]

        blockers = _landing_blockers(repo_root)
        wip_sha: str | None = None
        blocker_paths: list[str] = []
        if blockers:
            if not autostash:
                return head
            blocker_paths = [_porcelain_path(line) for line in blockers]
            wip_sha = _stash_operator_work(repo_root, blocker_paths)

        try:
            reset = subprocess.run(
                ["git", "reset", "--hard", target],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if reset.returncode == 0 and rescue:
                _replay_commits(repo_root, rescue)
        finally:
            if wip_sha:
                _restore_operator_work(repo_root, wip_sha, blocker_paths)

        if reset.returncode != 0:
            return _rev_parse(repo_root, "HEAD")
        # Eager preflight signal: local main just moved — if a lockfile changed
        # in the range, drop the venv marker so the next task re-syncs before
        # spending model budget. The marker fingerprint stays authoritative for
        # clones that never fast-forward through here (e.g. a worker box).
        if lock_changed_between(repo_root, head, target):
            invalidate_lock_marker(repo_root)
        return _rev_parse(repo_root, "HEAD")


def recover_task(
    workspace: Path, repo: str, task: str, title: str, *, queue: str | None = None
) -> TaskResult:
    """Re-attempt the squash-merge for a task whose validate passed but whose
    merge to the target repo's ``main`` failed (typically a dirty tree at the
    time).

    The worktree branch is preserved on such failures precisely so this cheap
    recovery is possible without re-running the worker. On success the branch
    and worktree are torn down; on failure they are left in place so the user
    can fix the blocker (e.g. commit their work) and retry again.
    """
    branch = worktree_branch(task, queue)
    repo_root = workspace / repo
    if not _branch_exists(repo_root, branch):
        return TaskResult(
            task=task, title=title, success=False,
            error=(
                "nothing to recover: the task branch no longer exists. "
                "Re-run the task instead."
            ),
        )

    # Autostash is an operator/global default in ``<workspace>/config.json``;
    # recovery doesn't carry a tasks_root so it reads the host config directly.
    try:
        host_config = load_config(workspace)
    except (FileNotFoundError, ValueError):
        host_config = {}
    autostash = bool(host_config.get("autostash_operator_work", True))
    sha, detail, _ = squash_to_main(
        workspace, repo, task, title, queue=queue, autostash=autostash
    )
    if sha is None:
        return TaskResult(
            task=task, title=title, success=False,
            error=detail or "squash-merge to main failed",
        )

    teardown_worktree(workspace, repo, task, queue=queue)
    return TaskResult(
        task=task, title=title, success=True, commit_sha=sha,
        result_line=f"recovered: landed ({sha})",
    )


# --------------------------------------------------------------------------- #
# Resolve — diagnose + agentic conflict/validation resolution
# --------------------------------------------------------------------------- #

RESOLVE_PROMPT_FILE = asset("prompts", "nightshift-resolve.md")
DEFAULT_MAX_RESOLVE_ATTEMPTS = 2


def build_resolve_prompt(task: str, *, task_file: str, context: str) -> str:
    """Build the prompt for the resolve agent — the resolution charter plus the
    concrete reason the squash-merge to main failed.

    ``task_file`` is the path to the materialised run-scratch brief (outside the
    worktree), never a path inside the target repo."""
    prompt_body = RESOLVE_PROMPT_FILE.read_text()
    return (
        f"Your task file is: {task_file}\n"
        f"The TASK variable is: {task}\n\n"
        f"## Why the merge to main failed\n\n{context}\n\n"
        f"{prompt_body}"
    )


def _link_worktree_artifacts(repo_root: Path, worktree_dir: Path) -> None:
    """Symlink build artifacts (`.venv`, node_modules) from the target repo into
    ``worktree_dir`` so a re-attached worktree can run ``just validate``."""
    for target in SYMLINK_TARGETS:
        src = repo_root / target
        dst = worktree_dir / target
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)


def _ensure_worktree_for_branch(
    workspace: Path, repo: str, task: str, *, queue: str | None = None
) -> Path | None:
    """Ensure the task's worktree exists on its preserved branch (re-attaching it
    if its checkout was cleaned up). Returns the dir, or ``None`` if the branch is
    gone. Unlike :func:`setup_worktree` this never deletes the branch."""
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)
    if not _branch_exists(repo_root, branch):
        return None
    wt_dir = worktree_dir(workspace, repo, task, queue)
    if not wt_dir.exists():
        wt_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(wt_dir), branch],
            cwd=repo_root,
            capture_output=True,
        )
    if not wt_dir.exists():
        return None
    _link_worktree_artifacts(repo_root, wt_dir)
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
    workspace: Path,
    repo: str,
    tasks_root: Path,
    task: str,
    title: str,
    *,
    emit: Listener = _noop,
    config: dict | None = None,
    backend_name: str | None = None,
    abort_reason: object = None,
    queue: str | None = None,
) -> TaskResult:
    """Resolve a task whose validated work failed to land on the target repo's
    ``main`` (``repo_root = workspace / repo``).

    Diagnoses first, then acts:

    * If a plain re-squash now works (a transient blocker has cleared) it lands
      immediately — this is the cheap legacy :func:`recover_task` path.
    * If ``main`` is still dirty (``recoverable``) it reports that: an agent can't
      touch the operator's unrelated local edits.
    * Otherwise it's a content conflict: an agent rebases the branch onto ``main``,
      resolves the conflicts, re-validates, and squashes — bounded by
      ``max_resolve_attempts``.

    The brief is read from ``tasks_root`` (the content store); rebase/squash run
    in ``repo_root``. Emits ``TASK_STARTED``/``TASK_STATUS``/``TASK_RESULT`` so the
    caller can drive it as a tracked job (live log + ``resolve`` phase). The
    branch is preserved on failure so the operator can still resolve it by hand.
    """
    tasks_rel = playlists.tasks_rel(queue)
    config = config or resolve_config(workspace, tasks_root, tasks_rel)
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)

    task_file = tasks_root / tasks_rel / f"{task}.md"
    meta: dict = {}
    body = ""
    if task_file.exists():
        meta, body = split_frontmatter(task_file.read_text())
    emit(Event(TASK_STARTED, {
        "task": task, "title": title, "repo": repo,
        "frontmatter": {**meta}, "body": body.strip(),
    }))

    if not _branch_exists(repo_root, branch):
        error = (
            "nothing to resolve: the task branch no longer exists. "
            "Re-run the task instead."
        )
        emit(Event(TASK_RESULT, {
            "task": task, "status": "error", "error": error, "repo": repo,
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
        workspace, repo, task, title, queue=queue, autostash=autostash
    )
    if sha is not None:
        loc = compute_code_loc(repo_root, sha)
        teardown_worktree(workspace, repo, task, queue=queue)
        # Backstop queue removal for a landed regular task (see drop_completed_task).
        if not task_is_evergreen(meta, task, config):
            drop_completed_task(tasks_root, task, tasks_rel, queue=queue)
        result_line = f"resolved: landed ({sha})"
        emit(Event(TASK_RESULT, {
            "task": task, "status": "completed", "repo": repo,
            "result_line": result_line, "commit_sha": sha, "loc": loc,
        }))
        return TaskResult(
            task=task, title=title, success=True, commit_sha=sha, loc=loc,
            result_line=result_line,
        )

    if recoverable:
        # Transient blocker (e.g. main has uncommitted edits): not an agent's job.
        emit(Event(TASK_RESULT, {
            "task": task, "status": "error", "error": detail, "repo": repo,
            "result_line": "blocked — clear main, then resolve",
            "recoverable": True, "failure_kind": "merge_rejected",
        }))
        return TaskResult(
            task=task, title=title, success=False, error=detail,
            failure_kind="merge_rejected",
        )

    # 2. Content conflict (or generic merge failure): hand it to the agent.
    return _agent_resolve(
        workspace, repo, tasks_root, task, title,
        conflict_detail=detail, emit=emit, config=config,
        backend_name=backend_name, abort_reason=abort_reason, queue=queue,
    )


def _agent_resolve(
    workspace: Path,
    repo: str,
    tasks_root: Path,
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
    """Rebase the task branch onto the target repo's ``main`` and drive an agent
    to resolve the conflicts / validation failures, then squash. Bounded by
    config ``max_resolve_attempts``."""
    from nightshift.backends import LAUNCH_FAILED, WorkerSpec

    tasks_rel = playlists.tasks_rel(queue)
    repo_root = workspace / repo

    def _emit_log(line: str) -> None:
        emit(Event(TASK_LOG, {"task": task, "line": line}))

    def _on_worker_start(pid: int) -> None:
        emit(Event(WORKER_STARTED, {"task": task, "pid": pid}))

    def _should_abort() -> str | None:
        return abort_reason() if callable(abort_reason) else None

    worktree_dir = _ensure_worktree_for_branch(workspace, repo, task, queue=queue)
    if worktree_dir is None:
        error = "could not prepare the task worktree for resolution"
        emit(Event(TASK_RESULT, {
            "task": task, "status": "error", "error": error, "repo": repo,
            "result_line": "resolve setup failed", "failure_kind": "merge_conflict",
        }))
        return TaskResult(
            task=task, title=title, success=False, error=error,
            failure_kind="merge_conflict",
        )

    max_attempts = int(config.get("max_resolve_attempts", DEFAULT_MAX_RESOLVE_ATTEMPTS))
    validate_cmd = resolve_validate_cmd(config)
    env = worker_env(worktree_dir)
    task_file = tasks_root / tasks_rel / f"{task}.md"
    meta: dict = {}
    body = ""
    if task_file.exists():
        meta, body = split_frontmatter(task_file.read_text())
    # Deliver the brief via a run-scratch file outside the worktree (the brief
    # never enters the target repo).
    scratch = materialize_brief(workspace, repo, task, body, queue=queue)
    resolved = resolve_frontmatter(meta, config)
    backend, model = select_run_backend(
        config.get("resolve_model") or resolved["model"],
        config.get("resolve_backend") or backend_name or config.get("worker_backend"),
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
                prompt=build_resolve_prompt(task, task_file=str(scratch), context=context),
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
                emit(Event(TASK_RESULT, {
                    "task": task, "status": worker.aborted, "repo": repo,
                }))
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
                    "task": task, "status": "error", "error": error, "repo": repo,
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
            workspace, repo, task, title, queue=queue,
            autostash=bool(config.get("autostash_operator_work", True)),
        )
        if sha is not None:
            loc = compute_code_loc(repo_root, sha)
            teardown_worktree(workspace, repo, task, queue=queue)
            # Backstop queue removal for a landed regular task (see drop_completed_task).
            if not task_is_evergreen(meta, task, config):
                drop_completed_task(tasks_root, task, tasks_rel, queue=queue)
            result_line = f"resolved: landed ({sha})"
            emit(Event(TASK_RESULT, {
                "task": task, "status": "completed", "repo": repo,
                "result_line": result_line, "commit_sha": sha, "loc": loc,
            }))
            return TaskResult(
                task=task, title=title, success=True, commit_sha=sha, loc=loc,
                result_line=result_line,
            )
        last_error = squash_detail or "squash-merge still failed after resolution"

    error = f"auto-resolve failed after {max_attempts} attempt(s):\n{last_error}"
    _write_failure_log(workspace, repo, worktree_dir, task, error)
    emit(Event(TASK_RESULT, {
        "task": task, "status": "error", "error": error, "repo": repo,
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

def _write_failure_log(
    workspace: Path,
    repo: str,
    worktree_dir: Path,
    task: str,
    error: str,
    *,
    validate_stdout: str = "",
    validate_stderr: str = "",
) -> Path:
    """Write a terse failure log so repeated failures are diagnosable. Logs live
    under the workspace-level worktree area for the repo
    (``<workspace>/.worktrees/<repo>/failures/<task>.log``), never in the target
    repo."""
    log_dir = workspace / ".worktrees" / repo / "failures"
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
    """Run deterministic auto-fixes and retry validate once (interruptibly).

    Ruff is run over the whole worktree (``.``); it honors the target repo's own
    ``pyproject.toml`` / ``ruff.toml`` (selected rules, ``exclude`` globs), so the
    repair pass stays correct without the engine knowing the repo's layout.
    """
    subprocess.run(
        [".venv/bin/ruff", "check", "--fix", "--unsafe-fixes", "."],
        cwd=worktree_dir,
        capture_output=True,
    )
    subprocess.run(
        [".venv/bin/ruff", "format", "."],
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


# Honest-failure sentinel: an agent that cannot complete a task makes **no
# commits** and emits a final log line ``NIGHTSHIFT_BLOCKED: <reason>`` (no
# ``.BLOCKED`` file is written anywhere). The worker/engine scan captured output
# for this marker to surface a ``blocked`` status + reason.
_BLOCKED_SENTINEL = re.compile(r"^\s*NIGHTSHIFT_BLOCKED:\s*(.*\S)?\s*$")


def extract_blocked_reason(text: str) -> str | None:
    """Return the reason from the **last** ``NIGHTSHIFT_BLOCKED: <reason>`` line in
    ``text``, or ``None`` when the sentinel is absent.

    A bare ``NIGHTSHIFT_BLOCKED:`` with no reason still counts as a block and
    yields a generic ``"blocked"`` reason. Scanning the whole captured log (last
    match wins) tolerates the marker appearing mid-stream before the agent's
    final summary."""
    reason: str | None = None
    for line in text.splitlines():
        match = _BLOCKED_SENTINEL.match(line)
        if match:
            reason = (match.group(1) or "").strip() or "blocked"
    return reason


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
    workspace: Path,
    tasks_root: Path,
    task: str,
    *,
    repo: str | None = None,
    emit: Listener = _noop,
    abort_reason: object = None,
    backend_name: str | None = None,
    tasks_rel: str = "main",
) -> TaskResult:
    """Run a single task end-to-end across the two roots. Always cleans up the
    worktree when done.

    The brief is read from ``tasks_root`` (the content store); the target repo is
    resolved per task (``repo`` override → queue ``config.json`` ``repo``) and all
    git ops run in ``repo_root = workspace / repo``. ``abort_reason`` is an
    optional zero-arg callable returning ``None`` to continue or a status string
    (``"skipped"`` / ``"stopped"``) to terminate the worker early.
    ``backend_name`` selects the worker shim (claude / cursor / anthropic /
    ollama); when ``None`` it falls back to ``config`` then the default.
    ``tasks_rel`` selects the queue dir (``main`` or an alternate queue) and whose
    config (incl. the ``validate`` command and default ``repo``) applies.

    Two non-error early exits exist: a malformed/absent ``repo`` reference is an
    **authoring** error (``RepoConfigError`` → ``TASK_RESULT`` ``error``), while a
    well-formed but currently-absent repo **pauses** the task
    (``TASK_RESULT`` ``"paused"`` + reason ``repo_unavailable``) without cutting a
    worktree. The final ``task_result`` event carries a ``timings`` dict of
    per-phase seconds (``worker`` / ``validate`` / ``commit`` / ``total``).
    """
    tasks_dir = tasks_root / tasks_rel
    task_file = tasks_dir / f"{task}.md"
    config = resolve_config(workspace, tasks_root, tasks_rel)
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

    if is_quarantined(meta):
        emit(Event(TASK_RESULT, {
            "task": task,
            "status": "skipped",
            "result_line": "skipped: task is quarantined",
        }))
        return TaskResult(
            task=task, title=title, success=False, status="skipped",
            result_line="skipped: task is quarantined",
        )

    if is_completed(meta):
        emit(Event(TASK_RESULT, {
            "task": task,
            "status": "skipped",
            "result_line": "skipped: task is completed",
        }))
        return TaskResult(
            task=task, title=title, success=False, status="skipped",
            result_line="skipped: task is completed",
        )

    # Resolve the target repo (task frontmatter override → queue default). A
    # malformed/absent reference is an authoring error (never dispatched).
    if not repo:
        try:
            repo = repos.resolve_repo(
                meta.get("repo"),
                load_queue_config(tasks_root, tasks_rel).get("repo"),
            )
        except repos.RepoConfigError as err:
            emit(Event(TASK_RESULT, {
                "task": task, "status": "error", "error": str(err),
                "result_line": "repo configuration error",
                "failure_kind": "repo_config",
            }))
            return TaskResult(
                task=task, title=title, success=False, error=str(err),
                result_line="repo configuration error", failure_kind="repo_config",
            )

    # A well-formed reference to an absent/`.git`-less repo pauses (not fails) the
    # task until the repo appears — no worktree is cut, no run is recorded.
    if not repos.repo_available(workspace, repo):
        result_line = f"paused: repo '{repo}' is not available"
        emit(Event(TASK_RESULT, {
            "task": task, "status": "paused", "repo": repo,
            "reason": "repo_unavailable", "result_line": result_line,
        }))
        return TaskResult(
            task=task, title=title, success=False, status="paused",
            result_line=result_line,
        )

    frontmatter = {**meta}
    frontmatter.setdefault("model", resolved["model"])
    # Carry the brief prose into the run record so History can show the original
    # brief after the task file is removed (completed tasks leave the queue).
    emit(Event(TASK_STARTED, {
        "task": task, "title": title, "repo": repo,
        "frontmatter": frontmatter, "body": body.strip(),
    }))
    emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "worker"}))

    def _should_abort() -> str | None:
        return abort_reason() if callable(abort_reason) else None

    repo_root = workspace / repo
    t_task_start = time.monotonic()
    # Deliver the brief via a run-scratch file outside the worktree (the brief
    # never enters the target repo), then cut the worktree from the target repo.
    scratch = materialize_brief(workspace, repo, task, body, queue=queue)
    worktree_dir = setup_worktree(workspace, repo, task, queue=queue)
    preserve_worktree = False

    # Imported here, not at module top, to avoid a backends<->engine import
    # cycle: backends reuses engine's claude argv/bin helpers.
    from nightshift.backends import LAUNCH_FAILED, WorkerSpec

    try:
        prompt = build_prompt(
            task,
            task_file=str(scratch),
            validate_cmd=str(config.get("validate") or DEFAULT_VALIDATE_CMD),
            loop=bool(meta.get("loop", False)),
            loop_max_iterations=int(meta.get("loop_max_iterations", 0)),
        )
        env = worker_env(worktree_dir)
        validate_cmd = resolve_validate_cmd(config)
        backend, run_model = select_run_backend(
            resolved["model"], backend_name or config.get("worker_backend")
        )
        spec = WorkerSpec(
            task=task,
            prompt=prompt,
            model=run_model,
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
            "line": f"  running worker [{backend.name}] ({run_model})...\n",
        }))
        t_worker = time.monotonic()
        worker = backend.run(spec, _emit_log, _should_abort, on_worker_start=_on_worker_start)
        timings["worker"] = round(time.monotonic() - t_worker, 1)

        if worker.aborted is not None:
            emit(Event(TASK_RESULT, {
                "task": task, "status": worker.aborted, "repo": repo,
                "timings": _with_total(),
            }))
            return TaskResult(task=task, title=title, success=False, status=worker.aborted)

        if worker.returncode == LAUNCH_FAILED:
            error = (
                f"{worker.error}. Add the worker binary to PATH or set its "
                "'*_bin' in config.json."
            )
            _write_failure_log(workspace, repo, worktree_dir, task, error)
            emit(Event(TASK_RESULT, {
                "task": task, "status": "error", "error": error, "repo": repo,
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
            _write_failure_log(workspace, repo, worktree_dir, task, error)
            line = error.splitlines()[0][:120]
            emit(Event(TASK_RESULT, {
                "task": task, "status": "error", "error": error, "repo": repo,
                "result_line": line, "failure_kind": "worker_error",
                "timings": _with_total(),
            }))
            return TaskResult(
                task=task, title=title, success=False, error=error,
                result_line=line, failure_kind="worker_error",
            )

        # No commits → nothing to validate or squash. Finish cleanly instead of
        # tripping the squash step (e.g. non-agentic completion backends).
        if not _worktree_has_commits(workspace, repo, task, queue=queue):
            result_line = "no changes produced (worker emitted output only)"
            # A no-changes completion never removed the brief (no branch to land),
            # so drop it here for regular tasks — a completed task must leave the
            # queue. Evergreen tasks keep their file and re-run.
            if not task_is_evergreen(meta, task, config):
                drop_completed_task(tasks_root, task, tasks_rel, queue=queue)
            emit(Event(TASK_RESULT, {
                "task": task, "status": "completed", "repo": repo,
                "result_line": result_line, "timings": _with_total(),
            }))
            return TaskResult(task=task, title=title, success=True, result_line=result_line)

        # Honour a stop/skip requested while the worker was running, before we
        # sink time into validate.
        if _should_abort() is not None:
            reason = _should_abort()
            emit(Event(TASK_RESULT, {
                "task": task, "status": reason, "repo": repo, "timings": _with_total(),
            }))
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
                emit(Event(TASK_RESULT, {
                    "task": task, "status": reason, "repo": repo,
                    "timings": _with_total(),
                }))
                return TaskResult(task=task, title=title, success=False, status=reason)

            if validate_result.returncode != 0:
                error = f"just validate failed:\n{validate_result.stdout[-2000:]}\n{validate_result.stderr[-2000:]}"
                _write_failure_log(
                    workspace, repo, worktree_dir, task, error,
                    validate_stdout=validate_result.stdout,
                    validate_stderr=validate_result.stderr,
                )
                result_line = extract_result_line(validate_result.stdout, validate_result.stderr)
                emit(Event(TASK_RESULT, {
                    "task": task, "status": "error", "error": error, "repo": repo,
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
            emit(Event(TASK_RESULT, {
                "task": task, "status": reason, "repo": repo, "timings": _with_total(),
            }))
            return TaskResult(task=task, title=title, success=False, status=reason)

        emit(Event(TASK_STATUS, {"task": task, "status": "running", "phase": "commit"}))
        t_commit = time.monotonic()
        sha, squash_error, recoverable = squash_to_main(
            workspace, repo, task, title, queue=queue,
            autostash=bool(config.get("autostash_operator_work", True)),
        )
        timings["commit"] = round(time.monotonic() - t_commit, 1)
        # A successful land that couldn't reapply set-aside operator work surfaces
        # squash_error as a warning (the commit still landed; stash is preserved).
        if sha is not None and squash_error:
            emit(Event(TASK_LOG, {"task": task, "line": f"  warning: {squash_error}\n"}))
        if sha is None:
            error = squash_error or "squash-merge to main failed"
            _write_failure_log(workspace, repo, worktree_dir, task, error)
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
                    workspace, repo, tasks_root, task, title,
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
                "task": task, "status": "error", "error": error, "repo": repo,
                "result_line": result_line,
                "recoverable": recoverable, "failure_kind": failure_kind,
                "timings": _with_total(),
            }))
            return TaskResult(
                task=task, title=title, success=False, error=error,
                failure_kind=failure_kind,
            )

        # Code lines churned by the squash commit this task landed on the target
        # repo's ``main`` (added + removed), excluding build files, docs, comments,
        # and build/output dirs — summed on the Stats page. The landed commit is
        # the one metric the Stats backfill can also reconstruct from a record's
        # ``commit_sha`` after the task branch is torn down, so live capture and
        # backfill report the *same* figure for every task (a branch-history sum
        # would diverge from any later backfill and make the total inconsistent).
        loc = compute_code_loc(repo_root, sha)
        # Backstop the worker's queue removal: a regular task that lands must
        # leave the queue. If the worker's branch didn't ``git rm`` its brief, the
        # squash kept it on ``main`` and the UI would keep listing a completed
        # task — drop it from the content store here. Evergreen tasks keep theirs.
        if not task_is_evergreen(meta, task, config):
            drop_completed_task(tasks_root, task, tasks_rel, queue=queue)
        emit(Event(TASK_RESULT, {
            "task": task,
            "status": "completed",
            "repo": repo,
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
            teardown_worktree(workspace, repo, task, queue=queue)


def run_queue(
    workspace: Path,
    tasks_root: Path,
    tasks: list[str],
    *,
    listeners: list[Listener] | None = None,
    controller: Controller | None = None,
    run_id: str | None = None,
    backend_name: str | None = None,
    tasks_rel: str = "main",
    follow_queue: bool = False,
    task_slot: Callable[[], AbstractContextManager[object]] | None = None,
    admit_task: Callable[[], str | None] | None = None,
) -> RunSummary:
    """Run tasks from a queue, emitting events to ``listeners``.

    Briefs are read from ``tasks_root`` (the content store) and each task resolves
    its own target repo inside :func:`run_task` (the run is paused, not failed, if
    that repo is currently absent). If a :class:`Controller` is supplied the loop
    honours pause/stop/skip; with no controller it runs straight through (the CLI
    path). ``backend_name`` selects the worker shim for every task in the run.
    ``tasks_rel`` selects the queue dir (``main`` or an alternate queue).

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

    There is no pre-run target-repo snapshot: briefs live in ``tasks_root`` and
    are delivered to the worker via a run-scratch file, so the target repo only
    ever receives the implementation squash.
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
                live = live_ordered_queue(tasks_root, tasks_rel)
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

            def _run() -> TaskResult:
                return run_task(
                    workspace,
                    tasks_root,
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
