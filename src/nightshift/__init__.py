"""Nightshift — a pull-based overnight agent task runner.

A *manager* owns the queues, the canonical task briefs, the centralized config,
Postgres-backed state, and the git landing authority. One or more *workers* poll
the manager, run and validate work with their configured backend, and submit the
result for the manager to land. See ``docs/setup-guide.md``.
"""

from __future__ import annotations


__version__ = "0.1.0"
