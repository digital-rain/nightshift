"""Structured run records for nightshift.

The engine emits :class:`Event` objects as a run progresses. A :class:`RunWriter`
persists them to disk so both the CLI and the server produce identical history:

    .tasks/runs/<run-id>/
        run.json        run metadata (id, started_at, finished_at, launched_by)
        events.jsonl    append-only event log (the SSE source of truth)
        <task>.log      per-task captured worker output (the UI log window)

:class:`RunStore` reads those records back into per-task records that drive the
completed pane.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any


RUNS_DIR = "main/runs"

# Event types emitted by the engine.
RUN_STARTED = "run_started"
TASK_STARTED = "task_started"
TASK_LOG = "task_log"
TASK_STATUS = "task_status"
TASK_RESULT = "task_result"
RUN_FINISHED = "run_finished"
# Emitted once a worker subprocess is launched, carrying its OS pid so stale-run
# reconciliation can tell a genuinely-busy worker (even an orphaned one) from an
# abandoned record.
WORKER_STARTED = "worker_started"

# Terminal status applied to a task whose run is no longer live but never
# recorded a result (server restart, crashed worker, killed process, ...).
ABORTED = "aborted"
ABORT_REASON_INTERRUPTED = "aborted: run ended before the task finished"
ABORT_REASON_NO_RUNNER = "aborted: no active runner (process ended)"

# A run with no new events for this long, that isn't the live run, is treated
# as abandoned by reconcile_stale(). Comfortably longer than the UI refresh.
ABORT_STALE_SECONDS = 120.0


def now_iso() -> str:
    """UTC timestamp suitable for sorting and display."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    """A sortable, collision-resistant run id."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _safe_name(task: str) -> str:
    """Make a task name safe to use as a log filename."""
    return task.replace("/", "_")


def _pid_alive(pid: int | None) -> bool:
    """True if a process with ``pid`` currently exists.

    Uses ``kill(pid, 0)``: ``ESRCH`` means it's gone, ``EPERM`` means it exists
    but is owned by another user (still alive). ``None``/non-positive → dead.
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _safe_mtime(path: Path) -> float:
    """``path`` mtime as an epoch float, or 0.0 (the distant past) if missing."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _merge_commit_shas(existing: str | None, new: str | None) -> str | None:
    """Fold a newly landed sha into the record's comma-separated commit field.

    A task can land more than once (initial run, then a resolve/recovery), so the
    history commit field accumulates every sha a task generated as a comma-
    separated list. Order is preserved and duplicates are dropped, so re-emitting
    the same result (idempotent replays) never grows the list. Returns ``existing``
    unchanged when ``new`` is empty, so a result without a sha can't clear prior
    lands.
    """
    if not new:
        return existing
    shas = [s for s in (existing or "").split(",") if s]
    for sha in new.split(","):
        sha = sha.strip()
        if sha and sha not in shas:
            shas.append(sha)
    return ", ".join(shas) if shas else None


@dataclass
class Event:
    """A single run event. ``payload`` carries type-specific fields."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "ts": self.ts, **self.payload}


Listener = Callable[[Event], None]


def fan_out(listeners: list[Listener]) -> Listener:
    """Combine several listeners into one emit callable."""

    def emit(event: Event) -> None:
        for listener in listeners:
            listener(event)

    return emit


class RunWriter:
    """Persists one run's records. Thread-safe; one writer per run."""

    def __init__(
        self,
        run_dir: Path,
        run_id: str,
        launched_by: str,
        *,
        playlist: str | None = None,
    ) -> None:
        self.dir = run_dir
        self.run_id = run_id
        self._lock = threading.Lock()
        self._events: IO[str] = (run_dir / "events.jsonl").open("a", encoding="utf-8")
        self._logs: dict[str, IO[str]] = {}
        self._meta: dict[str, Any] = {
            "id": run_id,
            "launched_by": launched_by,
            "started_at": now_iso(),
            "finished_at": None,
            # Playlist provenance: the name of the playlist (queue) this run ran
            # against, or None for the main `.tasks` queue. History uses it to
            # tag a run with its playlist.
            "playlist": playlist,
            # The process driving this run (server or CLI), and the current
            # worker subprocess pid (set while a worker is live, else None).
            # reconcile_stale() uses both to avoid aborting a busy run.
            "pid": os.getpid(),
            "worker_pid": None,
        }
        self._write_meta()

    def _write_meta(self) -> None:
        (self.dir / "run.json").write_text(json.dumps(self._meta, indent=2))

    def emit(self, event: Event) -> None:
        with self._lock:
            self._events.write(json.dumps(event.to_dict()) + "\n")
            self._events.flush()
            if event.type == TASK_LOG:
                self._append_log(event)
            elif event.type == WORKER_STARTED:
                self._meta["worker_pid"] = event.payload.get("pid")
                self._write_meta()
            elif event.type == TASK_RESULT:
                # The worker for this task is done — drop its pid so a later
                # reconcile doesn't mistake a recycled pid for a live worker.
                self._meta["worker_pid"] = None
                self._write_meta()
            elif event.type == RUN_FINISHED:
                self._meta["finished_at"] = now_iso()
                self._meta["worker_pid"] = None
                self._write_meta()

    def _append_log(self, event: Event) -> None:
        task = event.payload.get("task")
        line = event.payload.get("line", "")
        if not task:
            return
        fh = self._logs.get(task)
        if fh is None:
            fh = (self.dir / f"{_safe_name(task)}.log").open("a", encoding="utf-8")
            self._logs[task] = fh
        fh.write(line)
        fh.flush()

    def close(self) -> None:
        with self._lock:
            for fh in self._logs.values():
                fh.close()
            self._logs.clear()
            if not self._events.closed:
                self._events.close()


class RunStore:
    """Reads/writes run records under ``<root>/<runs_rel>``.

    ``runs_rel`` defaults to the main queue's ``main/runs`` (relative to the
    content store ``tasks_root``); a playlist supplies ``<name>/runs`` so its
    history is kept separate from the main queue.
    """

    def __init__(self, root: Path, runs_rel: str = RUNS_DIR) -> None:
        self.root = root
        self.base = root / runs_rel
        # Memoizes the per-sha code-LOC churn computed on demand during read-time
        # backfill (see :meth:`_backfill_loc`). A landed sha is immutable, so the
        # figure never changes once computed; caching avoids re-running ``git
        # show`` for the same commit across every ``/api/runs`` poll.
        self._loc_cache: dict[str, int] = {}

    def start(
        self,
        launched_by: str,
        *,
        playlist: str | None = None,
    ) -> RunWriter:
        self.base.mkdir(parents=True, exist_ok=True)
        run_id = new_run_id()
        run_dir = self.base / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return RunWriter(
            run_dir,
            run_id,
            launched_by,
            playlist=playlist,
        )

    def list_runs(self) -> list[dict[str, Any]]:
        """All runs, newest first, each with reconstructed per-task records."""
        if not self.base.exists():
            return []
        runs: list[dict[str, Any]] = []
        for run_dir in sorted(self.base.iterdir(), reverse=True):
            if not run_dir.is_dir():
                continue
            record = self._read_run(run_dir)
            if record is not None:
                runs.append(record)
        return runs

    def latest_run_dir(self) -> Path | None:
        if not self.base.exists():
            return None
        dirs = [d for d in sorted(self.base.iterdir(), reverse=True) if d.is_dir()]
        return dirs[0] if dirs else None

    def _read_run(self, run_dir: Path) -> dict[str, Any] | None:
        meta_path = run_dir / "run.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            meta = {"id": run_dir.name}
        tasks = self._reconstruct_tasks(run_dir)
        # Display safety net: a task can't still be "running" once its run has
        # finished — the worker was interrupted before a terminal result was
        # recorded. Show it as "aborted" so it's flagged for investigation.
        # (reconcile_stale() persists this; this covers the gap before then.)
        if meta.get("finished_at"):
            for rec in tasks:
                if rec["status"] == "running":
                    rec["status"] = ABORTED
                    rec["failure_kind"] = rec["failure_kind"] or "aborted"
                    if not rec["result_line"]:
                        rec["result_line"] = ABORT_REASON_INTERRUPTED
        # Recover any missing lines-of-code figures from the commits each task
        # landed, so the Stats page reflects shipped work even for records that
        # predate the engine capturing ``loc`` at land time.
        for rec in tasks:
            self._backfill_loc(rec)
        return {
            "id": meta.get("id", run_dir.name),
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "launched_by": meta.get("launched_by"),
            "playlist": meta.get("playlist"),
            "aborted": bool(meta.get("aborted")),
            "abort_reason": meta.get("abort_reason"),
            "tasks": tasks,
        }

    def delete_run(self, run_id: str) -> bool:
        """Remove a run's record directory. Returns True if it existed and was
        deleted. Guards against path traversal: ``run_id`` must name a direct
        child of the runs directory."""
        if not self.base.exists():
            return False
        run_dir = (self.base / run_id).resolve()
        if run_dir.parent != self.base.resolve() or not run_dir.is_dir():
            return False
        shutil.rmtree(run_dir)
        return True

    def clear_runs(self, keep: set[str] | None = None) -> int:
        """Delete every run record except ids in ``keep``. Returns the count
        removed. Used by the UI's "clear completed" action."""
        if not self.base.exists():
            return 0
        keep = keep or set()
        removed = 0
        for run_dir in list(self.base.iterdir()):
            if run_dir.is_dir() and run_dir.name not in keep:
                shutil.rmtree(run_dir)
                removed += 1
        return removed

    def abort_run(self, run_id: str, reason: str = ABORT_REASON_NO_RUNNER) -> bool:
        """Mark a run as aborted: append a terminal ``aborted`` result for every
        still-running task, finish the run, and record the reason in run.json so
        it can be investigated. Idempotent. Returns True if anything changed."""
        run_dir = (self.base / run_id).resolve()
        if run_dir.parent != self.base.resolve() or not run_dir.is_dir():
            return False
        meta_path = run_dir / "run.json"
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            meta = {"id": run_id}

        running = [
            rec["task"]
            for rec in self._reconstruct_tasks(run_dir)
            if rec["status"] == "running"
        ]
        already_finished = bool(meta.get("finished_at"))
        if not running and already_finished and meta.get("aborted"):
            return False

        events_path = run_dir / "events.jsonl"
        with events_path.open("a", encoding="utf-8") as fh:
            for task in running:
                fh.write(
                    json.dumps(
                        {
                            "type": TASK_RESULT,
                            "ts": now_iso(),
                            "task": task,
                            "status": ABORTED,
                            "result_line": reason,
                            "error": reason,
                            "failure_kind": "aborted",
                        }
                    )
                    + "\n"
                )
            if not already_finished:
                fh.write(
                    json.dumps(
                        {"type": RUN_FINISHED, "ts": now_iso(), "run_id": run_id}
                    )
                    + "\n"
                )

        if not already_finished:
            meta["finished_at"] = now_iso()
        meta["aborted"] = True
        meta["abort_reason"] = reason
        meta_path.write_text(json.dumps(meta, indent=2))
        return True

    def find_task(self, run_id: str, task: str) -> dict[str, Any] | None:
        """The reconstructed record for one task in one run, or ``None``."""
        run_dir = (self.base / run_id).resolve()
        if run_dir.parent != self.base.resolve() or not run_dir.is_dir():
            return None
        for rec in self._reconstruct_tasks(run_dir):
            if rec["task"] == task:
                self._backfill_loc(rec)
                return rec
        return None

    def append_task_result(
        self,
        run_id: str,
        task: str,
        *,
        status: str,
        result_line: str = "",
        commit_sha: str | None = None,
        loc: int | None = None,
        error: str | None = None,
        recoverable: bool = False,
        failure_kind: str | None = None,
    ) -> bool:
        """Append a terminal ``task_result`` to a (typically finished) run so its
        history record reflects a later out-of-band outcome — e.g. a manual
        recovery that finally lands the squash. Returns True if written."""
        run_dir = (self.base / run_id).resolve()
        if run_dir.parent != self.base.resolve() or not run_dir.is_dir():
            return False
        event = {
            "type": TASK_RESULT,
            "ts": now_iso(),
            "task": task,
            "status": status,
            "result_line": result_line,
            "commit_sha": commit_sha,
            "loc": loc,
            "error": error,
            "recoverable": recoverable,
            "failure_kind": failure_kind,
        }
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
        return True

    def _worktree_mtime(self, tasks: list[str]) -> float:
        """Most recent mtime across the worktree dirs of ``tasks`` (0.0 if none
        exist). A freshly-touched worktree is evidence a worker is still active
        even if the event log is momentarily idle (a buffering ``claude -p``).
        Only a cheap top-level stat — used as a fallback for legacy records that
        predate pid tracking."""
        latest = 0.0
        worktrees = self.root / ".worktrees"
        for task in tasks:
            # Queue-namespaced (`task-local-<queue>-<task>`) and the legacy flat
            # (`task-local-<task>`) scheme — match either so a live worktree is
            # found regardless of which queue cut it.
            for wt in worktrees.glob(f"task-local-*-{task}"):
                latest = max(latest, _safe_mtime(wt))
            latest = max(latest, _safe_mtime(worktrees / f"task-local-{task}"))
        return latest

    def reconcile_stale(
        self, active_run_ids: set[str], stale_seconds: float = ABORT_STALE_SECONDS
    ) -> list[str]:
        """Find runs that look live (a running task) but aren't the active run,
        and persist them as aborted. ``active_run_ids`` are the run ids the
        caller knows are genuinely in progress (e.g. the player's current run).

        Liveness is judged conservatively, in order:
          * a finished run with a still-"running" task → interrupted (abort);
          * the driving process (``pid``) or the worker (``worker_pid``) is
            still alive → leave it, even if the log looks idle (workers buffer,
            and an orphaned worker can outlive a crashed server);
          * we had pid info and nothing is alive → no runner (abort);
          * legacy records with no pid info → fall back to activity idleness:
            abort only once *both* the event log and the worktrees have been
            idle past ``stale_seconds``.

        Returns the ids that were aborted."""
        if not self.base.exists():
            return []
        aborted: list[str] = []
        now = datetime.now(UTC).timestamp()
        for run_dir in self.base.iterdir():
            if not run_dir.is_dir() or run_dir.name in active_run_ids:
                continue
            meta_path = run_dir / "run.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("aborted"):
                continue
            running_tasks = [
                rec["task"]
                for rec in self._reconstruct_tasks(run_dir)
                if rec["status"] == "running"
            ]
            if not running_tasks:
                continue

            if meta.get("finished_at"):
                reason = ABORT_REASON_INTERRUPTED
            else:
                owner_pid = meta.get("pid")
                worker_pid = meta.get("worker_pid")
                if _pid_alive(owner_pid) or _pid_alive(worker_pid):
                    continue  # a runner or its (possibly orphaned) worker lives
                if owner_pid is not None or worker_pid is not None:
                    reason = ABORT_REASON_NO_RUNNER  # had pids; all dead
                else:
                    log_idle = now - _safe_mtime(run_dir / "events.jsonl")
                    tree_idle = now - self._worktree_mtime(running_tasks)
                    if min(log_idle, tree_idle) < stale_seconds:
                        continue  # recent log or worktree activity — leave it
                    reason = ABORT_REASON_NO_RUNNER
            if self.abort_run(run_dir.name, reason):
                aborted.append(run_dir.name)
        return aborted

    def _backfill_loc(self, rec: dict[str, Any]) -> None:
        """Fill in a completed record's missing lines-of-code figure from the
        commits it actually landed on ``main``.

        The engine records ``loc`` at land time, but records written before that
        feature existed — and any land where the figure wasn't captured — carry
        ``loc = None`` even though the task landed real commits. The Stats page
        sums ``loc`` across history, so those gaps make the total read zero
        despite work having shipped. Here we recover the figure from the record's
        own ``commit_sha`` list (the squash commits on ``main``, the source of
        truth), summing each commit's code churn the same way the engine does.

        Only completed records with a sha are touched, and only when ``loc`` is
        absent or zero — a captured non-zero figure is authoritative and left
        alone. Any git error leaves ``loc`` untouched so a missing figure never
        breaks history rendering."""
        if rec.get("status") != "completed":
            return
        if rec.get("loc"):
            return
        shas = [s.strip() for s in (rec.get("commit_sha") or "").split(",") if s.strip()]
        if not shas:
            return
        # Lazy import: the legacy runner imports this module, so importing its
        # sibling layer at module load risks a cycle.
        from nightshift.git.squash import compute_code_loc

        total = 0
        for sha in shas:
            cached = self._loc_cache.get(sha)
            if cached is None:
                cached = compute_code_loc(self.root, sha)
                self._loc_cache[sha] = cached
            total += cached
        if total:
            rec["loc"] = total

    def _reconstruct_tasks(self, run_dir: Path) -> list[dict[str, Any]]:
        """Fold the event log into one record per task (in first-seen order)."""
        events_path = run_dir / "events.jsonl"
        if not events_path.exists():
            return []
        order: list[str] = []
        records: dict[str, dict[str, Any]] = {}
        for raw in events_path.read_text(errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            task = event.get("task")
            if not task:
                continue
            rec = records.get(task)
            if rec is None:
                rec = {
                    "task": task,
                    "title": task,
                    "frontmatter": {},
                    # The brief prose captured at task start, so History can show
                    # the original brief after the task file is removed. Empty for
                    # runs recorded before this was captured.
                    "body": "",
                    "status": "running",
                    "phase": None,
                "result_line": "",
                "commit_sha": None,
                # Code lines churned by the landed commit (added + removed),
                # excluding build files, docs, and comments. None until a result
                # records it; the Stats page sums it across history.
                "loc": None,
                "error": None,
                "recoverable": False,
                # Classified failure category (None unless the task failed), so
                # the UI can show the cause without opening the log. One of:
                # merge_conflict, merge_rejected, validation_error, worker_error,
                # worker_launch, timeout, aborted, no_changes.
                "failure_kind": None,
                "log": f"{_safe_name(task)}.log",
                    "started_at": None,
                    "finished_at": None,
                    "phase_started_at": None,
                    "timings": None,
                }
                records[task] = rec
                order.append(task)
            self._apply_event(rec, event)
        return [records[name] for name in order]

    @staticmethod
    def _apply_event(rec: dict[str, Any], event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == TASK_STARTED:
            rec["title"] = event.get("title", rec["title"])
            rec["frontmatter"] = event.get("frontmatter", rec["frontmatter"])
            rec["body"] = event.get("body", rec["body"])
            rec["started_at"] = event.get("ts", rec["started_at"])
            rec["phase_started_at"] = event.get("ts", rec["phase_started_at"])
        elif etype == TASK_STATUS:
            rec["phase"] = event.get("phase", rec["phase"])
            # Each phase transition resets the live elapsed clock the UI shows.
            rec["phase_started_at"] = event.get("ts", rec["phase_started_at"])
            if event.get("status"):
                rec["status"] = event["status"]
        elif etype == TASK_RESULT:
            rec["status"] = event.get("status", rec["status"])
            # The result event marks the task's own finish (distinct from the
            # run's finish), so per-task duration/"when" don't collapse onto the
            # whole-run window.
            rec["finished_at"] = event.get("ts", rec["finished_at"])
            rec["result_line"] = event.get("result_line", rec["result_line"])
            # A task can land more than once over its lifetime (the initial run,
            # then a later resolve/recovery that squashes again). Accumulate each
            # landed sha as a comma-separated list rather than overwriting, so the
            # history commit field tracks every commit the task generated.
            rec["commit_sha"] = _merge_commit_shas(
                rec["commit_sha"], event.get("commit_sha")
            )
            if event.get("loc") is not None:
                rec["loc"] = event["loc"]
            rec["error"] = event.get("error", rec["error"])
            rec["recoverable"] = event.get("recoverable", rec["recoverable"])
            rec["failure_kind"] = event.get("failure_kind", rec["failure_kind"])
            if event.get("timings") is not None:
                rec["timings"] = event["timings"]

    def read_log(self, run_id: str, task: str, offset: int = 0) -> dict[str, Any]:
        """Return log text from ``offset`` plus the new end offset."""
        log_path = self.base / run_id / f"{_safe_name(task)}.log"
        if not log_path.exists():
            return {"text": "", "offset": offset, "eof": offset}
        data = log_path.read_text(errors="replace")
        text = data[offset:] if 0 <= offset <= len(data) else data
        return {"text": text, "offset": offset, "eof": len(data)}
