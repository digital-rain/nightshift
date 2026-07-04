"""API-compat projections over ``attempts`` rows (Phase 8).

The ``/api/runs*`` endpoints, the ``/api/leases`` endpoint, and the SSE
snapshot keys keep their pre-Phase-8 shapes byte-for-byte: :func:`run_view`
and :func:`lease_view` project the merged attempt row back to the previous
run-row and lease-row dicts. :func:`run_view` hides the lease-side columns
(``base_ref``, ``acquired_at``, ``heartbeat_at``, ``deadline_at``,
``released_at``) — :func:`lease_view` still serves them, ``deadline_at`` as
``expires_at``, exactly as the leases table did. Truly internal and absent
from BOTH views: ``branch_ref``, ``head_sha``, and ``state`` (which surfaces
only as each view's projected ``status``).
"""

from __future__ import annotations

from typing import Any

from nightshift.lifecycle import run_status_of


# The exact pre-Phase-8 run-row key set and order (the retired store's create_run /
# the runs table SELECT *). ``status`` is projected from ``state``; every
# other key is a passthrough column.
RUN_VIEW_KEYS = (
    "id", "task", "queue", "worker_id", "backend", "model", "repo",
    "required_mcps", "status", "phase", "result_line", "commit_sha", "loc",
    "remote", "pushed", "turns", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "usage",
    "cost_usd", "failure_kind", "failure_reason", "validate_cmd", "worktree",
    "title", "body", "started_at", "finished_at",
    # Additive (post-Phase-8): enhance-on-create attribution + the operator's
    # thumbs verdict on the run.
    "enhanced", "rating",
)

# The exact pre-Phase-8 lease-row key set and order (the retired store's acquire_lease
# / the leases table SELECT *). ``id`` and ``run_id`` are both the attempt id
# (the id value changed from a uuid to the run-id string; the UI treats it
# opaquely), ``status`` is always ``leased`` (only live attempts are
# projected), ``expires_at`` is the attempt's ``deadline_at``.
LEASE_VIEW_KEYS = (
    "id", "task", "queue", "worker_id", "run_id", "status", "model",
    "base_ref", "acquired_at", "heartbeat_at", "expires_at", "released_at",
)


def run_view(attempt: dict[str, Any]) -> dict[str, Any]:
    """Project an attempt row to the previous run-row dict shape."""
    return {
        key: run_status_of(attempt["state"]) if key == "status" else attempt.get(key)
        for key in RUN_VIEW_KEYS
    }


# The normalized record the shared analytics UI consumes. Distinct from
# ``run_view`` on purpose: ``run_view`` is a frozen byte-compatible shape that
# collapses LANDED/NO_CHANGE to ``completed`` and hides ``state``, but the
# tuning KPI ("cost per landed change") must tell a landed change from a
# no-change completion — so this projection exposes an explicit ``landed`` flag
# derived from the raw ``state`` (which the analytics endpoint alone surfaces).
# The worker UI produces the same shape from its local JSONL client-side; keep
# the two in sync.
ANALYTICS_RUN_KEYS = (
    "id", "task", "queue", "worker_id", "backend", "model", "repo",
    "status", "landed", "loc", "turns",
    "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens",
    "cost_usd", "usage", "failure_kind", "started_at", "finished_at",
    "enhanced", "rating",
)


def analytics_run_view(attempt: dict[str, Any]) -> dict[str, Any]:
    """Project an attempt row to the analytics record shape (see
    :data:`ANALYTICS_RUN_KEYS`). ``landed`` is true only for the LANDED state —
    a real change reached ``main`` — not for NO_CHANGE completions."""
    state = attempt.get("state")
    out: dict[str, Any] = {}
    for key in ANALYTICS_RUN_KEYS:
        if key == "status":
            out[key] = run_status_of(state) if state is not None else None
        elif key == "landed":
            out[key] = state == "landed"
        else:
            out[key] = attempt.get(key)
    return out


def lease_view(attempt: dict[str, Any]) -> dict[str, Any]:
    """Project a LIVE attempt row to the previous lease-row dict shape."""
    return {
        "id": attempt["id"],
        "task": attempt["task"],
        "queue": attempt["queue"],
        "worker_id": attempt["worker_id"],
        "run_id": attempt["id"],
        "status": "leased",
        "model": attempt.get("model"),
        "base_ref": attempt.get("base_ref"),
        "acquired_at": attempt.get("acquired_at"),
        "heartbeat_at": attempt.get("heartbeat_at"),
        "expires_at": attempt.get("deadline_at"),
        "released_at": None,
    }
