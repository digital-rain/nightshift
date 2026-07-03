-- migrate:up

-- Phase 8 (the attempts schema): merge leases + runs into one `attempts`
-- entity with a single `state` lifecycle column (greenfield task lifecycle
-- §"Storage"). Columns are FLAT (not jsonb) and mirror today's runs columns
-- one-to-one, so the API-compat projections (manager/views.py) are
-- near-passthrough and the data copy below is column-to-column. The lease
-- fields ride along (base_ref/acquired_at/heartbeat_at/deadline_at/
-- released_at), plus branch_ref/head_sha persisted at the LANDING transition
-- for restart re-enqueue (internal-only; never projected to the API).
--
-- The fold CASE below is the SQL twin of nightshift.lifecycle.fold_legacy —
-- keep them in lockstep (test_lifecycle.py pins the Python side; the shape
-- test in test_nightshift_store.py pins this file's structure).
CREATE TABLE IF NOT EXISTS nightshift.attempts (
    id             text PRIMARY KEY,                  -- = the run id (new_run_id())
    task           text NOT NULL,
    queue          text NOT NULL DEFAULT '',          -- '' = main queue
    worker_id      text,
    backend        text,
    model          text,
    repo           text,
    required_mcps  jsonb NOT NULL DEFAULT '[]',
    -- running | landing | resolving | landed | no_change | blocked | failed
    -- | conflict | expired | aborted | skipped (+ claimed/submitted reserved
    -- by the vocabulary, unreachable today) — see lifecycle.AttemptState.
    state          text NOT NULL,
    phase          text,
    result_line    text,
    commit_sha     text,
    loc            integer,
    turns          integer,
    input_tokens   bigint,
    output_tokens  bigint,
    cost_usd       numeric(12, 6),
    failure_kind   text,
    failure_reason text,
    validate_cmd   text,
    worktree       text,
    title          text,
    body           text,
    remote         text,
    pushed         boolean,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    -- lease-side columns (internal; never appear in /api/runs or SSE runs)
    base_ref       text,
    acquired_at    timestamptz,
    heartbeat_at   timestamptz,
    deadline_at    timestamptz,                       -- was leases.expires_at
    released_at    timestamptz,
    branch_ref     text,
    head_sha       text
);

-- One live attempt per (queue, task) — invariant 1. RESOLVING is deliberately
-- NOT in the live set: resolve children never held leases, and including
-- them would newly block dispatch of the task they are repairing. Keep this
-- predicate byte-identical to lifecycle.ATTEMPT_LIVE_STATES and to the
-- ON CONFLICT clause in PgStore.create_attempt (index inference).
CREATE UNIQUE INDEX IF NOT EXISTS attempts_live_task_uniq
    ON nightshift.attempts (queue, task)
    WHERE state IN ('landing', 'running');

CREATE INDEX IF NOT EXISTS attempts_task_idx    ON nightshift.attempts (task);
CREATE INDEX IF NOT EXISTS attempts_worker_idx  ON nightshift.attempts (worker_id);
CREATE INDEX IF NOT EXISTS attempts_backend_idx ON nightshift.attempts (backend);
CREATE INDEX IF NOT EXISTS attempts_started_idx ON nightshift.attempts (started_at DESC);
CREATE INDEX IF NOT EXISTS attempts_state_idx   ON nightshift.attempts (state);

-- The old stats views read runs; drop them before the copy so the tables can
-- go away afterwards (recreated over attempts below).
DROP VIEW IF EXISTS nightshift.stats_by_queue;
DROP VIEW IF EXISTS nightshift.stats_by_model;
DROP VIEW IF EXISTS nightshift.stats_by_backend;
DROP VIEW IF EXISTS nightshift.stats_by_worker;
DROP VIEW IF EXISTS nightshift.stats_overall;

-- Data fold: every run (with its latest lease, if any) becomes one attempt;
-- leases without a surviving run become lease-only attempts. Guarded on the
-- source tables so a re-run after the drop below is a no-op (idempotent).
DO $$
BEGIN
    IF to_regclass('nightshift.runs') IS NULL THEN
        RETURN;
    END IF;

    -- Runs (LEFT JOIN latest lease) with the fold CASE — the SQL twin of
    -- lifecycle.fold_legacy: lease status dominates for cancelled/expired/
    -- landed; a live lease + running run splits RUNNING/LANDING on phase;
    -- everything else folds by run status alone (zombie combos canonicalize:
    -- cancelled/expired leases with running runs, released running rows, and
    -- orphaned lease-less running rows become terminal — the Phase 8 fixes).
    -- Terminal attempts get finished_at stamped (invariant 4): the run's own
    -- stamp, else the lease's released_at, else now().
    WITH latest_lease AS (
        SELECT DISTINCT ON (run_id) *
        FROM nightshift.leases
        WHERE run_id IS NOT NULL
        ORDER BY run_id, acquired_at DESC
    ),
    folded AS (
        SELECT
            r.*,
            l.base_ref     AS l_base_ref,
            l.acquired_at  AS l_acquired_at,
            l.heartbeat_at AS l_heartbeat_at,
            l.expires_at   AS l_expires_at,
            l.released_at  AS l_released_at,
            CASE
                WHEN l.status = 'cancelled' THEN 'aborted'
                WHEN l.status = 'expired'   THEN 'expired'
                WHEN l.status = 'landed'    THEN 'landed'
                WHEN l.status = 'leased' AND r.status = 'running'
                     AND r.phase = 'landing' THEN 'landing'
                WHEN l.status = 'leased' AND r.status = 'running' THEN 'running'
                WHEN r.status = 'completed' THEN 'no_change'
                WHEN r.status = 'blocked'   THEN 'blocked'
                WHEN r.status = 'error' AND r.failure_kind IN
                     ('merge_conflict', 'merge_rejected') THEN 'conflict'
                WHEN r.status = 'error'     THEN 'failed'
                WHEN r.status = 'skipped'   THEN 'skipped'
                WHEN r.status = 'running' AND l.status IS NULL
                     AND r.worker_id = 'manager:resolve' THEN 'resolving'
                ELSE 'aborted'
            END AS folded_state
        FROM nightshift.runs r
        LEFT JOIN latest_lease l ON l.run_id = r.id
    )
    INSERT INTO nightshift.attempts
        (id, task, queue, worker_id, backend, model, repo, required_mcps,
         state, phase, result_line, commit_sha, loc, turns, input_tokens,
         output_tokens, cost_usd, failure_kind, failure_reason, validate_cmd,
         worktree, title, body, remote, pushed, started_at, finished_at,
         base_ref, acquired_at, heartbeat_at, deadline_at, released_at)
    SELECT
        id, task, queue, worker_id, backend, model, repo, required_mcps,
        folded_state, phase, result_line, commit_sha, loc, turns,
        input_tokens, output_tokens, cost_usd, failure_kind, failure_reason,
        validate_cmd, worktree, title, body, remote, pushed, started_at,
        CASE WHEN folded_state IN ('landed', 'no_change', 'blocked', 'failed',
                                   'conflict', 'expired', 'aborted', 'skipped')
             THEN COALESCE(finished_at, l_released_at, now())
             ELSE finished_at END,
        l_base_ref, l_acquired_at, l_heartbeat_at, l_expires_at, l_released_at
    FROM folded
    ON CONFLICT DO NOTHING;

    -- Lease-only rows (run_id NULL or dangling — the acquire/create crash
    -- window): keep them as attempts keyed by the lease uuid, folded by
    -- lease status alone (a leased claim without a run is still live).
    INSERT INTO nightshift.attempts
        (id, task, queue, worker_id, model, state, base_ref,
         acquired_at, heartbeat_at, deadline_at, released_at,
         started_at, finished_at)
    SELECT
        l.id::text, l.task, l.queue, l.worker_id, l.model,
        CASE l.status
            WHEN 'cancelled' THEN 'aborted'
            WHEN 'expired'   THEN 'expired'
            WHEN 'landed'    THEN 'landed'
            WHEN 'leased'    THEN 'running'
            ELSE 'aborted'  -- released with no run: nothing ran; canonicalize
        END,
        l.base_ref, l.acquired_at, l.heartbeat_at, l.expires_at, l.released_at,
        l.acquired_at,
        CASE WHEN l.status <> 'leased'
             THEN COALESCE(l.released_at, now()) END
    FROM nightshift.leases l
    LEFT JOIN nightshift.runs r ON r.id = l.run_id
    WHERE l.run_id IS NULL OR r.id IS NULL
    ON CONFLICT DO NOTHING;

    DROP TABLE nightshift.runs;
    DROP TABLE nightshift.leases;
END $$;

-- Stats views over attempts. Output column names and types are identical to
-- the pre-phase views (see 20260730000001); only the bucket predicates change
-- to the state vocabulary: completed = landed|no_change, errored =
-- failed|conflict. 'expired' is deliberately unbucketed, exactly as the
-- zombie expired runs never were pre-phase. Keep in lockstep with
-- MemoryStore._aggregate.
CREATE VIEW nightshift.stats_overall AS
SELECT
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE state IN ('landed', 'no_change')) AS completed,
    count(*) FILTER (WHERE state IN ('failed', 'conflict'))  AS errored,
    count(*) FILTER (WHERE state = 'aborted')         AS aborted,
    count(*) FILTER (WHERE state = 'skipped')         AS skipped,
    coalesce(sum(loc) FILTER (WHERE state IN ('landed', 'no_change')), 0) AS total_loc,
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
FROM nightshift.attempts;

CREATE VIEW nightshift.stats_by_worker AS
SELECT
    worker_id,
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE state IN ('landed', 'no_change')) AS completed,
    count(*) FILTER (WHERE state IN ('failed', 'conflict'))  AS errored,
    coalesce(sum(loc) FILTER (WHERE state IN ('landed', 'no_change')), 0) AS total_loc,
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
FROM nightshift.attempts
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
FROM nightshift.attempts
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
FROM nightshift.attempts
WHERE model IS NOT NULL
GROUP BY model;

CREATE VIEW nightshift.stats_by_queue AS
SELECT
    queue,
    count(*)                                          AS total_runs,
    count(*) FILTER (WHERE state IN ('landed', 'no_change')) AS completed,
    count(*) FILTER (WHERE state IN ('failed', 'conflict'))  AS errored,
    coalesce(sum(loc) FILTER (WHERE state IN ('landed', 'no_change')), 0) AS total_loc,
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
FROM nightshift.attempts
GROUP BY queue;

-- migrate:down

-- Split attempts back into leases + runs (the SQL twin of
-- nightshift.lifecycle.split_state). Run ids are kept; lease ids regenerate
-- via gen_random_uuid (the originals were consumed by the fold; lease-only
-- attempts regain a row the same way). ABORTED canonicalizes to
-- (cancelled, aborted) — see fold_legacy's zombie notes.

CREATE TABLE IF NOT EXISTS nightshift.leases (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task         text NOT NULL,
    queue        text NOT NULL DEFAULT '',
    worker_id    text NOT NULL REFERENCES nightshift.workers(id) ON DELETE CASCADE,
    run_id       text,
    status       text NOT NULL DEFAULT 'leased',
    model        text,
    base_ref     text,
    acquired_at  timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,
    released_at  timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS leases_active_task_uniq
    ON nightshift.leases (queue, task)
    WHERE status IN ('leased', 'submitted');

CREATE INDEX IF NOT EXISTS leases_worker_idx ON nightshift.leases (worker_id);
CREATE INDEX IF NOT EXISTS leases_status_idx ON nightshift.leases (status);

CREATE TABLE IF NOT EXISTS nightshift.runs (
    id             text PRIMARY KEY,
    task           text NOT NULL,
    queue          text NOT NULL DEFAULT '',
    worker_id      text,
    backend        text,
    model          text,
    status         text NOT NULL DEFAULT 'running',
    phase          text,
    result_line    text,
    commit_sha     text,
    loc            integer,
    turns          integer,
    input_tokens   bigint,
    output_tokens  bigint,
    cost_usd       numeric(12, 6),
    failure_kind   text,
    failure_reason text,
    title          text,
    body           text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    required_mcps  jsonb NOT NULL DEFAULT '[]',
    repo           text,
    remote         text,
    pushed         boolean,
    validate_cmd   text,
    worktree       text
);

CREATE INDEX IF NOT EXISTS runs_task_idx    ON nightshift.runs (task);
CREATE INDEX IF NOT EXISTS runs_worker_idx  ON nightshift.runs (worker_id);
CREATE INDEX IF NOT EXISTS runs_backend_idx ON nightshift.runs (backend);
CREATE INDEX IF NOT EXISTS runs_started_idx ON nightshift.runs (started_at DESC);

DO $$
BEGIN
    IF to_regclass('nightshift.attempts') IS NULL THEN
        RETURN;
    END IF;

    -- Run rows: every attempt keeps its id; status is split_state's run half
    -- (RUNNING/LANDING/RESOLVING -> running, LANDED/NO_CHANGE -> completed,
    -- FAILED/CONFLICT -> error, EXPIRED -> running).
    INSERT INTO nightshift.runs
        (id, task, queue, worker_id, backend, model, status, phase,
         result_line, commit_sha, loc, turns, input_tokens, output_tokens,
         cost_usd, failure_kind, failure_reason, title, body, started_at,
         finished_at, required_mcps, repo, remote, pushed, validate_cmd,
         worktree)
    SELECT
        id, task, queue, worker_id, backend, model,
        CASE state
            WHEN 'landed'    THEN 'completed'
            WHEN 'no_change' THEN 'completed'
            WHEN 'blocked'   THEN 'blocked'
            WHEN 'failed'    THEN 'error'
            WHEN 'conflict'  THEN 'error'
            WHEN 'skipped'   THEN 'skipped'
            WHEN 'aborted'   THEN 'aborted'
            ELSE 'running'  -- running | landing | resolving | expired
        END,
        phase, result_line, commit_sha, loc, turns, input_tokens,
        output_tokens, cost_usd, failure_kind, failure_reason, title, body,
        started_at, finished_at, required_mcps, repo, remote, pushed,
        validate_cmd, worktree
    FROM nightshift.attempts
    ON CONFLICT (id) DO NOTHING;

    -- Lease rows: split_state's lease half. RESOLVING attempts never held a
    -- lease; the workers FK also skips manager-owned rows. Idempotent via
    -- the run_id existence guard (lease ids are regenerated).
    INSERT INTO nightshift.leases
        (task, queue, worker_id, run_id, status, model, base_ref,
         acquired_at, heartbeat_at, expires_at, released_at)
    SELECT
        a.task, a.queue, a.worker_id, a.id,
        CASE a.state
            WHEN 'running'   THEN 'leased'
            WHEN 'landing'   THEN 'leased'
            WHEN 'landed'    THEN 'landed'
            WHEN 'no_change' THEN 'released'
            WHEN 'blocked'   THEN 'released'
            WHEN 'failed'    THEN 'released'
            WHEN 'conflict'  THEN 'released'
            WHEN 'skipped'   THEN 'released'
            WHEN 'aborted'   THEN 'cancelled'
            WHEN 'expired'   THEN 'expired'
        END,
        a.model, a.base_ref,
        COALESCE(a.acquired_at, a.started_at),
        COALESCE(a.heartbeat_at, a.started_at),
        a.deadline_at, a.released_at
    FROM nightshift.attempts a
    WHERE a.state <> 'resolving'
      AND a.worker_id IN (SELECT id FROM nightshift.workers)
      AND NOT EXISTS (
          SELECT 1 FROM nightshift.leases le WHERE le.run_id = a.id
      );

    DROP TABLE nightshift.attempts;
END $$;

-- Recreate the pre-phase stats views over runs (byte-identical to
-- 20260730000001).
DROP VIEW IF EXISTS nightshift.stats_by_queue;
DROP VIEW IF EXISTS nightshift.stats_by_model;
DROP VIEW IF EXISTS nightshift.stats_by_backend;
DROP VIEW IF EXISTS nightshift.stats_by_worker;
DROP VIEW IF EXISTS nightshift.stats_overall;

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
