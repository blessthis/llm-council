"""Unit tests for seats_io (P4b): round-trip, atomicity, masking, validation."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from llm_council import seats_io
from llm_council.seats.loader import SeatsFileError

BIN = "/bin/echo"  # absolute, always resolvable
ARGS = ["-p", "{prompt}", "--model", "{model}"]


def minimal_doc(bin_path: str = BIN, env: dict | None = None) -> dict:
    return {
        "telemetry": {"enabled": False},
        "seats": {
            "alpha": {
                "models": ["m1", "m2"],
                "agent": {"bin": bin_path, "args": ARGS, "env": env or {}},
            },
        },
    }


def test_round_trip_write_load(tmp_path: Path):
    path = tmp_path / "seats.yaml"
    seats_io.atomic_write_seats(path, minimal_doc())
    assert path.exists()
    data = yaml.safe_load(path.read_text())
    assert data["seats"]["alpha"]["models"] == ["m1", "m2"]


def test_write_mode_0600(tmp_path: Path):
    path = tmp_path / "seats.yaml"
    seats_io.atomic_write_seats(path, minimal_doc())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_keeps_bak(tmp_path: Path):
    path = tmp_path / "seats.yaml"
    seats_io.atomic_write_seats(path, minimal_doc())
    doc2 = minimal_doc()
    doc2["seats"]["beta"] = {
        "models": ["x"], "agent": {"bin": BIN, "args": ARGS, "env": {}},
    }
    seats_io.atomic_write_seats(path, doc2)
    bak = tmp_path / "seats.yaml.bak"
    assert bak.exists()
    assert "beta" not in yaml.safe_load(bak.read_text())["seats"]
    assert "beta" in yaml.safe_load(path.read_text())["seats"]


def test_invalid_document_rejected_nothing_written(tmp_path: Path):
    path = tmp_path / "seats.yaml"
    seats_io.atomic_write_seats(path, minimal_doc())
    original = path.read_text()
    bad = minimal_doc()
    bad["seats"]["alpha"]["agent"]["args"] = ["--missing-placeholders"]  # no {prompt}
    with pytest.raises(SeatsFileError):
        seats_io.atomic_write_seats(path, bad)
    assert path.read_text() == original  # untouched
    assert not list(tmp_path.glob(".*.tmp-*"))  # no tmp litter


def test_allow_empty_zero_seat_document(tmp_path: Path):
    path = tmp_path / "seats.yaml"
    doc = {"telemetry": {"enabled": True}, "seats": {}}
    seats_io.atomic_write_seats(path, doc, allow_empty=True)
    assert yaml.safe_load(path.read_text())["seats"] == {}
    with pytest.raises(SeatsFileError):
        seats_io.atomic_write_seats(tmp_path / "other.yaml", doc)


def test_mask_secrets_last4(tmp_path: Path):
    doc = minimal_doc(env={"ANTHROPIC_API_KEY": "sk-ant-abcdef1234",
                           "EMPTY": "", "TINY": "ab"})
    masked = seats_io.mask_secrets(doc)
    env = masked["seats"]["alpha"]["agent"]["env"]
    assert env["ANTHROPIC_API_KEY"] == "****1234"
    assert env["EMPTY"] == ""
    assert env["TINY"] == "****"
    # original untouched
    assert doc["seats"]["alpha"]["agent"]["env"]["ANTHROPIC_API_KEY"] == "sk-ant-abcdef1234"


def test_edit_seats_file_round_trip(tmp_path: Path):
    path = tmp_path / "seats.yaml"
    path.write_text(
        "# top comment\n" + yaml.safe_dump(minimal_doc(), sort_keys=False),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)

    def mutate(d):
        d["seats"]["alpha"]["models"] = ["new-model"]

    seats_io.edit_seats_file(path, mutate)
    text = path.read_text()
    assert text.startswith("# top comment")  # ruamel preserved it
    assert "new-model" in yaml.safe_load(text)["seats"]["alpha"]["models"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_edit_rejects_invalid_mutation(tmp_path: Path):
    path = tmp_path / "seats.yaml"
    seats_io.atomic_write_seats(path, minimal_doc())

    def mutate(d):
        d["seats"]["alpha"]["agent"]["args"] = ["nonsense"]

    with pytest.raises(SeatsFileError):
        seats_io.edit_seats_file(path, mutate)
    # unchanged
    seats, _ = __import__("llm_council.seats", fromlist=["load_seats"]).load_seats(
        path, force=True)
    assert seats[0].agent.args == ARGS
