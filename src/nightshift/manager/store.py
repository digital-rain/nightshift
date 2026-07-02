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
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, assert_never

from nightshift.lifecycle import (
    LEASE_ACTIVE_STATUSES,
    LEASE_RELEASED_AT_STATUSES,
    RUN_TERMINAL_STATUSES,
    Outcome,
    Progress,
    Transition,
)
from nightshift.pg import PgPoolLike


def _now() -> datetime:
    return datetime.now(UTC)


# The run columns update_run may touch, derived from the shared models: every
# Outcome field that is a run column (landable/branch_ref/head_sha are
# transport-only, consumed by the submit handler) plus the manager-computed
# land/progress columns. Unknown fields raise instead of being silently
# dropped.
RUN_UPDATABLE_FIELDS = frozenset(
    set(Outcome.model_fields) - {"landable", "branch_ref", "head_sha"}
) | {"phase", "commit_sha", "loc", "remote", "pushed"}


def _check_run_fields(fields: dict[str, Any]) -> None:
    unknown = set(fields) - RUN_UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_run: unknown field(s): {sorted(unknown)}")


def _apply_run_fields(row: dict[str, Any], fields: dict[str, Any]) -> None:
    """Fold validated field updates into an in-memory run row, stamping
    ``finished_at`` once when the run reaches a terminal status."""
    row.update(fields)
    if fields.get("status") in RUN_TERMINAL_STATUSES:
        row["finished_at"] = row.get("finished_at") or _now()


def _run_update_sql(fields: dict[str, Any]) -> tuple[str, list[Any]]:
    """Assemble the run-row UPDATE for validated ``fields``: the SET clause
    (values fill ``$2..$n``; ``$1`` is the run id) plus the one-shot
    ``finished_at`` stamp when the run reaches a terminal status."""
    sets: list[str] = []
    values: list[Any] = []
    for i, (key, value) in enumerate(fields.items(), start=2):
        sets.append(f"{key} = ${i}")
        values.append(value)
    finish = ""
    if fields.get("status") in RUN_TERMINAL_STATUSES:
        finish = ", finished_at = COALESCE(finished_at, now())"
    return (
        f"UPDATE nightshift.runs SET {', '.join(sets)}{finish} WHERE id = $1",
        values,
    )


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


# SQL fragments derived from the lease vocabulary (values are today's strings;
# StrEnum guarantees the rendering is byte-identical to the old literals).
_LEASE_ACTIVE_SQL = ", ".join(f"'{s}'" for s in sorted(LEASE_ACTIVE_STATUSES))
_LEASE_RELEASED_AT_SQL = ", ".join(f"'{s}'" for s in sorted(LEASE_RELEASED_AT_STATUSES))


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

    # leases
    async def acquire_lease(
        self,
        *,
        task: str,
        queue: str | None,
        worker_id: str,
        model: str | None,
        base_ref: str | None,
        ttl_seconds: float,
    ) -> dict[str, Any] | None: ...
    async def active_leases(self) -> list[dict[str, Any]]: ...
    async def get_lease(self, lease_id: str) -> dict[str, Any] | None: ...
    async def set_lease_status(
        self, lease_id: str, status: str, *, run_id: str | None = None
    ) -> None: ...
    async def heartbeat_lease(self, lease_id: str, ttl_seconds: float) -> None: ...
    async def reclaim_expired_leases(self) -> list[dict[str, Any]]: ...

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

    # queue rename (migrate every row keyed on a queue name)
    async def rename_queue(self, old: str, new: str) -> None: ...

    # runs
    async def create_run(
        self,
        run_id: str,
        *,
        task: str,
        queue: str | None,
        worker_id: str | None,
        backend: str | None,
        model: str | None,
        title: str | None = None,
        body: str | None = None,
        required_mcps: list[str] | None = None,
        repo: str | None = None,
        validate_cmd: str | None = None,
    ) -> dict[str, Any]: ...
    async def update_run(self, run_id: str, **fields: Any) -> None: ...
    async def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    async def list_runs(
        self, *, limit: int = 200, queue: str | None = None, worker_id: str | None = None
    ) -> list[dict[str, Any]]: ...

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
        self._leases: dict[str, dict[str, Any]] = {}
        self._tasks: dict[tuple[str, str], dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._event_seq = 0
        # queue label -> bound worker ids (manager-side dedication)
        self._dedication: dict[str, list[str]] = {}

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

    # ---- leases ----------------------------------------------------------- #

    async def acquire_lease(
        self,
        *,
        task: str,
        queue: str | None,
        worker_id: str,
        model: str | None,
        base_ref: str | None,
        ttl_seconds: float,
    ) -> dict[str, Any] | None:
        with self._lock:
            qk = _qkey(queue)
            for lease in self._leases.values():
                if (
                    lease["queue"] == qk
                    and lease["task"] == task
                    and lease["status"] in LEASE_ACTIVE_STATUSES
                ):
                    return None
            now = _now()
            lease_id = str(uuid.uuid4())
            row = {
                "id": lease_id,
                "task": task,
                "queue": qk,
                "worker_id": worker_id,
                "run_id": None,
                "status": "leased",
                "model": model,
                "base_ref": base_ref,
                "acquired_at": now,
                "heartbeat_at": now,
                "expires_at": now + timedelta(seconds=ttl_seconds),
                "released_at": None,
            }
            self._leases[lease_id] = row
            return dict(row)

    async def active_leases(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(r)
                for r in self._leases.values()
                if r["status"] in LEASE_ACTIVE_STATUSES
            ]

    async def get_lease(self, lease_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._leases.get(lease_id)
            return dict(row) if row else None

    async def set_lease_status(
        self, lease_id: str, status: str, *, run_id: str | None = None
    ) -> None:
        with self._lock:
            row = self._leases.get(lease_id)
            if row is None:
                return
            row["status"] = status
            if run_id is not None:
                row["run_id"] = run_id
            if status in LEASE_RELEASED_AT_STATUSES:
                row["released_at"] = _now()

    async def heartbeat_lease(self, lease_id: str, ttl_seconds: float) -> None:
        with self._lock:
            row = self._leases.get(lease_id)
            if row is None:
                return
            now = _now()
            row["heartbeat_at"] = now
            row["expires_at"] = now + timedelta(seconds=ttl_seconds)

    async def reclaim_expired_leases(self) -> list[dict[str, Any]]:
        with self._lock:
            now = _now()
            reclaimed: list[dict[str, Any]] = []
            for row in self._leases.values():
                if row["status"] == "leased" and row["expires_at"] and row["expires_at"] < now:
                    row["status"] = "expired"
                    row["released_at"] = now
                    reclaimed.append(dict(row))
            return reclaimed

    # ---- transitions ------------------------------------------------------ #

    async def apply_transition(
        self,
        t: Transition,
        *,
        expected_status: str,
        expected_worker_id: str | None = None,
    ) -> list[int] | None:
        """Apply one lifecycle transition atomically (under the store lock).

        CAS: the lease must exist in ``expected_status`` (and, when given,
        belong to ``expected_worker_id`` — the submit fence); otherwise nothing
        is written and ``None`` is returned. On success the lease, run row,
        task overlay, and events all change together; the inserted event ids
        are returned so the caller can broadcast exactly what committed.
        """
        # Validate before mutating so a bad field set can't leave partial state.
        _check_run_fields(t.run_fields)
        with self._lock:
            lease = self._leases.get(t.ref.lease_id)
            if lease is None or lease["status"] != expected_status:
                return None
            if expected_worker_id is not None and lease["worker_id"] != expected_worker_id:
                return None
            lease["status"] = t.lease_status
            if t.lease_status in LEASE_RELEASED_AT_STATUSES:
                lease["released_at"] = _now()
            run = self._runs.get(t.ref.run_id)
            if run is not None and t.run_fields:
                _apply_run_fields(run, t.run_fields)
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
                row = self._tasks.get(key)
                if row is not None:
                    if (
                        row["attempts_without_progress"] == 0
                        and row["next_eligible_at"] is None
                    ):
                        self._tasks.pop(key)
                    else:
                        # Demote to a pure counter row (state NULL = no hold).
                        row.update(
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

    # ---- queue rename ----------------------------------------------------- #

    async def rename_queue(self, old: str, new: str) -> None:
        """Repoint every queue-keyed row from ``old`` to ``new`` (playlists only;
        the main queue is never renamed)."""
        if not old or old == new:
            return
        with self._lock:
            ok, nk = _qkey(old), _qkey(new)
            for lease in self._leases.values():
                if lease["queue"] == ok:
                    lease["queue"] = nk
            for run in self._runs.values():
                if run["queue"] == ok:
                    run["queue"] = nk
            for event in self._events:
                if event.get("queue") == old:
                    event["queue"] = new
            for key, row in list(self._tasks.items()):
                if row["queue"] == ok:
                    row["queue"] = nk
                    self._tasks[(nk, row["task"])] = self._tasks.pop(key)
            if old in self._dedication:
                self._dedication[new] = self._dedication.pop(old)

    # ---- runs ------------------------------------------------------------- #

    async def create_run(
        self,
        run_id: str,
        *,
        task: str,
        queue: str | None,
        worker_id: str | None,
        backend: str | None,
        model: str | None,
        title: str | None = None,
        body: str | None = None,
        required_mcps: list[str] | None = None,
        repo: str | None = None,
        validate_cmd: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            row = {
                "id": run_id,
                "task": task,
                "queue": _qkey(queue),
                "worker_id": worker_id,
                "backend": backend,
                "model": model,
                "repo": repo,
                "required_mcps": required_mcps or [],
                "status": "running",
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
                "started_at": _now(),
                "finished_at": None,
            }
            self._runs[run_id] = row
            return dict(row)

    async def update_run(self, run_id: str, **fields: Any) -> None:
        _check_run_fields(fields)
        with self._lock:
            row = self._runs.get(run_id)
            if row is None:
                return
            _apply_run_fields(row, fields)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._runs.get(run_id)
            return dict(row) if row else None

    async def list_runs(
        self, *, limit: int = 200, queue: str | None = None, worker_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._runs.values())
        if queue is not None:
            rows = [r for r in rows if r["queue"] == _qkey(queue)]
        if worker_id is not None:
            rows = [r for r in rows if r["worker_id"] == worker_id]
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

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
            runs = list(self._runs.values())
        return _aggregate(runs)

    async def stats_by_worker(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._runs.values())
        return _group_stats(runs, "worker_id")

    async def stats_by_backend(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._runs.values())
        return _group_stats(runs, "backend")

    async def stats_by_model(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._runs.values())
        return _group_stats(runs, "model")

    async def stats_by_queue(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._runs.values())
        return _group_stats(runs, "queue")


def _aggregate(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    completed = [r for r in runs if r["status"] == "completed"]
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
        "errored": sum(1 for r in runs if r["status"] == "error"),
        "aborted": sum(1 for r in runs if r["status"] == "aborted"),
        "skipped": sum(1 for r in runs if r["status"] == "skipped"),
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

    async def acquire_lease(
        self,
        *,
        task: str,
        queue: str | None,
        worker_id: str,
        model: str | None,
        base_ref: str | None,
        ttl_seconds: float,
    ) -> dict[str, Any] | None:
        # The ON CONFLICT predicate must match the partial unique index
        # (leases_active_task_uniq) in the schema migration *verbatim* for
        # index inference, so the dead 'submitted' status stays here until the
        # schema itself changes (Phase 8/9).
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO nightshift.leases
                    (task, queue, worker_id, model, base_ref, status,
                     acquired_at, heartbeat_at, expires_at)
                VALUES ($1, $2, $3, $4, $5, 'leased', now(), now(),
                        now() + ($6 || ' seconds')::interval)
                ON CONFLICT (queue, task) WHERE status IN ('leased', 'submitted')
                DO NOTHING
                RETURNING *
                """,
                task, _qkey(queue), worker_id, model, base_ref, str(int(ttl_seconds)),
            )
        return _lease_row(row) if row else None

    async def active_leases(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM nightshift.leases WHERE status IN ({_LEASE_ACTIVE_SQL})"
            )
        return [_lease_row(r) for r in rows]

    async def get_lease(self, lease_id: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM nightshift.leases WHERE id = $1::uuid", lease_id
            )
        return _lease_row(row) if row else None

    async def set_lease_status(
        self, lease_id: str, status: str, *, run_id: str | None = None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE nightshift.leases
                SET status = $2,
                    run_id = COALESCE($3, run_id),
                    released_at = CASE WHEN $2 IN ({_LEASE_RELEASED_AT_SQL})
                                       THEN now() ELSE released_at END
                WHERE id = $1::uuid
                """,
                lease_id, status, run_id,
            )

    async def heartbeat_lease(self, lease_id: str, ttl_seconds: float) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE nightshift.leases
                SET heartbeat_at = now(),
                    expires_at = now() + ($2 || ' seconds')::interval
                WHERE id = $1::uuid
                """,
                lease_id, str(int(ttl_seconds)),
            )

    async def reclaim_expired_leases(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE nightshift.leases
                SET status = 'expired', released_at = now()
                WHERE status = 'leased' AND expires_at IS NOT NULL AND expires_at < now()
                RETURNING *
                """
            )
        return [_lease_row(r) for r in rows]

    async def apply_transition(
        self,
        t: Transition,
        *,
        expected_status: str,
        expected_worker_id: str | None = None,
    ) -> list[int] | None:
        """Apply one lifecycle transition in a single transaction.

        The CAS is the guarded lease UPDATE: it matches only while the lease is
        in ``expected_status`` (and owned by ``expected_worker_id`` when given).
        No match → rollback, no writes, ``None``. Events are inserted in the
        same transaction (outbox); their ids come back for post-commit SSE.
        """
        _check_run_fields(t.run_fields)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE nightshift.leases
                SET status = $2,
                    released_at = CASE WHEN $2 IN ({_LEASE_RELEASED_AT_SQL})
                                       THEN now() ELSE released_at END
                WHERE id = $1::uuid AND status = $3
                  AND ($4::text IS NULL OR worker_id = $4)
                RETURNING id
                """,
                t.ref.lease_id, t.lease_status, expected_status, expected_worker_id,
            )
            if row is None:
                return None
            if t.run_fields:
                sql, values = _run_update_sql(t.run_fields)
                await conn.execute(sql, t.ref.run_id, *values)
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

    async def rename_queue(self, old: str, new: str) -> None:
        """Repoint every queue-keyed row from ``old`` to ``new`` (playlists only;
        the main queue is never renamed). Playlist queue keys equal the playlist
        name across runs/leases/tasks/events/queue_routing, so one value maps
        them all."""
        if not old or old == new:
            return
        async with self._pool.acquire() as conn:
            for table in ("runs", "leases", "tasks", "events", "queue_routing"):
                await conn.execute(
                    f"UPDATE nightshift.{table} SET queue = $2 WHERE queue = $1",
                    old, new,
                )

    async def create_run(
        self,
        run_id: str,
        *,
        task: str,
        queue: str | None,
        worker_id: str | None,
        backend: str | None,
        model: str | None,
        title: str | None = None,
        body: str | None = None,
        required_mcps: list[str] | None = None,
        repo: str | None = None,
        validate_cmd: str | None = None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO nightshift.runs
                    (id, task, queue, worker_id, backend, model, repo, required_mcps,
                     validate_cmd, status, title, body, started_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9,
                        'running', $10, $11, now())
                RETURNING *
                """,
                run_id, task, _qkey(queue), worker_id, backend, model, repo,
                json.dumps(required_mcps or []), validate_cmd, title, body,
            )
        return _run_row(row)

    async def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        _check_run_fields(fields)
        sql, values = _run_update_sql(fields)
        async with self._pool.acquire() as conn:
            await conn.execute(sql, run_id, *values)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM nightshift.runs WHERE id = $1", run_id)
        return _run_row(row) if row else None

    async def list_runs(
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
                f"SELECT * FROM nightshift.runs {where} "
                f"ORDER BY started_at DESC LIMIT ${len(values)}",
                *values,
            )
        return [_run_row(r) for r in rows]

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


def _run_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    if "required_mcps" in d:
        d["required_mcps"] = _jsonish(d.get("required_mcps")) or []
    return d


def _lease_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    if d.get("id") is not None:
        d["id"] = str(d["id"])
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
