"""P2 SQLite backend: init idempotency, final-8 schema, legacy drops, WAL."""

import asyncio

import pytest

from llm_council import db
from llm_council.db.schema import FINAL_TABLES, LEGACY_TABLES


@pytest.fixture()
def lite(tmp_path):
    url = f"sqlite:///{tmp_path}/state.db"

    async def _open():
        await db.init_pool(url)
        return db.backend()

    backend = asyncio.run(_open())
    yield backend, url
    asyncio.run(db.close_pool())


def _tables():
    rows = asyncio.run(
        db.fetch("SELECT name FROM sqlite_master WHERE type='table'")
    )
    return {r["name"] for r in rows}


def test_init_creates_exactly_8_final_tables(lite):
    tables = _tables() - {"sqlite_sequence"}
    assert tables == set(FINAL_TABLES)
    for legacy in LEGACY_TABLES:
        assert legacy not in tables


def test_init_is_idempotent(lite):
    backend, _url = lite
    asyncio.run(backend._init_db())  # second run: no-op, no error
    asyncio.run(backend._init_db())  # third run: still fine
    assert _tables() - {"sqlite_sequence"} == set(FINAL_TABLES)


def test_wal_mode_on(lite):
    assert asyncio.run(db.fetchval("PRAGMA journal_mode")) == "wal"


def test_foreign_keys_and_busy_timeout(lite):
    assert asyncio.run(db.fetchval("PRAGMA foreign_keys")) == 1
    assert asyncio.run(db.fetchval("PRAGMA busy_timeout")) == 5000


def test_legacy_tables_dropped_on_init(tmp_path):
    """A DB carrying pre-P2 legacy tables has them dropped (and renames applied)."""

    async def _seed():
        import aiosqlite

        async with aiosqlite.connect(tmp_path / "state.db") as conn:
            await conn.executescript(
                """
                CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER);
                CREATE TABLE model_switches (id INTEGER PRIMARY KEY);
                CREATE TABLE pending_attachments (id INTEGER PRIMARY KEY);
                CREATE TABLE model_health (model TEXT PRIMARY KEY, status TEXT NOT NULL,
                                           reason TEXT, last_error TEXT,
                                           cooldown_until TEXT, checked_at TEXT NOT NULL);
                INSERT INTO model_health VALUES ('glm-5.2','ok',NULL,NULL,NULL,
                                                 '2026-01-01T00:00:00+00:00');
                CREATE TABLE council_hats (
                    id INTEGER PRIMARY KEY, council_id INTEGER, hat TEXT, model TEXT,
                    session_id INTEGER, status TEXT, answer TEXT, error TEXT,
                    usage TEXT, claude_session_id TEXT,
                    created_at TEXT, updated_at TEXT);
                INSERT INTO council_hats (id, council_id, hat, model, claude_session_id)
                    VALUES (1, 1, 'hat1', 'glm-5.2', 'cli-session-xyz');
                """
            )

    asyncio.run(_seed())

    async def _init():
        await db.init_pool(f"sqlite:///{tmp_path}/state.db")
        return db.backend()

    backend = asyncio.run(_init())
    try:
        tables = _tables()
        for legacy in LEGACY_TABLES + ("model_health",):
            assert legacy not in tables
        # rename carried the row forward with seat='legacy'
        row = asyncio.run(db.fetchrow("SELECT * FROM seat_health"))
        assert row["seat"] == "legacy" and row["model"] == "glm-5.2"
        # claude_session_id renamed to session_id; seat_backend added
        cols = {r["name"] for r in asyncio.run(db.fetch("PRAGMA table_info(council_hats)"))}
        assert "session_id" in cols and "claude_session_id" not in cols
        assert "seat_backend" in cols
        hat = asyncio.run(db.fetchrow("SELECT * FROM council_hats"))
        assert hat["session_id"] == "cli-session-xyz"
        asyncio.run(backend._init_db())  # still idempotent after migration
    finally:
        asyncio.run(db.close_pool())


def test_concurrent_writers_no_lock_errors(lite):
    """4 parallel writers under WAL: no 'database is locked' (single-writer queue)."""

    async def _run():
        async def writer(n: int) -> None:
            for _i in range(20):
                await db.execute(
                    "INSERT INTO mcp_instances (instance_id, last_seen) VALUES (?, ?) "
                    "ON CONFLICT (instance_id) DO UPDATE SET last_seen=?",
                    (f"w{n}", db.utcnow(), db.utcnow()),
                )

        await asyncio.gather(*(writer(n) for n in range(4)))
        return await db.fetchval("SELECT count(*) FROM mcp_instances")

    assert asyncio.run(_run()) == 4
