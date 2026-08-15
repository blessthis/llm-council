"""PiRunner tests — fake-pi fixture per tests/fixtures/fake_bin/README.md.

Covers: session id capture + immediate on_session, agent_end answer parsing
(string + block-list content), --session resume token rendering, FAKE_PI_FAIL
error path with stderr tail, probe ok/fail, supports_progress False.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from llm_council.seats import runners as runner_registry
from llm_council.seats.base import AgentSpec, Seat
from llm_council.seats.runners import get_runner
from llm_council.seats.runners.pi import PiRunner

FAKE_BIN = str(Path(__file__).resolve().parents[1] / "fixtures" / "fake_bin")


def fake_seat(tmp_path, bin_name="fake-pi"):
    return Seat(
        name="piseat",
        models=["pi-model"],
        agent=AgentSpec(
            bin=bin_name,
            args=["--mode", "json", "-p", "{prompt}", "--model", "{model}"],
            env={"FAKE_PI_ENV": "1"},
        ),
        runner_kind="pi",
    )


@pytest.fixture()
def fake_path(monkeypatch):
    monkeypatch.setenv("PATH", FAKE_BIN + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("FAKE_PI_FAIL", raising=False)
    return FAKE_BIN


def run(coro):
    return asyncio.run(coro)


def test_registry_returns_pi_runner(reg):
    r = get_runner("pi")
    r = get_runner("pi")
    assert r.runner_kind == "pi"
    assert r.supports_progress() is False


def test_invoke_fires_on_session_with_stream_id(fake_path, tmp_path, reg):
    seat = fake_seat(tmp_path)
    seen: list[str] = []

    async def on_session(sid: str) -> None:
        seen.append(sid)

    res = run(PiRunner().invoke(seat, "pi-model", "hello", str(tmp_path), on_session=on_session))
    assert not res.is_error
    assert res.response == "FAKE PI ANSWER"
    assert res.session_id and res.session_id.startswith("fake-pi-sess-")
    assert seen == [res.session_id]  # fired with the streamed id
    assert res.usage.output_tokens == 0  # best-effort zeros absent usage (Q17)


def test_invoke_parses_block_list_content(fake_path, tmp_path):
    """A seat whose args echo a hand-built JSONL agent_end with block content."""
    seat = Seat(
        name="piblocks",
        models=["m"],
        agent=AgentSpec(bin=sys.executable, args=[
            "-c",
            "import json,sys;"
            "print(json.dumps({'type':'session','id':'s1'}));"
            "print(json.dumps({'type':'agent_end','messages':["
            "{'role':'user','content':'q'},"
            "{'role':'assistant','content':[{'type':'text','text':'A '},"
            "{'type':'tool_use','id':'x'},{'type':'text','text':'B'}]},"
            "{'role':'assistant','content':'LATER'}],"
            "'usage':{'input_tokens':3,'output_tokens':9}}))"
        ], env={}),
        runner_kind="pi",
    )
    res = run(PiRunner().invoke(seat, "m", "hi", str(tmp_path)))
    assert res.response == "LATER"  # LAST assistant message wins
    assert (res.usage.input_tokens, res.usage.output_tokens) == (3, 9)


def test_invoke_resume_renders_discrete_session_token(fake_path, tmp_path):
    """The fake ignores --session; we assert the argv via a wrapper that records."""
    recorder = tmp_path / "record.py"
    recorder.write_text(
        "import json,os,sys;"
        "open(os.environ['ARGV_LOG'],'a').write(json.dumps(sys.argv)+'\\n');"
        "print(json.dumps({'type':'session','id':'old'}));"
        "print(json.dumps({'type':'agent_end','messages':["
        "{'role':'assistant','content':'RESUMED'}]}))\n"
    )
    seat = Seat(
        name="piont",
        models=["m"],
        agent=AgentSpec(bin=sys.executable,
                        args=[str(recorder), "-p", "{prompt}", "--model", "{model}"],
                        env={}),
        runner_kind="pi",
    )
    log = tmp_path / "argv.jsonl"
    os.environ["ARGV_LOG"] = str(log)
    try:
        res = run(PiRunner().invoke(seat, "m", "again", str(tmp_path), resume="uuid-123"))
    finally:
        os.environ.pop("ARGV_LOG")
    assert res.response == "RESUMED"
    argv = json.loads(log.read_text().splitlines()[0])
    assert argv[-2:] == ["--session", "uuid-123"]  # discrete tokens, appended


def test_invoke_fail_flag_is_error_with_stderr(fake_path, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_FAIL", "1")
    seat = fake_seat(tmp_path)
    res = run(PiRunner().invoke(seat, "pi-model", "hello", str(tmp_path)))
    assert res.is_error
    assert "pi broken" in res.response
    assert res.subtype == "cli_error"


def test_invoke_missing_binary_raises_runtime_error(tmp_path):
    seat = Seat(
        name="pinope",
        models=["m"],
        agent=AgentSpec(bin="/nonexistent/definitely-not-pi",
                        args=["-p", "{prompt}", "--model", "{model}"], env={}),
        runner_kind="pi",
    )
    with pytest.raises(RuntimeError):
        run(PiRunner().invoke(seat, "m", "hi", str(tmp_path)))


def test_probe_ok_and_fail(fake_path, tmp_path, monkeypatch, reg):
    seat = fake_seat(tmp_path)
    ok = run(PiRunner().probe(seat, "pi-model"))
    assert ok.ok and ok.model == "pi-model"
    monkeypatch.setenv("FAKE_PI_FAIL", "1")
    bad = run(PiRunner().probe(seat, "pi-model"))
    assert not bad.ok and "pi broken" in (bad.error or "")
    # never raises, even for a missing binary
    dead = Seat(
        name="pidead",
        models=["m"],
        agent=AgentSpec(bin="/nonexistent/pi", args=["-p", "{prompt}", "--model", "{model}"],
                        env={}),
        runner_kind="pi",
    )
    res = run(PiRunner().probe(dead, "m"))
    assert not res.ok and res.error


def test_progress_unsupported():
    r = PiRunner()
    assert r.supports_progress() is False  # seatspec §3 v1 fallback
    assert r.read_progress("whatever") is None


@pytest.fixture()
def reg(monkeypatch):
    """Clear the registry cache so get_runner re-imports fresh per test."""
    monkeypatch.setattr(runner_registry, "_CACHE", {})
