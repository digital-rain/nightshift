-- migrate:up

-- Latest CI verdict per target repo, refreshed by the reconciler's
-- "repo CI refresh" duty and read by the poll path's dispatch gate.
-- One row per repo; absent row == never checked (treated as unknown).
CREATE TABLE IF NOT EXISTS nightshift.repo_ci (
    repo        text PRIMARY KEY,
    state       text NOT NULL,
    head_sha    text,
    url         text,
    detail      text,
    -- The auto-spawned fix task for the current red, and the sha it was
    -- spawned for: together these dedupe one fix task per failing commit.
    fix_task    text,
    fix_sha     text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- migrate:down

DROP TABLE IF EXISTS nightshift.repo_ci;
