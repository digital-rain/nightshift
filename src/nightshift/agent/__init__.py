"""In-house agentic backend — the ``nightshift`` provider.

A self-contained agentic harness nightshift owns end to end: it drives the model
APIs already reached over ``httpx`` (Phase 5 ``transport``), runs its own tool
loop (Phase 5 ``loop``) against a sandboxed registry (Phase 2 ``tools``), and
applies edits with a deterministic SEARCH/REPLACE applier (Phase 1 ``apply``) —
no apply-model round-trip.

Modules here import :mod:`nightshift.model_id` freely (it is dependency-free)
but reach ``engine``/``backends`` only lazily, to stay clear of the documented
``backends``↔``engine`` import cycle.
"""

from __future__ import annotations
