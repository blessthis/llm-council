"""GenericRunner tests — against the fake-cli fixture / a scratch failing bin."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from llm_council.seats.base import AgentSpec, Seat, Usage
from llm_council.seats.runners.generic import GenericRunner

FAKE_BIN = str(Path(__file__).resolve().parents[1] / "fixtures" / "fake_bin")


@pytest.fixture(autouse=True)
def fake_path(monkeypatch):
    monkeypatch.setenv("PATH", FAKE_BIN + os.pathsep + os.environ.get("PATH", ""))


def make_seat(bin_name: str) -> Seat:
    return Seat(
        name="mystery",
        models=["some-model"],
        agent=AgentSpec(bin=bin_name, args=["{prompt}", "--model", "{model}"], env={}),
        runner_kind="generic",
    )


def failing_bin(tmp_path: Path) -> str:
    fail = tmp_path / "failbin"
    fail.write_text("#!/bin/sh\necho boom >&2\nexit 5\n")
    fail.chmod(fail.stat().st_mode | stat.S_IEXEC)
    return str(fail)


def test_success_stdout_is_answer(tmp_path):
    r = GenericRunner()
    res = asyncio.run(r.invoke(make_seat("fake-cli"), "some-model", "hi", str(tmp_path)))
    assert res.is_error is False
    assert "fake seat answer" in res.response
    assert res.usage == Usage()  # zeros, not None
    assert res.session_id is None
    assert res.model == "some-model"


def test_nonzero_exit_is_error(tmp_path):
    r = GenericRunner()
    res = asyncio.run(r.invoke(make_seat(failing_bin(tmp_path)), "m", "hi", str(tmp_path)))
    assert res.is_error is True
    assert "boom" in res.response
    assert res.subtype == "exit_5"


def test_progress_unsupported():
    r = GenericRunner()
    assert r.supports_progress() is False
    assert r.read_progress("anything") is None
    assert r.runner_kind == "generic"


def test_probe_ok_and_fail(tmp_path):
    r = GenericRunner()
    ok = asyncio.run(r.probe(make_seat("fake-cli"), "m"))
    assert ok.ok is True and ok.model == "m"
    bad = asyncio.run(r.probe(make_seat(failing_bin(tmp_path)), "m"))
    assert bad.ok is False and bad.error
