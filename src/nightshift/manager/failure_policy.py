"""Per-queue failure/retry state machine (pure, no I/O).

Two-phase policy layered on top of the scheduler's normal dispatch:

* **Phase A (drain).** While ready (non-failed) tasks remain, a queue keeps
  running normally. Two *unrelated* failures in a row -- no landed success
  in between -- pause the queue. A single failure followed by a success
  disarms the watch (the spec's "at least one completed task in between is
  ok").
* **Phase B (retry).** Once only failed/blocked-retryable tasks remain, the
  manager (in ``app.py``) admits the earliest one back into dispatch. If it
  fails again, ``app.py`` quarantines that task and pauses the queue with
  reason ``"retry_failed"`` -- handled by the caller, not here, since it
  needs the store and the quarantine helper.

This module only tracks the phase-A watch; phase-B task selection is
:func:`pick_retry`, a pure ordering helper over already-fetched retryable
rows.
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


def record_outcome(state: QueueFailureState, *, is_failure: bool | None) -> bool:
    """Fold one task outcome into ``state``. Returns True iff this outcome is
    the second consecutive failure (the caller should pause the queue).

    ``is_failure``: True (worker error / honest block), False (a landed
    success), or None (neutral -- no-change/aborted/skipped: neither arms nor
    disarms, mirroring the existing no-progress-streak semantics).
    """
    if is_failure is None:
        return False
    if not is_failure:
        state.watch_armed = False
        return False
    if state.watch_armed:
        return True
    state.watch_armed = True
    return False


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


__all__ = ["QueueFailureState", "record_outcome", "pick_retry"]
