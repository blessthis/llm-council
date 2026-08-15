"""A6 wheel package-data tests: agents/ + seats.example.yaml + platforms.yaml in the wheel;
example yaml loads after secret substitution."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from llm_council.seats.loader import load_seats

REPO = Path(__file__).resolve().parents[2]


def test_wheel_contains_package_data(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv not available")
    subprocess.run(
        ["uv", "build", "-o", str(tmp_path)], cwd=REPO, check=True, capture_output=True
    )
    wheels = list(tmp_path.glob("*.whl"))
    assert wheels, "no wheel built"
    names = zipfile.ZipFile(wheels[0]).namelist()
    pi_agents = [n for n in names if n.startswith("llm_council/data/agents/pi/")]
    assert len(pi_agents) >= 3, pi_agents
    assert "llm_council/data/seats.example.yaml" in names
    assert "llm_council/installer/platforms.yaml" in names


def test_example_yaml_loads_after_secret_substitution(tmp_path: Path) -> None:
    src = REPO / "seats.example.yaml"
    dst = tmp_path / "seats.yaml"
    text = src.read_text(encoding="utf-8").replace("__REPLACE_ME__", "dummy")
    dst.write_text(text, encoding="utf-8")
    seats, warnings = load_seats(dst)
    assert len(seats) == 3
    # warnings allowed (e.g. copied-example hints); no exception is the contract
