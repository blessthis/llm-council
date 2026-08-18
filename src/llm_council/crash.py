"""Crash reporter: on unhandled exceptions show an alert + a prefilled GitHub
issue link (traceback, environment, recent log tail). No network calls — we
only build the URL and let the user open it.
"""

from __future__ import annotations

import os
import platform
import sys
import traceback
import urllib.parse
import webbrowser
from pathlib import Path

ISSUE_URL = "https://github.com/blessthis/llm-council/issues/new"
LOG_TAIL_LINES = 60
BODY_LIMIT = 6500  # GitHub caps ?body= well below 8k; keep headroom


def _version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("blessthis-llm-council")
    except PackageNotFoundError:  # running from source
        return "unknown (source)"


def _log_tail() -> str:
    from .config import state_dir

    path = Path(os.environ.get("LLM_COUNCIL_LOG_FILE",
                               state_dir() / "server.log"))
    if not path.is_file():
        return "(no server.log found)"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-LOG_TAIL_LINES:]) if lines else "(empty)"
    except OSError as exc:
        return f"(could not read {path}: {exc!r})"


def _env_block() -> str:
    seats = "yes" if (Path.home() / ".blessthis-llm-council" / "seats.yaml").is_file() else "no"
    return (
        f"- blessthis-llm-council: {_version()}\n"
        f"- python: {sys.version.split()[0]} ({platform.machine()})\n"
        f"- platform: {platform.system()} {platform.release()}\n"
        f"- seats.yaml present: {seats}\n"
    )


def build_issue_url(exc: BaseException) -> str:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    title = f"crash: {type(exc).__name__}: {str(exc)[:120]}"
    body = (
        "## Crash report (auto-generated)\n\n"
        "### Traceback\n```\n"
        f"{tb.strip()}\n"
        "```\n\n"
        "### Environment\n"
        f"{_env_block()}\n"
        "### Recent server.log tail\n```\n"
        f"{_log_tail()}\n"
        "```\n\n"
        "### What I was doing\n<!-- describe the steps you took before the crash -->\n"
    )
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + "\n…(truncated — attach server.log manually)\n"
    q = urllib.parse.urlencode({"title": title[:150], "body": body,
                                "labels": "crash"})
    return f"{ISSUE_URL}?{q}"


def install(*, headless_ok: bool = False) -> None:
    """Install the crash hook (CLI + MCP server entrypoints). Idempotent.

    headless_ok: MCP server — no user terminal; the alert goes to a desktop
    notification + server.log/stderr instead of terminal banner + browser.
    """
    if getattr(sys.excepthook, "_is_council_crashhook", False):
        return

    def _hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        url = build_issue_url(exc)
        from . import notify

        # Native alert with an action button where the platform allows it
        # (macOS terminal-notifier / Windows Popup / Linux zenity+kdialog).
        # Clicking the button opens the prefilled GitHub issue. Falls back to
        # a plain notification; never raises.
        clicked = notify.notify(
            "blessthis-llm-council crashed",
            f"{exc_type.__name__}: {str(exc)[:120]} — click to report",
            action_url=url, action_label="Open GitHub issue",
            close_label="Dismiss")
        if clicked and webbrowser.open(url):
            sys.__excepthook__(exc_type, exc, tb)
            return
        if not headless_ok:
            print("\n" + "=" * 70, file=sys.stderr)
            print("  blessthis-llm-council crashed — sorry!", file=sys.stderr)
            print(f"  {exc_type.__name__}: {exc}", file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            print("Everything needed is packed into this GitHub issue link",
                  file=sys.stderr)
            print("(traceback + environment + recent server.log):", file=sys.stderr)
            print(f"\n  {url}\n", file=sys.stderr)
            if not os.environ.get("LLM_COUNCIL_NO_BROWSER"):
                try:
                    webbrowser.open(url)
                    print("(opened in your browser; unchecked? copy the link above)",
                          file=sys.stderr)
                except Exception:  # noqa: BLE001 — headless / no handler
                    pass
            print("Auto-open annoying? Set LLM_COUNCIL_NO_BROWSER=1.",
                  file=sys.stderr)
        else:
            # MCP server: no terminal, no browser — log + stderr for the host.
            import logging

            logging.getLogger("llm_council").critical(
                "UNHANDLED CRASH %s: %s — prefilled issue: %s",
                exc_type.__name__, exc, url)
            print(f"llm-council crash: {exc_type.__name__}: {exc} — "
                  f"prefilled issue link written to server.log", file=sys.stderr)
        sys.__excepthook__(exc_type, exc, tb)

    _hook._is_council_crashhook = True  # type: ignore[attr-defined]
    sys.excepthook = _hook

