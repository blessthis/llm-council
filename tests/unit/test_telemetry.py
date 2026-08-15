"""P6 telemetry unit tests (acceptance P6): allowlist serializer, consent
gating (socket guard), retry queue with backoff, payload shape, and the
council_score -> enqueue path."""

import asyncio
import json
import os
from unittest import mock
from urllib.error import URLError

import pytest
import yaml

from llm_council import db, telemetry
from llm_council.telemetry import (
    TelemetryPayloadError,
    build_payload,
    serialize_event,
)


@pytest.fixture()
def lite(tmp_path):
    url = f"sqlite:///{tmp_path}/state.db"

    async def _open():
        await db.init_pool(url)
        return db.backend()

    backend = asyncio.run(_open())
    yield backend, tmp_path
    asyncio.run(db.close_pool())


def _council_row(**over):
    row = {
        "id": 1,
        "working_dir": "/tmp/x",
        "brief": "SECRET BRIEF never sent",
        "status": "done",
        "kind": "adhoc",
        "owner": "proc",
        "council_uuid": "uu-id-1234",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:01:00+00:00",
    }
    row.update(over)
    return row


def _event(**over):
    ev = {
        "model": "test-model",
        "kind": "adhoc",
        "score": 7,
        "usage": {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0},
        "tool_version": "1.0.0",
        "ts": "2026-01-01T00:00:00+00:00",
        "council_uuid": "uu-id-1234",
        "agent": "pi",
    }
    ev.update(over)
    return ev


# --------------------------------------------------------------------------- #
# Allowlist serializer
# --------------------------------------------------------------------------- #

def test_serialize_event_roundtrip_exact_keys():
    ev = _event()
    text = serialize_event(ev)
    assert set(json.loads(text)) == {
        "model", "kind", "score", "usage", "tool_version",
        "ts", "council_uuid", "agent",
    }


def test_serializer_rejects_extra_top_level_keys():
    with pytest.raises(TelemetryPayloadError):
        serialize_event(_event(brief="secret"))
    with pytest.raises(TelemetryPayloadError):
        serialize_event(_event(notes="secret notes"))


def test_serializer_rejects_extra_usage_keys_and_missing_keys():
    with pytest.raises(TelemetryPayloadError):
        serialize_event(_event(usage={"input": 1, "output": 2,
                                      "cache_read": 0, "cache_write": 0,
                                      "path": "/etc/passwd"}))
    bad = _event()
    del bad["ts"]
    with pytest.raises(TelemetryPayloadError):
        serialize_event(bad)


def test_build_payload_discards_brief_notes_paths():
    hats = [{"hat": "hat1", "usage": json.dumps(
        {"input": 5, "output": 6, "cache_read": 1, "cache_write": 2})}]
    scores = [{"hat": "hat1", "model": "m1", "score": 8}]
    events = build_payload(_council_row(), hats, scores, "1.2.3")
    assert len(events) == 1
    ev = events[0]
    blob = json.dumps(ev)
    assert "SECRET" not in blob and "brief" not in blob and "notes" not in blob
    assert ev["model"] == "m1" and ev["score"] == 8
    assert ev["usage"] == {"input": 5, "output": 6,
                           "cache_read": 1, "cache_write": 2}
    assert ev["council_uuid"] == "uu-id-1234"


# --------------------------------------------------------------------------- #
# Consent + socket guard
# --------------------------------------------------------------------------- #

def _write_seats(tmp_path, enabled):
    p = tmp_path / "seats.yaml"
    p.write_text(yaml.safe_dump({"telemetry": {"enabled": enabled},
                                 "seats": {}}), encoding="utf-8")
    return str(p)


def test_no_network_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_COUNCIL_TELEMETRY_DISABLED", raising=False)
    with mock.patch("urllib.request.urlopen") as up:
        # missing file -> no consent
        assert telemetry.consent_enabled(str(tmp_path / "nope.yaml")) is False
        # file exists, enabled false
        seats = _write_seats(tmp_path, False)
        assert telemetry.consent_enabled(seats) is False
        # file exists but key missing entirely -> invalid -> no consent
        bad = tmp_path / "bad.yaml"
        bad.write_text("seats: {}\n", encoding="utf-8")
        assert telemetry.consent_enabled(str(bad)) is False
        assert up.call_count == 0


def test_consent_true_only_when_explicit_true(tmp_path):
    assert telemetry.consent_enabled(_write_seats(tmp_path, True)) is True
    # enabled: "yes" (string) is NOT true
    assert telemetry.consent_enabled(_write_seats(tmp_path, "yes")) is False


def test_hard_off_env_override(tmp_path):
    seats = _write_seats(tmp_path, True)
    with mock.patch.dict(
        "os.environ", {"LLM_COUNCIL_TELEMETRY_DISABLED": "1"}
    ):
        assert telemetry.hard_disabled() is True
        assert telemetry.consent_enabled(seats) is True  # consent itself ok
        # but maybe_flush short-circuits with zero network:
        with mock.patch("urllib.request.urlopen") as up:
            asyncio.run(telemetry.maybe_flush_council(1, seats_file=seats))
            assert up.call_count == 0


# --------------------------------------------------------------------------- #
# Retry queue
# --------------------------------------------------------------------------- #

def test_failed_flush_queues_then_retry_succeeds(lite):
    backend, tmp_path = lite
    events = [_event(), _event(score=9, model="m2")]

    with mock.patch(
        "urllib.request.urlopen",
        side_effect=URLError("down"),
    ):
        sent = asyncio.run(telemetry.flush("https://x/v1/score", events))
    assert sent == 0
    rows = asyncio.run(db.fetch("SELECT * FROM telemetry_queue"))
    assert len(rows) == 2
    for r, ev in zip(rows, events, strict=True):
        assert json.loads(r["payload"]) == ev
        assert r["attempts"] == 0

    # Backoff: last_attempt NULL -> immediately eligible.
    with mock.patch("urllib.request.urlopen") as up:
        up.return_value.__enter__.return_value.status = 200
        sent = asyncio.run(telemetry.retry_pending("https://x/v1/score"))
    assert sent == 2
    assert up.call_count == 2
    rows = asyncio.run(db.fetch("SELECT * FROM telemetry_queue"))
    assert rows == []  # deleted on success


def test_retry_backoff_blocks_until_elapsed(lite):
    backend, _ = lite
    with mock.patch("urllib.request.urlopen", side_effect=URLError("down")):
        asyncio.run(telemetry.flush("https://x/v1/score", [_event()]))
    # failed retry attempt bumps attempts + last_attempt
    with mock.patch("urllib.request.urlopen", side_effect=URLError("still down")):
        asyncio.run(telemetry.retry_pending("https://x/v1/score"))
    row = asyncio.run(db.fetchrow("SELECT * FROM telemetry_queue"))
    assert row["attempts"] == 1 and row["last_attempt"] is not None
    # now still inside the 2^1 * 60s window -> no socket opened
    with mock.patch("urllib.request.urlopen") as up:
        sent = asyncio.run(telemetry.retry_pending("https://x/v1/score"))
    assert sent == 0 and up.call_count == 0
    # expire the window -> retry succeeds, row deleted
    asyncio.run(db.execute("UPDATE telemetry_queue SET last_attempt=?",
                           ("2000-01-01T00:00:00+00:00",)))
    with mock.patch("urllib.request.urlopen") as up:
        up.return_value.__enter__.return_value.status = 200
        sent = asyncio.run(telemetry.retry_pending("https://x/v1/score"))
    assert sent == 1
    assert asyncio.run(db.fetch("SELECT * FROM telemetry_queue")) == []


# --------------------------------------------------------------------------- #
# council_score -> enqueue path
# --------------------------------------------------------------------------- #

def test_maybe_flush_enabled_sends_events(lite, monkeypatch):
    backend, tmp_path = lite
    monkeypatch.delenv("LLM_COUNCIL_TELEMETRY_DISABLED", raising=False)
    seats = _write_seats(tmp_path, True)

    async def _seed():
        await db.execute(
            "INSERT INTO councils (working_dir, brief, owner, council_uuid, kind,"
            " created_at, updated_at) VALUES ('/w', 'SECRET', 'p', 'cu-1',"
            " 'adhoc', '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00')")
        await db.execute(
            "INSERT INTO council_hats (council_id, hat, model, status, usage,"
            " created_at, updated_at) VALUES (1, 'hat1', 'm1', 'done',"
            " ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (json.dumps({"input": 3, "output": 4,
                         "cache_read": 0, "cache_write": 0}),))
        await db.execute(
            "INSERT INTO council_scores (council_id, hat, model, score, notes,"
            " created_at) VALUES (1, 'hat1', 'm1', 9, 'SECRET NOTES',"
            " '2026-01-01T00:02:00+00:00')")

    asyncio.run(_seed())
    with mock.patch("urllib.request.urlopen") as up:
        up.return_value.__enter__.return_value.status = 200
        sent = asyncio.run(
            telemetry.maybe_flush_council(1, seats_file=seats,
                                          tool_version="1.0.0"))
    assert sent == 1
    assert up.call_count == 1
    body = json.loads(up.call_args.args[0].data)
    assert body == {
        "model": "m1", "kind": "adhoc", "score": 9,
        "usage": {"input": 3, "output": 4, "cache_read": 0, "cache_write": 0},
        "tool_version": "1.0.0",
        "ts": "2026-01-01T00:01:00+00:00", "council_uuid": "cu-1",
        "agent": telemetry._detect_agent(),
    }
    assert asyncio.run(db.fetch("SELECT * FROM telemetry_queue")) == []


def test_maybe_flush_unknown_council_no_network(lite, tmp_path):
    seats = _write_seats(tmp_path, True)
    with mock.patch("urllib.request.urlopen") as up:
        sent = asyncio.run(telemetry.maybe_flush_council(999, seats_file=seats))
    assert sent == 0 and up.call_count == 0


def test_endpoint_env_override_and_default(monkeypatch):
    monkeypatch.delenv("LLM_COUNCIL_TELEMETRY_ENDPOINT", raising=False)
    assert telemetry.get_endpoint() == telemetry.DEFAULT_ENDPOINT
    monkeypatch.setenv("LLM_COUNCIL_TELEMETRY_ENDPOINT", "https://other/x")
    assert telemetry.get_endpoint() == "https://other/x"


# --------------------------------------------------------------------------- #
# _detect_agent
# --------------------------------------------------------------------------- #

def _clear_agent_env(monkeypatch):
    for key in list(os.environ):
        if (
            key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CURSOR_TRACE_ID",
                    "VSCODE_PID", "GEMINI_CLI", "OPENAI_CODEX")
            or key.startswith("CODEX_")
        ):
            monkeypatch.delenv(key, raising=False)


def test_detect_agent_claude(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert telemetry._detect_agent() == "claude"
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    assert telemetry._detect_agent() == "claude"


def test_detect_agent_cursor(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("CURSOR_TRACE_ID", "t-123")
    assert telemetry._detect_agent() == "cursor"


def test_detect_agent_codex(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("CODEX_SANDBOX", "1")
    assert telemetry._detect_agent() == "codex"


def test_detect_agent_unknown(monkeypatch):
    _clear_agent_env(monkeypatch)
    assert telemetry._detect_agent() == "unknown"
