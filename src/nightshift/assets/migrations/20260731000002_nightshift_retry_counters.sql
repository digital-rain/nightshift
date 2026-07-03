-- migrate:up

-- Phase 5 (retry as data): the task row carries the retry counter and backoff
-- instead of the manager re-scanning run history on every submit.
--
--   * attempts_without_progress — consecutive no-progress attempts (worker
--     errors and completed-without-commit runs). A land resets it; environment
--     failures, aborts, and blocks are neutral. Quarantine becomes an O(1)
--     threshold check against this counter.
--   * next_eligible_at — retry backoff: dispatch skips the task until this
--     elapses. NULL = eligible now.
--
-- A row may now exist purely to hold a nonzero counter with no active hold, so
-- `state` becomes nullable (NULL = no hold). Every state-filtered view
-- (blocked/failed/repo_unavailable/retryable) ignores NULL-state rows.
ALTER TABLE nightshift.tasks
    ADD COLUMN IF NOT EXISTS attempts_without_progress integer NOT NULL DEFAULT 0;
ALTER TABLE nightshift.tasks
    ADD COLUMN IF NOT EXISTS next_eligible_at timestamptz;
ALTER TABLE nightshift.tasks
    ALTER COLUMN state DROP NOT NULL;

-- One-shot backfill from the retired no_progress_streak(runs) scan: per
-- (queue, task), count the no-progress runs (error, or completed without a
-- commit_sha) since the last run that landed (completed with a commit_sha);
-- blocked/aborted/skipped/running are neutral, exactly like the scanner.
-- Deliberately unwindowed — the old scan only saw the queue's most recent 50
-- runs, which is the window-eviction bug this phase fixes. Tasks whose briefs
-- no longer exist may gain a NULL-state counter row; those rows are invisible
-- to every view and harmless. next_eligible_at stays NULL (no retroactive
-- backoff). Idempotent: re-runs never overwrite a live nonzero counter.
WITH progress AS (
    SELECT queue, task, max(started_at) AS last_progress_at
    FROM nightshift.runs
    WHERE status = 'completed' AND commit_sha IS NOT NULL
    GROUP BY queue, task
),
streaks AS (
    SELECT r.queue, r.task, count(*) AS attempts
    FROM nightshift.runs r
    LEFT JOIN progress p ON p.queue = r.queue AND p.task = r.task
    WHERE (
        r.status = 'error'
        OR (r.status = 'completed' AND r.commit_sha IS NULL)
    )
      AND (p.last_progress_at IS NULL OR r.started_at > p.last_progress_at)
    GROUP BY r.queue, r.task
)
INSERT INTO nightshift.tasks (queue, task, state, attempts_without_progress, updated_at)
SELECT s.queue, s.task, NULL, s.attempts, now()
FROM streaks s
WHERE s.attempts > 0
ON CONFLICT (queue, task) DO UPDATE SET
    attempts_without_progress = EXCLUDED.attempts_without_progress,
    updated_at = now()
WHERE nightshift.tasks.attempts_without_progress = 0;

-- migrate:down

DELETE FROM nightshift.tasks WHERE state IS NULL;

ALTER TABLE nightshift.tasks
    ALTER COLUMN state SET NOT NULL;
ALTER TABLE nightshift.tasks
    DROP COLUMN IF EXISTS next_eligible_at;
ALTER TABLE nightshift.tasks
    DROP COLUMN IF EXISTS attempts_without_progress;
