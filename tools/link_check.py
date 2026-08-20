"""Checks that offers still active in the database still point at a live URL.

Deliberately conservative, same anti-ban posture as core/circuit_breaker.py:
one plain HTTP status check per offer, never a content scrape, only a handful
of offers per run, spaced out with a small random pause. Only an unambiguous
404/410 marks an offer dead -- a timeout, network error, or 403 (often an
anti-bot block on a perfectly live posting, not proof it's gone) changes
nothing rather than risk a false positive.

The trigger is "not re-confirmed in a while" (offers.last_seen_at, updated by
dedupe_node every time a scheduled or on-demand search lands on the same
offer again -- see graphs/discovery_graph.py), not "old since first found". An
offer that keeps resurfacing from the source doesn't need an HTTP check: its
reappearance already proves it's still live. Only an offer that's gone quiet
triggers an actual link check. A check that confirms the link alive also
refreshes last_seen_at -- the check itself counts as a fresh sighting, which
naturally re-arms the quiet window without needing a second timer for that
path. LINK_CHECK_RECHECK_DAYS plays a different role: it stops the same offer
from being re-tested every run while its last result stayed ambiguous
(403/timeout), the one case where last_seen_at doesn't move on its own.
"""
import os
import time
from random import uniform

import requests

from core.db import get_connection

LINK_CHECK_STALE_DAYS = int(os.environ.get("LINK_CHECK_STALE_DAYS", "14"))
LINK_CHECK_RECHECK_DAYS = int(os.environ.get("LINK_CHECK_RECHECK_DAYS", "7"))
LINK_CHECK_MAX_PER_RUN = int(os.environ.get("LINK_CHECK_MAX_PER_RUN", "10"))
LINK_CHECK_TIMEOUT_SECONDS = int(os.environ.get("LINK_CHECK_TIMEOUT_SECONDS", "8"))

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; hobot link-checker; personal use)"}
_DEAD_STATUS_CODES = {404, 410}


def _is_dead(url: str) -> bool | None:
    """True = confirmed dead (404/410), False = confirmed alive (2xx/3xx),
    None = ambiguous (timeout, network error, 403 anti-bot, ...) -- never
    treated as dead, only True ever changes anything in the database."""
    try:
        r = requests.head(url, headers=_HEADERS, timeout=LINK_CHECK_TIMEOUT_SECONDS, allow_redirects=True)
        if r.status_code == 405:  # some sites reject HEAD -> retry with GET
            r = requests.get(
                url, headers=_HEADERS, timeout=LINK_CHECK_TIMEOUT_SECONDS,
                allow_redirects=True, stream=True,
            )
        if r.status_code in _DEAD_STATUS_CODES:
            return True
        if r.status_code < 400:
            return False
        return None
    except requests.RequestException:
        return None


def sweep_dead_links(limit: int | None = None) -> list[dict]:
    """Checks up to `limit` candidate offers -- active, not re-confirmed
    within LINK_CHECK_STALE_DAYS (offers.last_seen_at), not already re-tested
    too recently (offers.link_checked_at, a guard for the ambiguous case) --
    and marks status='expired' on the ones confirmed dead. An offer confirmed
    still alive gets its last_seen_at refreshed: the HTTP check counts as a
    fresh sighting, same as reappearing in a search. Returns the offers
    marked expired this pass (for the end-of-run notification)."""
    limit = limit or LINK_CHECK_MAX_PER_RUN
    with get_connection() as conn:
        candidates = conn.execute(
            """SELECT id, title, company, url, last_seen_at FROM offers
               WHERE status NOT IN ('applied', 'excluded', 'expired') AND score IS NOT NULL
               AND url IS NOT NULL AND url != ''
               AND last_seen_at <= datetime('now', ?)
               AND (link_checked_at IS NULL OR link_checked_at <= datetime('now', ?))
               ORDER BY last_seen_at ASC LIMIT ?""",
            (f"-{LINK_CHECK_STALE_DAYS} days", f"-{LINK_CHECK_RECHECK_DAYS} days", limit),
        ).fetchall()

    expired = []
    with get_connection() as conn:
        for i, offer in enumerate(candidates):
            if i > 0:
                time.sleep(uniform(1, 3))  # spaced out, never a burst
            dead = _is_dead(offer["url"])
            if dead is True:
                conn.execute(
                    "UPDATE offers SET status = 'expired', link_checked_at = datetime('now') WHERE id = ?",
                    (offer["id"],),
                )
                expired.append(dict(offer))
            elif dead is False:
                conn.execute(
                    "UPDATE offers SET link_checked_at = datetime('now'), last_seen_at = datetime('now') "
                    "WHERE id = ?",
                    (offer["id"],),
                )
            else:
                conn.execute("UPDATE offers SET link_checked_at = datetime('now') WHERE id = ?", (offer["id"],))
        conn.commit()
    return expired
