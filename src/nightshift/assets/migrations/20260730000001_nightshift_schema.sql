-- migrate:up

-- Nightshift manager/worker state. Briefs stay canonical on disk (.tasks/*.md);
-- everything that used to be process-memory-only or filesystem run records moves
-- here so multiple browsers + multiple workers converge on a single source of
-- truth. The manager owns this schema; workers never touch it (they go through
-- the manager HTTP API).
CREATE SCHEMA IF NOT EXISTS nightshift;

-- Registered workers. A worker owns its backend and advertises routing
-- constraints (queues / priorities). `queues` null = any queue ("next
-- available"); a JSON array = sticky to those queues. `priorities` null = any;
-- a JSON array = the explicit set of accepted 0-5 priorities.
CREATE TABLE nightshift.workers (
    id                text PRIMARY KEY,
    backend           text NOT NULL,
    queues            jsonb,
    priorities        jsonb,
    status            text NOT NULL DEFAULT 'idle',   -- idle | busy | offline
    current_task      text,
    current_queue     text,
    current_run_id    text,
    registered_at     timestamptz NOT NULL DEFAULT now(),
    last_checkin_at   timestamptz NOT NULL DEFAULT now(),
    last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
    meta              jsonb NOT NULL DEFAULT '{}'
);

-- Per-task leases. A lease is the claim that lets exactly one worker run a task
-- at a time; landing stays globally serialized in the manager. The partial
-- unique index forbids two *active* leases on the same (queue, task).
CREATE TABLE nightshift.leases (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task         text NOT NULL,
    queue        text NOT NULL DEFAULT '',            -- '' = main .tasks queue
    worker_id    text NOT NULL REFERENCES nightshift.workers(id) ON DELETE CASCADE,
    run_id       text,
    status       text NOT NULL DEFAULT 'leased',      -- leased | submitted | landed | released | expired
    model        text,                                -- requested/pinned model (or auto/max)
    base_ref     text,                                -- pinned canonical main SHA at lease time
    acquired_at  timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,
    released_at  timestamptz
);

CREATE UNIQUE INDEX leases_active_task_uniq
    ON nightshift.leases (queue, task)
    WHERE status IN ('leased', 'submitted');

CREATE INDEX leases_worker_idx ON nightshift.leases (worker_id);
CREATE INDEX leases_status_idx ON nightshift.leases (status);

-- Task-state overlay on top of the canonical .tasks/ files. Used mainly to
-- record a *blocked* task (a pinned model whose backend no registered worker
-- runs) with a human reason, so the operator sees why it never gets picked up
-- instead of it sitting pending forever.
CREATE TABLE nightshift.tasks (
    queue          text NOT NULL DEFAULT '',          -- '' = main .tasks queue
    task           text NOT NULL,
    state          text NOT NULL DEFAULT 'pending',   -- pending | leased | blocked | done
    blocked_reason text,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (queue, task)
);

-- One row per run (a worker's attempt at a task). Records the backend + the
-- model the worker actually resolved, so History can compare backends head to
-- head on the same task.
CREATE TABLE nightshift.runs (
    id             text PRIMARY KEY,
    task           text NOT NULL,
    queue          text NOT NULL DEFAULT '',
    worker_id      text,
    backend        text,
    model          text,                              -- resolved model the worker used
    status         text NOT NULL DEFAULT 'running',   -- running | completed | error | skipped | aborted | blocked
    phase          text,
    result_line    text,
    commit_sha     text,
    loc            integer,
    -- Per-run agent telemetry (best-effort; backends that can't report leave
    -- these null). `turns` is the agent's turn count (1 for single-shot API
    -- backends); token counts are total input/output throughput; `cost_usd` is
    -- the backend-reported dollar cost (null for local/ollama). These roll up
    -- per worker / model / backend / queue in the stats views below.
    turns          integer,
    input_tokens   bigint,
    output_tokens  bigint,
    cost_usd       numeric(12, 6),
    failure_kind   text,
    failure_reason text,
    title          text,
    body           text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz
);

CREATE INDEX runs_task_idx   ON nightshift.runs (task);
CREATE INDEX runs_worker_idx ON nightshift.runs (worker_id);
CREATE INDEX runs_backend_idx ON nightshift.runs (backend);
CREATE INDEX runs_started_idx ON nightshift.runs (started_at DESC);

-- Append-only event log. This is the single SSE source of truth (replacing the
-- on-disk events.jsonl). The monotonic `id` is the delta-stream cursor: a
-- browser connects, gets a snapshot, then tails rows with id greater than the
-- snapshot's max. `kind` covers run lifecycle (task_started, task_log,
-- task_status, task_result, run_started, run_finished, worker_started) AND
-- structural/control changes (queue_changed, settings_changed,
-- worker_registered, worker_status, lease_acquired, lease_released,
-- task_blocked) so every browser converges regardless of who caused the change.
CREATE TABLE nightshift.events (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind    text NOT NULL,
    run_id  text,
    queue   text,
    task    text,
    payload jsonb NOT NULL DEFAULT '{}',
    ts      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX events_run_idx ON nightshift.events (run_id, id);
CREATE INDEX events_kind_idx ON nightshift.events (kind, id);

-- Stats views consumed by the History/Stats + Workers UI. Overall plus
-- per-worker, per-backend, per-model, and per-queue so the operator can compare
-- claude-code vs cursor vs ollama (and model vs model) on the same workload.
-- LOC is completed-only (only landed code counts); turns/tokens/cost aggregate
-- over *all* runs (a failed attempt still burns turns and tokens).
CREATE VIEW nightshift.stats_overall AS
SELECT
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE status = 'completed')      AS completed,
    count(*) FILTER (WHERE status = 'error')          AS errored,
    count(*) FILTER (WHERE status = 'aborted')        AS aborted,
    count(*) FILTER (WHERE status = 'skipped')        AS skipped,
    coalesce(sum(loc) FILTER (WHERE status = 'completed'), 0) AS total_loc,
    coalesce(
        avg(extract(epoch FROM (finished_at - started_at)))
            FILTER (WHERE finished_at IS NOT NULL),
        0
    )                                                 AS avg_seconds,
    coalesce(sum(turns), 0)                           AS total_turns,
    coalesce(avg(turns) FILTER (WHERE turns IS NOT NULL), 0) AS avg_turns,
    coalesce(sum(input_tokens), 0)                    AS total_input_tokens,
    coalesce(sum(output_tokens), 0)                   AS total_output_tokens,
    coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS total_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd
FROM nightshift.runs;

CREATE VIEW nightshift.stats_by_worker AS
SELECT
    worker_id,
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE status = 'completed')      AS completed,
    count(*) FILTER (WHERE status = 'error')          AS errored,
    coalesce(sum(loc) FILTER (WHERE status = 'completed'), 0) AS total_loc,
    coalesce(
        avg(extract(epoch FROM (finished_at - started_at)))
            FILTER (WHERE finished_at IS NOT NULL),
        0
    )                                                 AS avg_seconds,
    coalesce(sum(turns), 0)                           AS total_turns,
    coalesce(avg(turns) FILTER (WHERE turns IS NOT NULL), 0) AS avg_turns,
    coalesce(sum(input_tokens), 0)                    AS total_input_tokens,
    coalesce(sum(output_tokens), 0)                   AS total_output_tokens,
    coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS total_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd,
    max(started_at)                                   AS last_run_at
FROM nightshift.runs
WHERE worker_id IS NOT NULL
GROUP BY worker_id;

CREATE VIEW nightshift.stats_by_backend AS
SELECT
    backend,
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE status = 'completed')      AS completed,
    count(*) FILTER (WHERE status = 'error')          AS errored,
    coalesce(sum(loc) FILTER (WHERE status = 'completed'), 0) AS total_loc,
    coalesce(
        avg(extract(epoch FROM (finished_at - started_at)))
            FILTER (WHERE finished_at IS NOT NULL),
        0
    )                                                 AS avg_seconds,
    coalesce(sum(turns), 0)                           AS total_turns,
    coalesce(avg(turns) FILTER (WHERE turns IS NOT NULL), 0) AS avg_turns,
    coalesce(sum(input_tokens), 0)                    AS total_input_tokens,
    coalesce(sum(output_tokens), 0)                   AS total_output_tokens,
    coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS total_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd
FROM nightshift.runs
WHERE backend IS NOT NULL
GROUP BY backend;

CREATE VIEW nightshift.stats_by_model AS
SELECT
    model,
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE status = 'completed')      AS completed,
    count(*) FILTER (WHERE status = 'error')          AS errored,
    coalesce(sum(loc) FILTER (WHERE status = 'completed'), 0) AS total_loc,
    coalesce(
        avg(extract(epoch FROM (finished_at - started_at)))
            FILTER (WHERE finished_at IS NOT NULL),
        0
    )                                                 AS avg_seconds,
    coalesce(sum(turns), 0)                           AS total_turns,
    coalesce(avg(turns) FILTER (WHERE turns IS NOT NULL), 0) AS avg_turns,
    coalesce(sum(input_tokens), 0)                    AS total_input_tokens,
    coalesce(sum(output_tokens), 0)                   AS total_output_tokens,
    coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS total_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd
FROM nightshift.runs
WHERE model IS NOT NULL
GROUP BY model;

CREATE VIEW nightshift.stats_by_queue AS
SELECT
    queue,
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE status = 'completed')      AS completed,
    count(*) FILTER (WHERE status = 'error')          AS errored,
    coalesce(sum(loc) FILTER (WHERE status = 'completed'), 0) AS total_loc,
    coalesce(
        avg(extract(epoch FROM (finished_at - started_at)))
            FILTER (WHERE finished_at IS NOT NULL),
        0
    )                                                 AS avg_seconds,
    coalesce(sum(turns), 0)                           AS total_turns,
    coalesce(avg(turns) FILTER (WHERE turns IS NOT NULL), 0) AS avg_turns,
    coalesce(sum(input_tokens), 0)                    AS total_input_tokens,
    coalesce(sum(output_tokens), 0)                   AS total_output_tokens,
    coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS total_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd
FROM nightshift.runs
GROUP BY queue;

-- migrate:down
DROP VIEW IF EXISTS nightshift.stats_by_queue;
DROP VIEW IF EXISTS nightshift.stats_by_model;
DROP VIEW IF EXISTS nightshift.stats_by_backend;
DROP VIEW IF EXISTS nightshift.stats_by_worker;
DROP VIEW IF EXISTS nightshift.stats_overall;
DROP TABLE IF EXISTS nightshift.events;
DROP TABLE IF EXISTS nightshift.runs;
DROP TABLE IF EXISTS nightshift.tasks;
DROP TABLE IF EXISTS nightshift.leases;
DROP TABLE IF EXISTS nightshift.workers;
DROP SCHEMA IF EXISTS nightshift;
