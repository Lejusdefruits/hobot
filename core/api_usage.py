"""Monthly usage tracking for APIs with a limited free quota (Adzuna 2500/month,
Hunter.io and Snov.io 50/month each, Pappers 100/month, Tavily 1000 credits/month
-- 1 credit per basic search).

Without this counter, scheduled discovery or a burst of contact lookups can
burn through a free quota with zero warning before the API starts refusing
calls -- indistinguishable from a legitimate "nothing found" for Hunter/Snov,
which just return an empty list either way. `has_quota()` lets a connector
skip the call cleanly instead of attempting it for nothing once the quota's
used up.

Every limit below is a documented free-tier default (Pappers' isn't published
as precisely as the others -- confirm against your actual plan on
pappers.fr/api) and can be overridden per source without touching code, e.g.
PAPPERS_MONTHLY_QUOTA=300 in .env if you're on a paid plan."""
import os
from datetime import datetime

from core.db import get_connection

MONTHLY_QUOTAS = {
    "adzuna": int(os.environ.get("ADZUNA_MONTHLY_QUOTA", "2500")),
    "hunter": int(os.environ.get("HUNTER_MONTHLY_QUOTA", "50")),
    "snov": int(os.environ.get("SNOV_MONTHLY_QUOTA", "50")),
    "pappers": int(os.environ.get("PAPPERS_MONTHLY_QUOTA", "100")),
    "tavily": int(os.environ.get("TAVILY_MONTHLY_QUOTA", "1000")),
}

# Shared with every interface that displays quota usage (discord_bot.py's
# /quotas, chat_agent.py's quotas_api_restants, tui/panes/reports.py) so a
# newly-tracked source only needs a label added here, not in three places
# that can silently drift out of sync with each other.
QUOTA_LABELS = {
    "adzuna": "Adzuna", "hunter": "Hunter.io", "snov": "Snov.io",
    "pappers": "Pappers", "tavily": "Tavily",
}

# What each source actually needs configured to be considered "active" --
# checked here (not by importing each tools/sources_*.py, which would pull
# in requests/langchain-sized modules just to answer a yes/no) so
# quota_summary() only ever shows a source the user has actually set up,
# never a permanent "0/50 used" row for an API they never subscribed to.
_CONFIGURED_CHECK = {
    "adzuna": lambda: bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY")),
    "hunter": lambda: bool(os.environ.get("HUNTER_API_KEY")),
    "snov": lambda: bool(os.environ.get("SNOV_USER_ID") and os.environ.get("SNOV_API_SECRET")),
    "pappers": lambda: bool(os.environ.get("PAPPERS_API_TOKEN")),
    "tavily": lambda: bool(os.environ.get("TAVILY_API_KEY")),
}


def is_configured(source: str) -> bool:
    """True for a source with no check registered above -- same
    fail-open default as has_quota() for an unknown source."""
    return _CONFIGURED_CHECK.get(source, lambda: True)()


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
    """A per-source summary, configured sources only -- used by
    quotas_api_restants (chat_agent.py), /quotas (discord_bot.py), and the
    terminal UI's Reports pane."""
    return [
        {"source": source, "used": calls_this_month(source), "limit": limit,
         "remaining": max(limit - calls_this_month(source), 0)}
        for source, limit in MONTHLY_QUOTAS.items()
        if is_configured(source)
    ]
