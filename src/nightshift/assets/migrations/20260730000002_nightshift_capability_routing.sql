-- migrate:up

-- Capability-based routing + first-class MCP. Workers now advertise the models
-- they can serve and the MCP connectors wired into their harness; the manager
-- routes a task to the first polling worker whose capabilities cover it. Queue
-- dedication (a manager-side queue -> worker binding) lets the operator fence an
-- external system to a specific worker by configuration alone.

-- Advertised worker capabilities (operator-declared, sent on every checkin/poll).
-- `models` = request-facing model ids this worker accepts; a task pinning one of
-- these routes here regardless of which harness runs it. `mcps` = MCP connectors
-- this worker's harness has configured.
ALTER TABLE nightshift.workers
    ADD COLUMN models jsonb NOT NULL DEFAULT '[]',
    ADD COLUMN mcps   jsonb NOT NULL DEFAULT '[]';

-- The MCP connectors a brief declared (frontmatter `mcp:`). Recorded per run so
-- the operator can see a task's declared blast radius in History.
ALTER TABLE nightshift.runs
    ADD COLUMN required_mcps jsonb NOT NULL DEFAULT '[]';

-- Manager-side queue dedication: a queue's tasks are only offered to its bound
-- worker(s). Multiple rows per queue = a small dedicated pool. No row for a queue
-- = open to any matching worker (the default). `worker_id` is intentionally not a
-- foreign key: an operator may dedicate a queue to a worker before that worker
-- first checks in (the routing layer blocks the queue until it comes online).
CREATE TABLE nightshift.queue_routing (
    queue     text NOT NULL,                 -- queue label ('main' or playlist name)
    worker_id text NOT NULL,
    PRIMARY KEY (queue, worker_id)
);

CREATE INDEX queue_routing_queue_idx ON nightshift.queue_routing (queue);

-- migrate:down
DROP TABLE IF EXISTS nightshift.queue_routing;
ALTER TABLE nightshift.runs DROP COLUMN IF EXISTS required_mcps;
ALTER TABLE nightshift.workers
    DROP COLUMN IF EXISTS mcps,
    DROP COLUMN IF EXISTS models;
