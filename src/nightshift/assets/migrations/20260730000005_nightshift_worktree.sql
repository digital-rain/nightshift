-- migrate:up
-- Record the worktree directory a worker used for a task run.
ALTER TABLE nightshift.runs
    ADD COLUMN IF NOT EXISTS worktree text;

-- migrate:down
ALTER TABLE nightshift.runs
    DROP COLUMN IF EXISTS worktree;
