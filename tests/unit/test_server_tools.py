"""P1 acceptance: the FastMCP server registers EXACTLY 17 tools
(10 council + 6 chat + 1 discovery — Decisions #17/#18/#20)."""

import asyncio

from llm_council.server import mcp

EXPECTED = sorted([
    # council (10)
    "council_start", "council_poll", "council_answer", "council_ask",
    "council_reveal", "council_is_model_replied", "council_score", "council_close",
    "model_scores", "seat_health",
    # chat (6, P1 stubs)
    "chat_start", "chat_send", "chat_poll", "chat_history", "chat_list", "chat_close",
    # discovery (1, P1 stub)
    "list_seats",
])

# The 17 gateway-era tools removed in Decision #17 must be gone.
REMOVED = {
    "list_models", "create_session", "list_sessions", "list_working_dirs",
    "get_session", "send_message", "list_messages", "set_context_anchor",
    "compact_context", "switch_model", "delete_session", "context_status",
    "set_auto_compact", "attach_media", "detach_media", "list_attachments",
    "minimax_list_files", "minimax_upload_video",
}


def _tool_names() -> list[str]:
    return sorted(t.name for t in asyncio.run(mcp.list_tools()))


def test_exactly_17_tools_registered():
    names = _tool_names()
    assert len(names) == 17, names
    assert names == EXPECTED


def test_gateway_era_tools_removed():
    assert REMOVED.isdisjoint(_tool_names())
