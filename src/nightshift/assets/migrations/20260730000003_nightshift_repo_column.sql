-- Multi-repo workspace: record the workspace-relative target repo per task/run.
-- The ``repo`` is always a bare workspace-child name (never an absolute path),
-- so run history and the tasks overlay can be filtered per target repo and
-- debugged after the fact. Idempotent: safe to re-apply.

-- migrate:up

ALTER TABLE nightshift.tasks
    ADD COLUMN IF NOT EXISTS repo text;

ALTER TABLE nightshift.runs
    ADD COLUMN IF NOT EXISTS repo text;

-- migrate:down

ALTER TABLE nightshift.runs
    DROP COLUMN IF EXISTS repo;

ALTER TABLE nightshift.tasks
    DROP COLUMN IF EXISTS repo;
