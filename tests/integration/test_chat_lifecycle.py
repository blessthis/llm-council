"""Chat lifecycle (P3d) — fake seat CLIs, no real integration needed.

Exercises the full chat flow against fake-pi / fake-claude bins via a tmp
seats.yaml: chat_start -> send -> poll(done) -> send again (resume path) ->
history -> list -> close, plus the error contract paths (unknown seat,
unknown task, closed session).

Runs as a NORMAL test (no RUN_INTEGRATION needed) — see the module flag
consumed by tests/integration/conftest.py.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from llm_council import chat, db
from llm_council.config import Config

RUNS_WITHOUT_INTEGRATION = True  # conftest: never path-skip this module

FAKE_BIN = str(Path(__file__).resolve().parents[1] / "fixtures" / "fake_bin")

SEATS_YAML = """\
telemetry:
  enabled: false
seats:
  fakepi:
    models: [fake-pi-model]
    agent:
      bin: fake-pi
      args: ["--mode", "json", "-p", "{prompt}", "--model", "{model}"]
      env: {}
  fakeclaude:
    models: [fake-claude-model]
    agent:
      bin: fake-claude
      args: ["-p", "{prompt}", "--output-format", "json", "--model", "{model}"]
      env: {}
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", FAKE_BIN + os.pathsep + os.environ.get("PATH", ""))
    seats_file = tmp_path / "seats.yaml"
    seats_file.write_text(SEATS_YAML)
    seats_file.chmod(0o600)
    chat.set_config(Config(
        seats_file=str(seats_file),
        database_url=f"sqlite:///{tmp_path}/state.db",
        server_instance_id="test",
    ))
    chat._tasks.clear()
    asyncio.run(db.init_pool(f"sqlite:///{tmp_path}/state.db"))
    yield
    asyncio.run(db.close_pool())
    chat._tasks.clear()
    chat._cfg = None


def test_full_lifecycle_with_resume(env):
    async def flow():
        started = await chat.chat_start("fakepi")
        assert "error" not in started, started
        sid = started["chat_session_id"]
        assert started["seat"] == "fakepi"
        assert started["model"] == "fake-pi-model"
        assert started["seat_backend"] == "pi"

        sent = await chat.chat_send(sid, "hello seat")
        task_id = sent["task_id"]
        done = await chat.chat_poll(task_id, wait=True, timeout=30)
        assert done["status"] == "done", done
        assert done["reply"] == "FAKE PI ANSWER"
        assert done["session_id"] and done["session_id"].startswith("fake-pi-sess-")
        assert done["usage"]["output_tokens"] == 0  # pi fake carries no usage

        # second turn: resume path (cli_session_id set on the session row)
        sent2 = await chat.chat_send(sid, "follow-up")
        done2 = await chat.chat_poll(sent2["task_id"], wait=True, timeout=30)
        assert done2["status"] == "done", done2
        assert done2["reply"] == "FAKE PI ANSWER"

        history = await chat.chat_history(sid)
        assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
        assert history[0]["content"] == "hello seat"

        listed = await chat.chat_list()
        assert len(listed) == 1
        assert listed[0]["chat_session_id"] == sid
        assert listed[0]["closed"] is False
        assert listed[0]["status"] == "done"

        closed = await chat.chat_close(sid)
        assert closed["closed"] is True
        # history preserved after close
        assert len(await chat.chat_history(sid)) == 4
        after = await chat.chat_send(sid, "nope")
        assert after["error"]["code"] == "session_closed"

    asyncio.run(flow())


def test_claude_seat_usage_populated(env):
    async def flow():
        started = await chat.chat_start("fakeclaude")
        sid = started["chat_session_id"]
        assert started["seat_backend"] == "claude"
        sent = await chat.chat_send(sid, "hello")
        done = await chat.chat_poll(sent["task_id"], wait=True, timeout=30)
        assert done["status"] == "done", done
        assert "FAKE CLAUDE ANSWER" in done["reply"]
        assert done["usage"]["output_tokens"] == 50
        assert done["usage"]["input_tokens"] == 100

    asyncio.run(flow())


def test_unknown_seat(env):
    async def flow():
        out = await chat.chat_start("nonexistent")
        assert out["error"]["code"] == "unknown_seat"
        assert "nonexistent" in out["error"]["message"]

    asyncio.run(flow())


def test_unknown_task_after_registry_clear(env):
    async def flow():
        started = await chat.chat_start("fakepi")
        sent = await chat.chat_send(started["chat_session_id"], "hi")
        await chat.chat_poll(sent["task_id"], wait=True, timeout=30)
        chat._tasks.clear()  # simulate a server restart (A2)
        out = await chat.chat_poll("deadbeef", wait=False)
        assert out["error"]["code"] == "unknown_task"
        assert "server restarted" in out["error"]["message"]

    asyncio.run(flow())


def test_fire_and_forget_persists(env):
    """A caller that NEVER polls still gets its history written (A2/A13)."""

    async def flow():
        started = await chat.chat_start("fakepi")
        sid = started["chat_session_id"]
        await chat.chat_send(sid, "answer me later")
        # never poll — just wait for the background task itself
        entry = next(e for e in chat._tasks.values() if e["session_id"] == sid)
        await entry["task"]
        history = await chat.chat_history(sid)
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert history[1]["content"] == "FAKE PI ANSWER"

    asyncio.run(flow())


def test_list_seats_surfaces_warnings(env, tmp_path):
    async def flow():
        out = await chat.list_seats()
        assert {s["name"] for s in out["seats"]} == {"fakepi", "fakeclaude"}
        assert out["seats"][0]["runner_kind"] in ("pi", "claude")
        assert out["warnings"] == []

        # a broken seat: skipped, its error text lands in warnings
        bad = tmp_path / "bad_seats.yaml"
        bad.write_text(SEATS_YAML + """\
  broken:
    models: [m]
    agent:
      bin: /bin/echo
      args: ["-p", "{prompt}"]        # missing {model}
      env: {}
""")
        bad.chmod(0o600)
        chat.set_config(Config(
            seats_file=str(bad),
            database_url=f"sqlite:///{tmp_path}/state.db",
            server_instance_id="test",
        ))
        out2 = await chat.list_seats()
        names = {s["name"] for s in out2["seats"]}
        assert names == {"fakepi", "fakeclaude"}  # broken seat skipped
        assert any("broken" in w and "{model}" in w for w in out2["warnings"])

    asyncio.run(flow())
