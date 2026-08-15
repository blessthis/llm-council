"""Postgres backend (asyncpg) — optional, for server/multi-user deployments.

ONLY this module imports asyncpg ([postgres] extra, Decision #4), and ONLY this
module contains Postgres-isms: `$N` placeholders (rewritten from caller `?`),
RETURNING (inside insert_returning_id), TIMESTAMPTZ. Rows are converted to
plain dicts with datetimes rendered as ISO-8601 UTC strings, so the facade
boundary is identical to the SQLite backend.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .schema import LEGACY_TABLES, POSTGRES_SCHEMA

# ISO-8601 strings are adapted to datetime for TIMESTAMPTZ columns (asyncpg
# cannot encode str -> timestamptz).
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2}|Z)?$"
)

_QMARK_RE = re.compile(r"\?")


def to_dollar(sql: str) -> str:
    """Rewrite `?` placeholders to `$1, $2, ...` (callers always write `?`)."""
    n = 0

    def _sub(_m: re.Match) -> str:
        nonlocal n
        n += 1
        return f"${n}"

    return _QMARK_RE.sub(_sub, sql)


def _adapt_param(value: Any) -> Any:
    if isinstance(value, str) and _ISO_RE.match(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


def _render_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return value


class PostgresBackend:
    def __init__(self) -> None:
        self._pool: Any = None  # asyncpg.Pool

    async def init(self, database_url: str) -> None:
        try:
            import asyncpg
        except ImportError as e:
            raise RuntimeError(
                "asyncpg is not installed — install blessthis-llm-council[postgres] "
                "to use a postgres:// DATABASE_URL."
            ) from e
        self._pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=10)
        async with self._pool.acquire() as conn:
            await self._init_db(conn)
        # NOTE: council reaping is deliberately NOT done here. It is ownership-aware
        # and lives in council.register_instance(), called ONCE by the server on
        # startup. Reaping in init would let any process that merely opens the pool
        # nuke councils that are actively running in a different live process.

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------ #
    # Idempotent schema (guards via information_schema — PG has no
    # DROP COLUMN IF EXISTS prior to 9.6 patterns we can rely on).
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _table_exists(conn, name: str) -> bool:
        return bool(
            await conn.fetchval(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=$1",
                name,
            )
        )

    @staticmethod
    async def _columns(conn, table: str) -> set[str]:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1",
            table,
        )
        return {r["column_name"] for r in rows}

    async def _init_db(self, conn) -> None:
        # Pre-freeze renames (guarded).
        if await self._table_exists(conn, "model_health") and not await self._table_exists(
            conn, "seat_health"
        ):
            await conn.execute("ALTER TABLE model_health RENAME TO seat_health")
        if await self._table_exists(conn, "seat_health"):
            cols = await self._columns(conn, "seat_health")
            if "seat" not in cols:
                await conn.execute(
                    "ALTER TABLE seat_health "
                    "ADD COLUMN seat TEXT NOT NULL DEFAULT 'legacy'"
                )
        if await self._table_exists(conn, "council_hats"):
            cols = await self._columns(conn, "council_hats")
            if "claude_session_id" in cols:
                if "session_id" in cols:
                    # Pre-P2 DBs had session_id BIGINT REFERENCES sessions(id) — dead now.
                    await conn.execute("ALTER TABLE council_hats DROP COLUMN session_id")
                await conn.execute(
                    "ALTER TABLE council_hats "
                    "RENAME COLUMN claude_session_id TO session_id"
                )
            if "seat_backend" not in cols:
                await conn.execute("ALTER TABLE council_hats ADD COLUMN seat_backend TEXT")
        if await self._table_exists(conn, "councils"):
            if "council_uuid" not in await self._columns(conn, "councils"):
                await conn.execute("ALTER TABLE councils ADD COLUMN council_uuid TEXT")
        for legacy in LEGACY_TABLES:
            await conn.execute(f"DROP TABLE IF EXISTS {legacy} CASCADE")
        await conn.execute(POSTGRES_SCHEMA)

    # ------------------------------------------------------------------ #
    # Facade API
    # ------------------------------------------------------------------ #

    @staticmethod
    def _render_row(row: Any) -> dict[str, Any]:
        return {k: _render_value(v) for k, v in dict(row).items()}

    async def fetchrow(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(to_dollar(sql), *[_adapt_param(p) for p in params])
        return self._render_row(row) if row is not None else None

    async def fetch(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(to_dollar(sql), *[_adapt_param(p) for p in params])
        return [self._render_row(r) for r in rows]

    async def fetchval(self, sql: str, params: tuple = ()) -> Any:
        async with self._pool.acquire() as conn:
            val = await conn.fetchval(to_dollar(sql), *[_adapt_param(p) for p in params])
        return _render_value(val)

    async def execute(self, sql: str, params: tuple = ()) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(to_dollar(sql), *[_adapt_param(p) for p in params])

    async def insert_returning_id(self, sql: str, params: tuple = ()) -> int:
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    to_dollar(sql) + " RETURNING id",
                    *[_adapt_param(p) for p in params],
                )
            )
