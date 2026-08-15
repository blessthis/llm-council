"""E2E installer test: build wheel → fresh venv → full install/doctor/uninstall
against an ISOLATED fake home. Skipped unless E2E_INSTALL=1 (RUN_INTEGRATION
pattern, tests/integration/conftest.py).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("E2E_INSTALL") != "1",
                       reason="e2e installer tests need E2E_INSTALL=1"),
]

REPO = Path(__file__).resolve().parents[2]
# Real host configs the installer must NEVER touch (isolation guard).
REAL_CONFIGS = [
    Path.home() / p for p in (
        ".claude.json", ".codex/config.toml", ".cursor/mcp.json",
        ".gemini/settings.json", ".pi/agent/mcp.json",
    )
]


def _stat_snapshot() -> dict:
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in REAL_CONFIGS if p.exists()}


def _run(cmd: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=600)


def test_e2e_install_doctor_uninstall(tmp_path: Path) -> None:
    before = _stat_snapshot()

    # --- 1+2: wheel + fresh venv -------------------------------------------
    wheel_dir = tmp_path / "dist"
    subprocess.run(["uv", "build", "--wheel", "-o", str(wheel_dir)],
                   cwd=REPO, check=True, capture_output=True, timeout=300)
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True,
                   timeout=120)
    wheel = next(wheel_dir.glob("*.whl"))
    vpy = str(venv / "bin" / "python")
    install = ["uv", "pip", "install", "--python", vpy, str(wheel)]
    try:
        subprocess.run(install, check=True, capture_output=True, timeout=600)
    except (subprocess.CalledProcessError, FileNotFoundError):
        subprocess.run([vpy, "-m", "pip", "install", str(wheel)], check=True,
                       capture_output=True, timeout=900)

    # Isolated environment: fake home, restricted PATH (no host CLIs →
    # file-merge registration path, no accidental real-host touching).
    home = tmp_path / "fakehome"
    home.mkdir()
    (home / ".blessthis-llm-council").mkdir()
    seats = home / ".blessthis-llm-council" / "seats.yaml"
    shutil.copy2(REPO / "seats.example.yaml", seats)
    seats.chmod(0o600)
    (home / ".claude.json").write_text(json.dumps(
        {"mcpServers": {"other-server": {"command": "npx", "args": ["x"]}}}))

    env = {
        "HOME": str(home),
        "PATH": f"{venv / 'bin'}:/usr/bin:/bin",
        "SEATS_FILE": str(seats),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }

    def cli(*args: str) -> subprocess.CompletedProcess:
        return _run([str(venv / "bin" / "blessthis-llm-council"), *args],
                    cwd=tmp_path, env=env)

    # --- 3: both console scripts -------------------------------------------
    for script in ("blessthis-llm-council", "blessthis-llm-council-server"):
        proc = _run([str(venv / "bin" / script), "--help"], cwd=tmp_path, env=env)
        assert proc.returncode == 0, f"{script} --help rc={proc.returncode}: {proc.stderr}"

    # --- 5: non-interactive install (claude, wizard --yes --offline) -------
    proc = cli("install", "--host", "claude", "--yes", "--offline")
    assert proc.returncode == 0, f"install failed:\n{proc.stdout}\n{proc.stderr}"

    # --- 6: verify wiring in FAKE home --------------------------------------
    cfg = json.loads((home / ".claude.json").read_text())
    entry = cfg["mcpServers"]["llm-council"]
    assert entry["command"] in ("uvx", "uv")
    assert "blessthis-llm-council-server" in entry["args"]
    assert entry["env"]["SEATS_FILE"] == str(seats)
    assert cfg["mcpServers"]["other-server"] == {"command": "npx", "args": ["x"]}
    agents = sorted(p.name for p in (home / ".claude" / "agents").glob(
        "blessthis-council-*.md"))
    assert agents, "no agent files deployed"

    # --- 7: doctor ----------------------------------------------------------
    proc = cli("doctor", "--offline")
    assert proc.returncode == 0, f"doctor rc={proc.returncode}:\n{proc.stdout}"
    assert "claude" in proc.stdout

    # --- 9: status + seats list --------------------------------------------
    assert cli("status").returncode == 0
    assert cli("seats", "list").returncode == 0

    # --- 8: uninstall --------------------------------------------------------
    proc = cli("uninstall", "--host", "claude", "--yes")
    assert proc.returncode == 0, f"uninstall rc={proc.returncode}:\n{proc.stdout}"
    cfg = json.loads((home / ".claude.json").read_text())
    assert "llm-council" not in cfg["mcpServers"]
    assert cfg["mcpServers"]["other-server"]["command"] == "npx"  # preserved
    assert not list((home / ".claude" / "agents").glob("blessthis-council-*.md"))

    # --- isolation guard: real host configs untouched -----------------------
    after = _stat_snapshot()
    assert after == before, (
        f"installer touched REAL host configs: "
        f"{ {k: (before[k], after[k]) for k in after if before.get(k) != after[k]} }")
