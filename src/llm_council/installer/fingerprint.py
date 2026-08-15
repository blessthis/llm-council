"""Ownership detection by content fingerprint (PLAN Decision #12).

An MCP server entry is "ours" iff command is uvx/uv AND args contains the
string `blessthis-llm-council-server`. We NEVER inject non-standard keys
(no `_managed_by`) into host configs.
"""

from __future__ import annotations

SERVER_BINARY = "blessthis-llm-council-server"
CANONICAL_KEY = "llm-council"

_OUR_COMMANDS = {"uvx", "uv"}


def is_ours(entry: dict) -> bool:
    """True iff the server entry matches our content fingerprint."""
    if not isinstance(entry, dict):
        return False
    if entry.get("command") not in _OUR_COMMANDS:
        return False
    args = entry.get("args") or []
    return isinstance(args, (list, tuple)) and SERVER_BINARY in args


def find_ours(servers: dict) -> dict[str, dict]:
    """Return {key: entry} for every entry in *servers* matching our fingerprint."""
    if not isinstance(servers, dict):
        return {}
    return {k: v for k, v in servers.items() if is_ours(v)}
