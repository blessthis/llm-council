#!/usr/bin/env python3
"""P1 smoke test: start blessthis-llm-council-server over stdio and assert the
MCP handshake reports EXACTLY 17 tools (acceptance.md P1, item 11).

Usage: uv run python scripts/smoke_tools.py [path-to-server-binary]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

EXPECTED = sorted([
    "council_start", "council_poll", "council_answer", "council_ask",
    "council_reveal", "council_is_model_replied", "council_score", "council_close",
    "model_scores", "seat_health",
    "chat_start", "chat_send", "chat_poll", "chat_history", "chat_list", "chat_close",
    "list_seats",
])


def main() -> int:
    cmd = sys.argv[1:] or ["blessthis-llm-council-server"]
    # Negative acceptance: completely empty environment except PATH/HOME.
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    p = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
    )
    assert p.stdin and p.stdout

    def rpc(method, params=None, rid=1):
        p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        ) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    try:
        rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        })
        p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        ) + "\n")
        p.stdin.flush()
        names = sorted(t["name"] for t in rpc("tools/list", rid=2)["result"]["tools"])
        assert len(names) == 17, f"expected 17 tools, got {len(names)}: {names}"
        assert names == EXPECTED, f"tool mismatch: {names}"
    finally:
        p.terminate()
    print("OK: 17 tools")
    return 0


if __name__ == "__main__":
    sys.exit(main())
