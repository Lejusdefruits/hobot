"""Monthly usage tracking for APIs with a limited free quota (Adzuna 2500/month,
Hunter.io and Snov.io 50/month each, 1 credit per domain search).

Without this counter, scheduled discovery or a burst of contact lookups can
burn through a free quota with zero warning before the API starts refusing
calls -- indistinguishable from a legitimate "nothing found" for Hunter/Snov,
which just return an empty list either way. `has_quota()` lets a connector
skip the call cleanly instead of attempting it for nothing once the quota's
used up."""
from datetime import datetime

from core.db import get_connection

MONTHLY_QUOTAS = {
    "adzuna": 2500,
    "hunter": 50,
    "snov": 50,
}


def log_call(source: str) -> None:
    """Call this once per call that ACTUALLY went out to the API (after an
    HTTP response, success or application-level failure -- not on a network
    error before anything was sent, which burns no credit)."""
    with get_connection() as conn:
        conn.execute("INSERT INTO api_calls (source) VALUES (?)", (source,))
        conn.commit()


def calls_this_month(source: str) -> int:
    month_start = datetime.now().strftime("%Y-%m-01")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM api_calls WHERE source = ? AND called_at >= ?",
            (source, month_start),
        ).fetchone()
    return row["c"]


def has_quota(source: str) -> bool:
    """False if this source's known monthly quota is already used up. True
    for a source with no quota documented here."""
    limit = MONTHLY_QUOTAS.get(source)
    if limit is None:
        return True
    return calls_this_month(source) < limit


def quota_summary() -> list[dict]:
    """A per-source summary -- used by quotas_api_restants (chat_agent.py) and
    /quotas (discord_bot.py)."""
    return [
        {"source": source, "used": calls_this_month(source), "limit": limit,
         "remaining": max(limit - calls_this_month(source), 0)}
        for source, limit in MONTHLY_QUOTAS.items()
    ]
