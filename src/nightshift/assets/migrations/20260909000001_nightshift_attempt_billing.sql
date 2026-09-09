-- migrate:up

-- Billing attribution on the attempt, and the actual/notional spend split it
-- makes possible. The `claude` CLI reports `total_cost_usd` in both billing
-- modes, but under a claude.ai subscription login that figure is notional list
-- price, not money spent; `billing` records which account actually paid:
-- 'api' (a vendor API key was billed), 'subscription' (a subscription login —
-- `cost_usd` is then notional), or NULL when the backend cannot say.
--
-- Rollup rule, applied identically here, in the worker's local stats and in
-- the analytics UI: 'subscription' counts as notional, 'api' AND an absent
-- stamp count as actual. Absent-is-actual is the conservative reading — every
-- pre-existing row was API-billed, and an unattributed dollar treated as real
-- money can only overstate the actual figure, never understate it. Existing
-- history therefore stays readable as the API spend it was, and
-- `total_cost_usd` keeps its meaning as the sum of both figures.
ALTER TABLE nightshift.attempts
    ADD COLUMN IF NOT EXISTS billing text;

-- Recreate the stats views with the split, every existing column kept. Keep in
-- lockstep with the SQLite store's stats views (store_sqlite._SCHEMA).
DROP VIEW IF EXISTS nightshift.stats_by_queue;
DROP VIEW IF EXISTS nightshift.stats_by_enhanced;
DROP VIEW IF EXISTS nightshift.stats_by_model;
DROP VIEW IF EXISTS nightshift.stats_by_backend;
DROP VIEW IF EXISTS nightshift.stats_by_worker;
DROP VIEW IF EXISTS nightshift.stats_overall;

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
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd,
    coalesce(sum(cost_usd) FILTER (WHERE coalesce(billing, '') <> 'subscription'), 0) AS actual_cost_usd,
    coalesce(sum(cost_usd) FILTER (WHERE billing = 'subscription'), 0) AS notional_cost_usd
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
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd,
    coalesce(sum(cost_usd) FILTER (WHERE coalesce(billing, '') <> 'subscription'), 0) AS actual_cost_usd,
    coalesce(sum(cost_usd) FILTER (WHERE billing = 'subscription'), 0) AS notional_cost_usd,
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
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd,
    coalesce(sum(cost_usd) FILTER (WHERE coalesce(billing, '') <> 'subscription'), 0) AS actual_cost_usd,
    coalesce(sum(cost_usd) FILTER (WHERE billing = 'subscription'), 0) AS notional_cost_usd
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
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd,
    coalesce(sum(cost_usd) FILTER (WHERE coalesce(billing, '') <> 'subscription'), 0) AS actual_cost_usd,
    coalesce(sum(cost_usd) FILTER (WHERE billing = 'subscription'), 0) AS notional_cost_usd
FROM nightshift.attempts
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
    coalesce(sum(cost_usd) FILTER (WHERE coalesce(billing, '') <> 'subscription'), 0) AS actual_cost_usd,
    coalesce(sum(cost_usd) FILTER (WHERE billing = 'subscription'), 0) AS notional_cost_usd
FROM nightshift.attempts
GROUP BY enhanced;

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
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd,
    coalesce(sum(cost_usd) FILTER (WHERE coalesce(billing, '') <> 'subscription'), 0) AS actual_cost_usd,
    coalesce(sum(cost_usd) FILTER (WHERE billing = 'subscription'), 0) AS notional_cost_usd
FROM nightshift.attempts
GROUP BY queue;

-- migrate:down

DROP VIEW IF EXISTS nightshift.stats_by_queue;
DROP VIEW IF EXISTS nightshift.stats_by_enhanced;
DROP VIEW IF EXISTS nightshift.stats_by_model;
DROP VIEW IF EXISTS nightshift.stats_by_backend;
DROP VIEW IF EXISTS nightshift.stats_by_worker;
DROP VIEW IF EXISTS nightshift.stats_overall;

-- Restore the pre-migration views verbatim (the five from 20260731000005_up
-- with their cache totals, stats_by_enhanced from 20260801000001_up).
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
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
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
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
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
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
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
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd
FROM nightshift.attempts
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
GROUP BY enhanced;

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
    coalesce(sum(cache_read_input_tokens), 0)         AS total_cache_read_tokens,
    coalesce(sum(cache_creation_input_tokens), 0)     AS total_cache_creation_tokens,
    coalesce(sum(cost_usd), 0)                        AS total_cost_usd
FROM nightshift.attempts
GROUP BY queue;

ALTER TABLE nightshift.attempts DROP COLUMN IF EXISTS billing;
