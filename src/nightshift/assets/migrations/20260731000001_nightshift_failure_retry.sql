-- migrate:up

-- Phase-B retry eligibility: true only for worker-error ("failed") and
-- honest-decline ("blocked" from an agent NIGHTSHIFT_BLOCKED) outcomes. False
-- (the default) for repo_unavailable, authoring-blocked, landing-conflict
-- blocked, and quarantined rows, so the failure-retry policy never touches a
-- task that actually needs an operator/Resolve action.
ALTER TABLE nightshift.tasks
    ADD COLUMN IF NOT EXISTS retry_eligible boolean NOT NULL DEFAULT false;

-- migrate:down

ALTER TABLE nightshift.tasks
    DROP COLUMN IF EXISTS retry_eligible;
