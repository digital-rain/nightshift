"""Manager state store — the Postgres ``nightshift`` schema, behind a Protocol.

Two implementations share one async interface (:class:`NightshiftStore`):

* :class:`PgStore` — the production store over a :class:`PgPoolLike` pool. Per
  ``.cursor/rules/no-inline-asyncpg.mdc`` it never imports ``asyncpg``; it only
  takes a structural pool. This is the canonical durable store.
* :class:`MemoryStore` — an in-process store with the same interface, used by
  unit tests (no live DB) and as a co-located fallback when ``NIGHTSHIFT_PG_DSN``
  is unset.

Both keep the same shapes so the manager service is identical regardless of
which is mounted. :func:`open_store` picks one from the environment.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, assert_never

from nightshift.lifecycle import (
    ATTEMPT_LIVE_STATES,
    ATTEMPT_TERMINAL_STATES,
    Outcome,
    Progress,
    Transition,
)
from nightshift.pg import PgPoolLike


def _now() -> datetime:
    return datetime.now(UTC)


# The attempt columns update_attempt may touch, derived from the shared
# models: every Outcome field that is an attempt column (landable is
# transport-only, consumed by the submit handler) plus the manager-computed
# state/land/progress columns. Unknown fields raise instead of being silently
# dropped. ``state`` is updatable here (the direct-write resolve path);
# transitions carry it separately (Transition.state) and their ``fields``
# must not.
ATTEMPT_UPDATABLE_FIELDS = frozenset(
    set(Outcome.model_fields) - {"landable", "status"}
) | {"state", "phase", "commit_sha", "loc", "remote", "pushed"}


def _check_attempt_fields(fields: dict[str, Any], *, allow_state: bool) -> None:
    allowed = ATTEMPT_UPDATABLE_FIELDS if allow_state else (
        ATTEMPT_UPDATABLE_FIELDS - {"state"}
    )
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"update_attempt: unknown field(s): {sorted(unknown)}")


def _stamp_terminal(row: dict[str, Any]) -> None:
    """Terminal ⇒ finished (invariant 4): stamp ``finished_at`` and
    ``released_at`` once when an in-memory attempt reaches a terminal state."""
    if row["state"] in ATTEMPT_TERMINAL_STATES:
        now = _now()
        row["finished_at"] = row.get("finished_at") or now
        row["released_at"] = row.get("released_at") or now


def _attempt_set_sql(
    fields: dict[str, Any], new_state: Any, *, start: int
) -> tuple[str, list[Any]]:
    """Assemble the attempt-row SET clause for validated ``fields`` (values
    fill ``$start..``), with the one-shot ``finished_at``/``released_at``
    stamps when ``new_state`` is terminal (invariant 4)."""
    sets: list[str] = []
    values: list[Any] = []
    for i, (key, value) in enumerate(fields.items(), start=start):
        sets.append(f"{key} = ${i}")
        values.append(value)
    if new_state in ATTEMPT_TERMINAL_STATES:
        sets.append("finished_at = COALESCE(finished_at, now())")
        sets.append("released_at = COALESCE(released_at, now())")
    return ", ".join(sets), values


# The task-overlay upsert shared by set_task_state and apply_transition (the
# transactional hold write): $5 is the repo, which submit-path holds never
# carry (repo pauses are set by the poll path only). Deliberately silent on
# the Phase 5 retry columns so a hold write never clobbers the persisted
# counter/backoff.
_TASK_UPSERT_SQL = """
    INSERT INTO nightshift.tasks
        (queue, task, state, blocked_reason, repo, retry_eligible, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, now())
    ON CONFLICT (queue, task) DO UPDATE SET
        state = EXCLUDED.state,
        blocked_reason = EXCLUDED.blocked_reason,
        repo = EXCLUDED.repo,
        retry_eligible = EXCLUDED.retry_eligible,
        updated_at = now()
"""

# Phase 5: the counter op a transition carries (TaskEffects.progress), applied
# in the same transaction as the lease CAS. INCREMENT upserts so a task with
# no overlay row gains a pure counter row (state NULL = no hold); the backoff
# stamp is NULL when the transition carries none (make_interval is strict).
_TASK_INCREMENT_SQL = """
    INSERT INTO nightshift.tasks
        (queue, task, state, blocked_reason, repo, retry_eligible,
         attempts_without_progress, next_eligible_at, updated_at)
    VALUES ($1, $2, NULL, NULL, NULL, false, 1,
            now() + make_interval(secs => $3), now())
    ON CONFLICT (queue, task) DO UPDATE SET
        attempts_without_progress = nightshift.tasks.attempts_without_progress + 1,
        next_eligible_at = now() + make_interval(secs => $3),
        updated_at = now()
"""

_TASK_RESET_SQL = """
    UPDATE nightshift.tasks
    SET attempts_without_progress = 0, next_eligible_at = NULL, updated_at = now()
    WHERE queue = $1 AND task = $2
"""

# Row-lifecycle invariant: a tasks row exists iff it carries a hold (state is
# non-NULL) or retry state (a nonzero counter / a pending backoff). Clearing a
# hold deletes the row only when it is otherwise clean; a dirty row is demoted
# to a pure counter row instead (state NULL), invisible to every state view.
_TASK_DELETE_IF_CLEAN_SQL = """
    DELETE FROM nightshift.tasks
    WHERE queue = $1 AND task = $2
      AND attempts_without_progress = 0 AND next_eligible_at IS NULL
"""

_TASK_DEMOTE_SQL = """
    UPDATE nightshift.tasks
    SET state = NULL, blocked_reason = NULL, repo = NULL,
        retry_eligible = false, updated_at = now()
    WHERE queue = $1 AND task = $2
"""

# clear_task_state's explicit-release variants: unlike the transition pair
# above, a release means "dispatchable now", so the delete ignores a pending
# backoff and the demote clears it (the counter alone keeps the row alive).
_TASK_RELEASE_DELETE_SQL = """
    DELETE FROM nightshift.tasks
    WHERE queue = $1 AND task = $2 AND attempts_without_progress = 0
"""

_TASK_RELEASE_DEMOTE_SQL = """
    UPDATE nightshift.tasks
    SET state = NULL, blocked_reason = NULL, repo = NULL,
        retry_eligible = false, next_eligible_at = NULL, updated_at = now()
    WHERE queue = $1 AND task = $2
"""

# The pre-Phase-5 row shape, projected by the wire-facing views (list_blocked
# feeds /api/blocked verbatim) so the new columns never leak into responses.
_TASK_VIEW_COLUMNS = (
    "queue", "task", "state", "blocked_reason", "repo", "retry_eligible",
    "updated_at",
)


# SQL fragments derived from the attempt vocabulary (StrEnum guarantees the
# rendering is byte-identical to the migration's literals). The live fragment
# must match the ``attempts_live_task_uniq`` partial-index predicate verbatim
# for ON CONFLICT index inference.
_ATTEMPT_LIVE_SQL = ", ".join(f"'{s}'" for s in sorted(ATTEMPT_LIVE_STATES))
_ATTEMPT_TERMINAL_SQL = ", ".join(f"'{s}'" for s in sorted(ATTEMPT_TERMINAL_STATES))

# Row-lifecycle invariant for the durable transport state (Phase 7): a
# queue_state row exists only while it carries a pause or a non-default mode.
_QUEUE_STATE_PRUNE_SQL = """
    DELETE FROM nightshift.queue_state
    WHERE queue = $1 AND paused_reason IS NULL AND mode IS NULL
"""


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #


class NightshiftStore(Protocol):
    """Async CRUD the manager needs. Both backends implement this exactly."""

    async def init(self) -> None: ...
    async def close(self) -> None: ...

    # workers
    async def register_worker(
        self,
        worker_id: str,
        *,
        backend: str,
        queues: list[str] | None,
        priorities: list[int] | None,
        models: list[str] | None = None,
        mcps: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    async def set_worker_status(
        self,
        worker_id: str,
        *,
        status: str,
        current_task: str | None = None,
        current_queue: str | None = None,
        current_run_id: str | None = None,
    ) -> None: ...
    async def heartbeat_worker(self, worker_id: str) -> None: ...
    async def list_workers(self) -> list[dict[str, Any]]: ...
    async def get_worker(self, worker_id: str) -> dict[str, Any] | None: ...
    async def expire_stale_workers(self, ttl_seconds: float) -> list[str]: ...

    # attempts (Phase 8: the merged lease+run entity)
    async def create_attempt(
        self,
        attempt_id: str,
        *,
        task: str,
        queue: str | None,
        worker_id: str | None,
        backend: str | None,
        model: str | None,
        base_ref: str | None,
        ttl_seconds: float,
        title: str | None = None,
        body: str | None = None,
        required_mcps: list[str] | None = None,
        repo: str | None = None,
        validate_cmd: str | None = None,
        state: str = "running",
    ) -> dict[str, Any] | None: ...
    async def get_attempt(self, attempt_id: str) -> dict[str, Any] | None: ...
    async def update_attempt(self, attempt_id: str, **fields: Any) -> None: ...
    async def live_attempts(self) -> list[dict[str, Any]]: ...
    async def list_attempts(
        self, *, limit: int = 200, queue: str | None = None, worker_id: str | None = None
    ) -> list[dict[str, Any]]: ...
    async def heartbeat_attempt(self, attempt_id: str, ttl_seconds: float) -> None: ...

    # transitions (the one write path for lifecycle state changes)
    async def apply_transition(
        self,
        t: Transition,
        *,
        expected_status: str,
        expected_worker_id: str | None = None,
    ) -> list[int] | None: ...

    # task state overlay
    async def set_task_state(
        self,
        queue: str | None,
        task: str,
        state: str,
        *,
        blocked_reason: str | None = None,
        repo: str | None = None,
        retry_eligible: bool = False,
    ) -> None: ...
    async def get_task_state(self, queue: str | None, task: str) -> dict[str, Any] | None: ...
    async def list_blocked(self) -> list[dict[str, Any]]: ...
    async def tasks_in_state(self, state: str) -> list[dict[str, Any]]: ...
    async def retryable_tasks(self, queue: str | None) -> list[dict[str, Any]]: ...
    async def clear_task_state(
        self, queue: str | None, task: str, *, reset_progress: bool = False
    ) -> None: ...
    # retry backoff (Phase 5)
    async def tasks_backing_off(self) -> list[dict[str, Any]]: ...
    async def clear_task_backoff(self, queue: str | None, task: str) -> None: ...

    # queue dedication (manager-side queue -> worker binding)
    async def queue_dedication(self) -> dict[str, list[str]]: ...
    async def set_queue_dedication(
        self, queue_label: str, worker_ids: list[str]
    ) -> None: ...

    # queue transport state (Phase 7: durable pause reasons + playback modes,
    # keyed by queue LABEL — a manager restart no longer unpauses queues)
    async def queue_pauses(self) -> dict[str, str]: ...
    async def set_queue_pause(self, queue_label: str, reason: str | None) -> None: ...
    async def queue_modes(self) -> dict[str, str]: ...
    async def set_queue_mode(self, queue_label: str, mode: str | None) -> None: ...

    # queue rename (migrate every row keyed on a queue name)
    async def rename_queue(self, old: str, new: str) -> None: ...

    # events
    async def append_event(
        self,
        kind: str,
        *,
        run_id: str | None = None,
        queue: str | None = None,
        task: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int: ...
    async def events_since(self, cursor: int, *, limit: int = 500) -> list[dict[str, Any]]: ...
    async def max_event_id(self) -> int: ...
    async def run_events(self, run_id: str) -> list[dict[str, Any]]: ...

    # stats
    async def stats_overall(self) -> dict[str, Any]: ...
    async def stats_by_worker(self) -> list[dict[str, Any]]: ...
    async def stats_by_backend(self) -> list[dict[str, Any]]: ...
    async def stats_by_model(self) -> list[dict[str, Any]]: ...
    async def stats_by_queue(self) -> list[dict[str, Any]]: ...


def _qkey(queue: str | None) -> str:
    """Normalize a queue name to its storage key ('' = main)."""
    return queue or ""


# --------------------------------------------------------------------------- #
# In-memory store (tests / co-located fallback)
# --------------------------------------------------------------------------- #


class MemoryStore:
    """Thread-safe in-process store. Same interface as :class:`PgStore`.

    Useful for unit tests (no DB) and a single-machine co-located deployment
    where standing up Postgres would be overkill. State is lost on restart.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workers: dict[str, dict[str, Any]] = {}
        self._attempts: dict[str, dict[str, Any]] = {}
        self._tasks: dict[tuple[str, str], dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._event_seq = 0
        # queue label -> bound worker ids (manager-side dedication)
        self._dedication: dict[str, list[str]] = {}
        # queue label -> {"paused_reason", "mode"} (durable transport state;
        # a row exists only while it carries a pause or a non-default mode)
        self._queue_state: dict[str, dict[str, str | None]] = {}

    async def init(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # ---- workers ---------------------------------------------------------- #

    async def register_worker(
        self,
        worker_id: str,
        *,
        backend: str,
        queues: list[str] | None,
        priorities: list[int] | None,
        models: list[str] | None = None,
        mcps: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            now = _now()
            existing = self._workers.get(worker_id)
            registered = existing["registered_at"] if existing else now
            row = {
                "id": worker_id,
                "backend": backend,
                "queues": queues,
                "priorities": priorities,
                "models": models or [],
                "mcps": mcps or [],
                "status": "idle",
                "current_task": None,
                "current_queue": None,
                "current_run_id": None,
                "registered_at": registered,
                "last_checkin_at": now,
                "last_heartbeat_at": now,
                "meta": meta or {},
            }
            self._workers[worker_id] = row
            return dict(row)

    async def set_worker_status(
        self,
        worker_id: str,
        *,
        status: str,
        current_task: str | None = None,
        current_queue: str | None = None,
        current_run_id: str | None = None,
    ) -> None:
        with self._lock:
            row = self._workers.get(worker_id)
            if row is None:
                return
            row["status"] = status
            row["current_task"] = current_task
            row["current_queue"] = current_queue
            row["current_run_id"] = current_run_id
            row["last_heartbeat_at"] = _now()

    async def heartbeat_worker(self, worker_id: str) -> None:
        with self._lock:
            row = self._workers.get(worker_id)
            if row is not None:
                row["last_heartbeat_at"] = _now()

    async def list_workers(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in sorted(self._workers.values(), key=lambda r: r["id"])]

    async def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._workers.get(worker_id)
            return dict(row) if row else None

    async def expire_stale_workers(self, ttl_seconds: float) -> list[str]:
        with self._lock:
            cutoff = _now() - timedelta(seconds=ttl_seconds)
            stale = [
                wid
                for wid, r in self._workers.items()
                if r["status"] != "offline" and r["last_heartbeat_at"] < cutoff
            ]
            for wid in stale:
                self._workers[wid]["status"] = "offline"
            return stale

    # ---- attempts ---------------------------------------------------------- #

    async def create_attempt(
        self,
        attempt_id: str,
        *,
        task: str,
        queue: str | None,
        worker_id: str | None,
        backend: str | None,
        model: str | None,
        base_ref: str | None,
        ttl_seconds: float,
        title: str | None = None,
        body: str | None = None,
        required_mcps: list[str] | None = None,
        repo: str | None = None,
        validate_cmd: str | None = None,
        state: str = "running",
    ) -> dict[str, Any] | None:
        """Create one attempt row. When ``state`` is live, refuses (returns
        ``None``) while another live attempt exists for (queue, task) —
        mirroring the PG partial unique index (invariant 1). Resolve children
        pass ``state="resolving"`` (not live; no deadline)."""
        with self._lock:
            qk = _qkey(queue)
            if state in ATTEMPT_LIVE_STATES:
                for row in self._attempts.values():
                    if (
                        row["queue"] == qk
                        and row["task"] == task
                        and row["state"] in ATTEMPT_LIVE_STATES
                    ):
                        return None
            now = _now()
            row = {
                "id": attempt_id,
                "task": task,
                "queue": qk,
                "worker_id": worker_id,
                "backend": backend,
                "model": model,
                "repo": repo,
                "required_mcps": required_mcps or [],
                "state": state,
                "phase": None,
                "result_line": None,
                "commit_sha": None,
                "loc": None,
                "remote": None,
                "pushed": None,
                "turns": None,
                "input_tokens": None,
                "output_tokens": None,
                "cost_usd": None,
                "failure_kind": None,
                "failure_reason": None,
                "validate_cmd": validate_cmd,
                "worktree": None,
                "title": title,
                "body": body,
                "started_at": now,
                "finished_at": None,
                # lease-side columns
                "base_ref": base_ref,
                "acquired_at": now,
                "heartbeat_at": now,
                "deadline_at": (
                    now + timedelta(seconds=ttl_seconds)
                    if state in ATTEMPT_LIVE_STATES
                    else None
                ),
                "released_at": None,
                "branch_ref": None,
                "head_sha": None,
            }
            self._attempts[attempt_id] = row
            return dict(row)

    async def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._attempts.get(attempt_id)
            return dict(row) if row else None

    async def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        _check_attempt_fields(fields, allow_state=True)
        with self._lock:
            row = self._attempts.get(attempt_id)
            if row is None:
                return
            if fields.get("state") in ATTEMPT_LIVE_STATES:
                # Mirror PG's attempts_live_task_uniq partial index: setting a
                # live state must not create a second live attempt for the
                # task (invariant 1). Same-row moves (an already-live row
                # going RUNNING→LANDING) pass — the check excludes the row
                # being updated.
                for other_id, other in self._attempts.items():
                    if (
                        other_id != attempt_id
                        and other["queue"] == row["queue"]
                        and other["task"] == row["task"]
                        and other["state"] in ATTEMPT_LIVE_STATES
                    ):
                        raise ValueError(
                            "update_attempt: a live attempt already exists "
                            f"for ({row['queue']!r}, {row['task']!r})"
                        )
            row.update(fields)
            _stamp_terminal(row)

    async def live_attempts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(r)
                for r in self._attempts.values()
                if r["state"] in ATTEMPT_LIVE_STATES
            ]

    async def list_attempts(
        self, *, limit: int = 200, queue: str | None = None, worker_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._attempts.values())
        if queue is not None:
            rows = [r for r in rows if r["queue"] == _qkey(queue)]
        if worker_id is not None:
            rows = [r for r in rows if r["worker_id"] == worker_id]
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    async def heartbeat_attempt(self, attempt_id: str, ttl_seconds: float) -> None:
        with self._lock:
            row = self._attempts.get(attempt_id)
            if row is None or row["state"] not in ATTEMPT_LIVE_STATES:
                return
            now = _now()
            row["heartbeat_at"] = now
            row["deadline_at"] = now + timedelta(seconds=ttl_seconds)

    # ---- transitions ------------------------------------------------------ #

    async def apply_transition(
        self,
        t: Transition,
        *,
        expected_status: str,
        expected_worker_id: str | None = None,
    ) -> list[int] | None:
        """Apply one lifecycle transition atomically (under the store lock).

        CAS: the attempt must exist in ``expected_status`` (and, when given,
        belong to ``expected_worker_id`` — the submit fence); otherwise nothing
        is written and ``None`` is returned (invariant 2). On success the
        attempt row, task overlay, and events all change together (invariant
        3); the inserted event ids are returned so the caller can broadcast
        exactly what committed.
        """
        # Validate before mutating so a bad field set can't leave partial state.
        _check_attempt_fields(t.fields, allow_state=False)
        with self._lock:
            row = self._attempts.get(t.ref.id)
            if row is None or row["state"] != expected_status:
                return None
            if expected_worker_id is not None and row["worker_id"] != expected_worker_id:
                return None
            row["state"] = t.state
            if t.fields:
                row.update(t.fields)
            _stamp_terminal(row)
            key = (_qkey(t.ref.queue), t.ref.task)
            hold = t.effects.hold
            if hold is not None:
                prior = self._tasks.get(key)
                self._tasks[key] = {
                    "queue": key[0],
                    "task": t.ref.task,
                    "state": hold.kind,
                    "blocked_reason": hold.reason,
                    "repo": None,
                    "retry_eligible": hold.retry_eligible,
                    # A hold write never clobbers the retry counter/backoff.
                    "attempts_without_progress": (
                        prior["attempts_without_progress"] if prior else 0
                    ),
                    "next_eligible_at": prior["next_eligible_at"] if prior else None,
                    "updated_at": _now(),
                }
            self._apply_progress(key, t.effects.progress, t.effects.next_eligible_in)
            if hold is None and t.effects.clear_hold:
                task_row = self._tasks.get(key)
                if task_row is not None:
                    if (
                        task_row["attempts_without_progress"] == 0
                        and task_row["next_eligible_at"] is None
                    ):
                        self._tasks.pop(key)
                    else:
                        # Demote to a pure counter row (state NULL = no hold).
                        task_row.update(
                            state=None, blocked_reason=None, repo=None,
                            retry_eligible=False, updated_at=_now(),
                        )
            ids: list[int] = []
            for ev in t.events:
                self._event_seq += 1
                self._events.append({
                    "id": self._event_seq,
                    "kind": ev.kind,
                    "run_id": ev.run_id,
                    "queue": ev.queue,
                    "task": ev.task,
                    "payload": dict(ev.payload or {}),
                    "ts": _now(),
                })
                ids.append(self._event_seq)
            return ids

    # ---- task state ------------------------------------------------------- #

    def _apply_progress(
        self,
        key: tuple[str, str],
        progress: Progress,
        next_eligible_in: float | None,
    ) -> None:
        """Apply a transition's counter op (caller holds the lock). Mirrors
        the PG ``_TASK_INCREMENT_SQL`` / ``_TASK_RESET_SQL`` statements."""
        match progress:
            case Progress.NONE:
                return
            case Progress.INCREMENT:
                row = self._tasks.get(key)
                if row is None:
                    row = {
                        "queue": key[0],
                        "task": key[1],
                        "state": None,
                        "blocked_reason": None,
                        "repo": None,
                        "retry_eligible": False,
                        "attempts_without_progress": 0,
                        "next_eligible_at": None,
                        "updated_at": _now(),
                    }
                    self._tasks[key] = row
                row["attempts_without_progress"] += 1
                row["next_eligible_at"] = (
                    _now() + timedelta(seconds=next_eligible_in)
                    if next_eligible_in is not None
                    else None
                )
                row["updated_at"] = _now()
            case Progress.RESET:
                row = self._tasks.get(key)
                if row is not None:
                    row["attempts_without_progress"] = 0
                    row["next_eligible_at"] = None
                    row["updated_at"] = _now()
            case _:
                assert_never(progress)

    async def set_task_state(
        self,
        queue: str | None,
        task: str,
        state: str,
        *,
        blocked_reason: str | None = None,
        repo: str | None = None,
        retry_eligible: bool = False,
    ) -> None:
        with self._lock:
            key = (_qkey(queue), task)
            prior = self._tasks.get(key)
            self._tasks[key] = {
                "queue": key[0],
                "task": task,
                "state": state,
                "blocked_reason": blocked_reason,
                "repo": repo,
                "retry_eligible": retry_eligible,
                # Preserved on upsert, like _TASK_UPSERT_SQL's ON CONFLICT.
                "attempts_without_progress": (
                    prior["attempts_without_progress"] if prior else 0
                ),
                "next_eligible_at": prior["next_eligible_at"] if prior else None,
                "updated_at": _now(),
            }

    async def get_task_state(self, queue: str | None, task: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._tasks.get((_qkey(queue), task))
            return dict(row) if row else None

    async def list_blocked(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {k: r[k] for k in _TASK_VIEW_COLUMNS}
                for r in self._tasks.values()
                if r["state"] == "blocked"
            ]

    async def tasks_in_state(self, state: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "queue": r["queue"],
                    "task": r["task"],
                    "state": r["state"],
                    "repo": r.get("repo"),
                    "blocked_reason": r.get("blocked_reason"),
                }
                for r in self._tasks.values()
                if r["state"] == state
            ]

    async def retryable_tasks(self, queue: str | None) -> list[dict[str, Any]]:
        with self._lock:
            qk = _qkey(queue)
            return [
                {
                    "queue": r["queue"], "task": r["task"], "state": r["state"],
                    "blocked_reason": r.get("blocked_reason"),
                }
                for r in self._tasks.values()
                if r["queue"] == qk
                and (r["state"] == "failed" or (r["state"] == "blocked" and r.get("retry_eligible")))
            ]

    async def clear_task_state(
        self, queue: str | None, task: str, *, reset_progress: bool = False
    ) -> None:
        """Clear a task's hold. ``reset_progress`` (a landed resolve — real
        progress) also zeroes the retry counter, deleting the row outright;
        otherwise the counter survives on a demoted row while the backoff is
        cleared (an explicit release means "dispatchable now")."""
        with self._lock:
            key = (_qkey(queue), task)
            row = self._tasks.get(key)
            if row is None:
                return
            if reset_progress or row["attempts_without_progress"] == 0:
                self._tasks.pop(key)
            else:
                row.update(
                    state=None, blocked_reason=None, repo=None,
                    retry_eligible=False, next_eligible_at=None,
                    updated_at=_now(),
                )

    async def tasks_backing_off(self) -> list[dict[str, Any]]:
        with self._lock:
            now = _now()
            return [
                {
                    "queue": r["queue"], "task": r["task"],
                    "next_eligible_at": r["next_eligible_at"],
                }
                for r in self._tasks.values()
                if r["next_eligible_at"] is not None and r["next_eligible_at"] > now
            ]

    async def clear_task_backoff(self, queue: str | None, task: str) -> None:
        with self._lock:
            row = self._tasks.get((_qkey(queue), task))
            if row is not None and row["next_eligible_at"] is not None:
                row["next_eligible_at"] = None
                row["updated_at"] = _now()

    # ---- queue dedication ------------------------------------------------- #

    async def queue_dedication(self) -> dict[str, list[str]]:
        with self._lock:
            return {q: list(w) for q, w in self._dedication.items() if w}

    async def set_queue_dedication(
        self, queue_label: str, worker_ids: list[str]
    ) -> None:
        with self._lock:
            cleaned = [w for w in worker_ids if w]
            if cleaned:
                self._dedication[queue_label] = cleaned
            else:
                self._dedication.pop(queue_label, None)

    # ---- queue transport state (Phase 7) ----------------------------------- #

    def _queue_state_row(self, queue_label: str) -> dict[str, str | None]:
        return self._queue_state.setdefault(
            queue_label, {"paused_reason": None, "mode": None}
        )

    def _prune_queue_state(self, queue_label: str) -> None:
        row = self._queue_state.get(queue_label)
        if row is not None and row["paused_reason"] is None and row["mode"] is None:
            self._queue_state.pop(queue_label, None)

    async def queue_pauses(self) -> dict[str, str]:
        with self._lock:
            return {
                q: row["paused_reason"]
                for q, row in self._queue_state.items()
                if row["paused_reason"] is not None
            }

    async def set_queue_pause(self, queue_label: str, reason: str | None) -> None:
        with self._lock:
            self._queue_state_row(queue_label)["paused_reason"] = reason
            self._prune_queue_state(queue_label)

    async def queue_modes(self) -> dict[str, str]:
        with self._lock:
            return {
                q: row["mode"]
                for q, row in self._queue_state.items()
                if row["mode"] is not None
            }

    async def set_queue_mode(self, queue_label: str, mode: str | None) -> None:
        with self._lock:
            self._queue_state_row(queue_label)["mode"] = mode
            self._prune_queue_state(queue_label)

    # ---- queue rename ----------------------------------------------------- #

    async def rename_queue(self, old: str, new: str) -> None:
        """Repoint every queue-keyed row from ``old`` to ``new`` (playlists only;
        the main queue is never renamed)."""
        if not old or old == new:
            return
        with self._lock:
            ok, nk = _qkey(old), _qkey(new)
            for attempt in self._attempts.values():
                if attempt["queue"] == ok:
                    attempt["queue"] = nk
            for event in self._events:
                if event.get("queue") == old:
                    event["queue"] = new
            for key, row in list(self._tasks.items()):
                if row["queue"] == ok:
                    row["queue"] = nk
                    self._tasks[(nk, row["task"])] = self._tasks.pop(key)
            if old in self._dedication:
                self._dedication[new] = self._dedication.pop(old)
            if old in self._queue_state:
                self._queue_state[new] = self._queue_state.pop(old)

    # ---- events ----------------------------------------------------------- #

    async def append_event(
        self,
        kind: str,
        *,
        run_id: str | None = None,
        queue: str | None = None,
        task: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            self._event_seq += 1
            row = {
                "id": self._event_seq,
                "kind": kind,
                "run_id": run_id,
                "queue": queue,
                "task": task,
                "payload": payload or {},
                "ts": _now(),
            }
            self._events.append(row)
            return self._event_seq

    async def events_since(self, cursor: int, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._events if e["id"] > cursor][:limit]

    async def max_event_id(self) -> int:
        with self._lock:
            return self._event_seq

    async def run_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._events if e["run_id"] == run_id]

    # ---- stats ------------------------------------------------------------ #

    async def stats_overall(self) -> dict[str, Any]:
        with self._lock:
            runs = list(self._attempts.values())
        return _aggregate(runs)

    async def stats_by_worker(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._attempts.values())
        return _group_stats(runs, "worker_id")

    async def stats_by_backend(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._attempts.values())
        return _group_stats(runs, "backend")

    async def stats_by_model(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._attempts.values())
        return _group_stats(runs, "model")

    async def stats_by_queue(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._attempts.values())
        return _group_stats(runs, "queue")


# Stats bucket sets over attempt states — the wire-status projection applied
# to aggregation (keep in lockstep with the recreated SQL stats views in
# migration 20260731000004). "expired" is deliberately unbucketed, exactly as
# the zombie expired runs never were pre-phase.
_STATS_COMPLETED = frozenset({"landed", "no_change"})
_STATS_ERRORED = frozenset({"failed", "conflict"})


def _aggregate(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    completed = [r for r in runs if r["state"] in _STATS_COMPLETED]
    durations = [
        (r["finished_at"] - r["started_at"]).total_seconds()
        for r in runs
        if r.get("finished_at") and r.get("started_at")
    ]
    # Telemetry counts over *all* runs (a failed attempt still burns turns/tokens);
    # LOC stays completed-only. Mirrors the SQL stats views.
    turn_vals = [int(r["turns"]) for r in runs if r.get("turns") is not None]
    cost_vals = [float(r["cost_usd"]) for r in runs if r.get("cost_usd") is not None]
    total_input = sum(int(r["input_tokens"] or 0) for r in runs)
    total_output = sum(int(r["output_tokens"] or 0) for r in runs)
    return {
        "total_runs": len(runs),
        "completed": len(completed),
        "errored": sum(1 for r in runs if r["state"] in _STATS_ERRORED),
        "aborted": sum(1 for r in runs if r["state"] == "aborted"),
        "skipped": sum(1 for r in runs if r["state"] == "skipped"),
        "total_loc": sum(int(r["loc"] or 0) for r in completed),
        "avg_seconds": (sum(durations) / len(durations)) if durations else 0.0,
        "total_turns": sum(turn_vals),
        "avg_turns": (sum(turn_vals) / len(turn_vals)) if turn_vals else 0.0,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_cost_usd": sum(cost_vals),
    }


def _group_stats(runs: Sequence[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        gk = r.get(key)
        if gk is None:
            continue
        groups.setdefault(gk, []).append(r)
    out: list[dict[str, Any]] = []
    for gk, items in sorted(groups.items()):
        agg = _aggregate(items)
        agg[key] = gk
        out.append(agg)
    return out


# --------------------------------------------------------------------------- #
# Postgres store (PgPoolLike — no direct asyncpg)
# --------------------------------------------------------------------------- #


class PgStore:
    """Durable store over a structural :class:`PgPoolLike` pool.

    Never imports ``asyncpg`` (Invariant 3 / ``no-inline-asyncpg``); the pool is
    handed in. Rows come back as mappings; we coerce to plain dicts so callers
    don't depend on the driver's record type.
    """

    def __init__(self, pool: PgPoolLike) -> None:
        self._pool = pool

    async def init(self) -> None:
        return None

    async def close(self) -> None:
        await self._pool.close()

    async def register_worker(
        self,
        worker_id: str,
        *,
        backend: str,
        queues: list[str] | None,
        priorities: list[int] | None,
        models: list[str] | None = None,
        mcps: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO nightshift.workers
                    (id, backend, queues, priorities, models, mcps, status, meta,
                     registered_at, last_checkin_at, last_heartbeat_at)
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6::jsonb,
                        'idle', $7::jsonb, now(), now(), now())
                ON CONFLICT (id) DO UPDATE SET
                    backend = EXCLUDED.backend,
                    queues = EXCLUDED.queues,
                    priorities = EXCLUDED.priorities,
                    models = EXCLUDED.models,
                    mcps = EXCLUDED.mcps,
                    status = 'idle',
                    meta = EXCLUDED.meta,
                    last_checkin_at = now(),
                    last_heartbeat_at = now()
                RETURNING *
                """,
                worker_id,
                backend,
                json.dumps(queues) if queues is not None else None,
                json.dumps(priorities) if priorities is not None else None,
                json.dumps(models or []),
                json.dumps(mcps or []),
                json.dumps(meta or {}),
            )
            return _worker_row(row)

    async def set_worker_status(
        self,
        worker_id: str,
        *,
        status: str,
        current_task: str | None = None,
        current_queue: str | None = None,
        current_run_id: str | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE nightshift.workers
                SET status = $2, current_task = $3, current_queue = $4,
                    current_run_id = $5, last_heartbeat_at = now()
                WHERE id = $1
                """,
                worker_id, status, current_task, current_queue, current_run_id,
            )

    async def heartbeat_worker(self, worker_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE nightshift.workers SET last_heartbeat_at = now() WHERE id = $1",
                worker_id,
            )

    async def list_workers(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM nightshift.workers ORDER BY id")
        return [_worker_row(r) for r in rows]

    async def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM nightshift.workers WHERE id = $1", worker_id
            )
        return _worker_row(row) if row else None

    async def expire_stale_workers(self, ttl_seconds: float) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE nightshift.workers
                SET status = 'offline'
                WHERE status <> 'offline'
                  AND last_heartbeat_at < now() - ($1 || ' seconds')::interval
                RETURNING id
                """,
                str(int(ttl_seconds)),
            )
        return [r["id"] for r in rows]

    async def create_attempt(
        self,
        attempt_id: str,
        *,
        task: str,
        queue: str | None,
        worker_id: str | None,
        backend: str | None,
        model: str | None,
        base_ref: str | None,
        ttl_seconds: float,
        title: str | None = None,
        body: str | None = None,
        required_mcps: list[str] | None = None,
        repo: str | None = None,
        validate_cmd: str | None = None,
        state: str = "running",
    ) -> dict[str, Any] | None:
        # The ON CONFLICT predicate must match the partial unique index
        # (attempts_live_task_uniq) in migration 20260731000004 *verbatim*
        # for index inference (invariant 1: one live attempt per task).
        # A non-live attempt (resolve child) never trips it and carries no
        # deadline.
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO nightshift.attempts
                    (id, task, queue, worker_id, backend, model, repo,
                     required_mcps, validate_cmd, state, title, body,
                     base_ref, started_at, acquired_at, heartbeat_at, deadline_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11,
                        $12, $13, now(), now(), now(),
                        CASE WHEN $10 IN ({_ATTEMPT_LIVE_SQL})
                             THEN now() + ($14 || ' seconds')::interval END)
                ON CONFLICT (queue, task) WHERE state IN ({_ATTEMPT_LIVE_SQL})
                DO NOTHING
                RETURNING *
                """,
                attempt_id, task, _qkey(queue), worker_id, backend, model, repo,
                json.dumps(required_mcps or []), validate_cmd, state, title, body,
                base_ref, str(int(ttl_seconds)),
            )
        return _attempt_row(row) if row else None

    async def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM nightshift.attempts WHERE id = $1", attempt_id
            )
        return _attempt_row(row) if row else None

    async def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        if not fields:
            return
        _check_attempt_fields(fields, allow_state=True)
        sets, values = _attempt_set_sql(fields, fields.get("state"), start=2)
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE nightshift.attempts SET {sets} WHERE id = $1",
                attempt_id, *values,
            )

    async def live_attempts(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM nightshift.attempts WHERE state IN ({_ATTEMPT_LIVE_SQL})"
            )
        return [_attempt_row(r) for r in rows]

    async def list_attempts(
        self, *, limit: int = 200, queue: str | None = None, worker_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if queue is not None:
            values.append(_qkey(queue))
            clauses.append(f"queue = ${len(values)}")
        if worker_id is not None:
            values.append(worker_id)
            clauses.append(f"worker_id = ${len(values)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM nightshift.attempts {where} "
                f"ORDER BY started_at DESC LIMIT ${len(values)}",
                *values,
            )
        return [_attempt_row(r) for r in rows]

    async def heartbeat_attempt(self, attempt_id: str, ttl_seconds: float) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE nightshift.attempts
                SET heartbeat_at = now(),
                    deadline_at = now() + ($2 || ' seconds')::interval
                WHERE id = $1 AND state IN ({_ATTEMPT_LIVE_SQL})
                """,
                attempt_id, str(int(ttl_seconds)),
            )

    async def apply_transition(
        self,
        t: Transition,
        *,
        expected_status: str,
        expected_worker_id: str | None = None,
    ) -> list[int] | None:
        """Apply one lifecycle transition in a single transaction.

        The CAS is the guarded attempt UPDATE: it matches only while the
        attempt is in ``expected_status`` (and owned by ``expected_worker_id``
        when given). No match → rollback, no writes, ``None`` (invariant 2).
        Events are inserted in the same transaction (outbox, invariant 3);
        their ids come back for post-commit SSE.
        """
        _check_attempt_fields(t.fields, allow_state=False)
        sets, values = _attempt_set_sql(t.fields, t.state, start=5)
        extra = f", {sets}" if sets else ""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE nightshift.attempts
                SET state = $2{extra}
                WHERE id = $1 AND state = $3
                  AND ($4::text IS NULL OR worker_id = $4)
                RETURNING id
                """,
                t.ref.id, t.state, expected_status, expected_worker_id, *values,
            )
            if row is None:
                return None
            qk, task = _qkey(t.ref.queue), t.ref.task
            hold = t.effects.hold
            if hold is not None:
                await conn.execute(
                    _TASK_UPSERT_SQL,
                    qk, task, hold.kind, hold.reason, None, hold.retry_eligible,
                )
            match t.effects.progress:
                case Progress.INCREMENT:
                    await conn.execute(
                        _TASK_INCREMENT_SQL, qk, task, t.effects.next_eligible_in
                    )
                case Progress.RESET:
                    await conn.execute(_TASK_RESET_SQL, qk, task)
                case Progress.NONE:
                    pass
                case _:
                    assert_never(t.effects.progress)
            if hold is None and t.effects.clear_hold:
                await conn.execute(_TASK_DELETE_IF_CLEAN_SQL, qk, task)
                await conn.execute(_TASK_DEMOTE_SQL, qk, task)
            ids: list[int] = []
            for ev in t.events:
                ids.append(await conn.fetchval(
                    """
                    INSERT INTO nightshift.events (kind, run_id, queue, task, payload)
                    VALUES ($1, $2, $3, $4, $5::jsonb)
                    RETURNING id
                    """,
                    ev.kind, ev.run_id, ev.queue, ev.task, json.dumps(ev.payload or {}),
                ))
            return ids

    async def set_task_state(
        self,
        queue: str | None,
        task: str,
        state: str,
        *,
        blocked_reason: str | None = None,
        repo: str | None = None,
        retry_eligible: bool = False,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                _TASK_UPSERT_SQL,
                _qkey(queue), task, state, blocked_reason, repo, retry_eligible,
            )

    async def get_task_state(self, queue: str | None, task: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM nightshift.tasks WHERE queue = $1 AND task = $2",
                _qkey(queue), task,
            )
        return dict(row) if row else None

    async def list_blocked(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {', '.join(_TASK_VIEW_COLUMNS)} "
                "FROM nightshift.tasks WHERE state = 'blocked'"
            )
        return [dict(r) for r in rows]

    async def tasks_in_state(self, state: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT queue, task, state, repo, blocked_reason FROM nightshift.tasks WHERE state = $1",
                state,
            )
        return [dict(r) for r in rows]

    async def retryable_tasks(self, queue: str | None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT queue, task, state, blocked_reason FROM nightshift.tasks
                WHERE queue = $1 AND (state = 'failed' OR (state = 'blocked' AND retry_eligible))
                """,
                _qkey(queue),
            )
        return [dict(r) for r in rows]

    async def clear_task_state(
        self, queue: str | None, task: str, *, reset_progress: bool = False
    ) -> None:
        """Clear a task's hold. ``reset_progress`` (a landed resolve — real
        progress) also zeroes the retry counter, deleting the row outright;
        otherwise the counter survives on a demoted row while the backoff is
        cleared (an explicit release means "dispatchable now")."""
        async with self._pool.acquire() as conn, conn.transaction():
            if reset_progress:
                await conn.execute(
                    "DELETE FROM nightshift.tasks WHERE queue = $1 AND task = $2",
                    _qkey(queue), task,
                )
                return
            await conn.execute(_TASK_RELEASE_DELETE_SQL, _qkey(queue), task)
            await conn.execute(_TASK_RELEASE_DEMOTE_SQL, _qkey(queue), task)

    async def tasks_backing_off(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT queue, task, next_eligible_at FROM nightshift.tasks
                WHERE next_eligible_at IS NOT NULL AND next_eligible_at > now()
                """
            )
        return [dict(r) for r in rows]

    async def clear_task_backoff(self, queue: str | None, task: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE nightshift.tasks
                SET next_eligible_at = NULL, updated_at = now()
                WHERE queue = $1 AND task = $2 AND next_eligible_at IS NOT NULL
                """,
                _qkey(queue), task,
            )

    async def queue_dedication(self) -> dict[str, list[str]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT queue, worker_id FROM nightshift.queue_routing ORDER BY queue"
            )
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["queue"], []).append(r["worker_id"])
        return out

    async def set_queue_dedication(
        self, queue_label: str, worker_ids: list[str]
    ) -> None:
        cleaned = [w for w in worker_ids if w]
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM nightshift.queue_routing WHERE queue = $1",
                queue_label,
            )
            for wid in cleaned:
                await conn.execute(
                    """
                    INSERT INTO nightshift.queue_routing (queue, worker_id)
                    VALUES ($1, $2)
                    ON CONFLICT (queue, worker_id) DO NOTHING
                    """,
                    queue_label, wid,
                )

    async def queue_pauses(self) -> dict[str, str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT queue, paused_reason FROM nightshift.queue_state "
                "WHERE paused_reason IS NOT NULL"
            )
        return {r["queue"]: r["paused_reason"] for r in rows}

    async def set_queue_pause(self, queue_label: str, reason: str | None) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO nightshift.queue_state (queue, paused_reason, updated_at)
                VALUES ($1, $2, now())
                ON CONFLICT (queue) DO UPDATE SET
                    paused_reason = EXCLUDED.paused_reason, updated_at = now()
                """,
                queue_label, reason,
            )
            await conn.execute(_QUEUE_STATE_PRUNE_SQL, queue_label)

    async def queue_modes(self) -> dict[str, str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT queue, mode FROM nightshift.queue_state "
                "WHERE mode IS NOT NULL"
            )
        return {r["queue"]: r["mode"] for r in rows}

    async def set_queue_mode(self, queue_label: str, mode: str | None) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO nightshift.queue_state (queue, mode, updated_at)
                VALUES ($1, $2, now())
                ON CONFLICT (queue) DO UPDATE SET
                    mode = EXCLUDED.mode, updated_at = now()
                """,
                queue_label, mode,
            )
            await conn.execute(_QUEUE_STATE_PRUNE_SQL, queue_label)

    async def rename_queue(self, old: str, new: str) -> None:
        """Repoint every queue-keyed row from ``old`` to ``new`` (playlists only;
        the main queue is never renamed). Playlist queue keys equal the playlist
        name across attempts/tasks/events/queue_routing/queue_state, so one
        value maps them all."""
        if not old or old == new:
            return
        async with self._pool.acquire() as conn:
            for table in ("attempts", "tasks", "events", "queue_routing",
                          "queue_state"):
                await conn.execute(
                    f"UPDATE nightshift.{table} SET queue = $2 WHERE queue = $1",
                    old, new,
                )

    async def append_event(
        self,
        kind: str,
        *,
        run_id: str | None = None,
        queue: str | None = None,
        task: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO nightshift.events (kind, run_id, queue, task, payload)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                RETURNING id
                """,
                kind, run_id, queue, task, json.dumps(payload or {}),
            )

    async def events_since(self, cursor: int, *, limit: int = 500) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM nightshift.events WHERE id > $1 ORDER BY id LIMIT $2",
                cursor, limit,
            )
        return [_event_row(r) for r in rows]

    async def max_event_id(self) -> int:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval("SELECT COALESCE(max(id), 0) FROM nightshift.events")
        return int(value or 0)

    async def run_events(self, run_id: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM nightshift.events WHERE run_id = $1 ORDER BY id", run_id
            )
        return [_event_row(r) for r in rows]

    async def stats_overall(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM nightshift.stats_overall")
        return dict(row) if row else _aggregate([])

    async def stats_by_worker(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM nightshift.stats_by_worker ORDER BY worker_id")
        return [dict(r) for r in rows]

    async def stats_by_backend(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM nightshift.stats_by_backend ORDER BY backend")
        return [dict(r) for r in rows]

    async def stats_by_model(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM nightshift.stats_by_model ORDER BY model")
        return [dict(r) for r in rows]

    async def stats_by_queue(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM nightshift.stats_by_queue ORDER BY queue")
        return [dict(r) for r in rows]


def _jsonish(value: Any) -> Any:
    """Decode a JSONB column that the driver may hand back as a string."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _worker_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["queues"] = _jsonish(d.get("queues"))
    d["priorities"] = _jsonish(d.get("priorities"))
    d["models"] = _jsonish(d.get("models")) or []
    d["mcps"] = _jsonish(d.get("mcps")) or []
    d["meta"] = _jsonish(d.get("meta")) or {}
    return d


def _attempt_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    if "required_mcps" in d:
        d["required_mcps"] = _jsonish(d.get("required_mcps")) or []
    return d


def _event_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = _jsonish(d.get("payload")) or {}
    return d


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


async def open_store(dsn: str | None = None) -> NightshiftStore:
    """Open the durable store from the environment, or an in-memory fallback.

    Uses Postgres (``PgStore``) when a DSN is available (``dsn`` arg, else
    ``NIGHTSHIFT_PG_DSN``), else an in-memory store so a co-located
    single-machine run needs no DB. The asyncpg import is local to this factory
    so the module never imports a PG client at top level.
    """
    dsn = dsn or os.environ.get("NIGHTSHIFT_PG_DSN")
    if not dsn:
        store: NightshiftStore = MemoryStore()
        await store.init()
        return store
    # The single asyncpg seam (`nightshift.pg`) owns the client; we only ever
    # hold the structural PgPoolLike it returns.
    from nightshift.pg import open_pool

    pool = await open_pool(dsn)
    pg_store = PgStore(pool)
    await pg_store.init()
    return pg_store
