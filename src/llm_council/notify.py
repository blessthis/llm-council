"""Cross-platform desktop notification for crashes.

- macOS: alerter (action buttons, waits for click, auto-installed to
  ~/.blessthis-llm-council/bin) → terminal-notifier (brew) → osascript
- Linux: zenity / kdialog dialogs, notify-send fallback
- Windows: PowerShell toast / WScript Popup dialog

All best-effort: any failure is swallowed — a notification must never
mask the crash itself. Headless (SSH, CI) → no-op, stderr still carries
everything.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

_TIMEOUT = 5

ALERTER_URL = ("https://github.com/vjeantet/alerter/releases/download/"
               "v26.5/alerter-26.5.zip")
# alerter 26.5 ships a fat binary; the zip contains the arm64 build
ALERTER_URL_X64 = ("https://github.com/vjeantet/alerter/releases/download/"
                   "v26.5/alerter-26.5-intel.zip")


def _alerter_path() -> Path:
    return Path.home() / ".blessthis-llm-council" / "bin" / "alerter"


def _ensure_alerter() -> bool:
    """alerter present? If not, download once (both arch zips tried).
    Returns True when the binary is usable."""
    p = _alerter_path()
    if p.is_file() and os_access_x(p):
        return True
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        urls = ([ALERTER_URL_X64, ALERTER_URL]
                if sys.maxsize <= 2**32 or platform_is_x64() else
                [ALERTER_URL, ALERTER_URL_X64])
        # arch preference first; try both, keep whichever runs
        for url in urls:
            try:
                import io
                import zipfile

                with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310
                    data = r.read()
                zf = zipfile.ZipFile(io.BytesIO(data))
                member = next((n for n in zf.namelist()
                               if n.endswith("alerter")
                               or Path(n).name == "alerter"), None)
                if member is None:
                    continue
                p.write_bytes(zf.read(member))
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                        | stat.S_IXOTH)
                # smoke test: does it even execute on this arch?
                r = subprocess.run([str(p), "--help"], capture_output=True,
                                   timeout=15)
                if r.returncode == 0:
                    return True
            except Exception:  # noqa: BLE001 — try next url
                continue
        return False
    except Exception:  # noqa: BLE001
        return False


def platform_is_x64() -> bool:
    import platform

    return platform.machine() in ("x86_64", "amd64")


def os_access_x(p: Path) -> bool:
    import os

    return os.access(p, os.X_OK)


def notify(title: str, message: str, *, action_url: str | None = None,
           action_label: str = "Open issue",
           close_label: str = "Dismiss") -> bool:
    """Show a desktop notification on any platform. Best-effort, never raises.

    action_url: when given, shows an alert with an action button. Returns
    True when the user clicked it (caller then opens the URL itself).
    """
    try:
        if action_url is not None:
            return _alert(title, message, action_url, action_label, close_label)
        if sys.platform == "darwin":
            _macos(title, message)
        elif sys.platform.startswith("linux"):
            _linux(title, message)
        elif sys.platform == "win32":
            _windows(title, message)
    except Exception:  # noqa: BLE001 — notifications must never crash the crash handler
        pass
    return False


def _alert(title: str, message: str, action_url: str,
           action_label: str, close_label: str) -> bool:
    """Alert with a custom action button. True = user clicked the action."""
    try:
        if sys.platform == "darwin":
            return _macos_dialog(title, message, action_url, action_label,
                                 close_label)
        if sys.platform == "win32":
            return _windows_dialog(title, message, action_url)
        return _linux_dialog(title, message, action_url)
    except Exception:  # noqa: BLE001
        return False


def _macos(title: str, message: str) -> None:
    script = (f'display notification "{_esc(message)}" '
              f'with title "{_esc(title)}" sound name "Basso"')
    subprocess.run(["osascript", "-e", script], check=False,
                   timeout=_TIMEOUT, capture_output=True)


def _macos_dialog(title: str, message: str, action_url: str,
                  action_label: str, close_label: str) -> bool:
    """alerter gives a native notification with a custom action button that
    WAITS for the click. rc=0 + action label on stdout = action clicked.
    Falls back to terminal-notifier (click opens URL), then osascript."""
    if _ensure_alerter():
        try:
            # ONE plain action button — no dropdown (dropdown only appears
            # with multiple actions / dropdown-label). Default close label
            # "Dismiss" keeps the (x) side; the main button is the only thing
            # to click. Auto-dismiss after 5 min so we never hang forever.
            r = subprocess.run(
                [str(_alerter_path()),
                 "--title", title, "--message", message[:200],
                 "--actions", action_label,
                 "--sound", "Basso", "--group", "llm-council-crash",
                 "--timeout", "300"],
                check=False, timeout=310, capture_output=True, text=True)
            return (r.returncode == 0
                    and action_label in (r.stdout or ""))
        except Exception:  # noqa: BLE001
            pass
    # terminal-notifier fallback (installed by probe when brew exists)
    if shutil.which("terminal-notifier"):
        r = subprocess.run(
            ["terminal-notifier", "-title", title, "-message", message,
             "-sound", "Basso", "-open", action_url,
             "-group", "llm-council-crash", "-ignoreDnD"],
            check=False, timeout=15, capture_output=True)
        return False  # click opens the URL itself; we can't detect it
    # last resort: plain notification, link stays in terminal/log
    _macos(title, message)
    return False


def _linux(title: str, message: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-u", "critical", "-a",
                        "blessthis-llm-council", title, message],
                       check=False, timeout=_TIMEOUT, capture_output=True)


def _linux_dialog(title: str, message: str, action_url: str) -> bool:
    text = f"{message}\n\nOpen the prefilled GitHub issue?"
    if shutil.which("kdialog"):
        r = subprocess.run(["kdialog", "--title", title, "--yesno", text],
                           check=False, timeout=120, capture_output=True)
        return r.returncode == 0
    if shutil.which("zenity"):
        r = subprocess.run(
            ["zenity", "--question", "--title", title, "--text", text,
             "--width=480"],
            check=False, timeout=120, capture_output=True)
        return r.returncode == 0
    if shutil.which("notify-send"):  # headless fallback
        _linux(title, message)
    return False


def _windows(title: str, message: str) -> None:
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI."
        "Notifications, ContentType = WindowsRuntime] > $null; "
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::"
        "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::"
        "ToastText02); $x=$t.GetXml(); "
        "$x.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode("
        f"'{_esc(title)}'))> $null; "
        "$x.GetElementsByTagName('text').Item(1).AppendChild($t.CreateTextNode("
        f"'{_esc(message)}'))> $null; "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'blessthis-llm-council').Show("
        "[Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   check=False, timeout=_TIMEOUT, capture_output=True)


def _windows_dialog(title: str, message: str, action_url: str) -> bool:
    """WScript Popup: OK opens the URL (returns 1), Cancel dismisses (2)."""
    ps = (
        f"(New-Object -ComObject WScript.Shell).Popup("
        f"'{_esc(message)}\\n\\nOpen the prefilled GitHub issue?',"
        f"0,'{_esc(title)}',1+16)")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       check=False, timeout=120, capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() in ("1", "True")


def probe_notifications() -> None:
    """Called at install time: ensure alert plumbing works and trigger the
    macOS notification permission prompt EARLY (not during a future crash).
    Best-effort, never raises."""
    if sys.platform == "darwin":
        if _ensure_alerter():
            subprocess.run(
                [str(_alerter_path()), "--title", "blessthis-llm-council",
                 "--message", "Crash alerts will look like this.",
                 "--group", "llm-council-probe", "--timeout", "10"],
                check=False, timeout=15, capture_output=True)
        else:
            subprocess.run(
                ["osascript", "-e",
                 'display notification "Crash alerts will look like this." '
                 'with title "blessthis-llm-council"'],
                check=False, timeout=10, capture_output=True)
    elif sys.platform.startswith("linux") and shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-a", "blessthis-llm-council",
             "blessthis-llm-council", "Crash alerts will look like this."],
            check=False, timeout=10, capture_output=True)
    elif sys.platform == "win32":
        _windows("blessthis-llm-council", "Crash alerts will look like this.")


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:200]
