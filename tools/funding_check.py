"""Checks Maddyness/Frenchweb (tools/sources_funding_news.py) for a company
that just raised funding, as a lead worth watching on the ATS watchlist
(core/ats_watchlist.py) even before it has a posted opening -- a company
that just closed a round is a reasonable bet to be hiring soon. Runs on its
own schedule (daemon.py, every FUNDING_CHECK_INTERVAL_DAYS), independent of
job discovery.

Never adds a company automatically: this only NOTIFIES a candidate (Discord
+ desktop, via notify_all -- persisted in the notifications table like every
other proactive notification in this project), the same "propose, don't
act" pattern preparer_envoi_mail (graphs/chat_agent.py) already uses for a
real email send. Add one for real with surveiller_entreprise via /ask once
you've seen the notification, or ignore it if it's not relevant -- nothing
here writes to ats_watchlist itself.
"""
import logging
import traceback
from datetime import datetime

from core.ats_watchlist import list_companies
from core.db import get_connection
from core.llm import chat_json
from tools.notify_tools import notify_all
from tools.sources_ats import resolve_slug
from tools.sources_funding_news import fetch_funding_candidates

log = logging.getLogger("funding_check")

EXTRACT_PROMPT = """This is the headline of a French tech/business news article that MAY be
about a company raising funding (a "levee de fonds"):

"{title}"

If this headline is about ONE SPECIFIC company raising money, reply with
ONLY this JSON: {{"entreprise": "the company's name"}}
If it is NOT about a specific company raising funding (an opinion piece, a
retrospective, or about a person/policy/trend instead), reply with ONLY:
{{"entreprise": null}}"""


def _extract_company(title: str) -> str | None:
    try:
        result = chat_json(EXTRACT_PROMPT.format(title=title))
    except Exception as e:
        log.warning("funding check: LLM extraction failed for %r: %s", title, e)
        return None
    company = result.get("entreprise")
    return company.strip() if isinstance(company, str) and company.strip() else None


def _last_check_time() -> datetime | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT finished_at FROM run_log WHERE run_type = 'funding_check' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row or not row["finished_at"]:
        return None
    return datetime.fromisoformat(row["finished_at"])


def run_funding_check() -> None:
    """Entry point, scheduled by daemon.py. Never raises past this boundary
    -- a failed check must not crash the scheduler, same convention as
    daemon.py's _run_weekly_digest."""
    log.info("=== funding news check ===")
    try:
        since = _last_check_time()
        candidates = fetch_funding_candidates(since=since)
        already_watched = {c["company"].lower() for c in list_companies()}

        found = []
        seen_companies: set = set()  # the same company can appear in both feeds on one run
        for item in candidates:
            company = _extract_company(item["title"])
            if not company or company.lower() in already_watched or company.lower() in seen_companies:
                continue
            resolved = resolve_slug(company)
            if not resolved:
                continue  # no Greenhouse/Ashby/Lever board -- nothing surveiller_entreprise could act on anyway
            seen_companies.add(company.lower())
            platform, slug = resolved
            found.append({"company": company, "platform": platform, "title": item["title"], "link": item["link"]})

        with get_connection() as conn:
            conn.execute(
                "INSERT INTO run_log (run_type, source, finished_at, n_found, n_new) "
                "VALUES ('funding_check', 'funding_news', datetime('now'), ?, ?)",
                (len(candidates), len(found)),
            )

        if not found:
            log.info("funding check: %d funding headline(s) checked, nothing new to propose", len(candidates))
            return

        lines = [f"{f['company']} ({f['platform']}) -- {f['title']}" for f in found]
        notify_all(
            "hobot -- funding news",
            "Recently funded, has a Greenhouse/Ashby/Lever board, not on your watchlist yet:\n"
            + "\n".join(lines)
            + '\n\nAdd one via chat: "surveille <company>" (surveiller_entreprise).',
            kind="funding",
        )
        log.info("funding check: proposed %d compan%s", len(found), "y" if len(found) == 1 else "ies")
    except Exception:
        log.error("funding check failed:\n%s", traceback.format_exc())
