"""hobot's SQLite schema.

offers, company_contacts, emails, applications, run_log, user_profile,
memory_summaries, notifications. Every LangGraph node should read from here
with a targeted SELECT, never dump the whole table into the LLM's context.
"""
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DB_PATH = Path(__file__).resolve().parent.parent / os.environ.get("HOBOT_DB_PATH", "hobot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    url_hash        TEXT NOT NULL UNIQUE,
    dedup_key       TEXT,
    url             TEXT,
    title           TEXT,
    company         TEXT,
    location        TEXT,
    description     TEXT,
    salary          TEXT,
    posted_date     TEXT,
    score           INTEGER,
    score_reason    TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    origin          TEXT NOT NULL DEFAULT 'veille',
    company_domain  TEXT,
    first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
    link_checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status);
CREATE INDEX IF NOT EXISTS idx_offers_dedup_key ON offers(dedup_key);
CREATE INDEX IF NOT EXISTS idx_offers_score ON offers(score);

CREATE TABLE IF NOT EXISTS company_contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id        INTEGER NOT NULL REFERENCES offers(id),
    email           TEXT,
    nom             TEXT,
    poste           TEXT,
    type            TEXT,
    confiance       INTEGER,
    verifie         TEXT,
    source          TEXT NOT NULL,
    found_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_company_contacts_offer ON company_contacts(offer_id);

CREATE TABLE IF NOT EXISTS emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account         TEXT NOT NULL,
    uid             TEXT NOT NULL,
    message_id      TEXT,
    thread_id       TEXT,
    from_addr       TEXT,
    subject         TEXT,
    snippet         TEXT,
    category        TEXT,
    linked_offer_id INTEGER REFERENCES offers(id),
    status          TEXT NOT NULL DEFAULT 'unread',
    received_at     TEXT,
    UNIQUE(account, uid)
);
CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status);
CREATE INDEX IF NOT EXISTS idx_emails_thread ON emails(thread_id);

CREATE TABLE IF NOT EXISTS applications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id            INTEGER REFERENCES offers(id),
    thread_id           TEXT,
    cover_letter_path   TEXT,
    cv_path             TEXT,
    drafted_at          TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at             TEXT,
    status              TEXT NOT NULL DEFAULT 'draft'
);

CREATE TABLE IF NOT EXISTS run_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type        TEXT NOT NULL,
    source          TEXT NOT NULL,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    n_found         INTEGER,
    n_new           INTEGER,
    errors          TEXT,
    backoff_until   TEXT,
    query           TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_log_source ON run_log(source);

CREATE TABLE IF NOT EXISTS api_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    called_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_api_calls_source ON api_calls(source);

CREATE TABLE IF NOT EXISTS user_profile (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    full_name       TEXT,
    raw_text        TEXT,
    skills          TEXT,
    experience      TEXT,
    education       TEXT,
    target_roles    TEXT,
    target_locations TEXT,
    cv_source_path  TEXT,
    cv_format       TEXT,
    cv_uploaded_at  TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT NOT NULL,
    summary_text    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- What notify_all() (tools/notify_tools.py) actually sent, separate from
-- run_log (which logs a run's execution, not the TEXT shown to the user).
-- The chat agent (graphs/chat_agent.py, /ask) never sees a proactive
-- notification any other way -- it's posted straight to the Discord channel,
-- outside its per-thread conversation memory -- so without this, a reply to
-- one ("that posting", "that email from earlier") had nothing to anchor to
-- and drifted onto something else. Read by dernieres_notifications.
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    message     TEXT NOT NULL,
    offer_ids   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at);

-- Single-row kill switch, read by daemon.py before every scheduled job and
-- written by /pause /resume (Discord) and the terminal UI's own pause
-- toggle. DB-backed rather than a plain module global (core/daemon_state.py)
-- because the terminal UI runs as its own separate process -- it has no
-- access to the daemon's in-memory state, only what's in this database.
-- heartbeat_at/discord_status are the same idea applied to "is the daemon
-- process itself even running": daemon.py writes both every 30s (see its
-- _heartbeat job), so a heartbeat_at older than that (or NULL) means the
-- daemon isn't running right now, not just that it hasn't done anything
-- lately -- core.queries.get_daemon_liveness() is the one place that turns
-- this into a yes/no the terminal UI's Status pane shows.
CREATE TABLE IF NOT EXISTS daemon_flags (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    paused          INTEGER NOT NULL DEFAULT 0,
    heartbeat_at    TEXT,
    discord_status  TEXT
);

-- Companies tools/sources_ats.py checks for Greenhouse/Ashby/Lever openings
-- -- see core/ats_watchlist.py. Unlike every other source in this project,
-- these three ATS platforms have no keyword search, only a per-company feed,
-- so this table (not user_profile.target_roles) is what tells that connector
-- what to fetch.
CREATE TABLE IF NOT EXISTS ats_watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company     TEXT NOT NULL,
    platform    TEXT NOT NULL,
    slug        TEXT NOT NULL,
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(platform, slug)
);
"""


def parse_utc(iso_string: str) -> datetime:
    """Every naive timestamp this project stores (SQLite's datetime('now'),
    or a naive datetime.now(timezone.utc).isoformat()) is UTC, just without
    the offset -- fromisoformat() alone leaves it naive, which compares
    incorrectly against an aware datetime.now(timezone.utc) on a UTC+ host.
    Reached for a third time (core/queries.py's get_daemon_liveness,
    daemon.py's _first_run_at) after the same naive-vs-aware bug got fixed
    twice independently -- one shared helper instead of a fourth copy."""
    return datetime.fromisoformat(iso_string).replace(tzinfo=timezone.utc)


@contextmanager
def get_connection():
    """SQLite connection that actually closes when the `with` block exits.
    sqlite3.Connection's own context manager only commits/rolls back, it never
    calls close() (a classic stdlib gotcha). Without this wrapper, every one of
    the project's ~20 `with get_connection() as conn:` call sites leaked a
    connection, which matters for a daemon meant to run continuously (file
    descriptors pile up over time). Call sites don't change, only what happens
    on exit."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets a reader (e.g. a second process, once one exists) proceed while
    # a writer is mid-transaction instead of blocking on it; busy_timeout makes
    # a genuinely contested write retry for up to 10s instead of raising
    # "database is locked" on Python's stdlib 5s default almost immediately.
    # Both are cheap to reissue on every connection: WAL is a persistent file
    # property (re-setting it is a no-op check, not real work), busy_timeout is
    # per-connection so it does need to be set here every time.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_user_profile() -> dict | None:
    """Targeted single-row read: never the raw CV/text by default, just the
    structured fields the graphs actually need."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT full_name, skills, target_roles, target_locations FROM user_profile WHERE id = 1"
        ).fetchone()
    if not row:
        return None
    return {
        "full_name": row["full_name"],
        "skills": json.loads(row["skills"] or "[]"),
        "target_roles": json.loads(row["target_roles"] or "[]"),
        "target_locations": json.loads(row["target_locations"] or "[]"),
    }


def _add_column(conn: sqlite3.Connection, table: str, ddl: str) -> None:
    """ALTER TABLE ADD COLUMN, tolerant of a duplicate-column error: daemon.py
    and cli.py are separate processes that can both reach _add_missing_columns
    right after a git pull adds a new column -- the check-then-write above
    isn't itself locked, so both can see the column missing and both issue
    the ALTER TABLE. The loser used to crash on startup with
    'duplicate column name'; that specific error means the column is already
    there (harmlessly, by the other process), which is exactly the end state
    this function is trying to reach, so it's swallowed. Any other error
    still raises."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Must run BEFORE executescript(SCHEMA): on a database already created
    without `dedup_key`, the schema's CREATE INDEX ... ON offers(dedup_key)
    would fail on a missing column otherwise. SQLite has no ADD COLUMN IF NOT
    EXISTS."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "offers" not in tables:
        return  # first-ever creation -> the schema's own CREATE TABLE is enough
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(offers)")}
    if "dedup_key" not in cols:
        _add_column(conn, "offers", "dedup_key TEXT")
    if "origin" not in cols:
        # DEFAULT 'veille' applies directly to existing rows -- everything in
        # the table so far came from scheduled discovery, no separate backfill needed.
        _add_column(conn, "offers", "origin TEXT NOT NULL DEFAULT 'veille'")
    if "company_domain" not in cols:
        _add_column(conn, "offers", "company_domain TEXT")
    if "link_checked_at" not in cols:
        _add_column(conn, "offers", "link_checked_at TEXT")

    if "user_profile" in tables:
        profile_cols = {row["name"] for row in conn.execute("PRAGMA table_info(user_profile)")}
        if "full_name" not in profile_cols:
            _add_column(conn, "user_profile", "full_name TEXT")
        if "cv_source_path" not in profile_cols:
            _add_column(conn, "user_profile", "cv_source_path TEXT")
            _add_column(conn, "user_profile", "cv_format TEXT")
            _add_column(conn, "user_profile", "cv_uploaded_at TEXT")

    if "applications" in tables:
        app_cols = {row["name"] for row in conn.execute("PRAGMA table_info(applications)")}
        if "drafted_at" not in app_cols:
            # Backfilled to now rather than left NULL, specifically so the first
            # stale-draft check after this migration doesn't flag every
            # pre-existing draft as "pending for weeks" on day one.
            _add_column(conn, "applications", "drafted_at TEXT")
            conn.execute("UPDATE applications SET drafted_at = datetime('now') WHERE drafted_at IS NULL")
        if "cv_path" not in app_cols:
            _add_column(conn, "applications", "cv_path TEXT")

    if "run_log" in tables:
        run_log_cols = {row["name"] for row in conn.execute("PRAGMA table_info(run_log)")}
        if "query" not in run_log_cols:
            _add_column(conn, "run_log", "query TEXT")

    if "daemon_flags" in tables:
        flags_cols = {row["name"] for row in conn.execute("PRAGMA table_info(daemon_flags)")}
        if "heartbeat_at" not in flags_cols:
            _add_column(conn, "daemon_flags", "heartbeat_at TEXT")
        if "discord_status" not in flags_cols:
            _add_column(conn, "daemon_flags", "discord_status TEXT")
    conn.commit()


def _backfill_dedup_keys(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, title, company FROM offers WHERE dedup_key IS NULL").fetchall()
    if not rows:
        return
    from tools.common import normalize_text
    for row in rows:
        key = f"{normalize_text(row['company'])}|{normalize_text(row['title'])}"
        conn.execute("UPDATE offers SET dedup_key = ? WHERE id = ?", (key, row["id"]))
    conn.commit()


def _backfill_scored_status(conn: sqlite3.Connection) -> None:
    """Idempotent safety net: any row still at status='new' despite already
    having a score gets fixed to 'scored'. No-op once everything's consistent."""
    conn.execute("UPDATE offers SET status = 'scored' WHERE status = 'new' AND score IS NOT NULL")
    conn.commit()


def init_db() -> None:
    with get_connection() as conn:
        _add_missing_columns(conn)
        conn.executescript(SCHEMA)
        conn.commit()
        _backfill_dedup_keys(conn)
        _backfill_scored_status(conn)


if __name__ == "__main__":
    init_db()
    with get_connection() as conn:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
    print(f"DB initialized: {DB_PATH}")
    print(f"Tables: {', '.join(tables)}")
