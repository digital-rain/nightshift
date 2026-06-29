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

import os
import threading
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from nightshift.pg import PgPoolLike


def _now() -> datetime:
    return datetime.now(UTC)


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

    # task state overlay
    async def set_task_state(
        self,
        queue: str | None,
        task: str,
        state: str,
        *,
        blocked_reason: str | None = None,
        repo: str | None = None,
    ) -> None: ...
    async def get_task_state(self, queue: str | None, task: str) -> dict[str, Any] | None: ...
    async def list_blocked(self) -> list[dict[str, Any]]: ...
    async def tasks_in_state(self, state: str) -> list[dict[str, Any]]: ...
    async def clear_task_state(self, queue: str | None, task: str) -> None: ...

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
                    and lease["status"] in ("leased", "submitted")
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
                if r["status"] in ("leased", "submitted")
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
            if status in ("released", "landed", "expired"):
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

    # ---- task state ------------------------------------------------------- #

    async def set_task_state(
        self,
        queue: str | None,
        task: str,
        state: str,
        *,
        blocked_reason: str | None = None,
        repo: str | None = None,
    ) -> None:
        with self._lock:
            self._tasks[(_qkey(queue), task)] = {
                "queue": _qkey(queue),
                "task": task,
                "state": state,
                "blocked_reason": blocked_reason,
                "repo": repo,
                "updated_at": _now(),
            }

    async def get_task_state(self, queue: str | None, task: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._tasks.get((_qkey(queue), task))
            return dict(row) if row else None

    async def list_blocked(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._tasks.values() if r["state"] == "blocked"]

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

    async def clear_task_state(self, queue: str | None, task: str) -> None:
        with self._lock:
            self._tasks.pop((_qkey(queue), task), None)

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
        with self._lock:
            row = self._runs.get(run_id)
            if row is None:
                return
            for key, value in fields.items():
                if key in row:
                    row[key] = value
            if fields.get("status") in ("completed", "error", "skipped", "aborted"):
                row["finished_at"] = row.get("finished_at") or _now()

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
        import json

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
                "SELECT * FROM nightshift.leases WHERE status IN ('leased', 'submitted')"
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
                """
                UPDATE nightshift.leases
                SET status = $2,
                    run_id = COALESCE($3, run_id),
                    released_at = CASE WHEN $2 IN ('released', 'landed', 'expired')
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

    async def set_task_state(
        self,
        queue: str | None,
        task: str,
        state: str,
        *,
        blocked_reason: str | None = None,
        repo: str | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO nightshift.tasks (queue, task, state, blocked_reason, repo, updated_at)
                VALUES ($1, $2, $3, $4, $5, now())
                ON CONFLICT (queue, task) DO UPDATE SET
                    state = EXCLUDED.state,
                    blocked_reason = EXCLUDED.blocked_reason,
                    repo = EXCLUDED.repo,
                    updated_at = now()
                """,
                _qkey(queue), task, state, blocked_reason, repo,
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
            rows = await conn.fetch("SELECT * FROM nightshift.tasks WHERE state = 'blocked'")
        return [dict(r) for r in rows]

    async def tasks_in_state(self, state: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT queue, task, state, repo, blocked_reason FROM nightshift.tasks WHERE state = $1",
                state,
            )
        return [dict(r) for r in rows]

    async def clear_task_state(self, queue: str | None, task: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM nightshift.tasks WHERE queue = $1 AND task = $2",
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
        import json

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
        allowed = {
            "status", "phase", "result_line", "commit_sha", "loc",
            "failure_kind", "failure_reason", "model", "backend",
            "turns", "input_tokens", "output_tokens", "cost_usd",
            "remote", "pushed", "validate_cmd", "worktree",
        }
        sets: list[str] = []
        values: list[Any] = []
        for i, (key, value) in enumerate(
            ((k, v) for k, v in fields.items() if k in allowed), start=2
        ):
            sets.append(f"{key} = ${i}")
            values.append(value)
        if not sets:
            return
        finish = ""
        if fields.get("status") in ("completed", "error", "skipped", "aborted"):
            finish = ", finished_at = COALESCE(finished_at, now())"
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE nightshift.runs SET {', '.join(sets)}{finish} WHERE id = $1",
                run_id, *values,
            )

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
        import json

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
        import json

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
    single-machine run needs no DB. Nightshift owns its own DSN and does **not**
    fall back to longitude's ``LONG_PG_DSN`` — pass the same value explicitly to
    share a database. The asyncpg import is local to this factory so the module
    never imports a PG client at top level.
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
