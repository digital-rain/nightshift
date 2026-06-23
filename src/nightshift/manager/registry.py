"""Worker registry — thin policy over the store's ``workers`` table.

Tracks which workers are alive (heartbeat TTL), what backends are currently
available (so the scheduler can decide a pinned model is unsatisfiable), and
produces the snapshot the operator Workers page renders.
"""

from __future__ import annotations

from typing import Any

from nightshift.manager.store import NightshiftStore


class Registry:
    def __init__(self, store: NightshiftStore, *, stale_seconds: float) -> None:
        self._store = store
        self._stale_seconds = stale_seconds

    async def checkin(
        self,
        worker_id: str,
        *,
        backend: str,
        queues: list[str] | None,
        priorities: list[int] | None,
        models: list[str] | None = None,
        mcps: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._store.register_worker(
            worker_id,
            backend=backend,
            queues=queues,
            priorities=priorities,
            models=models,
            mcps=mcps,
            meta=meta,
        )

    async def heartbeat(self, worker_id: str) -> None:
        await self._store.heartbeat_worker(worker_id)

    async def set_busy(
        self, worker_id: str, *, task: str, queue: str | None, run_id: str
    ) -> None:
        await self._store.set_worker_status(
            worker_id,
            status="busy",
            current_task=task,
            current_queue=queue,
            current_run_id=run_id,
        )

    async def set_idle(self, worker_id: str) -> None:
        await self._store.set_worker_status(worker_id, status="idle")

    async def reap_stale(self) -> list[str]:
        """Mark silent workers offline. Returns the ids transitioned."""
        return await self._store.expire_stale_workers(self._stale_seconds)

    async def _live_workers(self) -> list[dict[str, Any]]:
        return [w for w in await self._store.list_workers() if w["status"] != "offline"]

    async def available_models(self) -> set[str]:
        """Union of models advertised by every non-offline worker.

        Used to decide whether a model-pinned task is currently routable; a task
        whose model no live worker advertises is *blocked*, not merely waiting.
        """
        out: set[str] = set()
        for w in await self._live_workers():
            out.update(str(m) for m in (w.get("models") or []))
        return out

    async def models_for_queue(self, queue_label: str) -> list[str]:
        """Models advertised by live workers that can serve *queue_label*.

        A worker serves a queue when its ``queues`` is None (any) or the label
        is in its list. Returns a sorted deduplicated list suitable for the UI
        model dropdown.
        """
        out: set[str] = set()
        for w in await self._live_workers():
            wq = w.get("queues")
            if wq is None or queue_label in wq:
                out.update(str(m) for m in (w.get("models") or []))
        return sorted(out)

    async def available_mcps(self) -> set[str]:
        """Union of MCP connectors advertised by every non-offline worker."""
        out: set[str] = set()
        for w in await self._live_workers():
            out.update(str(m) for m in (w.get("mcps") or []))
        return out

    async def online_worker_ids(self) -> set[str]:
        """Ids of every non-offline worker (for queue-dedication liveness)."""
        return {w["id"] for w in await self._live_workers()}

    async def snapshot(self) -> list[dict[str, Any]]:
        """Workers for the operator UI (id, backend, routing, status, current)."""
        return await self._store.list_workers()
