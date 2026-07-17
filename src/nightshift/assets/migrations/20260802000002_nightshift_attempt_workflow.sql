-- migrate:up

-- The workflow routing metadata for a workflow-step attempt (spec §6.2/§6.3).
-- The stored dict is the work order's workflow block MINUS "artifacts" (context
-- lives in the tasks repo; the row stores name/step/kind/output/signals only).
-- NULL for every non-workflow attempt.
ALTER TABLE nightshift.attempts
    ADD COLUMN IF NOT EXISTS workflow jsonb;

-- migrate:down

ALTER TABLE nightshift.attempts DROP COLUMN IF EXISTS workflow;
