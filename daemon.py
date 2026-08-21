"""daemon.py -- the single daemon process: offer discovery + optional email
watching + a weekly digest, all scheduled, plus the Discord bot.

One process: APScheduler (BackgroundScheduler, its own threads) for the
scheduled runs + the Discord client in the main asyncio loop. Both run
independently in the same process -- the scheduler never blocks the Discord
event loop, so /status, /offers, /ask stay responsive while a discovery
or email-watch run works in the background.

Email watching is only scheduled if at least one GMAIL_ACCOUNT_i is set in
.env (see start() below) -- without that, a Discord+discovery-only deployment
starts cleanly and never touches IMAP.

Kill switch: /pause and /resume (discord_bot.py) toggle core.daemon_state.paused,
checked at the start of every scheduled job.
"""
import logging
import os
import random
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from core import daemon_state
from core.db import get_connection, init_db
from graphs import discovery_graph, email_graph
from tools.discord_style import COLOR_DEFAULT, COLOR_ERROR, base_embed
from tools.notify_tools import notify_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("daemon")

DISCOVERY_HOURS_WEEKDAY = os.environ.get("DISCOVERY_HOURS_WEEKDAY", "9,11,13,15,17")
DISCOVERY_HOURS_WEEKEND = os.environ.get("DISCOVERY_HOURS_WEEKEND", "10,16")
DISCOVERY_JITTER_MIN = int(os.environ.get("JOBSPY_JITTER_MIN", "90"))
EMAIL_INTERVAL_MIN = int(os.environ.get("EMAIL_POLL_INTERVAL_MIN", "20"))
EMAIL_JITTER_MIN = int(os.environ.get("EMAIL_POLL_JITTER_MIN", "5"))
WEEKLY_DIGEST_DAY = os.environ.get("WEEKLY_DIGEST_DAY", "mon")
WEEKLY_DIGEST_HOUR = int(os.environ.get("WEEKLY_DIGEST_HOUR", "9"))
STALE_DRAFT_DAYS = int(os.environ.get("STALE_DRAFT_DAYS", "5"))

scheduler = BackgroundScheduler()


def _run_discovery() -> None:
    if daemon_state.paused:
        log.info("discovery skipped (paused)")
        return
    log.info("=== run discovery (JobSpy) ===")
    try:
        app = discovery_graph.build_graph()
        app.invoke({"raw_offers": [], "stats": [], "new_offers": [], "n_new_by_source": {}, "scored_offers": [], "drafted_letters": [], "skipped_irrelevant": 0, "skipped_cross_source": 0})
    except Exception:
        log.error("discovery failed:\n%s", traceback.format_exc())
        notify_all(
            "hobot -- error", "Offer discovery failed, check the daemon logs.",
            embed=base_embed("Error -- offer discovery", color=COLOR_ERROR,
                              description="Check the daemon logs for details."),
            kind="error",
        )
        return

    # Best-effort, independent of discovery's own success/failure above -- a
    # link check should never fail the scheduled run.
    try:
        from tools.link_check import sweep_dead_links
        expired = sweep_dead_links()
        if expired:
            log.info("%d offer(s) marked expired (dead link): %s",
                      len(expired), ", ".join(f"#{o['id']}" for o in expired))
            notify_all(
                f"hobot -- {len(expired)} offer(s) expired",
                "\n".join(f"#{o['id']} {o['title']} -- {o['company']}" for o in expired),
                embed=base_embed(
                    f"{len(expired)} offer(s) removed (dead link)", color=COLOR_DEFAULT,
                    description="\n".join(f"**#{o['id']}** {o['title']} -- {o['company']}" for o in expired)[:2048],
                ),
                kind="cleanup", offer_ids=[o["id"] for o in expired],
            )
    except Exception:
        log.error("link check failed:\n%s", traceback.format_exc())


def _run_email_watch() -> None:
    if daemon_state.paused:
        log.info("email watch skipped (paused)")
        return
    log.info("=== run email_watch ===")
    try:
        app = email_graph.build_graph()
        app.invoke({"raw_emails": [], "classified": []})
    except Exception:
        log.error("email_watch failed:\n%s", traceback.format_exc())
        notify_all(
            "hobot -- error", "Email watching failed, check the daemon logs.",
            embed=base_embed("Error -- email watch", color=COLOR_ERROR,
                              description="Check the daemon logs for details."),
            kind="error",
        )


def _run_weekly_digest() -> None:
    """Weekly summary -- not subject to the /pause kill switch: this isn't a
    call to an external source (so no anti-ban concern), just a local
    database read + a Discord message, and the user will probably still want
    to receive it even with active discovery paused for a while.

    Raw string date comparison (not datetime.fromisoformat): offers.first_seen_at
    and applications.sent_at are in SQLite's datetime('now') format
    ("YYYY-MM-DD HH:MM:SS"), emails.received_at has a "+00:00" suffix (comes
    from imap_tools) -- lexicographic `>=` comparison stays correct in both
    cases for a 7-day window (only a second-exact tie right at the boundary
    would be ambiguous, no impact here)."""
    log.info("=== weekly digest ===")
    try:
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        with get_connection() as conn:
            n_new = conn.execute(
                "SELECT COUNT(*) c FROM offers WHERE first_seen_at >= ?", (since,)
            ).fetchone()["c"]
            by_source = conn.execute(
                "SELECT source, COUNT(*) c FROM offers WHERE first_seen_at >= ? GROUP BY source ORDER BY c DESC",
                (since,),
            ).fetchall()
            top = conn.execute(
                "SELECT title, company, score FROM offers WHERE first_seen_at >= ? AND score IS NOT NULL "
                "AND status NOT IN ('applied', 'excluded', 'expired') ORDER BY score DESC LIMIT 5",
                (since,),
            ).fetchall()
            n_applied = conn.execute(
                "SELECT COUNT(*) c FROM applications WHERE status='applied' AND sent_at >= ?", (since,)
            ).fetchone()["c"]
            n_letters_pending = conn.execute(
                "SELECT COUNT(*) c FROM applications WHERE status='draft' AND cover_letter_path IS NOT NULL"
            ).fetchone()["c"]
            replies = conn.execute(
                "SELECT category, COUNT(*) c FROM emails WHERE received_at >= ? "
                "AND category IN ('recruteur', 'entretien', 'refus') GROUP BY category",
                (since,),
            ).fetchall()
            stale_since = (datetime.now() - timedelta(days=STALE_DRAFT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            stale_drafts = conn.execute(
                """SELECT o.id, o.title, o.company, a.drafted_at FROM applications a
                   JOIN offers o ON o.id = a.offer_id
                   WHERE a.status = 'draft' AND a.cover_letter_path IS NOT NULL AND a.drafted_at <= ?
                   ORDER BY a.drafted_at ASC LIMIT 10""",
                (stale_since,),
            ).fetchall()

        if n_new == 0 and n_applied == 0 and not replies and not stale_drafts:
            log.info("weekly digest: nothing to report this week, no notification")
            return

        lines = [f"This week's summary -- {n_new} new offer(s) found"]
        if by_source:
            lines.append("By source: " + ", ".join(f"{r['source']}: {r['c']}" for r in by_source))
        if top:
            lines.append("Best offers this week:")
            lines += [f"[{r['score']}] {r['title']} -- {r['company']}" for r in top]
        lines.append(f"Applications sent this week: {n_applied}")
        lines.append(f"Letter drafts pending (current total, not just this week): {n_letters_pending}")
        if replies:
            lines.append("Replies received this week: " + ", ".join(f"{r['category']}: {r['c']}" for r in replies))
        if stale_drafts:
            lines.append(f"Drafts pending for more than {STALE_DRAFT_DAYS} days:")
            lines += [f"#{r['id']} {r['title']} -- {r['company']} (since {r['drafted_at']})" for r in stale_drafts]

        embed = base_embed(f"{n_new} new offer(s) this week", color=COLOR_DEFAULT)
        if by_source:
            embed.add_field(
                name="New offers by source",
                value="\n".join(f"**{r['source']}** -- {r['c']}" for r in by_source),
                inline=False,
            )
        if top:
            embed.add_field(
                name="Best offers this week",
                value="\n".join(f"**{r['score']}** -- {r['title']} -- {r['company']}" for r in top)[:1024],
                inline=False,
            )
        embed.add_field(name="Applications sent", value=str(n_applied), inline=True)
        embed.add_field(name="Drafts pending (total)", value=str(n_letters_pending), inline=True)
        if replies:
            embed.add_field(
                name="Replies received",
                value="\n".join(f"**{r['category']}** -- {r['c']}" for r in replies),
                inline=False,
            )
        if stale_drafts:
            embed.add_field(
                name=f"Drafts pending for more than {STALE_DRAFT_DAYS} days",
                value="\n".join(f"**#{r['id']}** {r['title']} -- {r['company']}" for r in stale_drafts)[:1024],
                inline=False,
            )
        notify_all("hobot -- weekly digest", "\n".join(lines), embed=embed, kind="digest")
    except Exception:
        log.error("weekly digest failed:\n%s", traceback.format_exc())
        notify_all(
            "hobot -- error", "The weekly digest failed, check the daemon logs.",
            embed=base_embed("Error -- weekly digest", color=COLOR_ERROR,
                              description="Check the daemon logs for details."),
            kind="error",
        )


def _first_run_at(run_type: str, interval_minutes: int, max_initial_delay_minutes: int) -> datetime:
    """The first run after a process (re)start (manual restart, a crash
    restarted by systemd, or a reboot) should respect the cadence already
    under way, not start from scratch every time -- otherwise a plain reboot
    would trigger an almost-immediate search even if the last one was 10
    minutes ago, which both breaks the anti-ban pacing AND wastes calls for
    nothing.

    run_log survives restarts (unlike the scheduler, which resets from
    scratch in memory) -- used here to recover the real due time."""
    with get_connection() as conn:
        last = conn.execute(
            "SELECT finished_at FROM run_log WHERE run_type = ? ORDER BY id DESC LIMIT 1", (run_type,)
        ).fetchone()

    now = datetime.now()
    if last and last["finished_at"]:
        due = datetime.fromisoformat(last["finished_at"]) + timedelta(minutes=interval_minutes)
        if due > now:
            # small jitter around the normal due time, not a real cadence recalculation
            return due + timedelta(seconds=random.uniform(-60, 300))
    # never run before, or the due time already passed while the machine was
    # off/the process was stopped -> just a small startup delay, not a
    # pointless wait
    return now + timedelta(seconds=random.uniform(30, max_initial_delay_minutes * 60))


def start() -> None:
    init_db()

    # Cron rather than a blind interval: new postings cluster on weekday
    # mornings/early afternoons far more than nights or weekends, so firing
    # near those windows catches a fresh posting sooner than an interval that
    # lands at 3am exactly as often as at 11am on a Tuesday. Two CronTriggers
    # (weekday vs weekend) combined into ONE job via OrTrigger, not two
    # separate jobs -- /status and statut_veille look the job up by a fixed
    # id ("discovery"), which a single OrTrigger job keeps satisfying without
    # any change on their end.
    discovery_trigger = OrTrigger([
        CronTrigger(day_of_week="mon-fri", hour=DISCOVERY_HOURS_WEEKDAY, jitter=DISCOVERY_JITTER_MIN * 60),
        CronTrigger(day_of_week="sat,sun", hour=DISCOVERY_HOURS_WEEKEND, jitter=DISCOVERY_JITTER_MIN * 60),
    ])
    scheduler.add_job(
        _run_discovery, discovery_trigger,
        id="discovery", max_instances=1, coalesce=True,
    )

    # Email watching is entirely optional: no GMAIL_ACCOUNT_i configured -> the
    # job doesn't even get registered, instead of scheduling something that
    # would fail on every run (email_tools.CREDENTIALS is filled when the
    # module loads from .env, see tools/email_tools.py).
    from tools import email_tools
    if email_tools.CREDENTIALS:
        email_first_run = _first_run_at("email_watch", EMAIL_INTERVAL_MIN, min(EMAIL_INTERVAL_MIN, 5))
        log.info("First email run scheduled: %s", email_first_run)
        scheduler.add_job(
            _run_email_watch, "interval",
            minutes=EMAIL_INTERVAL_MIN, jitter=EMAIL_JITTER_MIN * 60,
            next_run_time=email_first_run,
            id="email_watch", max_instances=1, coalesce=True,
        )
    else:
        log.info("No Gmail account configured (GMAIL_ACCOUNT_1 missing from .env) -> email watching disabled.")

    scheduler.add_job(
        _run_weekly_digest, "cron",
        day_of_week=WEEKLY_DIGEST_DAY, hour=WEEKLY_DIGEST_HOUR,
        id="weekly_digest", max_instances=1, coalesce=True,
    )
    scheduler.start()
    daemon_state.scheduler = scheduler  # published for /status and the agent's statut_veille tool
    daemon_state.run_weekly_digest_fn = _run_weekly_digest  # published for /digest
    log.info(
        "Scheduler started: discovery on weekdays at %sh (+/-%dmin), weekends at %sh, weekly digest %s %dh",
        DISCOVERY_HOURS_WEEKDAY, DISCOVERY_JITTER_MIN, DISCOVERY_HOURS_WEEKEND, WEEKLY_DIGEST_DAY, WEEKLY_DIGEST_HOUR,
    )

    from tools.discord_bot import TOKEN, client
    client.run(TOKEN)  # blocking, the main asyncio loop


if __name__ == "__main__":
    start()
