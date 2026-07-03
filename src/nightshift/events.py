"""The run event vocabulary.

Everything here has a live consumer; the JSONL run-record fold
(``RunStore``/``RunWriter``/``fan_out``) was retired with the legacy runners
in Phase 9. What survives, and why:

* :class:`Event` + :data:`Listener` — the in-process stream from the resolve
  runner (``resolve_runner``, ``manager/resolve_job``) to the outbound Slack
  notifier (``slack/notify``).
* The event-kind constants — the vocabulary those producers/consumers match
  on (``RUN_*``/``TASK_*`` and ``WORKER_STARTED``).
* :func:`new_run_id` — run-id minting for the manager (``manager/app``,
  ``manager/api_worker``).
* :func:`now_iso` — :class:`Event`'s own ``ts`` factory.

Durable history lives in the manager store's attempts + events tables.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


# Event types emitted over a run's lifetime.
RUN_STARTED = "run_started"
TASK_STARTED = "task_started"
TASK_LOG = "task_log"
TASK_STATUS = "task_status"
TASK_RESULT = "task_result"
RUN_FINISHED = "run_finished"
# Emitted once a worker subprocess is launched, carrying its OS pid.
WORKER_STARTED = "worker_started"


def now_iso() -> str:
    """UTC timestamp suitable for sorting and display."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    """A sortable, collision-resistant run id."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


@dataclass
class Event:
    """A single run event. ``payload`` carries type-specific fields."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "ts": self.ts, **self.payload}


Listener = Callable[[Event], None]
