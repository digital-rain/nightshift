"""HTTP client the worker uses to talk to the manager.

Synchronous (the worker's execution path is subprocess-bound and serial), thin
wrapper over the manager's ``/api/worker/*`` endpoints with the optional
shared-secret header attached to every call.
"""

from __future__ import annotations

from typing import Any

import httpx


class ManagerClient:
    def __init__(
        self,
        base_url: str,
        *,
        shared_secret: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        headers = {"X-Nightshift-Secret": shared_secret} if shared_secret else {}
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def checkin(
        self,
        worker_id: str,
        *,
        backend: str | None = None,
        queues: list[str] | None,
        priorities: list[int] | None,
        models: list[str] | None = None,
        mcps: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resp = self._http.post(
            "/api/worker/checkin",
            json={
                "worker_id": worker_id,
                "backend": backend,
                "queues": queues,
                "priorities": priorities,
                "models": models,
                "mcps": mcps,
                "meta": meta,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def poll(
        self,
        worker_id: str,
        *,
        backend: str | None = None,
        queues: list[str] | None,
        priorities: list[int] | None,
        models: list[str] | None = None,
        mcps: list[str] | None = None,
    ) -> dict[str, Any] | None:
        resp = self._http.post(
            "/api/worker/poll",
            json={
                "worker_id": worker_id,
                "backend": backend,
                "queues": queues,
                "priorities": priorities,
                "models": models,
                "mcps": mcps,
            },
        )
        resp.raise_for_status()
        return resp.json().get("work")

    def heartbeat(
        self, worker_id: str, *, lease_id: str | None = None, phase: str | None = None
    ) -> None:
        try:
            self._http.post(
                "/api/worker/heartbeat",
                json={"worker_id": worker_id, "lease_id": lease_id, "phase": phase},
            )
        except httpx.HTTPError:
            pass  # heartbeat is best-effort; the next one re-establishes liveness

    def post_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        try:
            self._http.post(f"/api/worker/runs/{run_id}/events", json={"events": events})
        except httpx.HTTPError:
            pass  # log streaming is best-effort; the run record is authoritative

    def submit(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post(f"/api/worker/runs/{run_id}/submit", json=payload)
        resp.raise_for_status()
        return resp.json()

    def resolve_result(self, run_id: str, payload: dict[str, Any]) -> None:
        """Report an out-of-process resolve's final outcome to the manager.

        Best-effort: the resolve subprocess has already mutated git (the land is
        durable); a failed report only delays the manager's bookkeeping, which a
        later operator action or stale-run reconcile can still correct."""
        try:
            self._http.post(
                f"/api/worker/runs/{run_id}/resolve-result", json=payload
            ).raise_for_status()
        except httpx.HTTPError:
            pass
