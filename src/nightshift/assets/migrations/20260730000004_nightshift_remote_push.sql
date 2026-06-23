-- Record remote landing policy outcome per run (push/pr/none and whether it succeeded).
-- Idempotent: safe to re-apply.

-- migrate:up

ALTER TABLE nightshift.runs
    ADD COLUMN IF NOT EXISTS remote text;

ALTER TABLE nightshift.runs
    ADD COLUMN IF NOT EXISTS pushed boolean;

-- migrate:down

ALTER TABLE nightshift.runs
    DROP COLUMN IF EXISTS pushed;

ALTER TABLE nightshift.runs
    DROP COLUMN IF EXISTS remote;
