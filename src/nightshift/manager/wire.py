"""Manager wire shapes shared by the worker- and operator-facing APIs.

The request bodies of the worker API, the wiring protocols ``create_app``
injects into both API registrars, and the row-to-JSON coercion helper. Split
out of ``manager/api_worker.py`` in Phase 9 purely for module size (and so
``api_operator`` no longer imports from ``api_worker``); shapes and semantics
are unchanged — ``SubmitBody`` stays byte-compatible wire.
"""

from __future__ import annotations

from collections.abc import Awaitable
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel

from nightshift import repos
from nightshift.lifecycle import Outcome, RunStatus


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class CheckinBody(BaseModel):
    worker_id: str
    backend: str | None = None
    queues: list[str] | None = None
    priorities: list[int] | None = None
    # Advertised capabilities (operator-declared on the worker). ``models`` are
    # the request-facing model ids this worker can serve; ``mcps`` are the MCP
    # connectors wired into its harness. Both feed capability-based routing.
    models: list[str] | None = None
    mcps: list[str] | None = None
    meta: dict[str, Any] | None = None


class PollBody(BaseModel):
    worker_id: str
    backend: str | None = None
    queues: list[str] | None = None
    priorities: list[int] | None = None
    # The poll request *is* the routing filter: the manager returns the first
    # runnable task whose pinned model is in ``models`` (or is auto/max) and
    # whose required MCP set is a subset of ``mcps``.
    models: list[str] | None = None
    mcps: list[str] | None = None
    exclude_queues: list[str] | None = None


class HeartbeatBody(BaseModel):
    worker_id: str
    lease_id: str | None = None
    phase: str | None = None


class RunEventsBody(BaseModel):
    events: list[dict[str, Any]]


class SubmitBody(Outcome):
    """The worker's submit body: the unified :class:`Outcome` embedded flat
    (same wire keys as ever) plus the lease/task envelope."""

    worker_id: str
    lease_id: str
    task: str
    queue: str | None = None
    title: str
    # Wire-compat defaults kept from the pre-Outcome SubmitBody: a bare submit
    # is a completed, landable run with an optional backend.
    status: RunStatus = RunStatus.COMPLETED
    landable: bool = True
    backend: str | None = None  # type: ignore[assignment]
    # Worker-side quarantine flag: when the worker has quarantine mode enabled,
    # it sets this to True so the manager quarantines on the first failure
    # instead of waiting for the counter threshold
    # (RetryPolicy.immediate_quarantine).
    quarantine: bool = False


class ResolveResultBody(BaseModel):
    """Final outcome reported by an out-of-process resolve subprocess (see
    nightshift.manager.resolve_job)."""

    task: str
    queue: str | None = None
    # The original run that conflicted; updated alongside the resolve run so the
    # task's history reflects the eventual land.
    origin_run_id: str | None = None
    status: str = "error"
    landed: bool = False
    sha: str | None = None
    result_line: str | None = None
    failure_kind: str | None = None
    failure_reason: str | None = None
    loc: int | None = None
    remote: str | None = None
    pushed: bool | None = None
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class EmitFn(Protocol):
    """``create_app``'s event emitter: persist a state-change event and fan it
    out to every connected browser."""

    def __call__(
        self,
        kind: str,
        *,
        run_id: str | None = None,
        queue: str | None = None,
        task: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Awaitable[None]: ...


class StartResolveFn(Protocol):
    """``create_app``'s resolve spawner: create a resolve run + launch the
    out-of-process resolver, returning ``(started, child_run_id, error)``."""

    def __call__(
        self,
        origin_run_id: str,
        *,
        task: str,
        queue: str | None,
        repo: str,
        title: str,
    ) -> Awaitable[tuple[bool, str | None, str | None]]: ...


class BroadcastFn(Protocol):
    """``create_app``'s SSE fan-out: publish one already-persisted event to
    every connected browser (no store write — the outbox half of ``_emit``)."""

    def __call__(self, event: dict[str, Any]) -> Awaitable[None]: ...


def jsonable(row: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce datetimes/UUIDs/Decimals to JSON-safe values.

    Postgres hands ``numeric`` columns (cost_usd, avg_turns, …) back as
    ``Decimal``, which ``json.dumps`` can't serialize — coerce those to float so
    the stats/runs endpoints don't 500 under the PgStore.
    """
    if row is None:
        return {}
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def normalize_repo(value: object) -> str | None:
    """Validate an optional per-task repo override from a request payload.

    ``None`` / ``""`` / ``"default"`` clear the override (the task then inherits
    the queue default); any other value must be a bare workspace-child slug or
    it is rejected as a 400 (the path-traversal guard) — surfaced at edit time
    rather than silently written and only caught later at dispatch. Mirrors the
    legacy server's guard so the shared UI behaves identically on both backends.
    """
    if value in (None, "", "default"):
        return None
    repo = str(value).strip()
    if not repo:
        return None
    if not repos.is_valid_repo_ref(repo):
        raise ValueError(
            f"invalid repo reference {repo!r}: a repo must be a bare workspace "
            "child name matching [a-z0-9][a-z0-9-]* (no paths, '..', '/', or "
            "absolute paths)"
        )
    return repo
