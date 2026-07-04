-- migrate:up

-- Enhance-on-create tracking: did this attempt run an AI-enhanced brief, and
-- how did the operator rate the outcome? `enhanced` is stamped from the
-- brief's frontmatter when the attempt is created (false for pre-existing
-- history — those briefs predate enhancement). `rating` is the operator's
-- manual thumbs verdict on the run: 'up', 'down', or NULL (unrated).
ALTER TABLE nightshift.attempts
    ADD COLUMN IF NOT EXISTS enhanced boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS rating   text;

-- Enhanced-vs-raw rollup: the comparison view that answers "does enhancement
-- buy a lower failure rate / higher satisfaction?". Same outcome vocabulary
-- as the other stats views, plus the thumbs tallies. Keep in lockstep with
-- the SQLite store's twin (store_sqlite._SCHEMA).
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

-- Manager-side enhancement telemetry: one row per enhance-brief request
-- (success or failure), independent of whether the task ever runs — the
-- "manager's requests" half of the requests-vs-execution comparison.
CREATE TABLE IF NOT EXISTS nightshift.enhancements (
    id            text PRIMARY KEY,
    queue         text NOT NULL DEFAULT '',
    task          text,
    model         text,
    input_tokens  bigint,
    output_tokens bigint,
    duration_ms   integer,
    ok            boolean NOT NULL,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- migrate:down

DROP TABLE IF EXISTS nightshift.enhancements;
DROP VIEW IF EXISTS nightshift.stats_by_enhanced;
ALTER TABLE nightshift.attempts
    DROP COLUMN IF EXISTS enhanced,
    DROP COLUMN IF EXISTS rating;
