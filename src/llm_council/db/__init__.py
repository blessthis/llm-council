"""Async DB facade (P2): lazy backend selection from DATABASE_URL scheme.

  unset / sqlite:///...        -> SQLite (aiosqlite), default
                                 sqlite:///$HOME/.blessthis-llm-council/state.db
  postgres(ql)://...           -> Postgres (asyncpg, [postgres] extra)

Callers write `?` placeholders and pass a params tuple; each backend renders
its own placeholder style (`?` native on SQLite, rewritten to `$N` on
Postgres). Rows come back as plain dicts with timestamps as ISO-8601 UTC TEXT
on both backends.

NOTE: council reaping is deliberately NOT done here. It is ownership-aware and
lives in council.register_instance(), called ONCE by the server on startup —
reaping in init_pool would let any process that merely opens the pool (an
ad-hoc script, another project's MCP server) nuke councils that are actively
running in a different live process.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .base import Backend


class PoolClosedError(RuntimeError):
    """Raised by the db facade when the pool/backend has been closed (or was
    never opened) — e.g. a background seat task writing status after shutdown.
    Callers like council._safe treat this as expected, not exceptional."""


_backend: Backend | None = None


def utcnow() -> str:
    """Current time as TEXT ISO-8601 UTC — the only timestamp format (A12)."""
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value: Any) -> datetime | None:
    """Parse a facade timestamp (ISO str, possibly datetime from a raw backend)
    into an aware datetime; None if unset."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def init_pool(database_url: str) -> Backend:
    """Open the backend selected by the DATABASE_URL scheme and run init_db()."""
    global _backend
    if _backend is None:
        if database_url.startswith(("postgres://", "postgresql://")):
            from .postgres import PostgresBackend  # asyncpg imported inside

            backend: Backend = PostgresBackend()
        elif database_url.startswith("sqlite:"):
            from .sqlite import SqliteBackend

            backend = SqliteBackend()
        else:
            raise RuntimeError(
                f"unsupported DATABASE_URL scheme: {database_url!r} "
                "(expected sqlite:///... or postgres(ql)://...)"
            )
        await backend.init(database_url)
        _backend = backend
    return _backend


def backend() -> Backend:
    if _backend is None:
        raise PoolClosedError(
            "DB pool is closed (init_pool() not called or close_pool() already ran)"
        )
    return _backend


async def close_pool() -> None:
    global _backend
    if _backend is not None:
        await _backend.close()
        _backend = None


# Convenience wrappers so call sites never touch the backend object.

async def fetchrow(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    return await backend().fetchrow(sql, params)


async def fetch(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return await backend().fetch(sql, params)


async def fetchval(sql: str, params: tuple = ()) -> Any:
    return await backend().fetchval(sql, params)


async def execute(sql: str, params: tuple = ()) -> None:
    await backend().execute(sql, params)


async def insert_returning_id(sql: str, params: tuple = ()) -> int:
    return await backend().insert_returning_id(sql, params)
