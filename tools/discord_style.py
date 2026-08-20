"""Shared styling for the bot's Discord messages -- consistent colors between
slash commands (discord_bot.py) and the proactive notifications pushed from
the scheduled graphs (discovery_graph.py, daemon.py).

Deliberate choice: formatting goes through Discord embeds (title, fields,
color, clickable links) rather than emoji-decorated plain text -- more
legible, and it's literally what Discord provides these tools for.
"""
import discord

COLOR_DEFAULT = 0x3B82F6  # neutral blue -- general info
COLOR_ACTIVE = 0x22C55E   # green -- active / success
COLOR_PAUSED = 0xF59E0B   # amber -- paused / attention
COLOR_ERROR = 0xEF4444    # red -- failure / error


def base_embed(title: str, color: int = COLOR_DEFAULT, description: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, description=description)
    embed.set_footer(text="hobot")
    embed.timestamp = discord.utils.utcnow()
    return embed
