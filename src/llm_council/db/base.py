"""DB backend protocol — the facade API surface (P2).

Callers (council.py / availability.py / chat.py) NEVER talk to asyncpg or
aiosqlite directly; they go through the module-level functions in
`db/__init__.py`, which delegate to one `Backend` implementation selected from
the DATABASE_URL scheme:

  sqlite://...       -> db.sqlite  (aiosqlite, WAL, default)
  postgres(ql)://... -> db.postgres (asyncpg, optional [postgres] extra)

Conventions shared by every backend:

* Placeholders in caller SQL are `?` (qmark). The postgres backend rewrites
  them to positional dollar placeholders before executing.
* Timestamps are TEXT ISO-8601 UTC at the facade boundary on BOTH backends
  (postgres stores TIMESTAMPTZ but the backend converts datetime<->str at the
  boundary), so call sites never deal with two types.
* Rows are returned as plain dicts.
* `insert_returning_id` hides INSERT...RETURNING (postgres) vs lastrowid
  (sqlite) — RETURNING must never appear in call-site SQL.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

Params = tuple[Any, ...]


@runtime_checkable
class Backend(Protocol):
    """One database backend implementation (sqlite.py / postgres.py)."""

    async def init(self, database_url: str) -> None:
        """Open the pool/connection AND run the idempotent schema (init_db)."""
        ...

    async def close(self) -> None: ...

    async def fetchrow(self, sql: str, params: Params = ()) -> dict[str, Any] | None: ...

    async def fetch(self, sql: str, params: Params = ()) -> list[dict[str, Any]]: ...

    async def fetchval(self, sql: str, params: Params = ()) -> Any: ...

    async def execute(self, sql: str, params: Params = ()) -> None: ...

    async def insert_returning_id(self, sql: str, params: Params = ()) -> int:
        """Execute an INSERT (sql must NOT contain RETURNING) and return the new id."""
        ...
