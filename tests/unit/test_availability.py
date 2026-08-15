"""availability.probe() tests — CLI probe path, seat_health recording, TTL cache.

Uses a fake runner monkeypatched into the registry so no real CLI is spawned
(except one /bin/echo sanity case exercising the real PiRunner code path via a
python -c wrapper).
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from llm_council import availability, db
from llm_council.seats import runners as runner_registry
from llm_council.seats.base import AgentSpec, ProbeResult, Seat


def make_seat():
    return Seat(
        name="probeseat",
        models=["m1"],
        agent=AgentSpec(bin="/bin/echo", args=["-p", "{prompt}", "--model", "{model}"], env={}),
        runner_kind="pi",
    )


class FakeRunner:
    def __init__(self):
        self.calls = 0
        self.next = ProbeResult(ok=True, model="m1")

    @property
    def runner_kind(self):
        return "pi"

    async def probe(self, seat, model):
        self.calls += 1
        return self.next

    def supports_progress(self):
        return False

    def read_progress(self, session_id):
        return None


@pytest.fixture()
def lite(tmp_path):
    url = f"sqlite:///{tmp_path}/state.db"

    async def _open():
        await db.init_pool(url)
        return db.backend()

    asyncio.run(_open())
    yield
    asyncio.run(db.close_pool())


@pytest.fixture()
def fake_runner(monkeypatch):
    fr = FakeRunner()
    monkeypatch.setattr(runner_registry, "get_runner", lambda kind: fr)
    return fr


def test_probe_ok_records_health_row(lite, fake_runner):
    ok = asyncio.run(availability.probe(make_seat(), "m1"))
    assert ok is True
    row = asyncio.run(availability.get_health("probeseat", "m1"))
    assert row["status"] == "ok"
    assert row["reason"] is None


def test_probe_fail_records_cooldown_row(lite, fake_runner):
    fake_runner.next = ProbeResult(ok=False, model="m1", error="credits are depleted")
    ok = asyncio.run(availability.probe(make_seat(), "m1"))
    assert ok is False
    row = asyncio.run(availability.get_health("probeseat", "m1"))
    assert row["status"] == "cooldown"
    assert row["reason"] == "balance"  # classify() mapped the error text
    assert "depleted" in row["last_error"]
    assert asyncio.run(availability.is_available("probeseat", "m1")) is False


def test_probe_fail_non_capacity_still_records(lite, fake_runner):
    fake_runner.next = ProbeResult(ok=False, model="m1", error="random spawn noise")
    assert asyncio.run(availability.probe(make_seat(), "m1")) is False
    row = asyncio.run(availability.get_health("probeseat", "m1"))
    assert row["status"] == "cooldown"
    assert row["reason"] is None  # not capacity-class
    assert "random spawn noise" in row["last_error"]


def test_probe_ttl_cache_hit_skips_respawn(lite, fake_runner):
    seat = make_seat()
    assert asyncio.run(availability.probe(seat, "m1")) is True
    assert fake_runner.calls == 1
    # second probe within _OK_TTL: served from cache, runner NOT respawned
    assert asyncio.run(availability.probe(seat, "m1")) is True
    assert fake_runner.calls == 1


def test_probe_ttl_expired_respawns(lite, fake_runner):
    seat = make_seat()
    assert asyncio.run(availability.probe(seat, "m1")) is True
    # age the ok row past _OK_TTL (10 min)
    import datetime as dt

    async def _age():
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11)).isoformat()
        await db.execute(
            "UPDATE seat_health SET checked_at=? WHERE seat=? AND model=?",
            (old, "probeseat", "m1"),
        )
    asyncio.run(_age())
    assert asyncio.run(availability.probe(seat, "m1")) is True
    assert fake_runner.calls == 2  # respawned


def test_probe_never_raises_on_unknown_runner(lite, monkeypatch):
    def _boom(kind):
        raise RuntimeError(f"seat runner module 'llm_council.seats.runners.{kind}' is "
                           "not available")

    monkeypatch.setattr(runner_registry, "get_runner", _boom)
    seat = Seat(
        name="weird",
        models=["m1"],
        agent=AgentSpec(bin="/bin/echo", args=["-p", "{prompt}", "--model", "{model}"], env={}),
        runner_kind="generic",  # no runner importable
    )
    assert asyncio.run(availability.probe(seat, "m1")) is False
    row = asyncio.run(availability.get_health("weird", "m1"))
    assert row["status"] == "cooldown"


def test_classify_reason_mapping():
    assert availability.classify("Credits are depleted for this account") == "balance"
    assert availability.classify("You exceeded your current quota") == "quota"
    assert availability.classify(None) is None
    assert availability.classify("connection reset by peer") is None


def test_probe_via_real_pi_runner_code_path(lite, tmp_path):
    """End-to-end through the actual PiRunner with a python -c 'pi' fake."""
    seat = Seat(
        name="pyseat",
        models=["m1"],
        agent=AgentSpec(
            bin=sys.executable,
            args=["-c",
                  "import json;"
                  "print(json.dumps({'type':'session','id':'s'}));"
                  "print(json.dumps({'type':'agent_end','messages':["
                  "{'role':'assistant','content':'ok'}]}))"],
            env={},
        ),
        runner_kind="pi",
    )
    assert asyncio.run(availability.probe(seat, "m1")) is True
    row = asyncio.run(availability.get_health("pyseat", "m1"))
    assert row["status"] == "ok"
