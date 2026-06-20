"""Nightshift manager — queues, scheduling, git authority, centralized config.

The manager is the authoritative half of the split runtime: it owns the
canonical ``.tasks/`` briefs and ``config.json``, the Postgres ``nightshift``
schema (workers / leases / runs / events / stats), cross-queue scheduling, the
landing lock + git authority, and serves the operator UI. Workers talk to it
over HTTP; they never touch Postgres directly.
"""

from __future__ import annotations
