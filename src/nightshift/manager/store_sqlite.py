"""The SQLite store — the shared SQL query layer on an in-memory database.

Phase 9 replaces the hand-synchronized ``MemoryStore`` twin with the SAME
query layer :class:`~nightshift.manager.store.SqlStoreBase` runs against
Postgres, so unit tests exercise the production SQL semantics by
construction. The differences live entirely in this module's dialect seam:

* **SQL text translation** (:func:`_to_sqlite`): ``$n`` params become ``?n``,
  ``::jsonb``/``::text`` casts are stripped, and Postgres interval arithmetic
  (``now() ± (x || ' seconds')::interval``, ``make_interval``) becomes the
  registered ``ns_add_seconds`` scalar function. Everything else — upserts
  with the partial-index conflict target (invariant 1's
  ``attempts_live_task_uniq``), aggregate ``FILTER``, ``RETURNING`` — runs
  verbatim.
* **Timestamps** are stored as tz-aware UTC ISO-8601 strings at fixed
  microsecond precision (a registered ``now()`` function), so lexicographic
  ordering equals chronological ordering; row adapters parse them back to
  ``datetime`` so callers see the same Python types PgStore hands out.
* **Row fidelity**: JSON columns come back as text and are decoded by the
  shared row adapters; boolean columns come back as 0/1 and are re-widened
  here.
* **Concurrency**: one serialized connection (SQLite ``:memory:`` is
  per-connection) guarded by a threading lock — the store is hit from
  multiple event loops (TestClient portal threads and bare ``asyncio.run``
  callers), so no asyncio primitives. Statements run via
  ``asyncio.to_thread`` so no blocking call runs on an event loop; a
  transaction holds the lock from BEGIN to COMMIT/ROLLBACK.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

from nightshift.manager.store import SqlStoreBase


# Column-name-driven output conversion (the schema has no type introspection
# worth leaning on: SQLite is dynamically typed). Every timestamptz column in
# the schema plus the stats views' last_run_at; every boolean column.
_TS_COLUMNS = frozenset({
    "registered_at", "last_checkin_at", "last_heartbeat_at",
    "started_at", "finished_at", "acquired_at", "heartbeat_at",
    "deadline_at", "released_at", "updated_at", "next_eligible_at",
    "ts", "last_run_at", "created_at",
})
_BOOL_COLUMNS = frozenset({"pushed", "retry_eligible", "enhanced", "ok"})


def _now_iso() -> str:
    """UTC now at fixed microsecond precision — the SQL ``now()``."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _add_seconds(secs: Any) -> str | None:
    """``now() + interval 'secs seconds'`` (NULL-strict like make_interval)."""
    if secs is None:
        return None
    return (
        datetime.now(UTC) + timedelta(seconds=float(secs))
    ).isoformat(timespec="microseconds")


_INTERVAL_ADD_RE = re.compile(
    r"now\(\)\s*\+\s*\(\$(\d+) \|\| ' seconds'\)::interval"
)
_INTERVAL_SUB_RE = re.compile(
    r"now\(\)\s*-\s*\(\$(\d+) \|\| ' seconds'\)::interval"
)
_MAKE_INTERVAL_RE = re.compile(r"now\(\) \+ make_interval\(secs => \$(\d+)\)")
_PARAM_RE = re.compile(r"\$(\d+)")


@cache
def _to_sqlite(sql: str) -> str:
    """Translate one Postgres-dialect statement to SQLite (cached — the query
    layer uses a fixed set of statements)."""
    sql = _INTERVAL_ADD_RE.sub(r"ns_add_seconds($\1)", sql)
    sql = _MAKE_INTERVAL_RE.sub(r"ns_add_seconds($\1)", sql)
    sql = _INTERVAL_SUB_RE.sub(r"ns_add_seconds(-$\1)", sql)
    sql = sql.replace("::jsonb", "").replace("::text", "")
    # Postgres requires full-name qualification for the target row in an
    # upsert's DO UPDATE; SQLite resolves the unqualified name to it.
    sql = sql.replace(
        "nightshift.tasks.attempts_without_progress", "attempts_without_progress"
    )
    # Dedication lists keep the operator's given order: Postgres reads the
    # freshly rewritten rows off the heap in insertion order, SQLite walks the
    # (queue, worker_id) PK index — pin the tiebreak to insertion (rowid).
    sql = sql.replace(
        "FROM nightshift.queue_routing ORDER BY queue",
        "FROM nightshift.queue_routing ORDER BY queue, rowid",
    )
    return _PARAM_RE.sub(r"?\1", sql)


def _adapt_param(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    return value


def _adapt_row(row: sqlite3.Row) -> dict[str, Any]:
    """SQLite row → the Python types PgStore returns (tz-aware datetimes,
    real booleans). JSON text is left for the shared row adapters."""
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if value is not None:
            if key in _TS_COLUMNS and isinstance(value, str):
                value = datetime.fromisoformat(value)
            elif key in _BOOL_COLUMNS:
                value = bool(value)
        out[key] = value
    return out


# DDL mirroring the CURRENT post-migration Postgres schema (through
# 20260801000001_nightshift_enhance_tracking.sql) in SQLite types: jsonb → text
# holding JSON, timestamptz → text holding UTC ISO-8601, boolean → integer,
# numeric → real. The ``nightshift`` schema arrives via ATTACH so the shared
# SQL's qualified names run verbatim. Keep the attempts_live_task_uniq
# predicate byte-identical to the migration's (invariant 1 / ON CONFLICT
# index inference), and the stats views' output columns identical to the PG
# views' (dialect: julianday arithmetic instead of extract(epoch ...)).
_SCHEMA = """
CREATE TABLE nightshift.workers (
    id                text PRIMARY KEY,
    backend           text NOT NULL,
    queues            text,
    priorities        text,
    status            text NOT NULL DEFAULT 'idle',
    current_task      text,
    current_queue     text,
    current_run_id    text,
    registered_at     text NOT NULL,
    last_checkin_at   text NOT NULL,
    last_heartbeat_at text NOT NULL,
    meta              text NOT NULL DEFAULT '{}',
    models            text NOT NULL DEFAULT '[]',
    mcps              text NOT NULL DEFAULT '[]'
);

CREATE TABLE nightshift.attempts (
    id             text PRIMARY KEY,
    task           text NOT NULL,
    queue          text NOT NULL DEFAULT '',
    worker_id      text,
    backend        text,
    model          text,
    repo           text,
    required_mcps  text NOT NULL DEFAULT '[]',
    state          text NOT NULL,
    phase          text,
    result_line    text,
    commit_sha     text,
    loc            integer,
    turns          integer,
    input_tokens   integer,
    output_tokens  integer,
    cache_read_input_tokens     integer,
    cache_creation_input_tokens integer,
    usage          text,
    cost_usd       real,
    failure_kind   text,
    failure_reason text,
    validate_cmd   text,
    worktree       text,
    title          text,
    body           text,
    remote         text,
    pushed         integer,
    enhanced       integer NOT NULL DEFAULT 0,
    rating         text,
    started_at     text NOT NULL,
    finished_at    text,
    base_ref       text,
    acquired_at    text,
    heartbeat_at   text,
    deadline_at    text,
    released_at    text,
    branch_ref     text,
    head_sha       text
);

CREATE UNIQUE INDEX nightshift.attempts_live_task_uniq
    ON attempts (queue, task)
    WHERE state IN ('landing', 'running');

CREATE INDEX nightshift.attempts_task_idx    ON attempts (task);
CREATE INDEX nightshift.attempts_worker_idx  ON attempts (worker_id);
CREATE INDEX nightshift.attempts_started_idx ON attempts (started_at DESC);
CREATE INDEX nightshift.attempts_state_idx   ON attempts (state);

CREATE TABLE nightshift.tasks (
    queue          text NOT NULL DEFAULT '',
    task           text NOT NULL,
    state          text,
    blocked_reason text,
    repo           text,
    retry_eligible integer NOT NULL DEFAULT 0,
    attempts_without_progress integer NOT NULL DEFAULT 0,
    next_eligible_at text,
    updated_at     text NOT NULL,
    PRIMARY KEY (queue, task)
);

CREATE TABLE nightshift.events (
    id      integer PRIMARY KEY AUTOINCREMENT,
    kind    text NOT NULL,
    run_id  text,
    queue   text,
    task    text,
    payload text NOT NULL DEFAULT '{}',
    ts      text NOT NULL
);

CREATE INDEX nightshift.events_run_idx  ON events (run_id, id);
CREATE INDEX nightshift.events_kind_idx ON events (kind, id);

CREATE TABLE nightshift.queue_routing (
    queue     text NOT NULL,
    worker_id text NOT NULL,
    PRIMARY KEY (queue, worker_id)
);

CREATE TABLE nightshift.queue_state (
    queue           text PRIMARY KEY,
    paused_reason   text,
    mode            text,
    updated_at      text NOT NULL
);

CREATE TABLE nightshift.enhancements (
    id            text PRIMARY KEY,
    queue         text NOT NULL DEFAULT '',
    task          text,
    model         text,
    input_tokens  integer,
    output_tokens integer,
    duration_ms   integer,
    ok            integer NOT NULL,
    error         text,
    created_at    text NOT NULL
);

CREATE VIEW nightshift.stats_overall AS
SELECT
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE state IN ('landed', 'no_change')) AS completed,
    count(*) FILTER (WHERE state IN ('failed', 'conflict'))  AS errored,
    count(*) FILTER (WHERE state = 'aborted')         AS aborted,
    count(*) FILTER (WHERE state = 'skipped')         AS skipped,
    coalesce(sum(loc) FILTER (WHERE state IN ('landed', 'no_change')), 0) AS total_loc,
    coalesce(
        avg((julianday(finished_at) - julianday(started_at)) * 86400.0)
            FILTER (WHERE finished_at IS NOT NULL),
        0
    )                                                 AS avg_seconds,
    coalesce(sum(turns), 0)                           AS total_turns,
    coalesce(avg(turns) FILTER (WHERE turns IS NOT NULL), 0) AS avg_turns,
    coalesce(sum(input_tokens), 0)                    AS total_input_tokens,
    coalesce(sum(output_tokens), 0)                   AS total_output_tokens,
    coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS total_tokens,
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd
FROM attempts;

CREATE VIEW nightshift.stats_by_worker AS
SELECT
    worker_id,
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE state IN ('landed', 'no_change')) AS completed,
    count(*) FILTER (WHERE state IN ('failed', 'conflict'))  AS errored,
    coalesce(sum(loc) FILTER (WHERE state IN ('landed', 'no_change')), 0) AS total_loc,
    coalesce(
        avg((julianday(finished_at) - julianday(started_at)) * 86400.0)
            FILTER (WHERE finished_at IS NOT NULL),
        0
    )                                                 AS avg_seconds,
    coalesce(sum(turns), 0)                           AS total_turns,
    coalesce(avg(turns) FILTER (WHERE turns IS NOT NULL), 0) AS avg_turns,
    coalesce(sum(input_tokens), 0)                    AS total_input_tokens,
    coalesce(sum(output_tokens), 0)                   AS total_output_tokens,
    coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS total_tokens,
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd,
    max(started_at)                                   AS last_run_at
FROM attempts
WHERE worker_id IS NOT NULL
GROUP BY worker_id;

CREATE VIEW nightshift.stats_by_backend AS
SELECT
    backend,
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE state IN ('landed', 'no_change')) AS completed,
    count(*) FILTER (WHERE state IN ('failed', 'conflict'))  AS errored,
    coalesce(sum(loc) FILTER (WHERE state IN ('landed', 'no_change')), 0) AS total_loc,
    coalesce(
        avg((julianday(finished_at) - julianday(started_at)) * 86400.0)
            FILTER (WHERE finished_at IS NOT NULL),
        0
    )                                                 AS avg_seconds,
    coalesce(sum(turns), 0)                           AS total_turns,
    coalesce(avg(turns) FILTER (WHERE turns IS NOT NULL), 0) AS avg_turns,
    coalesce(sum(input_tokens), 0)                    AS total_input_tokens,
    coalesce(sum(output_tokens), 0)                   AS total_output_tokens,
    coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS total_tokens,
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd
FROM attempts
WHERE backend IS NOT NULL
GROUP BY backend;

CREATE VIEW nightshift.stats_by_model AS
SELECT
    model,
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE state IN ('landed', 'no_change')) AS completed,
    count(*) FILTER (WHERE state IN ('failed', 'conflict'))  AS errored,
    coalesce(sum(loc) FILTER (WHERE state IN ('landed', 'no_change')), 0) AS total_loc,
    coalesce(
        avg((julianday(finished_at) - julianday(started_at)) * 86400.0)
            FILTER (WHERE finished_at IS NOT NULL),
        0
    )                                                 AS avg_seconds,
    coalesce(sum(turns), 0)                           AS total_turns,
    coalesce(avg(turns) FILTER (WHERE turns IS NOT NULL), 0) AS avg_turns,
    coalesce(sum(input_tokens), 0)                    AS total_input_tokens,
    coalesce(sum(output_tokens), 0)                   AS total_output_tokens,
    coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS total_tokens,
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd
FROM attempts
WHERE model IS NOT NULL
GROUP BY model;

CREATE VIEW nightshift.stats_by_enhanced AS
SELECT
    enhanced,
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE state = 'landed')          AS landed,
    count(*) FILTER (WHERE state IN ('landed', 'no_change')) AS completed,
    count(*) FILTER (WHERE state IN ('failed', 'conflict'))  AS errored,
    count(*) FILTER (WHERE state = 'aborted')         AS aborted,
    count(*) FILTER (WHERE rating = 'up')             AS rated_up,
    count(*) FILTER (WHERE rating = 'down')           AS rated_down,
    coalesce(sum(loc) FILTER (WHERE state IN ('landed', 'no_change')), 0) AS total_loc,
    coalesce(
        avg((julianday(finished_at) - julianday(started_at)) * 86400.0)
            FILTER (WHERE finished_at IS NOT NULL),
        0
    )                                                 AS avg_seconds,
    coalesce(sum(turns), 0)                           AS total_turns,
    coalesce(avg(turns) FILTER (WHERE turns IS NOT NULL), 0) AS avg_turns,
    coalesce(sum(input_tokens), 0)                    AS total_input_tokens,
    coalesce(sum(output_tokens), 0)                   AS total_output_tokens,
    coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS total_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd
FROM attempts
GROUP BY enhanced;

CREATE VIEW nightshift.stats_by_queue AS
SELECT
    queue,
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE state IN ('landed', 'no_change')) AS completed,
    count(*) FILTER (WHERE state IN ('failed', 'conflict'))  AS errored,
    coalesce(sum(loc) FILTER (WHERE state IN ('landed', 'no_change')), 0) AS total_loc,
    coalesce(
        avg((julianday(finished_at) - julianday(started_at)) * 86400.0)
            FILTER (WHERE finished_at IS NOT NULL),
        0
    )                                                 AS avg_seconds,
    coalesce(sum(turns), 0)                           AS total_turns,
    coalesce(avg(turns) FILTER (WHERE turns IS NOT NULL), 0) AS avg_turns,
    coalesce(sum(input_tokens), 0)                    AS total_input_tokens,
    coalesce(sum(output_tokens), 0)                   AS total_output_tokens,
    coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS total_tokens,
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd
FROM attempts
GROUP BY queue;
"""


class _SqliteRunner:
    """The :class:`~nightshift.manager.store.SqlRunner` face of one store.

    ``locked=True`` means the store's lock is already held for the scope (a
    transaction) so statements run bare; otherwise each statement takes the
    lock for its own duration (SQLite autocommit).
    """

    def __init__(self, store: SqliteStore, *, locked: bool) -> None:
        self._store = store
        self._locked = locked

    async def execute(self, query: str, *args: Any) -> None:
        await asyncio.to_thread(self._store._exec, query, args, self._locked)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._store._fetch_all, query, args, self._locked
        )

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(
            self._store._fetch_all, query, args, self._locked
        )
        return rows[0] if rows else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await asyncio.to_thread(
            self._store._fetch_val, query, args, self._locked
        )


class SqliteStore(SqlStoreBase):
    """In-process store on in-memory SQLite. Same query layer as PgStore."""

    def __init__(self) -> None:
        # A primitive lock (not RLock): a transaction acquires it in one
        # to_thread hop and releases it in another, which only primitive
        # locks permit. Thread-safety matters because callers span multiple
        # event loops (TestClient portals + bare asyncio.run).
        self._db_lock = threading.Lock()
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.isolation_level = None  # autocommit; transactions BEGIN explicitly
        conn.row_factory = sqlite3.Row
        conn.execute("ATTACH DATABASE ':memory:' AS nightshift")
        # Unlike PG's transaction-stable now(), this evaluates per call; no
        # current statement relies on two now() evaluations agreeing.
        conn.create_function("now", 0, _now_iso)
        conn.create_function("ns_add_seconds", 1, _add_seconds)
        conn.executescript(_SCHEMA)
        self._db = conn

    # close() stays the inherited no-op: for Postgres, close releases the pool
    # while the DATA survives in the server; here the data lives in the
    # connection, so closing it would break the manager-restart-on-the-same-
    # store semantics (the reconciler recovery tests). The connection dies
    # with the object.

    # ---- the SqlStoreBase seams ------------------------------------------- #

    @asynccontextmanager
    async def _connection(self):
        yield _SqliteRunner(self, locked=False)

    @asynccontextmanager
    async def _transaction(self):
        # The begin needs a shield: if this task is cancelled (uvicorn cancels
        # handler tasks on client disconnect) the to_thread worker still runs
        # _begin to completion — acquiring the lock and opening a transaction
        # that nothing would ever end, deadlocking every later store call. The
        # shield keeps the begin future alive; on cancellation, a done-callback
        # guarantees begin-implies-eventual-end from a plain thread (the event
        # loop may be shutting down, so no loop primitives).
        begin = asyncio.ensure_future(asyncio.to_thread(self._begin))
        try:
            await asyncio.shield(begin)
        except asyncio.CancelledError:

            def _rollback_after_begin(fut: asyncio.Future) -> None:
                if fut.cancelled() or fut.exception() is not None:
                    return  # _begin released the lock itself; nothing to end
                threading.Thread(target=self._end, args=("ROLLBACK",)).start()

            begin.add_done_callback(_rollback_after_begin)
            raise
        try:
            yield _SqliteRunner(self, locked=True)
        except BaseException:
            # No shield needed on the ends: once awaited, the to_thread worker
            # runs _end to completion even if this await is cancelled, and
            # _end's finally releases the lock either way.
            await asyncio.to_thread(self._end, "ROLLBACK")
            raise
        else:
            await asyncio.to_thread(self._end, "COMMIT")

    def _begin(self) -> None:
        self._db_lock.acquire()
        try:
            self._db.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._db_lock.release()
            raise

    def _end(self, stmt: str) -> None:
        try:
            self._db.execute(stmt)
        finally:
            self._db_lock.release()

    # ---- synchronous statement execution (runs on worker threads) --------- #

    def _cursor(self, query: str, args: tuple[Any, ...]) -> sqlite3.Cursor:
        return self._db.execute(
            _to_sqlite(query), tuple(_adapt_param(a) for a in args)
        )

    def _exec(self, query: str, args: tuple[Any, ...], locked: bool) -> None:
        if locked:
            self._cursor(query, args)
            return
        with self._db_lock:
            self._cursor(query, args)

    def _fetch_all(
        self, query: str, args: tuple[Any, ...], locked: bool
    ) -> list[dict[str, Any]]:
        if locked:
            rows = self._cursor(query, args).fetchall()
        else:
            with self._db_lock:
                rows = self._cursor(query, args).fetchall()
        return [_adapt_row(r) for r in rows]

    def _fetch_val(self, query: str, args: tuple[Any, ...], locked: bool) -> Any:
        if locked:
            row = self._cursor(query, args).fetchone()
        else:
            with self._db_lock:
                row = self._cursor(query, args).fetchone()
        return None if row is None else row[0]
