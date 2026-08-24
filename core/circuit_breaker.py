"""Per-source circuit breaker, so a failing source never gets retried in a
tight loop. Uses `run_log.backoff_until`: 2h minimum backoff, doubled on each
consecutive failure, capped at 24h, reset the moment a fetch succeeds.
"""
from datetime import datetime, timedelta

from core.db import get_connection

BASE_BACKOFF_HOURS = 2
MAX_BACKOFF_HOURS = 24


def is_backed_off(source: str) -> tuple[bool, str | None]:
    """Check this before any call to a source. If True, don't call it at all
    (not even one attempt) -- that's the whole point of a circuit breaker."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT backoff_until FROM run_log WHERE source = ? ORDER BY id DESC LIMIT 1",
            (source,),
        ).fetchone()
    if not row or not row["backoff_until"]:
        return False, None
    until = datetime.fromisoformat(row["backoff_until"])
    return (until > datetime.now()), row["backoff_until"]


def _consecutive_failures(conn, source: str) -> int:
    # backoff_until, not errors: a run can log a non-null error while still
    # keeping substantial partial results (a fetch that throws partway
    # through a location/query loop but keeps what it already found), and
    # compute_backoff_until's caller now only asks for backoff on runs with
    # zero results -- counting by `errors` instead would keep inflating the
    # streak (and the backoff duration) off of those partial successes.
    rows = conn.execute(
        "SELECT backoff_until FROM run_log WHERE source = ? ORDER BY id DESC LIMIT 10", (source,)
    ).fetchall()
    count = 0
    for row in rows:
        if row["backoff_until"]:
            count += 1
        else:
            break
    return count


def compute_backoff_until(source: str, has_error: bool) -> str | None:
    """Call this right before writing the run_log row for the run that just
    finished. Success -> None (clears any prior backoff, since this becomes the
    most recent row is_backed_off will read)."""
    if not has_error:
        return None
    with get_connection() as conn:
        streak = _consecutive_failures(conn, source) + 1
    hours = min(MAX_BACKOFF_HOURS, BASE_BACKOFF_HOURS * (2 ** (streak - 1)))
    return (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
