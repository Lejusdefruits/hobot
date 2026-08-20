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


@tree.command(name="offers", description="Best-scored postings (already-applied excluded)")
async def offers(interaction: discord.Interaction):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, title, company, location, score, url FROM offers "
            "WHERE score IS NOT NULL AND status NOT IN ('applied', 'excluded', 'expired') ORDER BY score DESC LIMIT 8"
        ).fetchall()

    if not rows:
        embed = base_embed("Postings", description="No scored posting yet.")
        await interaction.response.send_message(embed=embed)
        return

    embed = base_embed(
        f"Best postings ({len(rows)})",
        description="Already applied to one of these? `/applied <number>` to drop it from the list.",
    )
    for r in rows:
        name = f"#{r['id']} · {r['score']}/100 — {r['title']}"[:256]
        value = r["company"] or "?"
        if r["location"]:
            value += f" — {r['location']}"
        if r["url"]:
            value += f"\n[View posting]({r['url']})"
        embed.add_field(name=name, value=value[:1024], inline=False)
    await interaction.response.send_message(embed=embed)


STATUS_LABELS = {
    "applied": "Applied", "excluded": "Excluded", "scored": "Scored",
    "new": "Not scored yet", "expired": "Expired (dead link)",
}


@tree.command(name="offer", description="Full detail on one posting (description, score, status)")
@app_commands.describe(offer_id="Posting number, shown with # in /offers")
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
    await interaction.response.send_message(embed=embed)


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


@tree.command(name="applied", description="Marks a posting as already applied to (drops it from /offers)")
@app_commands.describe(offer_id="Posting number, shown with # in /offers")
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
        conn.execute(
            "INSERT INTO applications (offer_id, status, sent_at) VALUES (?, 'applied', datetime('now'))",
            (offer_id,),
        )
        common.archive_cover_letter(conn, offer_id)
    embed = base_embed(
        "Application recorded", color=COLOR_ACTIVE,
        description=f"**#{offer_id}** dropped from the lists: {row['title']} — {row['company']}",
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


if __name__ == "__main__":
    client.run(TOKEN)
