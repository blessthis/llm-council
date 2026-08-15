"""DB lifecycle resilience (bug: 'no active connection' after close_pool).

Background seat tasks spawned by council.start persist status via db.execute
AFTER the pool may have been closed (process shutdown). The facade must raise
PoolClosedError, _safe must swallow it with a warning, the heartbeat must exit
cleanly, and poll(wait=True) must return an {error} dict instead of hanging
forever on a dead DB.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest

from llm_council import council, db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_backend", None)
    url = f"sqlite:///{tmp_path / 'state.db'}"
    asyncio.get_event_loop_policy()  # ensure a loop exists for lazy init paths
    loop = asyncio.new_event_loop()
    yield url, loop
    loop.close()
    # force-close any leftover backend so later tests start clean
    if db._backend is not None:
        l2 = asyncio.new_event_loop()
        try:
            l2.run_until_complete(db.close_pool())
        finally:
            l2.close()


def test_pool_closed_error_raised(tmp_db):
    url, loop = tmp_db

    async def main():
        await db.init_pool(url)
        await db.execute(
            "INSERT INTO mcp_instances (instance_id, last_seen) VALUES (?, ?)",
            (uuid.uuid4().hex, db.utcnow()),
        )
        await db.close_pool()
        with pytest.raises(db.PoolClosedError):
            await db.execute(
                "INSERT INTO mcp_instances (instance_id, last_seen) VALUES (?, ?)",
                (uuid.uuid4().hex, db.utcnow()),
            )

    loop.run_until_complete(main())


def test_safe_swallows_pool_closed(tmp_db, caplog):
    url, loop = tmp_db

    async def main():
        await db.init_pool(url)
        await db.close_pool()
        with caplog.at_level(logging.WARNING, logger="llm_council.council"):
            await council._safe(
                council._mark_running(123), "mark_running(hat=123)"
            )
        assert "pool closed, dropping write" in caplog.text

    loop.run_until_complete(main())


def test_background_write_task_survives_pool_close(tmp_db, caplog):
    """The actual bug repro: start a slow background write, close the pool
    mid-flight, the task must complete without raising."""
    url, loop = tmp_db

    async def slow_write():
        await asyncio.sleep(0.05)
        await council._mark_running(42)  # pool is closed by now

    async def main():
        await db.init_pool(url)
        task = asyncio.create_task(council._safe(slow_write(), "mark_running(hat=42)"))
        await db.close_pool()
        with caplog.at_level(logging.WARNING, logger="llm_council.council"):
            await asyncio.wait_for(task, timeout=5)  # must not raise / hang

    loop.run_until_complete(main())
    assert "pool closed, dropping write" in caplog.text


def test_heartbeat_exits_on_pool_close(tmp_db):
    url, loop = tmp_db

    async def main():
        await db.init_pool(url)
        council._hb_task = None
        council.start_heartbeat()
        hb = council._hb_task
        assert hb is not None
        await db.close_pool()
        await asyncio.sleep(council._HEARTBEAT_SECONDS + 1)
        assert hb.done(), "heartbeat must exit after pool close, not spin forever"
        hb.result()  # returns None — clean exit, no PoolClosedError/ValueError

    loop.run_until_complete(main())


def test_poll_returns_error_dict_on_closed_pool(tmp_db):
    url, loop = tmp_db

    async def main():
        await db.init_pool(url)
        await db.close_pool()
        out = await asyncio.wait_for(council.poll(999, wait=True, timeout=10), timeout=15)
        assert "error" in out
        assert out["error"]["code"] == "db_unavailable"

    loop.run_until_complete(main())
