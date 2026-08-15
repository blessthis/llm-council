"""SQLite backend (aiosqlite) — the zero-config default store (P2).

* WAL mode ON, foreign_keys ON, busy_timeout 5000 ms.
* Placeholders `?` natively.
* A single shared connection + an asyncio write lock = the single-writer queue;
  WAL gives concurrent readers while one writer is active.
* Timestamps are TEXT ISO-8601 UTC, produced by callers via db.utcnow().

init_db() is idempotent (Q22): CREATE TABLE IF NOT EXISTS + PRAGMA-guarded
renames/adds for pre-freeze databases, and drops the 4 legacy tables.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import aiosqlite

from ..db import PoolClosedError
from .schema import FINAL_TABLES, LEGACY_TABLES, SQLITE_SCHEMA


class SqliteBackend:
    def __init__(self) -> None:
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def init(self, database_url: str) -> None:
        path = database_url[len("sqlite:///"):] if database_url.startswith("sqlite:///") \
            else database_url[len("sqlite:"):]
        path = os.path.expanduser(path)
        parent = os.path.dirname(os.path.abspath(path))
        created = not os.path.isdir(parent)
        os.makedirs(parent, exist_ok=True)
        if created:
            os.chmod(parent, 0o700)  # Q21: state dir holds local seat state
        self._conn = await aiosqlite.connect(path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._init_db()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ #
    # Idempotent schema
    # ------------------------------------------------------------------ #

    async def _table_exists(self, name: str) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return await cur.fetchone() is not None

    async def _columns(self, table: str) -> list[str]:
        cur = await self._conn.execute(f"PRAGMA table_info({table})")
        return [r[1] for r in await cur.fetchall()]

    async def _init_db(self) -> None:
        """Run on every startup; a no-op when the schema is already current."""
        conn = self._conn
        # Pre-freeze renames BEFORE creating the final tables, so an existing
        # model_health / council_hats(claude_session_id) DB is carried forward.
        if await self._table_exists("model_health") and not await self._table_exists(
            "seat_health"
        ):
            await conn.execute("ALTER TABLE model_health RENAME TO seat_health")
        if await self._table_exists("seat_health"):
            cols = await self._columns("seat_health")
            if "seat" not in cols:
                await conn.execute(
                    "ALTER TABLE seat_health ADD COLUMN seat TEXT NOT NULL DEFAULT 'legacy'"
                )
        if await self._table_exists("council_hats"):
            cols = await self._columns("council_hats")
            if "claude_session_id" in cols:
                if "session_id" in cols:
                    # Pre-P2 DBs had session_id BIGINT REFERENCES sessions(id) — dead now.
                    await conn.execute("ALTER TABLE council_hats DROP COLUMN session_id")
                await conn.execute(
                    "ALTER TABLE council_hats RENAME COLUMN claude_session_id TO session_id"
                )
            if "seat_backend" not in await self._columns("council_hats"):
                await conn.execute("ALTER TABLE council_hats ADD COLUMN seat_backend TEXT")
        if await self._table_exists("councils"):
            if "council_uuid" not in await self._columns("councils"):
                await conn.execute("ALTER TABLE councils ADD COLUMN council_uuid TEXT")
        # Legacy tables are dropped, never migrated (fresh-install path; the PG
        # migration script is the data-carrying route).
        for legacy in LEGACY_TABLES:
            await conn.execute(f"DROP TABLE IF EXISTS {legacy}")
        await conn.executescript(SQLITE_SCHEMA)
        await conn.commit()

    # ------------------------------------------------------------------ #
    # Facade API
    # ------------------------------------------------------------------ #

    def _conn_or_raise(self) -> aiosqlite.Connection:
        # Shared single connection: closed == pool closed. Background tasks may
        # still hold references to the facade after close_pool(); surface that
        # cleanly instead of aiosqlite's "no active connection" ValueError.
        if self._conn is None:
            raise PoolClosedError("sqlite connection is closed")
        return self._conn

    async def _exec(self, sql: str, params: tuple) -> aiosqlite.Cursor:
        # Race guard: close() may have closed the underlying aiosqlite connection
        # between the None-check and this call — aiosqlite then raises
        # ValueError("no active connection"), which we surface as PoolClosedError.
        try:
            return await self._conn_or_raise().execute(sql, params)
        except ValueError as e:
            raise PoolClosedError(f"sqlite connection is closed: {e}") from e

    async def fetchrow(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        cur = await self._exec(sql, params)
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def fetch(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = await self._exec(sql, params)
        return [dict(r) for r in await cur.fetchall()]

    async def fetchval(self, sql: str, params: tuple = ()) -> Any:
        row = await self.fetchrow(sql, params)
        return next(iter(row.values())) if row else None

    async def execute(self, sql: str, params: tuple = ()) -> None:
        async with self._write_lock:
            await self._exec(sql, params)
            await self._conn.commit()

    async def insert_returning_id(self, sql: str, params: tuple = ()) -> int:
        async with self._write_lock:
            cur = await self._exec(sql, params)
            await self._conn.commit()
            return int(cur.lastrowid)


# Re-exported for the acceptance check / tests.
__all__ = ["SqliteBackend", "FINAL_TABLES"]
