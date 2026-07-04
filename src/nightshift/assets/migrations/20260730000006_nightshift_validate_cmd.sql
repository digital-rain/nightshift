-- migrate:up
-- Record the validate command a worker actually ran (reported on submit).
ALTER TABLE nightshift.runs
    ADD COLUMN IF NOT EXISTS validate_cmd text;

-- Renamed from 20260730000004_nightshift_validate_cmd.sql (serial collided
-- with the remote_push migration). Databases that applied it under the old
-- name re-apply harmlessly (ADD COLUMN IF NOT EXISTS); drop the stale row.
DELETE FROM _meta.schema_migrations
    WHERE filename = '20260730000004_nightshift_validate_cmd.sql';

-- migrate:down
ALTER TABLE nightshift.runs
    DROP COLUMN IF EXISTS validate_cmd;
