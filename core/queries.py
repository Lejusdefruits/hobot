"""Read (and a couple of destructive) queries shared by every interface --
Discord and the terminal UI (cli.py). Plain functions, no
`discord`/UI-framework import: this is what makes "one source of truth, both
interfaces call the same function" actually true rather than aspirational.
The SQL here was moved verbatim out of tools/discord_bot.py's command bodies
(each one used to be inline in an `async def`, with no separate function
another interface could call), not rewritten -- discord_bot.py now calls
these and keeps only its embed-formatting code.
"""
import os
from datetime import datetime, timedelta, timezone

from core.db import get_connection, parse_utc

DISCOVERY_HOURS_WEEKDAY = os.environ.get("DISCOVERY_HOURS_WEEKDAY", "9,11,13,15,17")
DISCOVERY_HOURS_WEEKEND = os.environ.get("DISCOVERY_HOURS_WEEKEND", "10,16")
EMAIL_INTERVAL_MIN = os.environ.get("EMAIL_POLL_INTERVAL_MIN", "20")


def next_discovery_run() -> str | None:
    """Discovery's schedule is an absolute cron window
    (DISCOVERY_HOURS_WEEKDAY/WEEKEND) -- apscheduler's own CronTrigger can
    compute the next matching time from that definition alone, no live
    scheduler needed (daemon.py builds the exact same trigger). Deliberately
    NOT core.daemon_state.scheduler.get_job(...).next_run_time: that object
    only exists inside daemon.py's own process (see that module's
    docstring), so reading it from the terminal UI or from graphs/
    chat_agent.py's statut_veille tool (used by both Discord, which does run
    inside the daemon process, and the terminal UI's Chat pane, which
    doesn't) silently always returned "not running" from the one place it
    was actually wrong -- confirmed live: the daemon was up the whole time,
    only the terminal UI's chat was ever misreporting it."""
    from apscheduler.triggers.combining import OrTrigger
    from apscheduler.triggers.cron import CronTrigger

    trigger = OrTrigger([
        CronTrigger(day_of_week="mon-fri", hour=DISCOVERY_HOURS_WEEKDAY),
        CronTrigger(day_of_week="sat,sun", hour=DISCOVERY_HOURS_WEEKEND),
    ])
    next_time = trigger.get_next_fire_time(None, datetime.now().astimezone())
    return next_time.strftime("%a %d %b, %H:%M") if next_time else None


def next_email_check(last_finished_at: str | None) -> str:
    """Email's schedule is a plain interval from the last actual run instead
    (EMAIL_POLL_INTERVAL_MIN after run_log's last email_watch row) -- that DB
    row is the authority here, not a live scheduler's own next_run_time,
    same reasoning as next_discovery_run() above."""
    if not last_finished_at:
        return "shortly after the daemon starts"
    try:
        # SQLite's datetime('now') is UTC and naive -- localize explicitly
        # before adding the interval, or the displayed time is off by the
        # local UTC offset.
        last_utc = parse_utc(last_finished_at)
    except ValueError:
        return "unknown"
    next_local = (last_utc + timedelta(minutes=int(EMAIL_INTERVAL_MIN))).astimezone()
    return next_local.strftime("%H:%M")


def get_status_summary() -> dict:
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
    return {"last_discovery": last_discovery, "last_email": last_email, "backlog": backlog, "best": best}


def list_offers(limit: int = 8) -> list:
    """description and first_seen_at are pulled here (not just id/title/...)
    so every caller can run tools.ghost_job.check_ghost_job() on each row
    without a second query per posting."""
    with get_connection() as conn:
        return conn.execute(
            """SELECT o.id, o.title, o.company, o.location, o.score, o.url,
                      o.description, o.first_seen_at,
                      (a.cover_letter_path IS NOT NULL) AS has_dossier
               FROM offers o LEFT JOIN applications a ON a.offer_id = o.id
               WHERE o.score IS NOT NULL AND o.status NOT IN ('applied', 'excluded', 'expired')
               ORDER BY o.score DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def list_unscored_offers(limit: int = 25) -> list:
    """Offers still waiting on score_node (graphs/discovery_graph.py) --
    same oldest-first order that queue is actually scored in
    (SCORE_QUEUE_QUERY), so this doubles as a preview of what a scoring run
    would pick up next."""
    with get_connection() as conn:
        return conn.execute(
            """SELECT id, title, company, location, source, first_seen_at FROM offers
               WHERE score IS NULL AND status NOT IN ('applied', 'excluded', 'expired')
               ORDER BY first_seen_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()


def get_offer_row(offer_id: int):
    """Just the offers row, None if it doesn't exist -- split out of
    get_offer_detail() below so a caller that doesn't need has_dossier (e.g.
    tui/modals.py, which runs its own broader applications query right after)
    isn't stuck paying for that query's result only to discard it."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT title, company, location, description, url, score, score_reason, status, "
            "last_seen_at, first_seen_at, source FROM offers WHERE id = ?", (offer_id,),
        ).fetchone()


def get_offer_detail(offer_id: int) -> tuple:
    """Returns (row, has_dossier) -- row is None if the posting doesn't exist."""
    row = get_offer_row(offer_id)
    if not row:
        return None, False
    with get_connection() as conn:
        has_dossier = conn.execute(
            "SELECT 1 FROM applications WHERE offer_id = ? AND cover_letter_path IS NOT NULL", (offer_id,),
        ).fetchone() is not None
    return row, has_dossier


def get_breakdown() -> dict:
    """Score-tier bucketing: shared business logic (what counts as "80 and
    up"), not Discord-specific formatting, so it lives here rather than in
    the command handler."""
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
    return {"unscored": unscored, "tiers": tiers}


def list_applications(limit: int = 15) -> list:
    with get_connection() as conn:
        return conn.execute(
            """SELECT a.status, a.sent_at, o.id, o.title, o.company FROM applications a
               JOIN offers o ON o.id = a.offer_id ORDER BY a.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def get_sources_status() -> list[dict]:
    """One row per ACTIVE_DISCOVERY_SOURCES entry, always -- a source that
    has never run (not configured yet, or configured but discovery hasn't
    reached it) used to be silently absent from this list entirely (the old
    query only returned sources that already had a run_log row), which
    looked identical to "this source doesn't exist" instead of "nothing to
    report yet." `configured` (is_source_configured) is what actually
    answers "is this one even set up" -- backoff state
    (core.circuit_breaker.is_backed_off) is computed per-row by the caller,
    same for every interface."""
    from graphs.discovery_graph import ACTIVE_DISCOVERY_SOURCES, is_source_configured

    placeholders = ",".join("?" * len(ACTIVE_DISCOVERY_SOURCES))
    with get_connection() as conn:
        last_runs = {
            r["source"]: r for r in conn.execute(
                f"""SELECT r.source, r.finished_at, r.n_found, r.n_new, r.errors
                   FROM run_log r
                   JOIN (SELECT source, MAX(id) AS max_id FROM run_log
                         WHERE run_type = 'discovery' AND source IN ({placeholders}) GROUP BY source) m
                     ON r.source = m.source AND r.id = m.max_id""",
                ACTIVE_DISCOVERY_SOURCES,
            ).fetchall()
        }
    return [
        {
            "source": source,
            "configured": is_source_configured(source),
            "finished_at": last_runs[source]["finished_at"] if source in last_runs else None,
            "n_found": last_runs[source]["n_found"] if source in last_runs else 0,
            "n_new": last_runs[source]["n_new"] if source in last_runs else 0,
            "errors": last_runs[source]["errors"] if source in last_runs else None,
        }
        for source in ACTIVE_DISCOVERY_SOURCES
    ]


def get_daemon_liveness(stale_after_seconds: int = 90) -> dict:
    """Is the daemon process actually running right now, and is its Discord
    bot connected -- based on daemon.py's own heartbeat (daemon_flags,
    written every 30s, see daemon.py::HEARTBEAT_INTERVAL_SECONDS), not just
    "did it do something recently." stale_after_seconds (90 = 3 heartbeat
    intervals, a generous margin for scheduler jitter) is how old a
    heartbeat can be before it's treated as stale; missing entirely also
    means not running. The terminal UI is its own separate process with no
    other way to know this."""
    with get_connection() as conn:
        row = conn.execute("SELECT heartbeat_at, discord_status FROM daemon_flags WHERE id = 1").fetchone()
    if not row or not row["heartbeat_at"]:
        return {"running": False, "heartbeat_at": None, "discord_status": None}

    from datetime import datetime, timedelta, timezone
    heartbeat_utc = parse_utc(row["heartbeat_at"])
    running = datetime.now(timezone.utc) - heartbeat_utc <= timedelta(seconds=stale_after_seconds)
    return {
        "running": running,
        "heartbeat_at": row["heartbeat_at"],
        "discord_status": row["discord_status"] if running else None,
    }


def get_strategy() -> list:
    with get_connection() as conn:
        return conn.execute(
            """SELECT r.source, r.query, r.n_found, r.finished_at
               FROM run_log r
               JOIN (SELECT source, MAX(id) AS max_id FROM run_log
                     WHERE query IS NOT NULL GROUP BY source) m
                 ON r.source = m.source AND r.id = m.max_id
               ORDER BY r.source"""
        ).fetchall()


def get_notifications(limit: int = 10) -> list:
    with get_connection() as conn:
        return conn.execute(
            "SELECT kind, title, message, offer_ids, created_at FROM notifications ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_run_log(limit: int = 12) -> list:
    with get_connection() as conn:
        return conn.execute(
            """SELECT source, query, n_found, n_new, errors, finished_at
               FROM run_log WHERE run_type = 'discovery' ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def wipe_database() -> None:
    """Deletes every row, children before the offers/applications they
    reference (company_contacts.offer_id etc. are declared REFERENCES, and
    PRAGMA foreign_keys = ON in get_connection() means deleting a parent
    first is rejected) -- then resets the autoincrement counters, so a fresh
    posting starts back at #1 instead of continuing from wherever it left
    off. Row-by-row DELETE rather than dropping/recreating tables so the
    daemon's already-open connections keep working with no restart needed."""
    with get_connection() as conn:
        for table in ("company_contacts", "applications", "emails", "offers",
                      "run_log", "api_calls", "notifications", "memory_summaries", "user_profile",
                      "ats_watchlist"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")


def wipe_files() -> None:
    """Clears outputs/ (generated CVs + letters) and profile_source/ (the
    uploaded CV) -- everything the database wipe leaves no working pointer to
    anyway, so leaving the files behind would just be orphaned disk usage no
    command can reach again."""
    import shutil

    from core.profile import PROFILE_DIR
    from tools.documents import OUTPUT_DIR

    for base in (OUTPUT_DIR, PROFILE_DIR):
        if not base.exists():
            continue
        for child in base.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
