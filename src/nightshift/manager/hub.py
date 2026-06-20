"""SSE broadcast hub — push-based multi-client convergence.

Every mutating handler (and every worker callback) publishes a state-change
event here; the hub fans it out to all connected browsers in process and the
store persists it (the durable cursor source). A browser that connects
mid-flight first receives a **snapshot** frame (current queue order, leases,
now-executing, workers, recent history) and then the live **delta** stream, so
it is correct on arrival rather than after the first change.

This is the in-process mechanism the plan calls for; a multi-process manager
would back the same fan-out with Postgres ``LISTEN/NOTIFY`` (the events table is
already the durable cursor), but a single manager process needs only this.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any


SnapshotFn = Callable[[], Awaitable[dict[str, Any]]]

# Bounded so a stalled browser can't grow memory without limit; an overflowing
# subscriber is dropped and must reconnect (its next snapshot re-syncs it).
_QUEUE_MAX = 1000


def sse_frame(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, default=str)}\n\n"


class Hub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    async def publish(self, event: dict[str, Any]) -> None:
        """Fan a persisted event row out to every live subscriber."""
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    async def stream(
        self,
        snapshot_fn: SnapshotFn,
        *,
        heartbeat_seconds: float = 15.0,
    ) -> AsyncIterator[str]:
        """Yield SSE frames: a snapshot frame, then live deltas.

        Subscribes *before* snapshotting and records the snapshot's event cursor,
        so any event that races the snapshot is delivered exactly once (deltas at
        or below the cursor are already reflected in the snapshot and skipped).
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subscribers.add(q)
        try:
            snap = await snapshot_fn()
            cursor = int(snap.get("cursor", 0))
            yield sse_frame({"type": "snapshot", **snap})
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=heartbeat_seconds)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if int(event.get("id", 0)) <= cursor:
                    continue
                yield sse_frame({"type": "event", **event})
        finally:
            self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


async def replay_into(
    hub: Hub, queue: asyncio.Queue[dict[str, Any]]
) -> None:  # pragma: no cover - reserved for LISTEN/NOTIFY bridge
    """Bridge an external (e.g. LISTEN/NOTIFY) source into the hub. Reserved for
    the multi-process deployment; unused by the single-process manager."""
    with contextlib.suppress(asyncio.CancelledError):
        while True:
            event = await queue.get()
            await hub.publish(event)
