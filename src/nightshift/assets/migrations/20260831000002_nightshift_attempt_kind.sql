-- migrate:up

-- Task classification carried from the brief's `kind:` frontmatter onto the
-- attempt, so History and the Stats page can split CI-resolution runs out
-- from ordinary work. NULL for every ordinary task (and every pre-existing
-- row).
ALTER TABLE nightshift.attempts
    ADD COLUMN IF NOT EXISTS kind text;

-- migrate:down

ALTER TABLE nightshift.attempts DROP COLUMN IF EXISTS kind;
