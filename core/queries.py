"""Read (and a couple of destructive) queries shared by every interface --
Discord today, the web dashboard once it exists. Plain functions, no
`discord`/web-framework import: this is what makes "one source of truth, both
interfaces call the same function" actually true rather than aspirational.
The SQL here was moved verbatim out of tools/discord_bot.py's command bodies
(each one used to be inline in an `async def`, with no separate function
another interface could call), not rewritten -- discord_bot.py now calls
these and keeps only its embed-formatting code.
"""
from core.db import get_connection


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
    with get_connection() as conn:
        return conn.execute(
            """SELECT o.id, o.title, o.company, o.location, o.score, o.url,
                      (a.cover_letter_path IS NOT NULL) AS has_dossier
               FROM offers o LEFT JOIN applications a ON a.offer_id = o.id
               WHERE o.score IS NOT NULL AND o.status NOT IN ('applied', 'excluded', 'expired')
               ORDER BY o.score DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def get_offer_detail(offer_id: int) -> tuple:
    """Returns (row, has_dossier) -- row is None if the posting doesn't exist."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT title, company, location, description, url, score, score_reason, status, "
            "last_seen_at, source FROM offers WHERE id = ?", (offer_id,),
        ).fetchone()
        if not row:
            return None, False
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


def get_sources_status() -> list:
    """Raw rows only -- backoff state (core.circuit_breaker.is_backed_off)
    is computed per-row by the caller, same for every interface."""
    from graphs.discovery_graph import ACTIVE_DISCOVERY_SOURCES

    placeholders = ",".join("?" * len(ACTIVE_DISCOVERY_SOURCES))
    with get_connection() as conn:
        return conn.execute(
            f"""SELECT r.source, r.finished_at, r.n_found, r.n_new, r.errors
               FROM run_log r
               JOIN (SELECT source, MAX(id) AS max_id FROM run_log
                     WHERE run_type = 'discovery' AND source IN ({placeholders}) GROUP BY source) m
                 ON r.source = m.source AND r.id = m.max_id
               ORDER BY r.source""",
            ACTIVE_DISCOVERY_SOURCES,
        ).fetchall()


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
                      "run_log", "api_calls", "notifications", "memory_summaries", "user_profile"):
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
