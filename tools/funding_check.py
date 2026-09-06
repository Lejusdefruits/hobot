"""Checks Maddyness/Frenchweb (tools/sources_funding_news.py) for a company
that just raised funding, as a lead worth watching on the ATS watchlist
(core/ats_watchlist.py) even before it has a posted opening -- a company
that just closed a round is a reasonable bet to be hiring soon. Runs on its
own schedule (daemon.py, every FUNDING_CHECK_INTERVAL_DAYS), independent of
job discovery.

Adds a match straight to the watchlist -- same effect as calling
surveiller_entreprise (graphs/chat_agent.py) by hand for it: a spontaneous-
application lead gets created and a contact looked up right away, then
notify_all reports what was added (Discord + desktop, persisted in the
notifications table like every other proactive notification here). Uses
tools/contact_research.py directly rather than importing graphs/chat_agent.py
for rechercher_contacts_entreprise -- same reasoning as contact_research.py's
own module docstring: this runs from the scheduled pipeline, and chat_agent.py
builds a whole LangGraph agent + LLM client at import time.
"""
import logging
import traceback
from datetime import datetime

from core.ats_watchlist import add_company, list_companies
from core.db import get_connection
from core.llm import chat_json
from tools.common import make_offer
from tools.contact_research import recherche_contact
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


def _add_lead(company: str, platform: str, slug: str, headline: str, link: str) -> bool:
    """add_company + a placeholder offer + an immediate contact lookup -- the
    same three steps surveiller_entreprise does by hand, run here for real
    instead of waiting on a chat command. Returns False on any failure
    (already watched by the time we get here, no board anymore, etc.)
    without raising -- one bad candidate must not stop the rest."""
    ok, _ = add_company(company, platform=platform, slug=slug)
    if not ok:
        return False
    from graphs.discovery_graph import persist_adhoc_offers
    lead = make_offer(
        source="ats_lead", external_id=None, title=f"Spontaneous interest -- {company}",
        company=company, location=None,
        description=(
            f"Added to the ATS watchlist automatically after a funding-news mention: "
            f"\"{headline}\" ({link}). No open posting yet -- its board is checked on "
            f"every scheduled run; in the meantime this is a spontaneous-application lead."
        ),
        url=None,
    )
    persisted = persist_adhoc_offers([lead])
    if persisted:
        recherche_contact(company, offer_id=persisted[0]["id"])
    return True


def run_funding_check() -> None:
    """Entry point, scheduled by daemon.py. Never raises past this boundary
    -- a failed check must not crash the scheduler, same convention as
    daemon.py's _run_weekly_digest."""
    log.info("=== funding news check ===")
    try:
        since = _last_check_time()
        candidates = fetch_funding_candidates(since=since)
        already_watched = {c["company"].lower() for c in list_companies()}

        added = []
        seen_companies: set = set()  # the same company can appear in both feeds on one run
        for item in candidates:
            company = _extract_company(item["title"])
            if not company or company.lower() in already_watched or company.lower() in seen_companies:
                continue
            resolved = resolve_slug(company)
            if not resolved:
                continue  # no Greenhouse/Ashby/Lever board -- nothing to watch
            seen_companies.add(company.lower())
            platform, slug = resolved
            if _add_lead(company, platform, slug, item["title"], item["link"]):
                added.append({"company": company, "platform": platform, "title": item["title"]})

        with get_connection() as conn:
            conn.execute(
                "INSERT INTO run_log (run_type, source, finished_at, n_found, n_new) "
                "VALUES ('funding_check', 'funding_news', datetime('now'), ?, ?)",
                (len(candidates), len(added)),
            )

        if not added:
            log.info("funding check: %d funding headline(s) checked, nothing new to watch", len(candidates))
            return

        lines = [f"{f['company']} ({f['platform']}) -- {f['title']}" for f in added]
        notify_all(
            "hobot -- funding news",
            "Recently funded, now on the ATS watchlist (a spontaneous-application lead "
            "and its contacts were created too -- retirer_entreprise_suivie in chat "
            "drops one if it's not relevant):\n" + "\n".join(lines),
            kind="funding",
        )
        log.info("funding check: added %d compan%s", len(added), "y" if len(added) == 1 else "ies")
    except Exception:
        log.error("funding check failed:\n%s", traceback.format_exc())
