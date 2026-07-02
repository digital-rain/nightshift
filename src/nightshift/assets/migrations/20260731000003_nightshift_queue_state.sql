-- migrate:up

-- Phase 7 (durable queue pause/mode): the manager's transport state moves out
-- of app.state so a restart no longer silently unpauses queues.
--
--   * queue         — the queue LABEL ('main' for the default queue, else the
--                     playlist name), matching the wire shape of the pause map
--                     (poll responses' queue_pauses / transport pause_reason).
--   * paused_reason — why dispatch is paused ('operator' |
--                     'consecutive_failures' | 'retry_failed'); NULL = playing.
--   * mode          — transport playback mode ('oneshot' | 'auto' | 'repeat');
--                     NULL = the default ('auto').
--
-- A row exists only while it carries a pause or a non-default mode; clearing
-- both deletes it (the store owns that lifecycle).
CREATE TABLE IF NOT EXISTS nightshift.queue_state (
    queue           text PRIMARY KEY,
    paused_reason   text,
    mode            text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- migrate:down

DROP TABLE IF EXISTS nightshift.queue_state;
