"""Cross-platform desktop notification for crashes.

- macOS: terminal-notifier (auto-installed via brew when available) →
  osascript notification fallback
- Linux: zenity / kdialog dialogs, notify-send fallback
- Windows: PowerShell toast / WScript Popup dialog

All best-effort: any failure is swallowed — a notification must never
mask the crash itself. Headless (SSH, CI) → no-op, stderr still carries
everything.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

_TIMEOUT = 5

_TN_BREW_ATTEMPT = False


def _ensure_terminal_notifier() -> bool:
    """terminal-notifier present? If brew exists, install it once per process
    (best-effort, non-fatal). Returns True when the binary is usable."""
    global _TN_BREW_ATTEMPTED
    if shutil.which("terminal-notifier"):
        return True
    if _TN_BREW_ATTEMPTED or not shutil.which("brew"):
        return False
    _TN_BREW_ATTEMPTED = True
    try:
        subprocess.run(["brew", "install", "terminal-notifier"],
                       check=False, timeout=300, capture_output=True)
    except OSError:  # best-effort only
        return False
    return bool(shutil.which("terminal-notifier"))


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
    """terminal-notifier notification: clicking the notification body opens
    the URL (-open). No custom buttons (macOS allows those only for signed
    apps), but the whole notification is clickable and it lingers in the
    Notification Center. Returns False always (click is handled by -open;
    the caller must not open the browser again).
    Falls back to a plain osascript notification."""
    if _ensure_terminal_notifier():
        subprocess.run(
            ["terminal-notifier", "-title", title, "-message", message[:200],
             "-sound", "Basso", "-open", action_url,
             "-group", "llm-council-crash", "-ignoreDnD"],
            check=False, timeout=15, capture_output=True)
        return False  # -open handles the click; never double-open
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
    """Called at install time: ensure notification plumbing works and trigger
    the macOS notification permission prompt EARLY (not during a future crash).
    Best-effort, never raises."""
    if sys.platform == "darwin":
        if _ensure_terminal_notifier():
            subprocess.run(
                ["terminal-notifier", "-title", "blessthis-llm-council",
                 "-message", "Crash alerts will look like this — click them "
                 "to open a prefilled GitHub issue.",
                 "-group", "llm-council-probe"],
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
