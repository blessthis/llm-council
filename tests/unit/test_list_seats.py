"""list_seats unit tests (P3d): valid file, warnings surfaced, error contract
for a missing / invalid seats file."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from llm_council import chat, db
from llm_council.config import Config
from llm_council.seats.loader import invalidate_cache

VALID = """\
telemetry:
  enabled: false
seats:
  fable:
    models: [m1, m2]
    agent:
      bin: /bin/echo
      args: ["-p", "{prompt}", "--model", "{model}", "--session", "{session_id}"]
      env: {}
"""


def _cfg(tmp_path, name="seats.yaml", text=None):
    p = tmp_path / name
    if text is not None:
        p.write_text(text)
        p.chmod(0o600)
    chat.set_config(Config(
        seats_file=str(p),
        database_url=f"sqlite:///{tmp_path}/state.db",
        server_instance_id="test",
    ))
    return p


@pytest.fixture()
def lite(tmp_path):
    asyncio.run(db.init_pool(f"sqlite:///{tmp_path}/state.db"))
    yield
    asyncio.run(db.close_pool())
    chat._cfg = None


@pytest.mark.usefixtures("lite")
def test_valid_file_lists_seats(tmp_path):
    _cfg(tmp_path, text=VALID)
    out = asyncio.run(chat.list_seats())
    assert out["seats"] == [{"name": "fable", "models": ["m1", "m2"],
                             "runner_kind": "generic"}]
    assert out["warnings"] == []


@pytest.mark.usefixtures("lite")
def test_warnings_included(tmp_path):
    # bin not on PATH -> loader warning surfaced verbatim
    _cfg(tmp_path, text=VALID.replace("/bin/echo", "no-such-binary-xyz"))
    out = asyncio.run(chat.list_seats())
    assert out["seats"][0]["name"] == "fable"
    assert any("no-such-binary-xyz" in w for w in out["warnings"])


@pytest.mark.usefixtures("lite")
def test_missing_file_error_contract(tmp_path):
    p = _cfg(tmp_path)  # exists=False
    assert not Path(p).exists()
    out = asyncio.run(chat.list_seats())
    assert out["error"]["code"] == "seats_file_missing"
    assert "not found" in out["error"]["message"]


@pytest.mark.usefixtures("lite")
def test_invalid_file_error_contract(tmp_path):
    _cfg(tmp_path, text="telemetry: [unclosed\n")
    invalidate_cache()
    out = asyncio.run(chat.list_seats())
    assert out["error"]["code"] == "seats_file_invalid"
    assert "invalid YAML" in out["error"]["message"]
