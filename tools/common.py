"""Shared helpers used by the job-source connectors and by cover letter
management (marquer_postule / annuler_postule / /applied -- see
archive_cover_letter below)."""
import hashlib
import re
import unicodedata
from pathlib import Path
from sqlite3 import Connection


def normalize_text(s) -> str:
    """Lowercase, no accents, no punctuation, whitespace collapsed -- the basis
    for the cross-source dedup key (simple: exact match on normalized text, no
    fuzzy matching). Any input that isn't already a string (None, or a pandas
    NaN for a missing JobSpy field -- a float, not None) becomes an empty
    string instead of crashing the caller."""
    if not isinstance(s, str):
        s = ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return " ".join(s.split())


def _clean_str(v):
    """None stays None, a string stays a string, everything else (a pandas NaN
    for a missing JobSpy field is a float, not None) becomes None instead of
    being stored as-is and breaking some display/processing step later on."""
    return v if v is None or isinstance(v, str) else None


def make_offer(*, source, external_id, title, company, location, description, url,
                salary=None, posted_date=None) -> dict:
    """Normalizes a source's result into the `offers` table schema."""
    title, company, location, description, url = (
        _clean_str(title), _clean_str(company), _clean_str(location),
        _clean_str(description), _clean_str(url),
    )
    key = external_id or url or f"{title}:{company}"
    return {
        "source": source,
        "url_hash": hashlib.sha256(f"{source}:{key}".encode()).hexdigest(),
        # Cross-source dedup: same company + same title (normalized) = same offer.
        "dedup_key": f"{normalize_text(company)}|{normalize_text(title)}",
        "url": url,
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "salary": salary,
        "posted_date": posted_date,
    }


def _archive_column(conn: Connection, offer_id: int, column: str) -> None:
    rows = conn.execute(
        f"SELECT id, {column} FROM applications WHERE offer_id = ? AND {column} IS NOT NULL",
        (offer_id,),
    ).fetchall()
    for row in rows:
        src = Path(row[column])
        if not src.exists() or src.parent.name == "archive":
            continue
        archive_dir = src.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / src.name
        src.rename(dest)
        conn.execute(f"UPDATE applications SET {column} = ? WHERE id = ?", (str(dest), row["id"]))


def _unarchive_column(conn: Connection, offer_id: int, column: str) -> None:
    rows = conn.execute(
        f"SELECT id, {column} FROM applications WHERE offer_id = ? AND {column} IS NOT NULL",
        (offer_id,),
    ).fetchall()
    for row in rows:
        src = Path(row[column])
        if not src.exists() or src.parent.name != "archive":
            continue
        dest = src.parent.parent / src.name
        src.rename(dest)
        conn.execute(f"UPDATE applications SET {column} = ? WHERE id = ?", (str(dest), row["id"]))


def upsert_application(conn: Connection, offer_id: int, defaults: dict | None = None, **fields) -> int:
    """Every offer gets at most one row here -- a cover letter saved, a CV
    tailored, and an applied mark are all updates to the SAME row, not
    separate events. Before this, each write path blindly INSERTed its own
    row instead of checking for one already there for this offer_id
    (confirmed live: a letter saved after a CV had already been tailored for
    the same offer left the CV path stranded on an orphaned row /files could
    never find, and /applications listed the same offer twice). `defaults`
    only apply when the row is created for the first time (e.g.
    status='draft'); an update that omits a column leaves its current value
    alone rather than resetting it. Returns the row id either way."""
    existing = conn.execute(
        "SELECT id FROM applications WHERE offer_id = ? ORDER BY id DESC LIMIT 1", (offer_id,),
    ).fetchone()
    if existing:
        conn.execute(
            f"UPDATE applications SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
            (*fields.values(), existing["id"]),
        )
        return existing["id"]
    all_fields = {**(defaults or {}), **fields}
    cur = conn.execute(
        f"INSERT INTO applications (offer_id, {', '.join(all_fields)}) VALUES (?, {', '.join('?' for _ in all_fields)})",
        (offer_id, *all_fields.values()),
    )
    return cur.lastrowid


def archive_cover_letter(conn: Connection, offer_id: int) -> None:
    """Moves an offer's cover letter and tailored CV (if any) into
    outputs/{offer_id}/archive/ once it's marked applied -- outputs/ then
    only holds files for offers still active, nothing is lost, just moved
    (and the path in the database follows). `conn` must be a connection
    already opened by the caller (same transaction as the status update)."""
    _archive_column(conn, offer_id, "cover_letter_path")
    _archive_column(conn, offer_id, "cv_path")


def unarchive_cover_letter(conn: Connection, offer_id: int) -> None:
    """Reverse of archive_cover_letter: puts the letter and tailored CV back
    in outputs/ when an 'applied' mark gets undone -- keeps the folder
    consistent with the actual status."""
    _unarchive_column(conn, offer_id, "cover_letter_path")
    _unarchive_column(conn, offer_id, "cv_path")


SPONTANEOUS_LEAD_SOURCES = ("lba_recruiter", "labonneboite")

# Readable labels for the `source` column -- used by /sources, /log, and
# strategie_recherche so a raw technical key (e.g. "jobspy") never leaks into
# a user-facing reply.
SOURCE_LABELS = {
    "lba": "La Bonne Alternance",
    "lba_recruiter": "La Bonne Alternance (spontaneous-application lead)",
    "adzuna": "Adzuna",
    "jobspy": "JobSpy (Indeed)",
    "francetravail": "France Travail",
    "labonneboite": "La Bonne Boite (spontaneous-application lead)",
}


def source_label(source: str | None) -> str:
    return SOURCE_LABELS.get(source or "", source or "?")


def offer_type_label(source: str | None) -> str:
    """Distinguishes a real published listing from a spontaneous-application
    lead (a company flagged as a potential employer, with no actual listing
    behind it -- see tools/sources_lba.py and tools/sources_labonneboite.py)
    -- so it's never left to guesswork from a free-text title."""
    return "Spontaneous-application lead (no listing published)" if source in SPONTANEOUS_LEAD_SOURCES else "Published listing"


FUNNEL_STAGES = ("found", "scored", "letter drafted", "sent", "reply received", "interview")


def funnel_stats(conn: Connection) -> dict:
    """One query per stage of the application funnel: found -> scored ->
    letter drafted -> sent -> reply received -> interview. Counts across the
    whole database (not just currently-active offers): the funnel answers
    "where is the process leaking", not "what's actionable right now"."""
    found = conn.execute("SELECT COUNT(*) c FROM offers").fetchone()["c"]
    scored = conn.execute("SELECT COUNT(*) c FROM offers WHERE score IS NOT NULL").fetchone()["c"]
    lettered = conn.execute(
        "SELECT COUNT(DISTINCT offer_id) c FROM applications WHERE cover_letter_path IS NOT NULL"
    ).fetchone()["c"]
    sent = conn.execute("SELECT COUNT(*) c FROM offers WHERE status = 'applied'").fetchone()["c"]
    replied = conn.execute(
        "SELECT COUNT(DISTINCT linked_offer_id) c FROM emails "
        "WHERE linked_offer_id IS NOT NULL AND category IN ('recruteur', 'entretien', 'refus')"
    ).fetchone()["c"]
    interview = conn.execute(
        "SELECT COUNT(DISTINCT linked_offer_id) c FROM emails "
        "WHERE linked_offer_id IS NOT NULL AND category = 'entretien'"
    ).fetchone()["c"]
    return {
        "found": found, "scored": scored, "letter drafted": lettered,
        "sent": sent, "reply received": replied, "interview": interview,
    }


def funnel_stages(stats: dict) -> list[dict]:
    """Turns the raw counts into displayable rows: count, % of the total
    found, % of the previous stage (this last figure is often the more
    telling one -- e.g. 1% of found offers end up with a letter, but 67% of
    drafted letters do get sent, two very different signals that % of total
    alone would hide)."""
    total = stats.get("found") or 0
    rows = []
    previous = None
    for label in FUNNEL_STAGES:
        count = stats.get(label, 0)
        pct_total = round(100 * count / total, 1) if total else 0.0
        pct_previous = round(100 * count / previous, 1) if previous else None
        rows.append({"label": label, "count": count, "pct_total": pct_total, "pct_previous": pct_previous})
        previous = count
    return rows


def funnel_insight(stages: list[dict]) -> str:
    """A deterministic sentence (no LLM) pointing at the biggest proportional
    drop between two consecutive stages -- skips stages with 0 offers at the
    start (nothing to compute) and segments with fewer than 3 offers at the
    start (too little data for a percentage to mean anything, e.g. 1 -> 0 =
    -100% without demonstrating anything)."""
    worst = None
    for prev, cur in zip(stages, stages[1:]):
        if prev["count"] < 3 or cur["pct_previous"] is None:
            continue
        drop = 100 - cur["pct_previous"]
        if worst is None or drop > worst[0]:
            worst = (drop, prev["label"], cur["label"], cur["pct_previous"])
    if worst is None:
        return "Not enough data yet to identify a significant drop-off."
    drop, prev_label, cur_label, pct = worst
    return f"Biggest proportional drop: '{prev_label}' -> '{cur_label}' ({pct}% conversion)."
