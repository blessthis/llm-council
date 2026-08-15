"""Unit tests for the wizard's non-interactive plumbing (P4b)."""

from __future__ import annotations

import pytest
from rich.console import Console

from llm_council import wizard


def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv("SEATS_FILE", raising=False)




class _StubAnswer:
    """questionary-style answer object for stubbing prompts."""

    def __init__(self, v):
        self.v = v

    def ask(self):
        return self.v


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


def test_edit_flow_merges_with_existing_seats(monkeypatch, tmp_path):
    """Edit-seats must MERGE: existing seats + telemetry survive, new seat
    lands on top, and the written file holds the complete merged doc."""
    isolate(monkeypatch, tmp_path)
    baseline = {
        "telemetry": {"enabled": False},
        "seats": {
            "fable": {"models": ["m1"], "agent": {"bin": "claude",
                     "args": ["-p", "{prompt}"], "env": {}}},
            "moonshot": {"models": ["m2"], "agent": {"bin": "claude",
                         "args": ["-p", "{prompt}"], "env": {}}},
        },
    }
    # non-interactive stubs: no removal, template add "glm", no custom adds,
    # telemetry confirm follows its baseline default
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: k.get("default", False))
    monkeypatch.setattr(wizard.questionary, "checkbox",
                        lambda *a, **k: _StubAnswer(["glm"]))
    monkeypatch.setattr(
        wizard, "build_seat_interactive",
        lambda *a, **k: {"glm": {"models": ["glm-5.2"], "agent": {
            "bin": "pi", "args": ["-p", "{prompt}"], "env": {}}}})
    doc = wizard.run_phase_a(Console(), yes=False, offline=True, baseline=baseline)
    out = tmp_path / "seats.yaml"
    from llm_council import seats_io
    seats_io.atomic_write_seats(out, doc, allow_empty=True)
    written = seats_io.load_yaml(out)
    assert set(written["seats"]) == {"fable", "moonshot", "glm"}
    assert written["telemetry"] == {"enabled": False}
    # untouched seats survive verbatim
    assert written["seats"]["fable"] == baseline["seats"]["fable"]


def test_edit_mode_preview_marks_untouched_seats(monkeypatch, tmp_path):
    """Edit-mode preview collapses untouched seats to a one-line comment and
    fully renders touched ones; telemetry block stays rendered."""
    doc = {
        "telemetry": {"enabled": False},
        "seats": {
            "fable": {"models": ["m1"], "agent": {"bin": "claude",
                     "args": ["-p", "{prompt}"], "env": {}}},
            "glm": {"models": ["glm-5.2"], "agent": {"bin": "pi",
                    "args": ["-p", "{prompt}"], "env": {}}},
        },
    }
    preview = wizard._preview_yaml(doc, untouched=["fable"])
    assert "fable:  # unchanged" in preview
    assert "m1" not in preview.split("seats:", 1)[1]  # fable body not dumped
    assert "glm-5.2" in preview
    assert "enabled: false" in preview


def test_no_changes_skips_write(monkeypatch, tmp_path):
    """Edit mode with zero changes leaves the file byte-identical and skips
    the write confirm entirely (goes straight to Phase B)."""
    isolate(monkeypatch, tmp_path)
    baseline = {
        "telemetry": {"enabled": False},
        "seats": {
            "fable": {"models": ["m1"], "agent": {"bin": "claude",
                     "args": ["-p", "{prompt}"], "env": {}}},
        },
    }
    # user selects no templates, removes nothing, telemetry keeps baseline;
    # empty template selection needs the "continue with no seats" confirm = Yes
    monkeypatch.setattr(
        wizard, "_confirm",
        lambda c, q, **k: True if "No seats selected" in q else k.get("default", False))
    monkeypatch.setattr(wizard.questionary, "checkbox",
                        lambda *a, **k: _StubAnswer([]))
    # allow the empty-selection continue confirm
    changes: dict = {}
    doc = wizard.run_phase_a(Console(), yes=False, offline=True,
                             baseline=baseline, changes=changes)
    added, modified, removed, untouched = wizard._seat_changes(baseline, doc, changes)
    assert added == [] and modified == [] and removed == []
    write_needed = wizard._print_change_summary(
        Console(), added, modified, removed, untouched,
        telemetry_changed=doc.get("telemetry") != baseline.get("telemetry"))
    assert write_needed is False


def test_replace_from_scratch_starts_empty(monkeypatch, tmp_path):
    """No baseline → phase A builds a fresh doc from scratch only."""
    isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: k.get("default", False))
    monkeypatch.setattr(wizard.questionary, "checkbox",
                        lambda *a, **k: _StubAnswer(["glm"]))
    monkeypatch.setattr(
        wizard, "build_seat_interactive",
        lambda *a, **k: {"glm": {"models": ["glm-5.2"], "agent": {
            "bin": "pi", "args": ["-p", "{prompt}"], "env": {}}}})
    doc = wizard.run_phase_a(Console(), yes=False, offline=True, baseline=None)
    assert set(doc["seats"]) == {"glm"}
    assert doc["telemetry"] == {"enabled": True}
