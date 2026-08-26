"""Every test gets its own throwaway SQLite file instead of hobot.db --
core.db.DB_PATH is a plain module global read fresh on every get_connection()
call, so monkeypatching it here is enough to redirect every module in the
codebase that imports get_connection from core.db (same function object,
its __globals__ still point at core.db)."""
from datetime import datetime

import pytest

from core import db as db_module


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()
    return db_module


def insert_offer(conn, **overrides):
    """Minimal valid offers row for tests, any column overridable (including
    last_seen_at/link_checked_at, both otherwise DB-defaulted to now/NULL --
    explicit here so link_check tests can backdate a row to look stale)."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    fields = {
        "source": "jobspy_indeed",
        "url_hash": "hash-1",
        "dedup_key": "acme|backend engineer",
        "url": "https://example.com/job/1",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Paris",
        "description": "A" * 250,
        "salary": None,
        "posted_date": None,
        "score": None,
        "status": "new",
        "last_seen_at": now,
        "link_checked_at": None,
    }
    fields.update(overrides)
    cur = conn.execute(
        """INSERT INTO offers (source, url_hash, dedup_key, url, title, company, location,
                                description, salary, posted_date, score, status,
                                last_seen_at, link_checked_at)
           VALUES (:source, :url_hash, :dedup_key, :url, :title, :company, :location,
                   :description, :salary, :posted_date, :score, :status,
                   :last_seen_at, :link_checked_at)""",
        fields,
    )
    return cur.lastrowid
