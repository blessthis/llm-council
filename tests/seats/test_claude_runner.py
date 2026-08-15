"""ClaudeRunner tests — against the fake-claude fixture (never the real CLI).

No pytest-asyncio in this project: async bodies run via `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from llm_council.seats.base import AgentSpec, Seat, Usage
from llm_council.seats.runners import claude as claude_mod
from llm_council.seats.runners import get_runner
from llm_council.seats.runners.claude import ClaudeRunner

FAKE_BIN = str(Path(__file__).resolve().parents[1] / "fixtures" / "fake_bin")


@pytest.fixture(autouse=True)
def fake_path(monkeypatch):
    monkeypatch.setenv("PATH", FAKE_BIN + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("FAKE_CLAUDE_FAIL", raising=False)


def make_seat(bin_name: str = "fake-claude") -> Seat:
    return Seat(
        name="fable",
        models=["claude-fable-5"],
        agent=AgentSpec(
            bin=bin_name,
            args=[
                "-p",
                "{prompt}",
                "--model",
                "{model}",
                "--output-format",
                "json",
                "--dangerously-skip-permissions",
                "--max-turns",
                "80",
            ],
            env={"ANTHROPIC_BASE_URL": "http://gw"},
        ),
        runner_kind="claude",
    )


def test_success_parse(tmp_path):
    r = get_runner("claude")
    assert isinstance(r, ClaudeRunner)
    res = asyncio.run(r.invoke(make_seat(), "claude-fable-5", "hello world", str(tmp_path)))
    assert res.is_error is False
    assert res.response == "FAKE CLAUDE ANSWER: prompt received"
    assert res.usage == Usage(
        input_tokens=100, output_tokens=50, cache_read_tokens=10, cache_write_tokens=5
    )
    assert res.session_id and res.session_id.startswith("fake-sess-")
    assert res.model == "claude-fable-5"
    assert res.num_turns == 2
    assert res.subtype == "success"


def test_resume_renders_resume_token(tmp_path):
    r = get_runner("claude")
    seen: list[str] = []

    async def on_session(sid: str) -> None:
        seen.append(sid)

    res = asyncio.run(
        r.invoke(
            make_seat(),
            "claude-fable-5",
            "follow up",
            str(tmp_path),
            resume="abcdef1234567890",
            on_session=on_session,
        )
    )
    # fake-claude echoes the resumed session id derived from --resume <id>
    assert res.session_id == "fake-sess-resumed-abcdef12"
    assert seen and seen[0] == "abcdef1234567890"  # fired immediately (caller-known)


def test_fail_is_error_with_stderr(tmp_path, monkeypatch):
    r = ClaudeRunner(transient_retries=0)
    assert r.runner_kind == "claude"
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    res = asyncio.run(r.invoke(make_seat(), "claude-fable-5", "x", str(tmp_path)))
    assert res.is_error is True
    assert "fake auth error" in res.response
    assert res.session_id is None


def test_on_session_fired_for_fresh_run(tmp_path):
    r = get_runner("claude")
    seen: list[str] = []

    async def on_session(sid: str) -> None:
        seen.append(sid)

    res = asyncio.run(
        r.invoke(make_seat(), "claude-fable-5", "hello", str(tmp_path), on_session=on_session)
    )
    # pinned uuid fired immediately, then the CLI-assigned id after parse
    assert len(seen) == 2
    assert seen[1] == res.session_id


def test_timeout_kills_subprocess(tmp_path, monkeypatch):
    class FakeProc:
        def __init__(self):
            self.killed = False
            self.returncode = -9

        async def communicate(self):
            await asyncio.sleep(999)

        def kill(self):
            self.killed = True

        async def wait(self):
            return None

    procs: list[FakeProc] = []

    async def fake_exec(*args, **kwargs):
        del args, kwargs
        p = FakeProc()
        procs.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    r = ClaudeRunner(transient_retries=0)
    # timeout on the only attempt = total failure → RuntimeError (seatspec §1)
    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(r.invoke(make_seat(), "claude-fable-5", "x", str(tmp_path), timeout=1))
    assert procs[0].killed is True


def test_think_stripping():
    out = claude_mod._clean("<think>secret</think>Conclusion: ship it")
    assert out == "Conclusion: ship it"
    out2 = claude_mod._clean("A <THINK>x</THINK>\nB <think>y</think> C")
    assert "think" not in out2.lower()
    assert "A" in out2 and "C" in out2
    assert claude_mod._clean(None) == ""


def test_parse_single_json_line():
    r = ClaudeRunner()
    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "session_id": "s1",
            "result": "answer",
            "usage": {"input_tokens": 3, "output_tokens": 4},
            "num_turns": 1,
            "is_error": False,
            "model": "m",
        }
    )
    res = r.parse(line)
    assert res.response == "answer"
    assert res.usage.input_tokens == 3 and res.usage.output_tokens == 4
    assert res.session_id == "s1" and res.model == "m"
    assert res.num_turns == 1 and res.is_error is False and res.subtype == "success"


def test_render_argv_placeholders_and_flags():
    seat = make_seat()
    argv = ClaudeRunner.render_argv(
        seat,
        "m1",
        "do it",
        "/tmp/w",
        session_id="sid-1",
        system_prompt="be terse",
        max_turns=5,
        extra={"add-dir": "/extra"},
    )
    assert argv[0] == "fake-claude"
    assert "do it" in argv and "m1" in argv and "sid-1" in argv
    assert "be terse" in argv and "/extra" in argv
    assert "--session-id" in argv and "--append-system-prompt" in argv
    argv_r = ClaudeRunner.render_argv(seat, "m1", "p", "/tmp/w", resume="old-id")
    i = argv_r.index("--resume")
    assert argv_r[i + 1] == "old-id"
    assert "--session-id" not in argv_r  # mutually exclusive


def test_read_progress_missing_session():
    r = ClaudeRunner()
    assert r.read_progress("no-such-session-id-xyz") is None
    assert r.supports_progress() is True


def test_registry_lazy_and_generic_fallback():
    assert isinstance(get_runner("claude"), ClaudeRunner)
    from llm_council.seats.runners.generic import GenericRunner

    assert isinstance(get_runner("totally-unknown"), GenericRunner)
    assert isinstance(get_runner("generic"), GenericRunner)
