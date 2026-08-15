"""CodexRunner tests — fake-codex fixture per tests/fixtures/fake_bin/README.md.

Covers: thread_id capture + immediate on_session, agent_message text parsing
(and concatenation of multiple items), usage normalization, resume rendering
(``exec resume <thread_id> <prompt>`` via wrapper-script argv assert),
FAKE_CODEX_FAIL error path with stderr, probe ok/fail, supports_progress
False, registry lookup.
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
from llm_council.seats.runners.codex import CodexRunner

FAKE_BIN = str(Path(__file__).resolve().parents[1] / "fixtures" / "fake_bin")


def fake_seat(tmp_path, bin_name="fake-codex"):
    return Seat(
        name="codexseat",
        models=["gpt-test"],
        agent=AgentSpec(
            bin=bin_name,
            args=[
                "exec", "--json", "--skip-git-repo-check", "-s", "read-only",
                "--color", "never", "-m", "{model}", "-C", "{workdir}", "{prompt}",
            ],
            env={"FAKE_CODEX_ENV": "1"},
        ),
        runner_kind="codex",
    )


@pytest.fixture()
def fake_path(monkeypatch):
    monkeypatch.setenv("PATH", FAKE_BIN + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("FAKE_CODEX_FAIL", raising=False)
    return FAKE_BIN


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def reg(monkeypatch):
    monkeypatch.setattr(runner_registry, "_CACHE", {})


def test_registry_returns_codex_runner(reg):
    r = get_runner("codex")
    assert isinstance(r, CodexRunner)
    assert r.runner_kind == "codex"
    assert r.supports_progress() is False


def test_invoke_fires_on_session_with_thread_id(fake_path, tmp_path, reg):
    seat = fake_seat(tmp_path)
    seen: list[str] = []

    async def on_session(sid: str) -> None:
        seen.append(sid)

    res = run(CodexRunner().invoke(seat, "gpt-test", "hello", str(tmp_path), on_session=on_session))
    assert not res.is_error
    assert res.response == "FAKE CODEX ANSWER"
    assert res.session_id and res.session_id.startswith("fake-thread-")
    assert seen == [res.session_id]  # fired with the streamed thread id
    # usage normalized from turn.completed
    assert (res.usage.input_tokens, res.usage.output_tokens) == (80, 40)


def test_invoke_concatenates_multiple_agent_messages(fake_path, tmp_path):
    seat = Seat(
        name="codexmulti",
        models=["m"],
        agent=AgentSpec(
            bin=sys.executable,
            args=[
                "-c",
                "import json;"
                "print(json.dumps({'type':'thread.started','thread_id':'t1'}));"
                "print(json.dumps({'type':'item.completed',"
                "'item':{'type':'agent_message','text':'PART A '}}));"
                "print(json.dumps({'type':'item.completed',"
                "'item':{'type':'agent_message','text':'PART B'}}));"
                "print(json.dumps({'type':'item.completed',"
                "'item':{'type':'reasoning','text':'NOISE'}}));"
                "print(json.dumps({'type':'turn.completed',"
                "'usage':{'input_tokens':5,'output_tokens':7}}))",
            ],
            env={},
        ),
        runner_kind="codex",
    )
    res = run(CodexRunner().invoke(seat, "m", "hi", str(tmp_path)))
    assert res.response == "PART A PART B"
    assert (res.usage.input_tokens, res.usage.output_tokens) == (5, 7)


def test_invoke_resume_renders_exec_resume_subcommand(fake_path, tmp_path):
    """Assert the argv via a wrapper script that records sys.argv."""
    recorder = tmp_path / "record.py"
    recorder.write_text(
        "import json,os,sys;"
        "open(os.environ['ARGV_LOG'],'a').write(json.dumps(sys.argv)+'\\n');"
        "print(json.dumps({'type':'thread.started','thread_id':'old'}));"
        "print(json.dumps({'type':'item.completed',"
        "'item':{'type':'agent_message','text':'RESUMED'}}))\n"
    )
    seat = Seat(
        name="codexresume",
        models=["m"],
        agent=AgentSpec(
            bin=sys.executable,
            args=[str(recorder), "exec", "--json", "-m", "{model}", "{prompt}"],
            env={},
        ),
        runner_kind="codex",
    )
    log = tmp_path / "argv.jsonl"
    os.environ["ARGV_LOG"] = str(log)
    try:
        res = run(CodexRunner().invoke(seat, "m", "again-please", str(tmp_path), resume="th-42"))
    finally:
        os.environ.pop("ARGV_LOG")
    assert res.response == "RESUMED"
    argv = json.loads(log.read_text().splitlines()[0])
    # exec resume <thread_id> ... <prompt> — resume is a SUBCOMMAND after `exec`
    assert argv[1:] == ["exec", "resume", "th-42", "--json", "-m", "m", "again-please"]


def test_invoke_fail_flag_is_error_with_stderr(fake_path, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")
    seat = fake_seat(tmp_path)
    res = run(CodexRunner().invoke(seat, "gpt-test", "hello", str(tmp_path)))
    assert res.is_error
    assert "codex auth failed" in res.response
    assert res.subtype == "cli_error"


def test_invoke_stream_error_event_is_error(fake_path, tmp_path):
    seat = Seat(
        name="codexerr",
        models=["m"],
        agent=AgentSpec(
            bin=sys.executable,
            args=["-c", "import json;print(json.dumps({'type':'error','message':'boom'}))"],
            env={},
        ),
        runner_kind="codex",
    )
    res = run(CodexRunner().invoke(seat, "m", "hi", str(tmp_path)))
    assert res.is_error
    assert "boom" in res.response


def test_invoke_missing_binary_raises_runtime_error(tmp_path):
    seat = Seat(
        name="codexnope",
        models=["m"],
        agent=AgentSpec(
            bin="/nonexistent/definitely-not-codex",
            args=["exec", "{prompt}"],
            env={},
        ),
        runner_kind="codex",
    )
    with pytest.raises(RuntimeError):
        run(CodexRunner().invoke(seat, "m", "hi", str(tmp_path)))


def test_probe_ok_and_fail(fake_path, tmp_path, monkeypatch, reg):
    seat = fake_seat(tmp_path)
    ok = run(CodexRunner().probe(seat, "gpt-test"))
    assert ok.ok and ok.model == "gpt-test"
    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")
    bad = run(CodexRunner().probe(seat, "gpt-test"))
    assert not bad.ok and "codex auth failed" in (bad.error or "")
    # never raises, even for a missing binary
    dead = Seat(
        name="codexdead",
        models=["m"],
        agent=AgentSpec(bin="/nonexistent/codex", args=["exec", "{prompt}"], env={}),
        runner_kind="codex",
    )
    res = run(CodexRunner().probe(dead, "m"))
    assert not res.ok and res.error


def test_progress_unsupported():
    r = CodexRunner()
    assert r.supports_progress() is False  # seatspec §3 v1 default
    assert r.read_progress("whatever") is None
