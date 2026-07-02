"""Pre-run checks — environment preflight (venv/lockfile sync), disk and
tooling preconditions, the workspace lock, and interruptible subprocess runs.

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration.
The working-tree status helpers (:func:`landing_blockers`,
:func:`porcelain_path`) moved here from ``git/squash.py`` in Phase 6: they are
preflight information now — landing became a ref operation that never touches
(or is blocked by) operator WIP.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nightshift.git import GitRunner


def _tracked_changes(repo_root: Path) -> list[str]:
    """Porcelain status lines for *tracked* changes in ``repo_root`` (ignores
    untracked ``??`` files, which a land can never clobber)."""
    result = GitRunner(repo_root).run("status", "--porcelain")
    return [
        line for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("??")
    ]


def porcelain_path(line: str) -> str:
    """Extract the working path from a ``git status --porcelain`` line.

    Lines are ``XY <path>``; a rename is ``R  <old> -> <new>`` — we want the
    destination. Surrounding quotes (git quotes paths with special chars) are
    stripped so callers see a clean repo-relative path."""
    path = line[3:] if len(line) > 3 else line.strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip().strip('"')


def landing_blockers(repo_root: Path) -> list[str]:
    """Tracked operator code WIP in the target repo, reported at preflight.

    The content store is a *separate* repo, so briefs/queue config never live in
    ``repo_root`` and can't show up here. Since Phase 6 this WIP never blocks a
    land (landing is a ref operation that leaves the working tree alone) — it
    only means an overlapping land will leave the checkout behind ``main``."""
    return _tracked_changes(repo_root)


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
    res = GitRunner(repo_root).run("diff", "--name-only", old, new, "--", *_LOCK_FILENAMES)
    return res.ok and bool(res.stdout.strip())


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


def kill_process_group(proc: subprocess.Popen) -> None:
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
                kill_process_group(proc)
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    out, err = "", ""
                return subprocess.CompletedProcess(cmd, proc.returncode or 130, out, err)


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
    # Untracked files never block a run, and since Phase 6 neither does tracked
    # code WIP: landing is a ref operation that leaves the working tree alone.
    # The only consequence is that a land overlapping this WIP will refuse to
    # advance the checkout (it stays behind main) — worth a note up front.
    # Briefs/queue config live in the separate content store, never in the
    # target repo, so they can't appear here.
    blockers = landing_blockers(repo_root)
    if blockers:
        shown = "\n".join(f"  {line}" for line in blockers[:10])
        print(
            "note: main has uncommitted code — lands never touch it, but a "
            "land that overlaps it will leave your checkout behind main "
            f"(commit or stash to advance):\n{shown}"
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
