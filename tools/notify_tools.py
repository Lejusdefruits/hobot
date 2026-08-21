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


def notify_desktop(title: str, message: str, urgency: str = "normal") -> bool:
    """urgency: 'low' | 'normal' | 'critical'. Returns False if notify-send is
    missing or fails, instead of crashing the graph over a failed notification."""
    try:
        subprocess.run(
            ["notify-send", "--app-name=hobot", f"--urgency={urgency}", title, message],
            check=True, timeout=5,
        )
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
