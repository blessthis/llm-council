"""P2: chat_sessions / chat_messages columns match Decision #19 exactly."""

import asyncio

import pytest

from llm_council import db

CHAT_SESSIONS_COLS = [
    "id", "seat", "model", "seat_backend", "working_dir", "system_prompt",
    "cli_session_id", "status", "created_at", "last_activity", "closed",
]
CHAT_MESSAGES_COLS = [
    "id", "chat_session_id", "role", "content", "usage", "created_at",
]


@pytest.fixture()
def lite(tmp_path):
    asyncio.run(db.init_pool(f"sqlite:///{tmp_path}/state.db"))
    yield
    asyncio.run(db.close_pool())


def _cols(table: str) -> list[str]:
    rows = asyncio.run(db.fetch(f"PRAGMA table_info({table})"))
    return [r["name"] for r in rows]


@pytest.mark.usefixtures("lite")
def test_chat_sessions_columns():
    assert _cols("chat_sessions") == CHAT_SESSIONS_COLS


@pytest.mark.usefixtures("lite")
def test_chat_messages_columns():
    assert _cols("chat_messages") == CHAT_MESSAGES_COLS


@pytest.mark.usefixtures("lite")
def test_chat_messages_fk_cascade():
    rows = asyncio.run(db.fetch("PRAGMA foreign_key_list(chat_messages)"))
    assert any(r["table"] == "chat_sessions" and r["on_delete"] == "CASCADE"
               for r in rows)


@pytest.mark.usefixtures("lite")
def test_council_hats_final_columns():
    cols = _cols("council_hats")
    assert "session_id" in cols and "seat_backend" in cols
    assert "claude_session_id" not in cols
