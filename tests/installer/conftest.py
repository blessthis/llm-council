"""Shared fixtures for installer tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_host_clis(monkeypatch, tmp_path):
    """Force file-merge path even on machines with claude/gemini CLIs installed,
    and isolate PATH/HOME so nothing can touch the real user config."""
    for cls in ("ClaudeBinding", "GeminiBinding"):
        monkeypatch.setattr(
            f"llm_council.installer.hosts.{cls}.cli_binary", None, raising=False
        )
    monkeypatch.setenv("HOME", str(tmp_path))
