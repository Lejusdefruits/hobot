"""The list of companies tools/sources_ats.py checks for Greenhouse/Ashby/
Lever openings -- unlike every other discovery source in this project,
those three platforms have no keyword search, so what to fetch has to be an
explicit list of companies rather than derived from
user_profile.target_roles. Two ways onto it: ATS_WATCHLIST (.env, company
names, resolved once and cached in the ats_watchlist table on first use) and
add_company()/remove_company() (graphs/chat_agent.py's chat tools, for
growing the list as you go, per the user's own request rather than only a
fixed default)."""
import os

from core.db import get_connection
from tools.sources_ats import resolve_slug

DEFAULT_WATCHLIST = [c.strip() for c in os.environ.get("ATS_WATCHLIST", "").split(",") if c.strip()]


def list_companies() -> list[dict]:
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, company, platform, slug, added_at FROM ats_watchlist ORDER BY company"
        ).fetchall()]


def add_company(company: str, platform: str | None = None, slug: str | None = None) -> tuple[bool, str]:
    """Resolves `company` to a working (platform, slug) via
    tools.sources_ats.resolve_slug() unless both are already given
    explicitly -- the escape hatch for the rare case its spelling guesses
    miss a real board: pass the slug straight from the company's careers
    page URL instead (e.g. boards.greenhouse.io/THIS-PART). Returns
    (ok, message), never raises."""
    if not platform or not slug:
        resolved = resolve_slug(company)
        if resolved is None:
            return False, (
                f'No Greenhouse/Ashby/Lever board found for "{company}" under the usual slug spellings. '
                f"If it's known to use one of those three, pass its exact slug from the careers page URL "
                f"instead (e.g. boards.greenhouse.io/THIS-PART)."
            )
        platform, slug = resolved
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT company FROM ats_watchlist WHERE platform = ? AND slug = ?", (platform, slug),
        ).fetchone()
        if existing:
            return False, f'Already watching "{existing["company"]}" ({platform}/{slug}).'
        conn.execute(
            "INSERT INTO ats_watchlist (company, platform, slug) VALUES (?, ?, ?)",
            (company, platform, slug),
        )
    return True, f'Now watching "{company}" ({platform}/{slug}).'


def remove_company(company: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM ats_watchlist WHERE company = ? COLLATE NOCASE", (company,))
        return cur.rowcount > 0


def ensure_defaults_loaded() -> None:
    """Resolves and inserts any ATS_WATCHLIST (.env) entry not already in
    the table -- idempotent and cheap to call at the start of every
    discovery run: an HTTP resolve call only happens for a company seen for
    the first time, everything already in the table is a no-op here."""
    if not DEFAULT_WATCHLIST:
        return
    with get_connection() as conn:
        already = {r["company"].lower() for r in conn.execute("SELECT company FROM ats_watchlist").fetchall()}
    for company in DEFAULT_WATCHLIST:
        if company.lower() not in already:
            add_company(company)
