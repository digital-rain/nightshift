"""Minimal asyncpg seam for Nightshift's Postgres store.

The manager store needs only two things from a Postgres client: a structural
pool type for annotations and a factory that opens a connection pool. Keeping
both here means the rest of the package never imports ``asyncpg`` directly, and
unit tests that exercise the in-memory store stay importable without the
``asyncpg`` C extension installed (the import is deferred to first use).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PgConnLike(Protocol):
    """The subset of ``asyncpg.Connection`` that Nightshift uses."""

    async def execute(
        self, query: str, *args: Any, timeout: float | None = ...
    ) -> str: ...

    async def fetch(
        self, query: str, *args: Any, timeout: float | None = ...
    ) -> list[Any]: ...

    async def fetchrow(
        self, query: str, *args: Any, timeout: float | None = ...
    ) -> Any: ...

    async def fetchval(
        self, query: str, *args: Any, column: int = ..., timeout: float | None = ...
    ) -> Any: ...


@runtime_checkable
class PgPoolLike(Protocol):
    """Structural type of an ``asyncpg.Pool``.

    ``acquire()`` returns an async context manager yielding a connection;
    ``close()`` shuts the pool down. That is all the store needs.
    """

    def acquire(
        self, *, timeout: float | None = ...
    ) -> AbstractAsyncContextManager[PgConnLike]: ...

    async def close(self) -> None: ...


async def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 5) -> Any:
    """Open an asyncpg pool. The import is lazy so tests run without asyncpg."""
    import asyncpg

    return await asyncpg.create_pool(dsn=dsn, min_size=min_size, max_size=max_size)
