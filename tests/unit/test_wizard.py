"""Unit tests for the wizard's non-interactive plumbing (P4b)."""

from __future__ import annotations

import pytest

from llm_council import wizard


def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv("SEATS_FILE", raising=False)


def test_non_tty_guard_refuses(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        wizard.run_wizard()
    assert exc.value.code == 1


def test_non_tty_guard_message_printed(monkeypatch, tmp_path, capsys):
    isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    with pytest.raises(SystemExit):
        wizard.run_wizard()
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "not a TTY" in out


def test_templates_cover_expected_seats():
    assert set(wizard.TEMPLATES) == {"fable", "moonshot", "minimax", "glm", "gpt-5"}


def test_args_templates_validate_placeholders():
    for binname, args in wizard.ARGS_TEMPLATES.items():
        assert "{prompt}" in args, binname
        assert "{model}" in args, binname


def test_seat_name_regex():
    assert wizard.SEAT_NAME_RE.match("fable-2")
    assert not wizard.SEAT_NAME_RE.match("Bad Name")
    assert not wizard.SEAT_NAME_RE.match("-x")


def test_probe_seat_never_raises(tmp_path):
    ok, err = wizard.probe_seat({
        "name": "dead",
        "models": ["m"],
        "agent": {"bin": "/nonexistent/binary", "args": ["-p", "{prompt}",
                                                         "--model", "{model}"],
                  "env": {}},
    })
    assert ok is False
    assert err
