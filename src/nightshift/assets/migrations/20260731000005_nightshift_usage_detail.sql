-- migrate:up

-- Token usage granularity: split the folded `input_tokens` into its cache
-- components, and retain the vendor-shaped raw usage blob for detail the
-- normalized columns can't hold (Gemini thinking/tool tokens, Claude Code
-- per-model usage, the harness's per-turn breakdown). `input_tokens` and
-- `output_tokens` keep their existing folded meaning (invariant:
-- cache_read_input_tokens + cache_creation_input_tokens <= input_tokens,
-- uncached-input is derivable) so existing history stays comparable.
-- Backends that can't report cache activity leave the new columns NULL,
-- same convention as the other best-effort telemetry columns; pre-existing
-- rows are backfilled to 0 ("no recorded cache activity") since none of them
-- predate cache_control support.
ALTER TABLE nightshift.attempts
    ADD COLUMN IF NOT EXISTS cache_read_input_tokens     bigint,
    ADD COLUMN IF NOT EXISTS cache_creation_input_tokens bigint,
    ADD COLUMN IF NOT EXISTS usage                       jsonb;

UPDATE nightshift.attempts
SET cache_read_input_tokens = 0, cache_creation_input_tokens = 0
WHERE cache_read_input_tokens IS NULL AND cache_creation_input_tokens IS NULL;

-- Recreate the stats views with cache totals. Keep in lockstep with the
-- SQLite store's stats views (store_sqlite._SCHEMA).
DROP VIEW IF EXISTS nightshift.stats_by_queue;
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

-- migrate:down

DROP VIEW IF EXISTS nightshift.stats_by_queue;
DROP VIEW IF EXISTS nightshift.stats_by_model;
DROP VIEW IF EXISTS nightshift.stats_by_backend;
DROP VIEW IF EXISTS nightshift.stats_by_worker;
DROP VIEW IF EXISTS nightshift.stats_overall;

-- Restore the pre-migration views (byte-identical to 20260731000004_up).
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

ALTER TABLE nightshift.attempts
    DROP COLUMN IF EXISTS cache_read_input_tokens,
    DROP COLUMN IF EXISTS cache_creation_input_tokens,
    DROP COLUMN IF EXISTS usage;
