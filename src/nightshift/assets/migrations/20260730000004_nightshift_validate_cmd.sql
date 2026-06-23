-- migrate:up
-- Record the validate command a worker actually ran (reported on submit).
ALTER TABLE nightshift.runs
    ADD COLUMN IF NOT EXISTS validate_cmd text;

-- migrate:down
ALTER TABLE nightshift.runs
    DROP COLUMN IF EXISTS validate_cmd;
