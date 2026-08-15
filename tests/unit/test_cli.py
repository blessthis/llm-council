"""Unit tests for the CLI command family (P4b) — non-interactive plumbing only."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from typer.testing import CliRunner

from llm_council import cli
from llm_council.installer.fingerprint import CANONICAL_KEY

runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb"})


def _plain(out: str) -> str:
    """Strip ANSI codes so flag assertions survive rich terminal-width wrapping."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", out)

BIN = "/bin/echo"
ARGS = ["-p", "{prompt}", "--model", "{model}"]

def isolate(monkeypatch, tmp_path):
    """Fake HOME + neutralized env (a dev .env must not leak DATABASE_URL)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv("SEATS_FILE", raising=False)


OUR_ENTRY = {
    "type": "stdio", "command": "uvx",
    "args": ["blessthis-llm-council-server"],
    "env": {"SEATS_FILE": "/tmp/seats.yaml"},
}


def write_seats(home: Path, env: dict | None = None) -> Path:
    d = home / ".blessthis-llm-council"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "seats.yaml"
    path.write_text(yaml.safe_dump({
        "telemetry": {"enabled": False},
        "seats": {"alpha": {
            "models": ["m1"],
            "agent": {"bin": BIN, "args": ARGS, "env": env or {}},
        }},
    }, sort_keys=False))
    os.chmod(path, 0o600)
    return path


# --- help trees ---------------------------------------------------------------------

def test_root_help():
    r = runner.invoke(cli.app, ["--help"])
    assert r.exit_code == 0
    for word in ("install", "seats", "doctor", "status", "uninstall", "update"):
        assert word in _plain(r.output)


def test_seats_help_tree():
    r = runner.invoke(cli.app, ["seats", "--help"])
    assert r.exit_code == 0
    for word in ("list", "add", "edit", "remove", "probe", "path"):
        assert word in _plain(r.output)


def test_install_help_flags():
    r = runner.invoke(cli.app, ["install", "--help"])
    assert r.exit_code == 0
    for flag in ("--host", "--project-path", "--yes", "--offline"):
        assert flag in _plain(r.output)


def test_doctor_help_deep():
    r = runner.invoke(cli.app, ["doctor", "--help"])
    assert r.exit_code == 0
    assert "--deep" in _plain(r.output)


def test_uninstall_help_purge():
    r = runner.invoke(cli.app, ["uninstall", "--help"])
    assert r.exit_code == 0
    assert "--purge" in _plain(r.output)


# --- seats path / list ----------------------------------------------------------------

def test_seats_path(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    r = runner.invoke(cli.app, ["seats", "path"])
    assert r.exit_code == 0
    assert str(tmp_path / ".blessthis-llm-council" / "seats.yaml") in _plain(r.output)


def test_seats_list_masked(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    write_seats(tmp_path, env={"ANTHROPIC_API_KEY": "sk-ant-secret1234"})
    r = runner.invoke(cli.app, ["seats", "list"])
    assert r.exit_code == 0
    assert "alpha" in _plain(r.output)
    assert "ANTHROPIC_API_KEY" in _plain(r.output)
    assert "sk-ant-secret1234" not in _plain(r.output)


def test_seats_list_missing_file_exits_1(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    r = runner.invoke(cli.app, ["seats", "list"])
    assert r.exit_code == 1


# --- status ---------------------------------------------------------------------------

def test_status_structure(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    write_seats(tmp_path)
    r = runner.invoke(cli.app, ["status"])
    assert r.exit_code == 0
    for word in ("seats file", "1 seat", "db backend", "sqlite", "claude", "copilot",
                 "gemini"):
        assert word in _plain(r.output)


# --- doctor ---------------------------------------------------------------------------

def test_doctor_empty_home_fails(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_spawn_check", lambda sf: (True, ""))
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 1
    assert "FAIL" in _plain(r.output)
    assert "seats file not found" in _plain(r.output)


def test_doctor_valid_setup_passes(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    write_seats(tmp_path)
    monkeypatch.setattr(cli, "_spawn_check", lambda sf: (True, ""))
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 0
    assert "FAIL" not in _plain(r.output)
    assert "PASS" in _plain(r.output)
    assert "0600" in _plain(r.output).replace("\n", "")


def test_doctor_bad_mode_warns_but_ok(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    path = write_seats(tmp_path)
    os.chmod(path, 0o644)
    monkeypatch.setattr(cli, "_spawn_check", lambda sf: (True, ""))
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 0
    assert "WARN" in _plain(r.output)
    assert "0600" in _plain(r.output)


def test_doctor_invalid_seats_fails(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    d = tmp_path / ".blessthis-llm-council"
    d.mkdir()
    (d / "seats.yaml").write_text("::: not yaml [")
    monkeypatch.setattr(cli, "_spawn_check", lambda sf: (True, ""))
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 1
    assert "FAIL" in _plain(r.output)


# --- uninstall --------------------------------------------------------------------------

def _write_claude_config(home: Path, servers: dict) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".claude.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def test_uninstall_removes_fingerprint_only(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    home = tmp_path
    write_seats(home)
    _write_claude_config(home, {CANONICAL_KEY: OUR_ENTRY, "other": {
        "command": "npx", "args": ["something-else"],
    }})
    # fake agent files: ours + a foreign one
    agents = home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "blessthis-council-architect.md").write_text("ours")
    (agents / "someone-elses.md").write_text("foreign")

    r = runner.invoke(cli.app, ["uninstall", "--host", "claude", "--yes"])
    assert r.exit_code == 0, r.output
    data = json.loads((home / ".claude.json").read_text())
    assert CANONICAL_KEY not in data["mcpServers"]
    assert "other" in data["mcpServers"]  # untouched
    assert not (agents / "blessthis-council-architect.md").exists()
    assert (agents / "someone-elses.md").exists()  # untouched
    # seats.yaml kept without --purge
    assert (home / ".blessthis-llm-council" / "seats.yaml").exists()


def test_uninstall_purge_deletes_seats_and_db(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    home = tmp_path
    seats = write_seats(home)
    (home / ".blessthis-llm-council" / "state.db").write_bytes(b"")
    _write_claude_config(home, {CANONICAL_KEY: OUR_ENTRY})
    r = runner.invoke(cli.app, ["uninstall", "--host", "claude", "--purge", "--yes"])
    assert r.exit_code == 0, r.output
    assert not seats.exists()
    assert not (home / ".blessthis-llm-council" / "state.db").exists()


def test_uninstall_unknown_host(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    r = runner.invoke(cli.app, ["uninstall", "--host", "windsurf"])
    assert r.exit_code == 1


# --- update -----------------------------------------------------------------------------

def test_update_prints_hint(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_spawn_check", lambda sf: (True, ""))
    r = runner.invoke(cli.app, ["update"])
    assert r.exit_code == 0
    assert "uv cache clean" in _plain(r.output)


def test_update_spawn_failure_exits_1(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_spawn_check", lambda sf: (False, "boom"))
    r = runner.invoke(cli.app, ["update"])
    assert r.exit_code == 1
