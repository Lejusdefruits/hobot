"""Discord bot: read-only base commands (/status, /offers, /applied, /pause,
/resume) plus /ask, which wires in the conversational agent (chat_agent.py),
the only path that can send a real email, and only through the confirmation
button below (never the agent on its own).

Slash commands (app_commands) rather than prefix commands: no need to enable
the "message content" privileged intent on the Discord developer portal.
Synced to the target channel's guild specifically (not globally) so they're
available immediately instead of waiting up to an hour for Discord's global
propagation.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from core import daemon_state
from core.db import get_connection
from graphs.chat_agent import PENDING_SENDS
from graphs.chat_agent import ask as agent_ask
from graphs.chat_agent import axes_progression as axes_progression_tool
from tools import common, email_tools
from tools.discord_style import COLOR_ACTIVE, COLOR_DEFAULT, COLOR_ERROR, COLOR_PAUSED, base_embed

GMAIL_DAILY_SEND_CAP = int(os.environ.get("GMAIL_DAILY_SEND_CAP", "15"))

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


_synced = False  # on_ready can fire again after a reconnect -- no need to
                 # re-sync commands or send a message again each time.

DISCORD_MSG_LIMIT = 1900  # headroom under Discord's real limit (2000) for formatting


def chunk_message(text: str, limit: int = DISCORD_MSG_LIMIT) -> list[str]:
    """Splits a text into several Discord messages instead of truncating it,
    cutting at a newline near the limit (or a space, failing that) so a word
    or sentence never gets cut mid-way."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n ")
    if text:
        chunks.append(text)
    return chunks


def notify_channel(message: str) -> None:
    """Pushes a message to the Discord channel from any thread. The daemon's
    scheduler (daemon.py) runs on its own threads, not the bot's asyncio loop,
    hence the bridge through run_coroutine_threadsafe. Silent no-op if the bot
    isn't connected (e.g. a graph run standalone outside the daemon): the
    desktop notification stays the guaranteed channel in that case."""
    if not client.is_ready() or client.loop is None:
        return
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        return

    async def _send_all():
        for chunk in chunk_message(message):
            await channel.send(chunk)

    asyncio.run_coroutine_threadsafe(_send_all(), client.loop)


def notify_channel_embed(embed: discord.Embed) -> None:
    """Same as notify_channel, but for an embed (rich summaries: new postings,
    weekly digest) instead of a plain text message. Same thread->asyncio-loop
    bridge, same silent no-op if the bot isn't connected."""
    if not client.is_ready() or client.loop is None:
        return
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        return
    asyncio.run_coroutine_threadsafe(channel.send(embed=embed), client.loop)


@client.event
async def on_ready():
    global _synced
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print(f"[discord_bot] Connected as {client.user}, but channel {CHANNEL_ID} wasn't found "
              f"(is the bot actually a member of the server?)")
        return
    if not _synced:
        # Commands are registered as global (@tree.command with no guild=); they
        # need to be copied into the guild's bucket before syncing there, or
        # sync(guild=...) finds nothing to publish.
        tree.copy_global_to(guild=channel.guild)
        await tree.sync(guild=channel.guild)
        _synced = True
    print(f"[discord_bot] Connected as {client.user}, target channel #{channel.name} on {channel.guild.name}")


@tree.command(name="status", description="Summary of the last discovery/mail run")
async def status(interaction: discord.Interaction):
    with get_connection() as conn:
        last_discovery = conn.execute(
            "SELECT * FROM run_log WHERE run_type='discovery' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_email = conn.execute(
            "SELECT * FROM run_log WHERE run_type='email_watch' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        backlog = conn.execute("SELECT COUNT(*) c FROM offers WHERE score IS NULL").fetchone()["c"]
        best = conn.execute(
            "SELECT title, company, score FROM offers "
            "WHERE score IS NOT NULL AND status NOT IN ('applied', 'excluded', 'expired') ORDER BY score DESC LIMIT 1"
        ).fetchone()

    next_discovery = next_email = "unknown (daemon not running)"
    if daemon_state.scheduler is not None:
        job_d = daemon_state.scheduler.get_job("discovery")
        job_e = daemon_state.scheduler.get_job("email_watch")
        if job_d and job_d.next_run_time:
            next_discovery = job_d.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
        if job_e and job_e.next_run_time:
            next_email = job_e.next_run_time.strftime("%Y-%m-%d %H:%M:%S")

    embed = base_embed(
        "hobot status",
        color=COLOR_PAUSED if daemon_state.paused else COLOR_ACTIVE,
    )
    embed.add_field(name="State", value="Paused" if daemon_state.paused else "Active", inline=True)
    embed.add_field(name="Scoring queue", value=f"{backlog} unscored posting(s)", inline=True)

    if last_discovery:
        discovery_line = (f"Last: {last_discovery['finished_at']} "
                           f"({last_discovery['source']}, {last_discovery['n_new']} new)\n"
                           f"Next: {next_discovery}")
    else:
        discovery_line = f"Never run\nNext: {next_discovery}"
    embed.add_field(name="Discovery", value=discovery_line, inline=False)

    if last_email:
        mail_line = (f"Last: {last_email['finished_at']} ({last_email['n_new']} mail(s))\n"
                      f"Next: {next_email}")
    else:
        mail_line = f"Never run\nNext: {next_email}"
    embed.add_field(name="Mail watch", value=mail_line, inline=False)

    if best:
        embed.add_field(
            name="Best score in database",
            value=f"**{best['score']}** — {best['title']} ({best['company']})",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


class MarkAppliedButton(discord.ui.Button):
    """One button per posting listed in /offers -- skips retyping `/applied
    <number>` after already reading the number right above it. Same logic as
    the /applied command below, just triggered by a click."""

    def __init__(self, offer_id: int, title: str, company: str | None):
        super().__init__(label=f"#{offer_id} applied", style=discord.ButtonStyle.secondary)
        self.offer_id = offer_id
        self.title = title
        self.company = company

    async def callback(self, interaction: discord.Interaction) -> None:
        with get_connection() as conn:
            row = conn.execute("SELECT status FROM offers WHERE id = ?", (self.offer_id,)).fetchone()
            if not row:
                await interaction.response.send_message(f"No posting #{self.offer_id}.", ephemeral=True)
                return
            if row["status"] == "applied":
                await interaction.response.send_message(
                    f"#{self.offer_id} was already marked applied.", ephemeral=True,
                )
                return
            conn.execute("UPDATE offers SET status = 'applied' WHERE id = ?", (self.offer_id,))
            row_id = common.upsert_application(conn, self.offer_id, status="applied")
            conn.execute("UPDATE applications SET sent_at = datetime('now') WHERE id = ?", (row_id,))
            common.archive_cover_letter(conn, self.offer_id)
        if self.view is not None:
            _disable_offer_buttons(self.view, self.offer_id)
            await interaction.response.edit_message(view=self.view)
        else:
            await interaction.response.defer()
        await interaction.followup.send(
            f"**#{self.offer_id}** marked applied: {self.title} -- {self.company or '?'}", ephemeral=True,
        )


class ExcludeButton(discord.ui.Button):
    """Counterpart to MarkAppliedButton -- exclude directly from /offers or
    /offer without going through /ask, same logic as the /exclude command
    below."""

    def __init__(self, offer_id: int, title: str, company: str | None):
        super().__init__(label=f"#{offer_id} exclude", style=discord.ButtonStyle.danger)
        self.offer_id = offer_id
        self.title = title
        self.company = company

    async def callback(self, interaction: discord.Interaction) -> None:
        with get_connection() as conn:
            row = conn.execute("SELECT status FROM offers WHERE id = ?", (self.offer_id,)).fetchone()
            if not row:
                await interaction.response.send_message(f"No posting #{self.offer_id}.", ephemeral=True)
                return
            if row["status"] in ("applied", "excluded"):
                already = "applied" if row["status"] == "applied" else "excluded"
                await interaction.response.send_message(f"#{self.offer_id} is already {already}.", ephemeral=True)
                return
            conn.execute("UPDATE offers SET status = 'excluded' WHERE id = ?", (self.offer_id,))
        if self.view is not None:
            _disable_offer_buttons(self.view, self.offer_id)
            await interaction.response.edit_message(view=self.view)
        else:
            await interaction.response.defer()
        await interaction.followup.send(
            f"**#{self.offer_id}** excluded: {self.title} -- {self.company or '?'}", ephemeral=True,
        )


def _disable_offer_buttons(view: discord.ui.View, offer_id: int) -> None:
    """Disables both buttons (applied + exclude) for the same posting after an
    action on either -- once applied or excluded, the other action no longer
    makes sense."""
    for child in view.children:
        if getattr(child, "offer_id", None) == offer_id:
            child.disabled = True


class OffersView(discord.ui.View):
    def __init__(self, rows) -> None:
        super().__init__(timeout=3600)  # same duration as ConfirmSendView -- past that, just retype /offers
        for r in rows:
            self.add_item(MarkAppliedButton(r["id"], r["title"], r["company"]))
            self.add_item(ExcludeButton(r["id"], r["title"], r["company"]))


@tree.command(name="offers", description="Best-scored postings (already-applied excluded)")
async def offers(interaction: discord.Interaction):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT o.id, o.title, o.company, o.location, o.score, o.url,
                      (a.cover_letter_path IS NOT NULL) AS has_dossier
               FROM offers o LEFT JOIN applications a ON a.offer_id = o.id
               WHERE o.score IS NOT NULL AND o.status NOT IN ('applied', 'excluded', 'expired')
               ORDER BY o.score DESC LIMIT 8"""
        ).fetchall()

    if not rows:
        embed = base_embed("Postings", description="No scored posting yet.")
        await interaction.response.send_message(embed=embed)
        return

    embed = base_embed(
        f"Best postings ({len(rows)})",
        description="Already applied to one of these? Click its button below, or `/applied <number>`.",
    )
    for r in rows:
        name = f"#{r['id']} · {r['score']}/100 — {r['title']}"[:256]
        value = r["company"] or "?"
        if r["location"]:
            value += f" — {r['location']}"
        if r["has_dossier"]:
            value += "\nCV + letter already ready (`/files`)"
        if r["url"]:
            value += f"\n[View posting]({r['url']})"
        embed.add_field(name=name, value=value[:1024], inline=False)
    await interaction.response.send_message(embed=embed, view=OffersView(rows))


STATUS_LABELS = {
    "applied": "Applied", "excluded": "Excluded", "scored": "Scored",
    "new": "Not scored yet", "expired": "Expired (dead link)",
}


async def _send_offer_files(interaction: discord.Interaction, offer_id: int) -> None:
    """Sends the CV + cover letter PDFs for a posting as ephemeral
    attachments -- shared by /files and /offer's "View application" button.
    Reads both paths straight from `applications` (cv_path, cover_letter_path)
    rather than deriving one from the other's directory, since hobot stores
    each explicitly."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT a.cover_letter_path, a.cv_path, o.title, o.company FROM applications a "
            "JOIN offers o ON o.id = a.offer_id WHERE a.offer_id = ? AND a.cover_letter_path IS NOT NULL "
            "ORDER BY a.id DESC LIMIT 1",
            (offer_id,),
        ).fetchone()
    if not row:
        embed = base_embed(
            "No file yet", color=COLOR_ERROR,
            description=f"No CV/letter generated for #{offer_id} yet. Ask for one via `/ask` "
                        f"(e.g. \"prepare a CV and letter for posting {offer_id}\").",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    letter_path = Path(row["cover_letter_path"])
    if not letter_path.exists():
        embed = base_embed(
            "File missing", color=COLOR_ERROR,
            description=f"The letter for #{offer_id} is recorded in the database but {letter_path} is missing.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    files = [discord.File(letter_path)]
    if row["cv_path"] and Path(row["cv_path"]).exists():
        files.append(discord.File(row["cv_path"]))
    await interaction.response.send_message(
        content=f"Application files -- #{offer_id} {row['title']} ({row['company'] or '?'})",
        files=files, ephemeral=True,
    )


class ShowLetterButton(discord.ui.Button):
    """/offer's button to grab the already-generated CV + letter directly,
    without retyping the number into /files."""

    def __init__(self, offer_id: int):
        super().__init__(label="View application (CV + letter)", style=discord.ButtonStyle.secondary)
        self.offer_id = offer_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await _send_offer_files(interaction, self.offer_id)


class OffreView(discord.ui.View):
    def __init__(self, offer_id: int, title: str, company: str | None, status: str, has_dossier: bool) -> None:
        super().__init__(timeout=3600)
        if status not in ("applied", "excluded"):
            self.add_item(MarkAppliedButton(offer_id, title, company))
            self.add_item(ExcludeButton(offer_id, title, company))
        if has_dossier:
            self.add_item(ShowLetterButton(offer_id))


def _offer_choices(rows) -> list[app_commands.Choice[int]]:
    """Formats offer rows (id, title, company, optional score) into Discord
    Choice objects -- shared by the 3 autocompletes below."""
    choices = []
    for r in rows:
        score = r["score"] if "score" in r.keys() else None
        prefix = f"#{r['id']} -- {score}/100" if score is not None else f"#{r['id']}"
        label = f"{prefix} -- {r['title'] or '?'} -- {r['company'] or '?'}"
        choices.append(app_commands.Choice(name=label[:100], value=r["id"]))
    return choices


def _filter_offer_rows(rows, current: str, limit: int = 25) -> list:
    """Filters already-fetched offer rows against the typed text, in Python
    rather than SQL -- reuses common.normalize_text so an accented title
    ("Ingenieur" vs "INGÉNIEUR") still matches regardless of case/accents,
    the same normalization already trusted elsewhere for offer dedup. An
    exact numeric match on the id is sorted first, or typing "1" would bury
    offer #1 under every id that merely CONTAINS a 1 (#421, #419...)."""
    current = current.strip()
    if not current:
        return rows[:limit]
    needle = common.normalize_text(current)
    matches = [
        r for r in rows
        if needle in common.normalize_text(f"{r['id']} {r['title'] or ''} {r['company'] or ''}")
    ]
    if current.isdigit():
        exact_id = int(current)
        matches.sort(key=lambda r: 0 if r["id"] == exact_id else 1)
    return matches[:limit]


async def _offer_id_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    """Suggests "id -- score -- title -- company" while typing -- for
    /applied, limited to postings not already applied to/excluded/expired
    (same candidates as /offers). Loads every candidate row (no LIMIT, see
    _filter_offer_rows) then filters/caps in Python, or a posting outside
    some arbitrary pre-loaded batch would never come up no matter what's typed."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, title, company, score FROM offers WHERE status NOT IN ('applied', 'excluded', 'expired') "
            "ORDER BY score DESC"
        ).fetchall()
    return _offer_choices(_filter_offer_rows(rows, current))


async def _any_offer_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    """Same as _offer_id_autocomplete but with no status filter -- for /offer
    and /exclude, where looking up detail or lifting an exclusion on an
    already-applied/excluded posting is still useful."""
    with get_connection() as conn:
        rows = conn.execute("SELECT id, title, company, score FROM offers ORDER BY id DESC").fetchall()
    return _offer_choices(_filter_offer_rows(rows, current))


async def _dossier_offer_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    """Only postings that already have a CV + letter generated -- for /files."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT o.id, o.title, o.company, o.score FROM offers o
               JOIN applications a ON a.offer_id = o.id
               WHERE a.cover_letter_path IS NOT NULL ORDER BY o.id DESC"""
        ).fetchall()
    return _offer_choices(_filter_offer_rows(rows, current))


@tree.command(name="offer", description="Full detail on one posting (description, score, status, contacts)")
@app_commands.describe(offer_id="Posting number, shown with # in /offers")
@app_commands.autocomplete(offer_id=_any_offer_autocomplete)
async def offer_cmd(interaction: discord.Interaction, offer_id: int):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT title, company, location, description, url, score, score_reason, status, "
            "last_seen_at, source FROM offers WHERE id = ?", (offer_id,),
        ).fetchone()
        if not row:
            embed = base_embed("Posting not found", color=COLOR_ERROR, description=f"No posting #{offer_id}.")
            await interaction.response.send_message(embed=embed)
            return
        has_dossier = conn.execute(
            "SELECT 1 FROM applications WHERE offer_id = ? AND cover_letter_path IS NOT NULL", (offer_id,),
        ).fetchone() is not None

    if row["status"] == "applied":
        color = COLOR_ACTIVE
    elif row["status"] in ("excluded", "expired"):
        color = COLOR_ERROR
    else:
        color = COLOR_DEFAULT

    embed = base_embed(f"#{offer_id} — {row['title']}"[:256], color=color)
    embed.add_field(name="Company", value=row["company"] or "?", inline=True)
    embed.add_field(name="Location", value=row["location"] or "?", inline=True)
    embed.add_field(name="Type", value=common.offer_type_label(row["source"]), inline=True)
    embed.add_field(name="Status", value=STATUS_LABELS.get(row["status"], row["status"]), inline=True)
    if row["score"] is not None:
        embed.add_field(name="Score", value=f"{row['score']}/100", inline=True)
    embed.add_field(name="Last seen live", value=row["last_seen_at"] or "?", inline=True)
    if row["score_reason"]:
        embed.add_field(name="Reasoning", value=row["score_reason"][:1024], inline=False)
    if row["description"]:
        embed.add_field(name="Description", value=row["description"][:1024], inline=False)
    if row["url"]:
        embed.add_field(name="Listing", value=row["url"][:1024], inline=False)

    view = OffreView(offer_id, row["title"], row["company"], row["status"], has_dossier)
    await interaction.response.send_message(embed=embed, view=view)


@tree.command(name="files", description="Sends the already-generated CV + letter for a posting")
@app_commands.describe(offer_id="Posting number, shown with # in /offers")
@app_commands.autocomplete(offer_id=_dossier_offer_autocomplete)
async def files_cmd(interaction: discord.Interaction, offer_id: int):
    await _send_offer_files(interaction, offer_id)


@tree.command(name="funnel", description="Conversion funnel: found -> scored -> letter -> sent -> reply -> interview")
async def funnel(interaction: discord.Interaction):
    with get_connection() as conn:
        stats = common.funnel_stats(conn)
    stages = common.funnel_stages(stats)
    embed = base_embed("Conversion funnel", description=common.funnel_insight(stages))
    for s in stages:
        value = str(s["count"])
        if s["pct_previous"] is not None:
            value += f" ({s['pct_previous']}% of previous stage, {s['pct_total']}% of total)"
        embed.add_field(name=s["label"].capitalize(), value=value, inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name="breakdown", description="How many postings per score tier, and how many still waiting to be scored")
async def breakdown(interaction: discord.Interaction):
    with get_connection() as conn:
        unscored = conn.execute("SELECT COUNT(*) c FROM offers WHERE score IS NULL").fetchone()["c"]
        rows = conn.execute(
            "SELECT score FROM offers WHERE score IS NOT NULL AND status NOT IN ('applied', 'excluded', 'expired')"
        ).fetchall()
    scores = [r["score"] for r in rows]
    tiers = [
        ("80 and up", sum(1 for s in scores if s >= 80)),
        ("60-79", sum(1 for s in scores if 60 <= s < 80)),
        ("40-59", sum(1 for s in scores if 40 <= s < 60)),
        ("Under 40", sum(1 for s in scores if s < 40)),
    ]
    embed = base_embed("Score breakdown")
    for label, n in tiers:
        embed.add_field(name=label, value=str(n), inline=True)
    embed.add_field(name="Waiting to be scored", value=str(unscored), inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name="applications", description="Lists applications already sent or marked")
async def applications_cmd(interaction: discord.Interaction):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT a.status, a.sent_at, o.id, o.title, o.company FROM applications a
               JOIN offers o ON o.id = a.offer_id ORDER BY a.id DESC LIMIT 15"""
        ).fetchall()
    if not rows:
        embed = base_embed("Applications", description="No application recorded yet.")
        await interaction.response.send_message(embed=embed)
        return
    embed = base_embed(f"Applications ({len(rows)})", color=COLOR_ACTIVE)
    for r in rows:
        name = f"#{r['id']} -- {r['title']}"[:256]
        value = f"{r['company'] or '?'}\n{r['status']} -- {r['sent_at'] or 'unknown date'}"
        embed.add_field(name=name, value=value[:1024], inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name="sources", description="Status of each discovery source (last attempt, errors, backing off or not)")
async def sources_status(interaction: discord.Interaction):
    from core.circuit_breaker import is_backed_off
    from graphs.discovery_graph import ACTIVE_DISCOVERY_SOURCES

    placeholders = ",".join("?" * len(ACTIVE_DISCOVERY_SOURCES))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT r.source, r.finished_at, r.n_found, r.n_new, r.errors
               FROM run_log r
               JOIN (SELECT source, MAX(id) AS max_id FROM run_log
                     WHERE run_type = 'discovery' AND source IN ({placeholders}) GROUP BY source) m
                 ON r.source = m.source AND r.id = m.max_id
               ORDER BY r.source""",
            ACTIVE_DISCOVERY_SOURCES,
        ).fetchall()

    if not rows:
        embed = base_embed("Sources", description="No source has run yet.")
        await interaction.response.send_message(embed=embed)
        return

    embed = base_embed("Discovery source status")
    for r in rows:
        backed_off, until = is_backed_off(r["source"])
        if backed_off:
            value = f"**Backed off** until {until}\nLast attempt: {r['finished_at']}"
            if r["errors"]:
                value += f"\n{r['errors'][:200]}"
        elif r["errors"]:
            value = f"Last attempt failed ({r['finished_at']}):\n{r['errors'][:200]}"
        else:
            value = f"OK -- {r['finished_at']}\n{r['n_found']} found, {r['n_new']} new"
        embed.add_field(name=common.source_label(r["source"]), value=value[:1024], inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name="strategy", description="Search keyword currently in use for each discovery source")
async def strategy(interaction: discord.Interaction):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT r.source, r.query, r.n_found, r.finished_at
               FROM run_log r
               JOIN (SELECT source, MAX(id) AS max_id FROM run_log
                     WHERE query IS NOT NULL GROUP BY source) m
                 ON r.source = m.source AND r.id = m.max_id
               ORDER BY r.source"""
        ).fetchall()

    if not rows:
        embed = base_embed("Search strategy", description="No keyword recorded yet.")
        await interaction.response.send_message(embed=embed)
        return

    embed = base_embed(
        "Current search strategy",
        description="No per-run adaptation: each keyword comes straight from your target roles "
                     "and stays fixed until the profile changes.",
    )
    for r in rows:
        name = f"{common.source_label(r['source'])} -- \"{r['query']}\""[:256]
        value = f"{r['n_found']} result(s) last run ({r['finished_at']})"
        embed.add_field(name=name, value=value[:1024], inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name="log", description="Recent history of discovery runs (keywords searched, results found)")
@app_commands.describe(limit="How many runs to show (default 12, max 20)")
async def log_cmd(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 12):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT source, query, n_found, n_new, errors, finished_at
               FROM run_log WHERE run_type = 'discovery' ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    if not rows:
        embed = base_embed("Discovery log", description="No discovery run recorded yet.")
        await interaction.response.send_message(embed=embed)
        return

    embed = base_embed(f"Discovery log -- {len(rows)} most recent run(s)")
    for r in reversed(rows):  # oldest to newest, more natural to read
        name = f"{r['finished_at'] or '?'} -- {common.source_label(r['source'])}"
        if r["query"]:
            name += f" -- \"{r['query']}\""
        value = (f"{r['n_found'] if r['n_found'] is not None else '?'} result(s), "
                 f"{r['n_new'] if r['n_new'] is not None else '?'} new")
        if r["errors"]:
            value += f"\nError: {r['errors'][:200]}"
        embed.add_field(name=name[:256], value=value[:1024], inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name="notifications", description="History of notifications sent (postings, mail, digest, cleanup)")
@app_commands.describe(limit="How many notifications to show (default 10, max 20)")
async def notifications_cmd(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 10):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT kind, title, message, offer_ids, created_at FROM notifications ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    if not rows:
        embed = base_embed("Notifications", description="No notification sent yet.")
        await interaction.response.send_message(embed=embed)
        return

    embed = base_embed(f"Notifications -- {len(rows)} most recent")
    for r in reversed(rows):  # oldest to newest, more natural to read
        name = f"{r['created_at']} -- ({r['kind']}) {r['title']}"[:256]
        value = r["message"][:900]
        offer_ids = json.loads(r["offer_ids"]) if r["offer_ids"] else []
        if offer_ids:
            value += "\nPostings: " + ", ".join(f"#{i}" for i in offer_ids)
        embed.add_field(name=name, value=value[:1024], inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name="quotas", description="This month's usage for quota-limited APIs (Adzuna, Hunter.io, Snov.io)")
async def quotas(interaction: discord.Interaction):
    from core.api_usage import quota_summary

    labels = {"adzuna": "Adzuna", "hunter": "Hunter.io", "snov": "Snov.io"}
    embed = base_embed("This month's API quotas")
    for q in quota_summary():
        label = labels.get(q["source"], q["source"])
        exhausted = " (exhausted)" if q["remaining"] <= 0 else ""
        embed.add_field(
            name=label,
            value=f"{q['used']}/{q['limit']} used -- {q['remaining']} remaining{exhausted}",
            inline=True,
        )
    await interaction.response.send_message(embed=embed)


def _disable_draft_buttons(view: discord.ui.View, uid: str) -> None:
    """Disables both buttons (send + delete) of the same draft after an
    action on either -- same logic as _disable_offer_buttons above."""
    for child in view.children:
        if getattr(child, "uid", None) == uid:
            child.disabled = True


class SendDraftButton(discord.ui.Button):
    """/drafts' "Send" button -- sends the draft as-is (email_tools.
    envoyer_brouillon_existant re-reads its FULL content, the embed's preview
    is truncated to 200 characters) then deletes it from the Drafts folder.
    Respects the same daily anti-ban cap as ConfirmSendView above -- otherwise
    it could be used to bypass that cap by resending from an already-written
    draft over and over."""

    def __init__(self, uid: str):
        super().__init__(label="Send", style=discord.ButtonStyle.success)
        self.uid = uid

    async def callback(self, interaction: discord.Interaction) -> None:
        with get_connection() as conn:
            sent_today = conn.execute(
                "SELECT COUNT(*) c FROM run_log WHERE run_type='email_send' AND date(started_at) = date('now')"
            ).fetchone()["c"]
        if sent_today >= GMAIL_DAILY_SEND_CAP:
            await interaction.response.send_message(
                f"Daily cap of {GMAIL_DAILY_SEND_CAP} sends reached -- the draft stays pending, try again tomorrow.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: email_tools.envoyer_brouillon_existant(self.uid)
        )
        failed = result.startswith("Error") or result.startswith("No draft")
        if not failed:
            with get_connection() as conn:
                conn.execute(
                    "INSERT INTO run_log (run_type, source, started_at, finished_at, n_found, n_new) "
                    "VALUES ('email_send', ?, datetime('now'), datetime('now'), 1, 1)",
                    (email_tools.SEND_ACCOUNT,),
                )
            _disable_draft_buttons(self.view, self.uid)
            await interaction.message.edit(view=self.view)
        await interaction.followup.send(result, ephemeral=True)


class DeleteDraftButton(discord.ui.Button):
    """/drafts' "Delete" button -- irreversible on the IMAP side, same logic
    as email_tools.supprimer_brouillon (also used by supprimer_brouillon_mail,
    chat_agent.py)."""

    def __init__(self, uid: str):
        super().__init__(label="Delete", style=discord.ButtonStyle.danger)
        self.uid = uid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: email_tools.supprimer_brouillon(self.uid)
        )
        if not result.startswith("Error"):
            _disable_draft_buttons(self.view, self.uid)
            await interaction.message.edit(view=self.view)
        await interaction.followup.send(result, ephemeral=True)


MAX_DRAFT_BUTTONS = 10  # 2 buttons/draft -- stays under Discord's 25-component limit


class DraftsView(discord.ui.View):
    def __init__(self, drafts: list[dict]) -> None:
        super().__init__(timeout=3600)
        for d in drafts[:MAX_DRAFT_BUTTONS]:
            self.add_item(SendDraftButton(d["uid"]))
            self.add_item(DeleteDraftButton(d["uid"]))


@tree.command(name="drafts", description="Pending reply drafts on the sending account (Gmail)")
async def drafts_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        drafts = await asyncio.get_event_loop().run_in_executor(None, email_tools.lister_brouillons)
    except Exception as e:
        embed = base_embed("Error", color=COLOR_ERROR, description=f"Couldn't read the drafts: {e}")
        await interaction.followup.send(embed=embed)
        return

    if not drafts:
        embed = base_embed("Pending drafts", description="No pending draft.")
        await interaction.followup.send(embed=embed)
        return

    description = None
    if len(drafts) > MAX_DRAFT_BUTTONS:
        description = f"{len(drafts) - MAX_DRAFT_BUTTONS} more not shown here (Discord's button limit)."
    embed = base_embed(f"Pending drafts ({len(drafts)})", color=COLOR_PAUSED, description=description)
    for d in drafts[:MAX_DRAFT_BUTTONS]:
        destinataire = d["destinataire"]
        if isinstance(destinataire, (list, tuple)):
            destinataire = ", ".join(destinataire)
        embed.add_field(
            name=(d["sujet"] or "(no subject)")[:256],
            value=f"To {destinataire} -- {d['date']}\n{d['apercu']}"[:1024],
            inline=False,
        )
    await interaction.followup.send(embed=embed, view=DraftsView(drafts))


@tree.command(name="applied", description="Marks a posting as already applied to (drops it from /offers)")
@app_commands.describe(offer_id="Posting number, shown with # in /offers")
@app_commands.autocomplete(offer_id=_offer_id_autocomplete)
async def applied(interaction: discord.Interaction, offer_id: int):
    with get_connection() as conn:
        row = conn.execute("SELECT title, company, status FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if not row:
            embed = base_embed("Posting not found", color=COLOR_ERROR, description=f"No posting #{offer_id}.")
            await interaction.response.send_message(embed=embed)
            return
        if row["status"] == "applied":
            embed = base_embed(
                "Already applied", color=COLOR_PAUSED,
                description=f"#{offer_id} ({row['title']}) was already marked applied.",
            )
            await interaction.response.send_message(embed=embed)
            return
        conn.execute("UPDATE offers SET status = 'applied' WHERE id = ?", (offer_id,))
        row_id = common.upsert_application(conn, offer_id, status="applied")
        conn.execute("UPDATE applications SET sent_at = datetime('now') WHERE id = ?", (row_id,))
        common.archive_cover_letter(conn, offer_id)
    embed = base_embed(
        "Application recorded", color=COLOR_ACTIVE,
        description=f"**#{offer_id}** dropped from the lists: {row['title']} — {row['company']}",
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="exclude", description="Manually excludes a posting (no longer appears in /offers, /status, searches)")
@app_commands.describe(offer_id="Posting number", reason="Optional note, just for the confirmation message (not stored)")
@app_commands.autocomplete(offer_id=_any_offer_autocomplete)
async def exclude(interaction: discord.Interaction, offer_id: int, reason: str = ""):
    with get_connection() as conn:
        row = conn.execute("SELECT title, company, status FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if not row:
            embed = base_embed("Posting not found", color=COLOR_ERROR, description=f"No posting #{offer_id}.")
            await interaction.response.send_message(embed=embed)
            return
        if row["status"] == "applied":
            embed = base_embed(
                "Already applied", color=COLOR_PAUSED,
                description=f"#{offer_id} is already marked applied -- undo that first (via `/ask`) if you want to exclude it.",
            )
            await interaction.response.send_message(embed=embed)
            return
        if row["status"] == "excluded":
            embed = base_embed(
                "Already excluded", color=COLOR_PAUSED, description=f"#{offer_id} ({row['title']}) is already excluded.",
            )
            await interaction.response.send_message(embed=embed)
            return
        conn.execute("UPDATE offers SET status = 'excluded' WHERE id = ?", (offer_id,))
    detail = f" ({reason})" if reason else ""
    embed = base_embed(
        "Posting excluded", color=COLOR_ERROR,
        description=f"**#{offer_id}** excluded: {row['title']} — {row['company']}{detail}",
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="pause", description="Stops scheduled checks (postings + mail) until /resume")
async def pause(interaction: discord.Interaction):
    daemon_state.paused = True
    embed = base_embed(
        "Monitoring paused", color=COLOR_PAUSED,
        description="Scheduled checks (postings + mail) are suspended. `/resume` to restart them.",
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="resume", description="Restarts scheduled checks")
async def resume(interaction: discord.Interaction):
    daemon_state.paused = False
    embed = base_embed(
        "Monitoring restarted", color=COLOR_ACTIVE,
        description="Scheduled checks are back to normal.",
    )
    await interaction.response.send_message(embed=embed)


def _wipe_database() -> None:
    """Deletes every row, children before the offers/applications they
    reference (company_contacts.offer_id etc. are declared REFERENCES, and
    PRAGMA foreign_keys = ON in get_connection() means deleting a parent
    first is rejected) -- then resets the autoincrement counters, so a fresh
    posting starts back at #1 instead of continuing from wherever it left
    off. Row-by-row DELETE rather than dropping/recreating tables so the
    daemon's already-open connections keep working with no restart needed."""
    with get_connection() as conn:
        for table in ("company_contacts", "applications", "emails", "offers",
                      "run_log", "api_calls", "notifications", "memory_summaries", "user_profile"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")


def _wipe_files() -> None:
    """Clears outputs/ (generated CVs + letters) and profile_source/ (the
    uploaded CV) -- everything /reset's database wipe leaves no working
    pointer to anyway, so leaving the files behind would just be orphaned
    disk usage no command can reach again."""
    import shutil

    from core.profile import PROFILE_DIR
    from tools.documents import OUTPUT_DIR

    for base in (OUTPUT_DIR, PROFILE_DIR):
        if not base.exists():
            continue
        for child in base.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()


class ConfirmResetView(discord.ui.View):
    """Same danger-button-requires-a-click pattern as ConfirmSendView above --
    /reset is the most destructive command in the bot, it doesn't get to run
    off a single slash command with no second step."""

    def __init__(self) -> None:
        super().__init__(timeout=60)

    @discord.ui.button(label="Confirm reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        _wipe_database()
        _wipe_files()
        embed = base_embed(
            "Reset complete", color=COLOR_ACTIVE,
            description="Every posting, application, contact, notification, and the uploaded CV are gone. "
                        "Run `/profile` again to set one up.",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="reset", description="Wipes everything: postings, applications, contacts, uploaded CV -- start clean")
async def reset_cmd(interaction: discord.Interaction) -> None:
    embed = base_embed(
        "Reset everything?", color=COLOR_ERROR,
        description="This deletes every posting, application, contact, notification, and the uploaded CV. "
                    "Cannot be undone.",
    )
    await interaction.response.send_message(embed=embed, view=ConfirmResetView(), ephemeral=True)


class ConfirmSendView(discord.ui.View):
    """The only path to a real SMTP send: a human click, never the agent alone."""

    def __init__(self, pending_id: str):
        super().__init__(timeout=3600)
        self.pending_id = pending_id

    @discord.ui.button(label="Confirm send", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        payload = PENDING_SENDS.pop(self.pending_id, None)
        if not payload:
            embed = base_embed(
                "Proposal expired", color=COLOR_ERROR,
                description="This proposal is no longer valid (already sent or expired).",
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        with get_connection() as conn:
            sent_today = conn.execute(
                "SELECT COUNT(*) c FROM run_log WHERE run_type='email_send' AND date(started_at) = date('now')"
            ).fetchone()["c"]

        if sent_today >= GMAIL_DAILY_SEND_CAP:
            # Anti-ban cap reached -> draft instead of a flat refusal.
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: email_tools.creer_brouillon(
                    email_compte=email_tools.SEND_ACCOUNT, destinataire=payload["destinataire"],
                    sujet=payload["sujet"], contenu=payload["contenu"],
                )
            )
            # email_tools always prefixes its errors with "Error" (a convention
            # of that module, the same check rechercher_emails/chat_agent.py
            # use) -- checked here so a failed draft never gets reported as a
            # success.
            failed = result.startswith("Error")
            title = "Draft failed" if failed else "Daily cap reached — saved as draft"
            color = COLOR_ERROR if failed else COLOR_PAUSED
            description = result if failed else f"Daily cap of {GMAIL_DAILY_SEND_CAP} sends reached. {result}"
        else:
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: email_tools.envoyer_ou_repondre_email(
                    email_compte=email_tools.SEND_ACCOUNT, destinataire=payload["destinataire"],
                    sujet=payload["sujet"], contenu=payload["contenu"],
                )
            )
            failed = result.startswith("Error")
            # Only sends that actually went out count against the daily cap --
            # a silent SMTP failure (wrong password, network outage...) must
            # never eat into the anti-ban cap or get reported as a misleading
            # success.
            if not failed:
                with get_connection() as conn:
                    conn.execute(
                        "INSERT INTO run_log (run_type, source, started_at, finished_at, n_found, n_new) "
                        "VALUES ('email_send', ?, datetime('now'), datetime('now'), 1, 1)",
                        (email_tools.SEND_ACCOUNT,),
                    )
            title = "Send failed" if failed else "Email sent"
            color = COLOR_ERROR if failed else COLOR_ACTIVE
            description = result

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=base_embed(title, color=color, description=description))


AGENT_TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "180"))


@tree.command(name="profile", description="Set your profile from a CV file (PDF or .docx)")
@app_commands.describe(file="Your CV, as a PDF or .docx file")
async def profile_cmd(interaction: discord.Interaction, file: discord.Attachment):
    from core import profile as profile_mod

    await interaction.response.defer()
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".pdf", ".docx"):
        await interaction.followup.send(embed=base_embed(
            "Unsupported file", color=COLOR_ERROR,
            description="Only PDF and .docx are supported for now.",
        ))
        return

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        await file.save(Path(tmp.name))
        tmp_path = Path(tmp.name)

    try:
        fmt = await asyncio.get_event_loop().run_in_executor(None, profile_mod.detect_format, tmp_path)
        parsed = await asyncio.get_event_loop().run_in_executor(
            None, lambda: profile_mod.parse_cv(tmp_path, fmt=fmt)
        )
        await asyncio.get_event_loop().run_in_executor(None, profile_mod.save_profile, parsed)
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: profile_mod.save_profile_source(tmp_path, fmt)
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    embed = base_embed("Profile saved", color=COLOR_ACTIVE)
    embed.add_field(name="Name", value=parsed.get("full_name") or "(not detected)", inline=True)
    embed.add_field(name="Skills", value=", ".join(parsed.get("skills", [])) or "(none)", inline=False)
    embed.add_field(name="Target roles", value=", ".join(parsed.get("target_roles", [])) or "(none)", inline=True)
    embed.add_field(name="Target locations", value=", ".join(parsed.get("target_locations", [])) or "(none)",
                     inline=True)
    await interaction.followup.send(embed=embed)

    # Gaps are computed deterministically (no LLM call, see
    # core/profile.py::detect_gaps) -- the model's only job below is to
    # phrase already-identified gaps as natural questions, not to notice
    # them on its own from a bare "I uploaded a CV" message, which is the
    # kind of implicit reasoning this project already treats small local
    # models as unreliable at (see the README's model recommendation).
    gaps = profile_mod.detect_gaps(parsed)
    if gaps:
        prompt = (
            "I just uploaded my CV. Here's what looks thin or missing: "
            f"{', '.join(gaps)}. Ask me 2-4 natural follow-up questions to fill these in, "
            "referencing what's already in my profile where relevant."
        )
        try:
            reply = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, agent_ask, prompt, str(interaction.user.id)),
                timeout=AGENT_TIMEOUT_SECONDS,
            )
            for chunk in chunk_message(reply or ""):
                await interaction.followup.send(chunk)
        except asyncio.TimeoutError:
            pass


@tree.command(name="ask", description="Ask the agent for something (look up a posting, draft a reply, a letter...)")
@app_commands.describe(text="Your request, in plain language")
async def ask(interaction: discord.Interaction, text: str):
    await interaction.response.defer()  # the agent (local LLM) can take a few seconds
    before = set(PENDING_SENDS.keys())
    try:
        reponse = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, agent_ask, text, str(interaction.user.id)),
            timeout=AGENT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        embed = base_embed(
            "Timed out", color=COLOR_ERROR,
            description=(
                f"That took longer than {AGENT_TIMEOUT_SECONDS}s, giving up. Try again with a simpler "
                f"or more specific request (e.g. one account instead of all of them)."
            ),
        )
        await interaction.followup.send(embed=embed)
        return
    new = set(PENDING_SENDS.keys()) - before
    view = ConfirmSendView(next(iter(new))) if new else None
    chunks = chunk_message(reponse or "(empty reply)")
    for i, chunk in enumerate(chunks):
        kwargs = {"view": view} if (view is not None and i == len(chunks) - 1) else {}
        await interaction.followup.send(chunk, **kwargs)


@tree.command(name="gaps", description="AI analysis of the gaps that show up most often in poorly-scored postings")
async def gaps(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        resultat = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, lambda: axes_progression_tool.invoke({})),
            timeout=AGENT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        embed = base_embed(
            "Timed out", color=COLOR_ERROR,
            description=f"The analysis took longer than {AGENT_TIMEOUT_SECONDS}s, try again later.",
        )
        await interaction.followup.send(embed=embed)
        return
    embed = base_embed("Gaps to work on", description=resultat[:4000])
    await interaction.followup.send(embed=embed)


@tree.command(name="digest", description="Triggers the weekly digest right now (summary of the week)")
async def digest(interaction: discord.Interaction):
    if daemon_state.run_weekly_digest_fn is None:
        embed = base_embed(
            "Daemon not active", color=COLOR_ERROR,
            description="The weekly digest is handled by the scheduled daemon (daemon.py), which isn't active here.",
        )
        await interaction.response.send_message(embed=embed)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, daemon_state.run_weekly_digest_fn),
            timeout=AGENT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        embed = base_embed(
            "Timed out", color=COLOR_ERROR,
            description=f"The digest took longer than {AGENT_TIMEOUT_SECONDS}s, try again later.",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    embed = base_embed(
        "Digest triggered", color=COLOR_ACTIVE,
        description="The weekly summary was just computed -- it shows up in this channel if there's "
                    "anything to report this week.",
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


if __name__ == "__main__":
    client.run(TOKEN)
