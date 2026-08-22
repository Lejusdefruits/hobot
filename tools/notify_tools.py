"""Proactive notifications -- desktop (notify-send) and Discord together via
notify_all(), so you find out even away from the machine instead of having to
type /status to see what happened during a scheduled run.

notify_all() also persists every notification into the `notifications` table --
the chat agent (graphs/chat_agent.py, /ask) has no other visibility into what
went out this way (posted straight to the Discord channel, outside its
per-thread conversation memory), so without this a reply to one ("that
posting", "that email") had nothing to anchor to. See
graphs/chat_agent.py::dernieres_notifications, which reads it back.
"""
import json
import subprocess
import sys


def _sanitize_for_script_literal(text: str) -> str:
    """Neither AppleScript's nor PowerShell's string-literal escaping rules
    can be exercised on this Linux box (osascript/powershell.exe don't exist
    here to test against) -- rather than trust untested escaping logic
    against a title/message that can contain arbitrary text (an offer title,
    a company name), strip the characters that could break out of a quoted
    literal (or, worse, get interpreted as script) instead of escaping them.
    A title/message occasionally missing a quote character is a cosmetic
    loss; a broken or injectable notification command is not."""
    for ch in ('"', "'", "`", "\\", "$"):
        text = text.replace(ch, "")
    return " ".join(text.split())  # also collapses newlines to spaces


def _notify_linux(title: str, message: str, urgency: str) -> None:
    subprocess.run(
        ["notify-send", "--app-name=hobot", f"--urgency={urgency}", title, message],
        check=True, timeout=5,
    )


def _notify_macos(title: str, message: str) -> None:
    # osascript ships with every macOS install -- no extra dependency, unlike
    # the Linux/Windows paths which both rely on something that may or may not
    # be present (notify-send, PowerShell's Forms assembly).
    title, message = _sanitize_for_script_literal(title), _sanitize_for_script_literal(message)
    script = f'display notification "{message}" with title "{title}"'
    subprocess.run(["osascript", "-e", script], check=True, timeout=5)


def _notify_windows(title: str, message: str) -> None:
    # No new pip dependency: System.Windows.Forms.NotifyIcon ships with every
    # Windows .NET install, reachable from the powershell.exe already on PATH.
    # A balloon tip, not a modern Action Center toast -- the latter needs a
    # registered AppUserModelID (effectively an installed app identity),
    # which a plain Python script run from a terminal doesn't have.
    title, message = _sanitize_for_script_literal(title), _sanitize_for_script_literal(message)
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        f'$n.BalloonTipTitle = "{title}"; '
        f'$n.BalloonTipText = "{message}"; '
        "$n.Visible = $true; "
        "$n.ShowBalloonTip(5000); "
        "Start-Sleep -Seconds 5; "
        "$n.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, timeout=10)


def notify_desktop(title: str, message: str, urgency: str = "normal") -> bool:
    """urgency: 'low' | 'normal' | 'critical' -- only meaningful on Linux
    (notify-send), ignored on macOS/Windows, neither of which expose an
    equivalent through the mechanism used here. Returns False (never raises)
    if the platform's notifier is missing or fails, so a failed desktop
    notification never crashes the graph that triggered it -- Discord (if
    configured) and the notifications table (core/db.py) stay the guaranteed
    channels either way."""
    try:
        if sys.platform.startswith("linux"):
            _notify_linux(title, message, urgency)
        elif sys.platform == "darwin":
            _notify_macos(title, message)
        elif sys.platform == "win32":
            _notify_windows(title, message)
        else:
            return False
        return True
    except Exception as e:
        print(f"[notify_desktop] failed: {e}")
        return False


def _persist_notification(kind: str, title: str, message: str, offer_ids: list[int] | None) -> None:
    """Best-effort: a notification that fails to persist must never block the
    actual send (desktop/Discord), which stays notify_all's real purpose."""
    try:
        from core.db import get_connection
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO notifications (kind, title, message, offer_ids) VALUES (?, ?, ?, ?)",
                (kind, title, message, json.dumps(offer_ids, ensure_ascii=False) if offer_ids else None),
            )
    except Exception as e:
        print(f"[notify_all] failed to persist: {e}")


def notify_all(
    title: str, message: str, urgency: str = "normal", embed=None,
    kind: str = "info", offer_ids: list[int] | None = None,
) -> None:
    """Single entry point for the scheduled graphs' proactive notifications:
    always desktop (plain text, an OS notification can't render an embed),
    plus Discord if the bot/daemon is connected -- as an embed (discord.Embed)
    if one's given, otherwise plain text as before.

    kind classes the notification ('offers', 'mail', 'digest', 'error', 'info'
    by default) and offer_ids lists the postings involved when known at the
    call site (e.g. this run's best postings) -- both are read back by
    dernieres_notifications (graphs/chat_agent.py) to give the agent context
    for a notification the user comments on without repeating an id."""
    notify_desktop(title, message, urgency=urgency)
    _persist_notification(kind, title, message, offer_ids)
    try:
        from tools.discord_bot import notify_channel, notify_channel_embed
        if embed is not None:
            notify_channel_embed(embed)
        else:
            notify_channel(f"**{title}**\n{message}")
    except Exception as e:
        print(f"[notify_all] Discord unavailable: {e}")


if __name__ == "__main__":
    ok = notify_desktop("hobot", "Desktop notification test -- if you see this, it works.")
    print("OK" if ok else "FAILED")
