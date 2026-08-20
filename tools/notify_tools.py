"""Proactive notifications -- desktop (notify-send) and Discord together via
notify_all(), so you find out even away from the machine instead of having to
type /status to see what happened during a scheduled run.
"""
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


def notify_all(title: str, message: str, urgency: str = "normal", embed=None) -> None:
    """Single entry point for the scheduled graphs' proactive notifications:
    always desktop (plain text, an OS notification can't render an embed),
    plus Discord if the bot/daemon is connected -- as an embed (discord.Embed)
    if one's given, otherwise plain text as before."""
    notify_desktop(title, message, urgency=urgency)
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
