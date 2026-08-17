"""How the MCP server binary is launched (registry vs local dev install).

`uvx blessthis-llm-council-server` does NOT work: uvx treats its first
non-flag argument as a PACKAGE name, and the package is
`blessthis-llm-council` — the server is a console script inside it. The
correct registry invocation is:

    uvx --from blessthis-llm-council blessthis-llm-council-server

When the installer itself was launched from a local source (a wheel/sdist
path or a checkout dir via `uvx --from <path>` / `uv tool install
--editable .`), the registered entry should launch the server the SAME way,
so local testing works without publishing to PyPI.

Resolution order for the launch command:
1. LLM_COUNCIL_SERVER_CMD env (override, argv separated by shell rules).
2. If our own running environment came from a uvx `--from <src>` run
   (detected via UV env), use ["uvx", "--from", src, binary].
3. Default: ["uvx", "--from", "blessthis-llm-council", binary].
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVER_BINARY = "blessthis-llm-council-server"
PACKAGE_NAME = "blessthis-llm-council"


def _local_source() -> str | None:
    """Detect if we are running from a uvx `--from <local path>` environment."""
    # uv sets UV_TOOL_... for tool installs; for uvx runs the archive cache
    # path contains archive-v0 — not distinguishable. Instead check if the
    # package we live in is importable from a local checkout (pyproject next
    # to src/) — that covers `uvx --from <dir>` and editable installs.
    try:
        import llm_council

        pkg_dir = Path(llm_council.__file__).resolve().parent
        repo_root = pkg_dir.parent.parent  # <repo>/src/llm_council
        if (repo_root / "pyproject.toml").exists() and (repo_root / "src").is_dir():
            return str(repo_root)
    except (OSError, ImportError, ValueError):
        pass
    return None


def _python_flag() -> list[str]:
    """Pin the interpreter the installer itself runs under (spawn checks).

    Without this, the uvx subprocess inherits the system default python,
    which may be an x86_64 build under Rosetta on an otherwise arm64 Mac —
    cryptography and other wheels then have no matching binaries. Passing
    the exact interpreter we are running on keeps architectures consistent.
    """
    pinned = os.environ.get("LLM_COUNCIL_SERVER_PYTHON", "").strip()
    if pinned:
        return ["--python", pinned]
    interp = sys.executable
    if interp and Path(interp).exists():
        return ["--python", interp]
    return []


def server_args() -> list[str]:
    """Full argv prefix that launches the MCP server (without CLI flags)."""
    override = os.environ.get("LLM_COUNCIL_SERVER_CMD", "").strip()
    if override:
        return override.split()

    src = _local_source()
    if src:
        return ["uvx", *_python_flag(), "--from", src, SERVER_BINARY]

    return ["uvx", *_python_flag(), "--from", PACKAGE_NAME, SERVER_BINARY]


def spawn_check_command() -> list[str]:
    """Command used by installer spawn checks (uvx cache warm-up)."""
    return [*server_args(), "--help"]
