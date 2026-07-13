-- migrate:up

-- Free-form notes field on attempt records. Copied from the task's notes
-- at dispatch time; independently editable thereafter.
ALTER TABLE nightshift.attempts
    ADD COLUMN IF NOT EXISTS notes text;

-- migrate:down

ALTER TABLE nightshift.attempts DROP COLUMN IF EXISTS notes;
