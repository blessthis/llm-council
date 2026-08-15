"""Per-host MCP registration bindings (PLAN P4a, Decision #11).

One class per host; quirks live IN CODE (Q7) — platforms.yaml is data only.

Merge safety (all hosts): read the whole config → add ONLY our `llm-council`
entry → write back atomically (tmp file + os.replace) keeping a `.bak` backup.
Other entries survive untouched. Ownership is by content fingerprint
(Decision #12); we never write non-standard keys.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import tomlkit

from llm_council.installer.fingerprint import CANONICAL_KEY, find_ours, is_ours
from llm_council.installer.platforms import Platform, load_platforms

log = logging.getLogger(__name__)

STOP_MESSAGE = (
    "A server named 'llm-council' already exists and isn't ours. "
    "We won't clobber it. Rename/remove it, then re-run."
)
FOREIGN_KEY_MESSAGE = (
    "An llm-council server matching our fingerprint is already registered "
    "under a different key ({key!r}). We won't create a duplicate — "
    "rename or remove it, then re-run."
)


class RegistrationConflict(Exception):
    """Refused to register: conflict with a user-owned entry (installer-ux §3.2)."""


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    elif path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    _atomic_write(path, json.dumps(data, indent=2) + "\n")


class HostBinding(ABC):
    """Base class for one consumer host."""

    #: top-level key of the server collection in the config file
    servers_key = "mcpServers"
    #: host exposes an `mcp add`-style CLI we prefer (Decision #11)
    cli_binary: str | None = None
    #: project-scoped hosts (copilot) resolve paths against a project root
    project_scoped = False

    def __init__(self, platform: Platform, home: Path | None = None,
                 project_path: Path | None = None) -> None:
        self.platform = platform
        self._home = Path(home) if home is not None else Path.home()
        self._project = Path(project_path) if project_path is not None else Path.cwd()

    # -- paths -----------------------------------------------------------
    def _expand(self, p: str) -> Path:
        if p.startswith("~"):
            return self._home / p[2:]
        candidate = Path(p)
        if not candidate.is_absolute():
            return self._project / candidate
        return candidate

    def config_path(self) -> Path:
        return self._expand(self.platform.mcp_config_path)

    def agents_dir(self) -> Path:
        return self._expand(self.platform.agent_dir)

    # -- detection -------------------------------------------------------
    def detect(self) -> bool:
        """Host appears installed (detect_paths exist / CLI on PATH)."""
        for p in self.platform.detect_paths:
            if p.startswith("~"):
                if (self._home / p[2:]).exists():
                    return True
            elif self._expand(p).exists():
                return True
        if self.cli_binary and shutil.which(self.cli_binary):
            return True
        return False

    # -- registration ----------------------------------------------------
    def build_entry(self, seats_file_abs: str) -> dict:
        return {
            "type": "stdio",
            "command": "uvx",
            "args": ["blessthis-llm-council-server"],
            "env": {"SEATS_FILE": str(seats_file_abs)},
        }

    def _check_conflicts(self, servers: dict) -> None:
        ours = find_ours(servers)
        foreign_keys = sorted(set(ours) - {CANONICAL_KEY})
        if foreign_keys:
            raise RegistrationConflict(FOREIGN_KEY_MESSAGE.format(key=foreign_keys[0]))
        existing = servers.get(CANONICAL_KEY)
        if existing is not None and not is_ours(existing):
            raise RegistrationConflict(STOP_MESSAGE)

    def register(self, server_name: str, seats_file_abs: str) -> dict:
        """Upsert our entry under the canonical key. Returns a status dict."""
        if server_name != CANONICAL_KEY:
            raise ValueError(
                f"Canonical server name is {CANONICAL_KEY!r}, got {server_name!r} "
                "(Decision #10)"
            )
        if self.cli_binary and shutil.which(self.cli_binary):
            rc = self._register_via_cli(seats_file_abs)
            if rc == 0:
                return {"method": "cli", "binary": self.cli_binary}
            log.warning(
                "%s CLI failed (rc=%s); falling back to file-merge",
                self.cli_binary, rc,
            )
        return self._register_via_file(seats_file_abs)

    def _register_via_cli(self, seats_file_abs: str) -> int:
        cmd = self._cli_command(seats_file_abs)
        try:
            return subprocess.run(cmd, check=False, capture_output=True).returncode
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("CLI spawn failed: %s", exc)
            return 1

    def _cli_command(self, seats_file_abs: str) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    @abstractmethod
    def _register_via_file(self, seats_file_abs: str) -> dict: ...

    @abstractmethod
    def uninstall(self) -> list[str]:
        """Remove fingerprint-matching entries (any key); return removed keys."""


class JsonHostBinding(HostBinding):
    """Shared file-merge implementation for JSON-config hosts.

    Claude/Gemini/Cursor/pi use `mcpServers`; Copilot uses `servers`.
    """

    def _entry_extra(self, seats_file_abs: str) -> dict:
        return {}

    def _register_via_file(self, seats_file_abs: str) -> dict:
        path = self.config_path()
        data = _read_json(path)
        servers = data.setdefault(self.servers_key, {})
        if not isinstance(servers, dict):
            raise RegistrationConflict(
                f"{self.servers_key} in {path} is not an object; refusing."
            )
        self._check_conflicts(servers)
        entry = self.build_entry(seats_file_abs)
        entry.update(self._entry_extra(seats_file_abs))
        servers[CANONICAL_KEY] = entry
        _write_json(path, data)
        return {"method": "file-merge", "path": str(path)}

    def uninstall(self) -> list[str]:
        path = self.config_path()
        if not path.exists():
            return []
        data = _read_json(path)
        servers = data.get(self.servers_key)
        if not isinstance(servers, dict):
            return []
        removed = [k for k in find_ours(servers)]
        for k in removed:
            del servers[k]
        if removed:
            _write_json(path, data)
        return sorted(removed)


class ClaudeBinding(JsonHostBinding):
    cli_binary = "claude"

    def _cli_command(self, seats_file_abs: str) -> list[str]:
        return [
            "claude", "mcp", "add", "--scope", "user", "--transport", "stdio",
            "--env", f"SEATS_FILE={seats_file_abs}",
            CANONICAL_KEY, "--", "uvx", "blessthis-llm-council-server",
        ]


class GeminiBinding(JsonHostBinding):
    cli_binary = "gemini"

    def _cli_command(self, seats_file_abs: str) -> list[str]:
        return [
            "gemini", "mcp", "add", "-s", "user",
            "-e", f"SEATS_FILE={seats_file_abs}",
            CANONICAL_KEY, "uvx", "blessthis-llm-council-server",
        ]


class PiBinding(JsonHostBinding):
    """pi quirks: lifecycle lazy-keep-alive + toolPrefix mcp (no `type` key)."""

    def build_entry(self, seats_file_abs: str) -> dict:
        entry = super().build_entry(seats_file_abs)
        entry.pop("type", None)  # pi entries in the wild carry no `type`
        return entry

    def _entry_extra(self, seats_file_abs: str) -> dict:
        return {"lifecycle": "lazy-keep-alive", "toolPrefix": "mcp"}


class CursorBinding(JsonHostBinding):
    pass


class CopilotBinding(JsonHostBinding):
    """Per-project `.vscode/mcp.json`, top-level `servers` key."""

    servers_key = "servers"
    project_scoped = True


class CodexBinding(HostBinding):
    """Codex: `~/.codex/config.toml` via tomlkit (comments survive)."""

    def _entry_table(self, seats_file_abs: str) -> dict[str, Any]:
        return {
            "command": "uvx",
            "args": ["blessthis-llm-council-server"],
            "env": {"SEATS_FILE": str(seats_file_abs)},
            "enabled": True,
        }

    def _register_via_file(self, seats_file_abs: str) -> dict:
        path = self.config_path()
        if path.exists():
            doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        else:
            doc = tomlkit.document()
        table = doc.setdefault("mcp_servers", {})
        if not hasattr(table, "keys"):
            raise RegistrationConflict("mcp_servers in config.toml is not a table")
        servers = dict(table)
        self._check_conflicts(servers)
        table[CANONICAL_KEY] = self._entry_table(seats_file_abs)  # type: ignore[index]
        _atomic_write(path, tomlkit.dumps(doc))
        return {"method": "file-merge", "path": str(path)}

    def uninstall(self) -> list[str]:
        path = self.config_path()
        if not path.exists():
            return []
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        table = doc.get("mcp_servers")
        if not hasattr(table, "keys"):
            return []
        servers = dict(table)
        removed = [k for k in find_ours(servers)]
        for k in removed:
            del table[k]  # type: ignore[operator]
        if removed:
            _atomic_write(path, tomlkit.dumps(doc))
        return sorted(removed)


_HOST_CLASSES: dict[str, type[HostBinding]] = {
    "claude": ClaudeBinding,
    "pi": PiBinding,
    "cursor": CursorBinding,
    "codex": CodexBinding,
    "copilot": CopilotBinding,
    "gemini": GeminiBinding,
}


def get_host_binding(name: str, home: Path | None = None,
                     project_path: Path | None = None) -> HostBinding:
    platforms = load_platforms()
    if name not in platforms:
        raise KeyError(f"Unknown host {name!r}; known: {sorted(platforms)}")
    return _HOST_CLASSES[name](platforms[name], home=home, project_path=project_path)
