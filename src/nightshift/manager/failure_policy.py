"""Per-queue failure/retry state (pure, no I/O).

Two-phase policy layered on top of the scheduler's normal dispatch:

* **Phase A (drain).** While ready (non-failed) tasks remain, a queue keeps
  running normally. Two *unrelated* failures in a row -- no landed success
  in between -- pause the queue. A single failure followed by a success
  disarms the watch (the spec's "at least one completed task in between is
  ok"). The fold itself lives in the transition table
  (``lifecycle.on_submit``/``on_land_result``): a transition reads the armed
  flag from ``SubmitPolicy`` and reports the new flag + any pause on its
  effects; this module only holds the per-queue :class:`QueueFailureState`
  the app wiring keeps between submits.
* **Phase B (retry).** Once only failed/blocked-retryable tasks remain, the
  manager (in ``worker_poll``) admits the earliest one back into dispatch —
  :func:`pick_retry`, a pure ordering helper over already-fetched retryable
  rows. If it fails again, the submit transition quarantines that task and
  pauses the queue with reason ``"retry_failed"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class QueueFailureState:
    """Phase-A watch for one queue: armed after exactly one failure, with no
    landed success since. A second failure while armed is the "two unrelated
    tasks failed in a row" trigger."""

    watch_armed: bool = False


def pick_retry(retryable: list[dict[str, Any]], *, order: list[str]) -> str | None:
    """The earliest retryable task in the queue's configured order.

    Tasks absent from ``order`` (e.g. manually created, never reordered) sort
    after every ordered task, preserving their relative ``retryable`` order.
    """
    if not retryable:
        return None
    rank = {task: i for i, task in enumerate(order)}
    ranked = sorted(
        range(len(retryable)),
        key=lambda i: (rank.get(retryable[i]["task"], len(rank)), i),
    )
    return retryable[ranked[0]]["task"]


__all__ = ["QueueFailureState", "pick_retry"]
