-- migrate:up

-- Per-phase wall-clock split for an attempt (Outcome.timings): seconds per
-- phase the run actually entered plus "total", e.g.
-- {"worker": 512.3, "preflight": 4.1, "validate": 88.0, "total": 604.4},
-- measured by the worker loop's on_phase transitions. NULL for attempts that
-- never entered a phase (environment failures) or predate the column.
ALTER TABLE nightshift.attempts
    ADD COLUMN IF NOT EXISTS timings jsonb;

-- migrate:down

ALTER TABLE nightshift.attempts DROP COLUMN IF EXISTS timings;
