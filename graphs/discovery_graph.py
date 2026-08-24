"""discovery_graph -- the offer discovery pipeline.

fetch_jobspy / fetch_lba / fetch_adzuna / fetch_francetravail / fetch_labonneboite / fetch_ats
(parallel) -> dedupe -> persist_new -> score -> draft_letters -> log_run.

JobSpy (scraping, no key required, see tools/sources_jobspy.py) is the only
source active by default. The next four are official French APIs, each
individually optional (missing key -> the connector returns an empty list,
the fetch node logs it and moves on): La Bonne Alternance and France Travail
"Offres d'emploi v2" (apprenticeship, France only), Adzuna (general-purpose,
multi-country but tuned for France here), La Bonne Boite (spontaneous-
application leads, access subject to manual approval by France Travail --
see tools/sources_labonneboite.py). The search term and target cities come
from user_profile (target_roles / target_locations), never hardcoded -- see
core/profile.py for how that profile gets filled in (a CV or free text
through chat). La Bonne Alternance/La Bonne Boite search by ROME code
(LBA_ROME_CODES, .env) rather than a free-text keyword: these are fixed-
taxonomy APIs, France Travail doesn't support free search.

fetch_ats (tools/sources_ats.py, core/ats_watchlist.py) is the odd one out:
Greenhouse/Ashby/Lever have no keyword search at all, only a per-company
feed, so it works off an explicit company list (ATS_WATCHLIST in .env, or
grown via the surveiller_entreprise/retirer_entreprise_suivie/
lister_entreprises_suivies chat tools) rather than user_profile -- not
France-specific, but skews tech/software/startup hiring by nature of what
uses these three platforms.

Discovery and scoring are two separate steps: dedupe/persist_new save EVERY
new relevant offer (score=NULL), that's the durable "already seen" memory --
nothing is silently dropped. score_node then pulls from that queue (FIFO by
discovery date) up to DISCOVERY_MAX_SCORE_PER_RUN per run, so it never loops
back over the same offers.

is_backed_off/compute_backoff_until (core/circuit_breaker.py): a failing
source gets backed off (2h, doubled on each failure, capped at 24h) instead
of being retried without limit on the next cycle.

draft_letters automatically writes and saves a cover letter draft for any
offer above DISCOVERY_LETTER_SCORE_THRESHOLD that doesn't have an
`applications` row yet -- never a send, just a file in outputs/. A web search
(tools/web_search.py, self-hosted SearXNG, optional) precedes the writing
when it's configured: one query per letter, never more.
"""
import operator
import os
import sys
from pathlib import Path
from typing import Annotated, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.graph import END, START, StateGraph

from core import ats_watchlist, locations
from core.circuit_breaker import compute_backoff_until, is_backed_off
from core.db import get_connection, get_user_profile, init_db
from core.llm import chat_json
from tools import (
    sources_adzuna, sources_ats, sources_francetravail, sources_jobspy,
    sources_labonneboite, sources_lba, web_search,
)
from tools.common import normalize_text, upsert_application
from tools.discord_style import COLOR_ACTIVE, base_embed
from tools.notify_tools import notify_all

# Fallback when user_profile.target_locations is empty or contains no city
# recognized by core/locations.py -- Paris alone (not one more hardcoded
# city, just the registry's own default). Used by sources that need
# coordinates/a commune code (LBA, France Travail, La Bonne Boite), not by
# JobSpy, which accepts any free-text city name.
DEFAULT_TARGET_LOCATIONS = [locations.DEFAULT_LOCATION]

# ROME code(s) for La Bonne Alternance / La Bonne Boite -- fixed taxonomy, not
# a free-text keyword (see the module docstring). Defaults to IT/software
# development (M1805); change it in .env for a different target role.
LBA_ROME_CODES = os.environ.get("LBA_ROME_CODES", "M1805")

# Sources currently wired into the scheduled graph -- source of truth for
# /sources (discord_bot.py), which filters on this so a source no longer
# fetched never shows up there as if it were still active.
ACTIVE_DISCOVERY_SOURCES = ("jobspy", "lba", "adzuna", "francetravail", "labonneboite", "ats")


def is_source_configured(source: str) -> bool:
    """Whether this source actually has what it needs to fetch anything --
    distinct from whether it HAS fetched anything: a source can be fully
    configured and still show "never run" if discovery hasn't reached it
    yet, and conversely a source with no key just silently returns nothing
    every run (see each connector's own module docstring) without ever
    surfacing "you forgot to set this up" on its own. /sources and the
    terminal UI's Reports > Sources tab both show this alongside run
    history so that distinction is visible instead of looking identical to
    "configured but nothing found yet." labonneboite additionally needs a
    manual France Travail approval on top of the same credentials
    (README's French job sources section) that can't be checked from
    config alone -- "configured" here only means the credentials are set."""
    if source == "jobspy":
        return True
    if source == "lba":
        from tools.sources_lba import LBA_API_KEY
        return bool(LBA_API_KEY)
    if source == "adzuna":
        from tools.sources_adzuna import APP_ID, APP_KEY
        return bool(APP_ID and APP_KEY)
    if source in ("francetravail", "labonneboite"):
        from core.france_travail_auth import CLIENT_ID, CLIENT_SECRET
        return bool(CLIENT_ID and CLIENT_SECRET)
    if source == "ats":
        from core.ats_watchlist import list_companies
        return bool(list_companies())
    return False

# JobSpy and Adzuna only return postings within a recent window (see
# JOBSPY_HOURS_OLD / ADZUNA_MAX_DAYS_OLD in their own connectors) -- fine once
# there's a backlog to keep fresh, but on a near-empty database (a fresh
# install, or right after a reset) that same narrow window mostly returns
# nothing: there's nothing to compare a "new" posting against, so recency
# filtering just throws away candidates instead of narrowing good ones. Below
# this many offers total, fetch_jobspy/fetch_adzuna widen their window for
# this run only, falling back to the normal (tighter) default once past it.
# Self-correcting: a widened run finds more offers, raises the count past the
# threshold, and the next run narrows back on its own -- no separate
# "backfill mode" flag to remember to turn off.
SPARSE_DB_THRESHOLD = int(os.environ.get("DISCOVERY_SPARSE_THRESHOLD", "30"))
ADZUNA_SPARSE_MAX_DAYS_OLD = int(os.environ.get("ADZUNA_SPARSE_MAX_DAYS_OLD", "14"))
JOBSPY_SPARSE_HOURS_OLD = int(os.environ.get("JOBSPY_SPARSE_HOURS_OLD", str(14 * 24)))


def _is_db_sparse() -> bool:
    with get_connection() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM offers").fetchone()["c"]
    return n < SPARSE_DB_THRESHOLD


def _resolve_target_locations(profile: dict) -> list[dict]:
    """Resolves user_profile.target_locations against the city registry
    (core/locations.py) for sources that need coordinates or a commune code.
    Falls back to DEFAULT_TARGET_LOCATIONS if the profile is empty or none of
    its cities are recognized -- never an empty list out."""
    resolved = locations.resolve_many(profile.get("target_locations"))
    return resolved or DEFAULT_TARGET_LOCATIONS


def _search_terms(profile: dict) -> list[str]:
    """One query per target role instead of joining them all into a single
    comma-separated string -- a query like "Backend Developer, Data Analyst"
    is one literal string match against these APIs/scrapers, not an OR of two
    terms, so a profile with several target roles was only ever really
    searching for the first one in practice. Falls back to a single generic
    term if the profile has no target role set yet."""
    return [r for r in (profile.get("target_roles") or []) if r] or ["emploi"]


# LLM scoring is the bottleneck (~10-30s/offer locally) -- MAX_SCORE_PER_RUN
# bounds how many unscored offers get processed in THIS run, regardless of how
# many new offers were found (they all get saved anyway, see
# persist_new_node).
MAX_SCORE_PER_RUN = int(os.environ.get("DISCOVERY_MAX_SCORE_PER_RUN", "40"))


class DiscoveryState(TypedDict):
    raw_offers: Annotated[list[dict], operator.add]
    stats: Annotated[list[dict], operator.add]
    new_offers: list[dict]
    n_new_by_source: dict
    scored_offers: list[dict]
    drafted_letters: list[dict]
    skipped_irrelevant: int
    skipped_cross_source: int
    skipped_formation: int


def _log(msg: str) -> None:
    print(msg, flush=True)


def _run_fetch(source: str, label: str, offers: list, fill, query: str | None = None) -> dict:
    """Shared try/except/else + logging + stats-dict shape for every
    fetch_*_node below. `fill()` appends into `offers` (declared by the
    caller -- each source's search_term/location/page nesting differs too
    much to express generically) and may raise partway through; whatever it
    already appended before that stays, which is the whole point (a source
    that dies on its 4th of 5 locations still keeps the first 3). One copy
    of this instead of five near-identical ones that had already started to
    drift -- labonneboite used to special-case AccessNotGranted with its own
    log line here; that class of failure reads fine through the same
    "failed partway through" message as everything else, since the
    exception's own text already says what happened."""
    error = None
    try:
        fill()
    except Exception as e:
        error = str(e)
        _log(f"[fetch] {label} -> failed partway through ({e}), keeping {len(offers)} offer(s) already found")
    else:
        _log(f"[fetch] {label} -> {len(offers)} results")
    stat = {"source": source, "n_found": len(offers), "error": error}
    if query is not None:
        stat["query"] = query
    return {"raw_offers": offers, "stats": [stat]}


def fetch_jobspy_node(state: DiscoveryState) -> dict:
    backed_off, until = is_backed_off("jobspy")
    if backed_off:
        _log(f"[fetch] JobSpy -> backed off until {until}, skipped (not attempted)")
        return {"raw_offers": [], "stats": []}

    profile = get_user_profile() or {"skills": [], "target_roles": [], "target_locations": []}
    search_terms = _search_terms(profile)
    country = os.environ.get("JOBSPY_COUNTRY", "France")
    # JobSpy geolocates better with the country spelled out in the query --
    # verified live: "Lyon" alone returns 0 results where "Lyon, France" finds some.
    locations = [
        loc if "," in loc else f"{loc}, {country}"
        for loc in (profile["target_locations"] or [country])
    ]

    sparse = _is_db_sparse()
    hours_old = JOBSPY_SPARSE_HOURS_OLD if sparse else None
    _log(f"[fetch] JobSpy ({search_terms} @ {', '.join(locations)}"
         f"{', widened window: sparse db' if sparse else ''})...")
    offers = []

    def fill():
        for search_term in search_terms:
            for location in locations:
                offers.extend(sources_jobspy.search_offers(search_term=search_term, location=location, hours_old=hours_old))

    return _run_fetch("jobspy", "JobSpy", offers, fill, query=" | ".join(search_terms))


def fetch_lba_node(state: DiscoveryState) -> dict:
    if not sources_lba.LBA_API_KEY:
        _log("[fetch] LBA -> LBA_API_KEY not set, source skipped")
        return {"raw_offers": [], "stats": []}
    backed_off, until = is_backed_off("lba")
    if backed_off:
        _log(f"[fetch] LBA -> backed off until {until}, skipped (not attempted)")
        return {"raw_offers": [], "stats": []}
    profile = get_user_profile() or {"target_locations": []}
    _log("[fetch] LBA...")
    offers = []

    def fill():
        for loc in _resolve_target_locations(profile):
            offers.extend(sources_lba.search_offers(romes=LBA_ROME_CODES, latitude=loc["lat"], longitude=loc["lon"], radius=30))

    return _run_fetch("lba", "LBA", offers, fill)


ADZUNA_PAGES_PER_LOCATION = int(os.environ.get("ADZUNA_PAGES_PER_LOCATION", "2"))


def fetch_adzuna_node(state: DiscoveryState) -> dict:
    if not (sources_adzuna.APP_ID and sources_adzuna.APP_KEY):
        _log("[fetch] Adzuna -> ADZUNA_APP_ID/ADZUNA_APP_KEY not set, source skipped")
        return {"raw_offers": [], "stats": []}
    backed_off, until = is_backed_off("adzuna")
    if backed_off:
        _log(f"[fetch] Adzuna -> backed off until {until}, skipped (not attempted)")
        return {"raw_offers": [], "stats": []}
    profile = get_user_profile() or {"target_roles": [], "target_locations": []}
    queries = _search_terms(profile)
    sparse = _is_db_sparse()
    max_days_old = ADZUNA_SPARSE_MAX_DAYS_OLD if sparse else None
    _log(f"[fetch] Adzuna ({queries}{', widened window: sparse db' if sparse else ''})...")
    offers = []

    def fill():
        for query in queries:
            for loc in _resolve_target_locations(profile):
                # Page 1 alone caps out at 15 results/location/query regardless
                # of how many are actually available -- ADZUNA_PAGES_PER_LOCATION
                # (2 by default) fetches further pages as long as a full page
                # suggests there's more (a half-empty page means end of
                # results, not worth spending another quota call on).
                for page in range(1, ADZUNA_PAGES_PER_LOCATION + 1):
                    page_offers = sources_adzuna.search_offers(
                        what=query, where=loc["label"], results_per_page=15, page=page, max_days_old=max_days_old,
                    )
                    offers.extend(page_offers)
                    if len(page_offers) < 15:
                        break

    return _run_fetch("adzuna", "Adzuna", offers, fill, query=" | ".join(queries))


def fetch_francetravail_node(state: DiscoveryState) -> dict:
    if not sources_francetravail.CLIENT_ID:
        _log("[fetch] France Travail -> FRANCE_TRAVAIL_CLIENT_ID not set, source skipped")
        return {"raw_offers": [], "stats": []}
    backed_off, until = is_backed_off("francetravail")
    if backed_off:
        _log(f"[fetch] France Travail -> backed off until {until}, skipped (not attempted)")
        return {"raw_offers": [], "stats": []}
    profile = get_user_profile() or {"target_roles": [], "target_locations": []}
    queries = _search_terms(profile)
    _log(f"[fetch] France Travail ({queries})...")
    offers = []

    def fill():
        for query in queries:
            for loc in _resolve_target_locations(profile):
                offers.extend(sources_francetravail.search_offers(mots_cles=query, ville=loc["label"]))

    return _run_fetch("francetravail", "France Travail", offers, fill, query=" | ".join(queries))


def fetch_labonneboite_node(state: DiscoveryState) -> dict:
    """By ROME code (like LBA), not by keyword -- see the note on LBA_ROME_CODES."""
    if not sources_labonneboite.CLIENT_ID:
        _log("[fetch] La Bonne Boite -> FRANCE_TRAVAIL_CLIENT_ID not set, source skipped")
        return {"raw_offers": [], "stats": []}
    backed_off, until = is_backed_off("labonneboite")
    if backed_off:
        _log(f"[fetch] La Bonne Boite -> backed off until {until}, skipped (not attempted)")
        return {"raw_offers": [], "stats": []}
    profile = get_user_profile() or {"target_locations": []}
    _log("[fetch] La Bonne Boite...")
    offers = []

    def fill():
        for loc in _resolve_target_locations(profile):
            offers.extend(sources_labonneboite.search_offers(
                romes=LBA_ROME_CODES, latitude=loc["lat"], longitude=loc["lon"], distance=30,
            ))

    return _run_fetch("labonneboite", "La Bonne Boite", offers, fill)


def fetch_ats_node(state: DiscoveryState) -> dict:
    """Greenhouse/Ashby/Lever, one call per watched company (core/ats_watchlist.py)
    -- no keyword search here, see tools/sources_ats.py's module docstring on
    why. A single company's board failing (moved off that ATS, temporarily
    down) is logged and skipped, not treated as this whole source failing --
    only an empty watchlist or every single company failing backs off "ats"
    as a whole."""
    backed_off, until = is_backed_off("ats")
    if backed_off:
        _log(f"[fetch] ATS watchlist -> backed off until {until}, skipped (not attempted)")
        return {"raw_offers": [], "stats": []}

    ats_watchlist.ensure_defaults_loaded()
    companies = ats_watchlist.list_companies()
    if not companies:
        return {"raw_offers": [], "stats": []}

    _log(f"[fetch] ATS watchlist ({len(companies)} compan{'y' if len(companies) == 1 else 'ies'})...")
    offers = []
    n_failed = 0
    for c in companies:
        try:
            jobs = sources_ats.fetch_company_jobs(c["platform"], c["slug"])
            offers += [sources_ats.normalize_job(c["platform"], c["company"], j) for j in jobs]
        except Exception as e:
            n_failed += 1
            _log(f"[fetch] ATS watchlist -> {c['company']} ({c['platform']}/{c['slug']}) failed: {e}")
    _log(f"[fetch] ATS watchlist -> {len(offers)} results ({n_failed} compan{'y' if n_failed == 1 else 'ies'} failed)")
    error = f"{n_failed}/{len(companies)} companies failed" if n_failed == len(companies) else None
    return {"raw_offers": offers, "stats": [{"source": "ats", "n_found": len(offers), "error": error}]}


def _relevance_keywords(target_roles: list[str]) -> list[str]:
    """Derives a cheap pre-filter (no LLM) from the profile's target roles,
    instead of a fixed keyword list -- a hardcoded pre-filter for one role
    would silently break relevance for any other. Every significant word
    (>=4 letters) of a target role becomes an accepted keyword. Empty (no
    profile set) -> no filter, everything passes."""
    keywords = set()
    for role in target_roles:
        for word in normalize_text(role).split():
            if len(word) >= 4:
                keywords.add(word)
    return list(keywords)


def _is_relevant(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    return any(kw in normalize_text(title) for kw in keywords)


# Some listings aren't real employer postings but ads from a training
# organization recruiting students for THEIR OWN diploma program, with a
# vague "partner company" that's never actually named. Markers are in French
# because that's the language these listings are written in (French-market
# sources only) -- requires both signals (the intermediary euphemism + a
# mention of the organization's own training program) so a real recruiting
# firm that just says "our client" without pitching its own training program
# doesn't get blocked.
FORMATION_PARTNER_MARKERS = ("entreprise partenaire", "entreprises partenaires")
FORMATION_SCHOOL_MARKERS = (
    "formation diplômante", "formations diplômantes", "spécialiste de la formation",
    "centre de formation", "organisme de formation", "notre école", "l'école qui",
    "nos formations", "notre formation",
)


def _is_formation_intermediary(description: str) -> bool:
    d = (description or "").lower()
    return any(m in d for m in FORMATION_PARTNER_MARKERS) and any(m in d for m in FORMATION_SCHOOL_MARKERS)


def persist_adhoc_offers(raw_offers: list[dict]) -> list[dict]:
    """Dedup + persistence for an ON-DEMAND search (chercher_offres_maintenant,
    triggered from chat) -- not a node in the scheduled graph, deliberately a
    separate function so it never touches the dedupe_node/persist_new_node
    pipeline. Same dedup (hash + cross-source dedup_key + database) as
    scheduled discovery, but WITHOUT the relevance pre-filter: an on-demand
    search is already deliberately targeted by the user (mot_cle/ville chosen
    by hand), no need to re-filter it against the profile's keywords. The
    training-organization filter stays active: that's a reliability problem
    with the offer, not a topic-relevance one, so it applies no matter what
    was searched for. origin='ponctuelle' in the database distinguishes these
    offers from ones found by scheduled discovery. Returns each offer with
    its real id (already existing, or just inserted), so the agent can then
    use details_offre/marquer_postule/etc. on it."""
    result = []
    seen_hashes: set[str] = set()
    seen_dedup_keys: set[str] = set()

    with get_connection() as conn:
        for offer in raw_offers:
            url_hash = offer["url_hash"]
            dedup_key = offer.get("dedup_key") or ""
            has_real_key = dedup_key not in ("", "|")

            if url_hash in seen_hashes:
                continue
            if has_real_key and dedup_key in seen_dedup_keys:
                continue
            seen_hashes.add(url_hash)
            if has_real_key:
                seen_dedup_keys.add(dedup_key)

            row = conn.execute(
                "SELECT id FROM offers WHERE url_hash = ? OR (? AND dedup_key = ?)",
                (url_hash, has_real_key, dedup_key),
            ).fetchone()
            if row:
                conn.execute("UPDATE offers SET last_seen_at = datetime('now') WHERE id = ?", (row["id"],))
                result.append({**offer, "id": row["id"]})
                continue
            if _is_formation_intermediary(offer.get("description")):
                continue

            cur = conn.execute(
                """INSERT OR IGNORE INTO offers
                   (source, url_hash, dedup_key, url, title, company, location, description,
                    salary, posted_date, status, origin)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', 'ponctuelle')""",
                (
                    offer["source"], url_hash, offer.get("dedup_key"), offer.get("url"),
                    offer.get("title"), offer.get("company"), offer.get("location"),
                    offer.get("description"), offer.get("salary"), offer.get("posted_date"),
                ),
            )
            if cur.rowcount:
                result.append({**offer, "id": cur.lastrowid})
        conn.commit()
    return result


def dedupe_node(state: DiscoveryState) -> dict:
    """Three-level dedup: intra-batch (an in-memory set), cross-source
    (dedup_key = normalized company+title -- see tools/common.py), and
    against the database. Doesn't cap anything: everything new and relevant
    gets kept for persist_new_node to save."""
    profile = get_user_profile() or {"target_roles": []}
    keywords = _relevance_keywords(profile["target_roles"])

    new_offers = []
    skipped_irrelevant = 0
    skipped_cross_source = 0
    skipped_formation = 0
    seen_hashes: set[str] = set()
    seen_dedup_keys: set[str] = set()

    with get_connection() as conn:
        for offer in state["raw_offers"]:
            url_hash = offer["url_hash"]
            dedup_key = offer.get("dedup_key") or ""
            has_real_key = dedup_key not in ("", "|")

            if url_hash in seen_hashes:
                continue
            if has_real_key and dedup_key in seen_dedup_keys:
                skipped_cross_source += 1
                continue
            seen_hashes.add(url_hash)
            if has_real_key:
                seen_dedup_keys.add(dedup_key)

            row = conn.execute(
                "SELECT id FROM offers WHERE url_hash = ? OR (? AND dedup_key = ?)",
                (url_hash, has_real_key, dedup_key),
            ).fetchone()
            if row:
                conn.execute("UPDATE offers SET last_seen_at = datetime('now') WHERE id = ?", (row["id"],))
            elif not _is_relevant(offer.get("title"), keywords):
                skipped_irrelevant += 1
            elif _is_formation_intermediary(offer.get("description")):
                skipped_formation += 1
            else:
                new_offers.append(offer)

    parts = [f"{len(state['raw_offers'])} raw -> {len(new_offers)} new to save"]
    if skipped_cross_source:
        parts.append(f"{skipped_cross_source} cross-source duplicate(s)")
    if skipped_irrelevant:
        parts.append(f"{skipped_irrelevant} dropped by the pre-filter")
    if skipped_formation:
        parts.append(f"{skipped_formation} dropped (training-organization intermediary)")
    _log("[dedupe] " + ", ".join(parts))
    return {
        "new_offers": new_offers,
        "skipped_irrelevant": skipped_irrelevant,
        "skipped_cross_source": skipped_cross_source,
        "skipped_formation": skipped_formation,
    }


def persist_new_node(state: DiscoveryState) -> dict:
    """Saves EVERY new offer, score = NULL. This is the "already seen" memory:
    once an offer's here, dedupe_node will never surface it again, scored or
    not -- that's what stops the pipeline from looping back over the same
    offers."""
    n_new_by_source: dict[str, int] = {}
    with get_connection() as conn:
        for offer in state["new_offers"]:
            n_new_by_source[offer["source"]] = n_new_by_source.get(offer["source"], 0) + 1
            conn.execute(
                """INSERT OR IGNORE INTO offers
                   (source, url_hash, dedup_key, url, title, company, location, description, salary, posted_date, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')""",
                (
                    offer["source"], offer["url_hash"], offer.get("dedup_key"), offer.get("url"),
                    offer.get("title"), offer.get("company"), offer.get("location"),
                    offer.get("description"), offer.get("salary"), offer.get("posted_date"),
                ),
            )
        conn.commit()
    if state["new_offers"]:
        _log(f"[persist] {len(state['new_offers'])} new offers saved (awaiting score)")
    return {"n_new_by_source": n_new_by_source}


SCORE_PROMPT = """You're evaluating how relevant a job offer is for this candidate.

Candidate profile:
- Skills: {skills}
- Target roles: {target_roles}
- Target locations: {target_locations}

Offer to evaluate:
- Title: {title}
- Company: {company}
- Location: {location}
- Description: {description}

Information found about the company (automatic web search -- BE CAREFUL: it
may be outdated, off-topic, or about an unrelated company with the same name.
Ignore it entirely if the industry doesn't clearly match the offer, or if
you're not sure it's actually the same company):
{infos_entreprise}

Reply with ONLY JSON: {{"score": <integer 0-100>, "reason": "one sentence explaining the score"}}
High score if the offer matches the candidate's skills and target well.
Low score if the offer is off-topic (different field), poorly located, or clearly the wrong seniority level.
The web search information is only a SUPPLEMENT for thin offer descriptions --
never use it as the sole justification for a high score, the offer's own
description remains the primary source."""

SCORE_QUEUE_QUERY = """
    SELECT id, source, url, title, company, location, description
    FROM offers
    WHERE score IS NULL
    ORDER BY first_seen_at ASC
    LIMIT ?
"""

# Web search before scoring, reserved for offers whose description is too
# short to be usable as-is (an arbitrary but generous threshold) -- no need
# to look elsewhere for an offer that's already well detailed. Capped
# independently of MAX_SCORE_PER_RUN so a scoring run never turns into a
# burst of SearXNG queries. Without SEARXNG_HOST set, web_search.search_company
# just returns an empty string -- this step degrades silently, nothing breaks.
THIN_DESCRIPTION_THRESHOLD = 200
MAX_COMPANY_SEARCHES_PER_RUN = int(os.environ.get("DISCOVERY_MAX_COMPANY_SEARCHES_PER_RUN", "10"))


def score_node(state: DiscoveryState) -> dict:
    """Scores directly from the database queue (not from state["new_offers"]):
    covers both this run's offers and the unscored backlog from previous runs."""
    profile = get_user_profile() or {"skills": [], "target_roles": [], "target_locations": []}
    scored = []
    company_searches_done = 0
    searched_companies: dict[str, str] = {}  # avoids searching the same company twice in this run
    with get_connection() as conn:
        rows = conn.execute(SCORE_QUEUE_QUERY, (MAX_SCORE_PER_RUN,)).fetchall()
        total = len(rows)
        for i, row in enumerate(rows, 1):
            title = (row["title"] or "(untitled)")[:55]

            infos_entreprise = "(not searched -- the offer's own description was already enough)"
            company_key = (row["company"] or "").strip().lower()
            description_len = len(row["description"] or "")
            if description_len < THIN_DESCRIPTION_THRESHOLD and company_key:
                if company_key in searched_companies:
                    infos_entreprise = searched_companies[company_key]
                elif company_searches_done < MAX_COMPANY_SEARCHES_PER_RUN:
                    infos_entreprise = web_search.search_company(
                        row["company"] or "", hint=f"{row['title'] or ''} {row['location'] or ''}".strip()
                    ) or "(no information found)"
                    company_searches_done += 1
                    searched_companies[company_key] = infos_entreprise
                else:
                    infos_entreprise = "(web search cap reached for this run)"

            try:
                result = chat_json(SCORE_PROMPT.format(
                    skills=", ".join(profile["skills"]),
                    target_roles=", ".join(profile["target_roles"]),
                    target_locations=", ".join(profile["target_locations"]),
                    title=row["title"] or "",
                    company=row["company"] or "",
                    location=row["location"] or "",
                    description=(row["description"] or "")[:1500],
                    infos_entreprise=infos_entreprise,
                ))
                score, reason = result.get("score"), result.get("reason")
                # A syntactically valid JSON reply that's still missing the
                # "score" key (seen with weaker/local models) must NOT be
                # marked "scored" -- that status means "score IS NOT NULL"
                # everywhere else reads it (core/queries.py's list_offers).
                # Same "scored" if score is not None else "new" convention as
                # annuler_postule/inclure_offre (chat_agent.py) -- 'new' keeps
                # it eligible for the next scoring run instead of a one-off
                # status string most of the app (tools/common.py's
                # STATUS_LABELS included) has never heard of.
                status = "scored" if score is not None else "new"
                if score is None and not reason:
                    reason = "scoring failed: model response had no 'score' field"
                score_str = str(score) if score is not None else "?"
                _log(f"[score] {i}/{total} - [{score_str:>3}] {title} -- {row['company'] or '?'} :: {reason}")
            except Exception as e:
                score, reason, status = None, f"scoring failed: {e}", "new"
                _log(f"[score] {i}/{total} - [ERR] {title} -- failed: {e}")

            conn.execute(
                "UPDATE offers SET score = ?, score_reason = ?, status = ? WHERE id = ?",
                (score, reason, status, row["id"]),
            )
            conn.commit()
            scored.append({**dict(row), "score": score, "score_reason": reason})
    return {"scored_offers": scored}


def run_scoring_now() -> list[dict]:
    """On-demand scoring, outside the scheduled discovery run -- for
    lancer_scoring (graphs/chat_agent.py, reachable through /ask on Discord
    and the terminal UI's Chat pane) and the terminal UI's own "Score now"
    button (tui/panes/offers.py). score_node reads its queue straight from
    the database (not from graph state), so it's already safe to call on its
    own like this; still capped at MAX_SCORE_PER_RUN per call, same as a
    scheduled run -- call it again if the backlog is bigger than that."""
    return score_node({})["scored_offers"]


LETTER_SCORE_THRESHOLD = int(os.environ.get("DISCOVERY_LETTER_SCORE_THRESHOLD", "80"))
MAX_LETTERS_PER_RUN = int(os.environ.get("DISCOVERY_MAX_LETTERS_PER_RUN", "5"))
OUTPUT_DIR = Path(__file__).resolve().parent.parent / os.environ.get("HOBOT_OUTPUT_DIR", "outputs")

LETTER_PROMPT = """Write a short (250-350 word), professional cover letter for this offer, on
behalf of {full_name}. Write it in the same language as the offer's own description.

Candidate profile:
- Skills: {skills}
- Target roles: {target_roles}

Offer:
- Title: {title}
- Company: {company}
- Description: {description}

Information found about the company (automatic web search -- BE CAREFUL: it
may be outdated, off-topic, or about an unrelated company with the same name.
Only use a detail if you're reasonably sure it's actually about THIS company
(industry/city line up); when in doubt, ignore it entirely rather than risk a
factual error in the letter):
{infos_entreprise}

Reply with ONLY JSON: {{"lettre": "the full text, with an appropriate greeting and closing, signed {full_name}"}}"""


def draft_letters_node(state: DiscoveryState) -> dict:
    """The agent's own initiative: above a score threshold, writes and saves
    a letter draft WITHOUT being asked -- never a send, just a file in
    outputs/ + a row in `applications` (status='draft'). Sweeps the entire
    database (not just this run) to also catch up on already-scored offers,
    capped per run to avoid triggering a burst of generations all at once."""
    profile = get_user_profile() or {"skills": [], "target_roles": [], "target_locations": []}
    full_name = profile.get("full_name") or "[Your name]"
    drafted = []
    with get_connection() as conn:
        candidates = conn.execute(
            """SELECT id, title, company, location, description, score FROM offers
               WHERE score >= ? AND status NOT IN ('applied', 'excluded', 'expired')
               AND id NOT IN (
                   SELECT offer_id FROM applications WHERE cover_letter_path IS NOT NULL
               )
               ORDER BY score DESC LIMIT ?""",
            (LETTER_SCORE_THRESHOLD, MAX_LETTERS_PER_RUN),
        ).fetchall()

        if candidates:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        for offer in candidates:
            try:
                hint = f"{offer['title'] or ''} {offer['location'] or ''}".strip()
                infos_entreprise = web_search.search_company(offer["company"] or "", hint=hint)
                result = chat_json(LETTER_PROMPT.format(
                    full_name=full_name,
                    skills=", ".join(profile["skills"]), target_roles=", ".join(profile["target_roles"]),
                    title=offer["title"] or "", company=offer["company"] or "",
                    description=(offer["description"] or "")[:3000],
                    infos_entreprise=infos_entreprise or "(no additional information found)",
                ))
                lettre = result.get("lettre", "")
                from tools.documents import generate_letter_pdf
                path = generate_letter_pdf(offer["id"], lettre, full_name=full_name)
                # upsert, not a blind INSERT: a CV may already have been
                # tailored for this offer on its own (adapter_cv via /ask)
                # before this run, which already created the shared
                # applications row -- see common.upsert_application.
                app_id = upsert_application(
                    conn, offer["id"], defaults={"status": "draft"}, cover_letter_path=str(path),
                )
                # Best-effort, never blocks a letter that already succeeded --
                # returns None (no CV uploaded via /profile yet, or the
                # tailoring attempt itself failed) far more often than not on
                # a fresh install, that's expected, not an error.
                from tools.cv_tailor import tailor_cv
                cv = tailor_cv(offer["id"], offer["title"] or "", offer["description"] or "")
                if cv:
                    conn.execute("UPDATE applications SET cv_path = ? WHERE id = ?", (str(cv), app_id))
                    _log(f"[cv] #{offer['id']} tailored -> {cv}")
                conn.commit()
                drafted.append(dict(offer))
                _log(f"[letter] #{offer['id']} {offer['title']} ({offer['score']}) -> {path}")
            except Exception as e:
                _log(f"[letter] #{offer['id']} -> failed: {e}")
    return {"drafted_letters": drafted}


HIGHLIGHT_SCORE_THRESHOLD = int(os.environ.get("DISCOVERY_HIGHLIGHT_SCORE", "70"))


def _n_new_for_stat_source(n_new_by_source: dict, source: str) -> int:
    """n_new_by_source (built by persist_new_node) is keyed by the exact,
    fine-grained value each offer's own `source` column got --
    "jobspy_indeed"/"jobspy_linkedin", "lba"/"lba_recruiter",
    "ats_greenhouse"/"ats_ashby"/"ats_lever" -- never the coarser label a
    fetch node's own stats use ("jobspy", "lba", "ats"). A plain dict lookup
    by that coarser label was silently always 0 whenever a source splits
    into sub-labels this way (caught while adding the "ats" source, which
    inherits the same split jobspy/lba already had) -- sum every key that
    either matches exactly or is that label's own sub-label instead."""
    return sum(n for key, n in n_new_by_source.items() if key == source or key.startswith(source + "_"))


def log_run_node(state: DiscoveryState) -> dict:
    n_new_by_source = state.get("n_new_by_source", {})
    with get_connection() as conn:
        for stat in state["stats"]:
            error = stat.get("error")
            # A non-null error alone isn't a circuit-breaker failure anymore:
            # fetch_*_node keeps partial results after a mid-loop exception,
            # so a source that still found something this run shouldn't have
            # its backoff doubled as if the whole attempt came back empty.
            has_error = bool(error) and not stat.get("n_found")
            backoff_until = compute_backoff_until(stat["source"], has_error=has_error)
            if backoff_until:
                _log(f"[circuit-breaker] {stat['source']} failing -> backed off until {backoff_until}")
            conn.execute(
                """INSERT INTO run_log (run_type, source, finished_at, n_found, n_new, errors, backoff_until, query)
                   VALUES ('discovery', ?, datetime('now'), ?, ?, ?, ?, ?)""",
                (stat["source"], stat.get("n_found", 0), _n_new_for_stat_source(n_new_by_source, stat["source"]),
                 error, backoff_until, stat.get("query")),
            )

    scored = state.get("scored_offers") or []
    letters = state.get("drafted_letters") or []
    if scored or letters:
        n_new = len(state.get("new_offers") or [])
        title = f"hobot -- {n_new} new offer(s), {len(scored)} scored"
        lines = []
        notified_ids: list[int] = []  # offers mentioned in THIS notification, see notify_all(offer_ids=...)
        embed = base_embed(f"{n_new} new offer(s) found", color=COLOR_ACTIVE)
        if letters:
            lines.append(f"{len(letters)} cover letter(s) drafted automatically (score >= {LETTER_SCORE_THRESHOLD}):")
            lines += [f"[{o['score']}] {o['title']} -- {o['company']}" for o in letters]
            notified_ids += [o["id"] for o in letters if o.get("id") is not None]
            embed.add_field(
                name=f"Letters drafted automatically (score >= {LETTER_SCORE_THRESHOLD})",
                value="\n".join(f"**{o['score']}** -- {o['title']} -- {o['company']}" for o in letters)[:1024],
                inline=False,
            )
        if scored:
            highlights = sorted(
                (o for o in scored if (o.get("score") or 0) >= HIGHLIGHT_SCORE_THRESHOLD),
                key=lambda o: o.get("score") or 0, reverse=True,
            )
            if highlights:
                lines.append("Best offers this run:")
                lines += [f"[{o['score']}] {o['title']} -- {o['company']}" for o in highlights[:5]]
                notified_ids += [o["id"] for o in highlights[:5] if o.get("id") is not None]
                embed.add_field(
                    name="Best offers this run",
                    value="\n".join(f"**{o['score']}** -- {o['title']} -- {o['company']}" for o in highlights[:5])[:1024],
                    inline=False,
                )
            elif not letters:
                best = max(scored, key=lambda o: o.get("score") or 0)
                lines.append(f"Best score this run: {best.get('score')} -- {best['title']} ({best['company']})")
                if best.get("id") is not None:
                    notified_ids.append(best["id"])
                embed.add_field(
                    name="Best score this run",
                    value=f"**{best.get('score')}** -- {best['title']} ({best['company']})",
                    inline=False,
                )
        skipped_irrelevant = state.get("skipped_irrelevant") or 0
        skipped_cross_source = state.get("skipped_cross_source") or 0
        skipped_formation = state.get("skipped_formation") or 0
        if skipped_irrelevant or skipped_cross_source or skipped_formation:
            filter_bits = []
            if skipped_irrelevant:
                filter_bits.append(f"{skipped_irrelevant} outside profile")
            if skipped_cross_source:
                filter_bits.append(f"{skipped_cross_source} cross-source duplicate(s)")
            if skipped_formation:
                filter_bits.append(f"{skipped_formation} training-organization intermediary(ies)")
            filter_line = "Dropped this run: " + ", ".join(filter_bits)
            lines.append(filter_line)
            embed.add_field(name="Dropped this run", value=", ".join(filter_bits), inline=False)
        notify_all(title, "\n".join(lines), embed=embed, kind="offers", offer_ids=notified_ids or None)
    return {}


def build_graph():
    graph = StateGraph(DiscoveryState)
    graph.add_node("fetch_jobspy", fetch_jobspy_node)
    graph.add_node("fetch_lba", fetch_lba_node)
    graph.add_node("fetch_adzuna", fetch_adzuna_node)
    graph.add_node("fetch_francetravail", fetch_francetravail_node)
    graph.add_node("fetch_labonneboite", fetch_labonneboite_node)
    graph.add_node("fetch_ats", fetch_ats_node)
    graph.add_node("dedupe", dedupe_node)
    graph.add_node("persist_new", persist_new_node)
    graph.add_node("score", score_node)
    graph.add_node("draft_letters", draft_letters_node)
    graph.add_node("log_run", log_run_node)

    for fetch_node in ("fetch_jobspy", "fetch_lba", "fetch_adzuna", "fetch_francetravail", "fetch_labonneboite", "fetch_ats"):
        graph.add_edge(START, fetch_node)
        graph.add_edge(fetch_node, "dedupe")
    graph.add_edge("dedupe", "persist_new")
    graph.add_edge("persist_new", "score")
    graph.add_edge("score", "draft_letters")
    graph.add_edge("draft_letters", "log_run")
    graph.add_edge("log_run", END)
    return graph.compile()


if __name__ == "__main__":
    init_db()
    app = build_graph()
    result = app.invoke({"raw_offers": [], "stats": [], "new_offers": [], "n_new_by_source": {}, "scored_offers": [], "drafted_letters": [], "skipped_irrelevant": 0, "skipped_cross_source": 0, "skipped_formation": 0})

    print(f"Sources: {result['stats']}")
    print(f"{len(result['new_offers'])} new offers found, {len(result['scored_offers'])} scored this run\n")

    for o in sorted(result["scored_offers"], key=lambda x: x.get("score") or 0, reverse=True):
        print(f"[{o.get('score')}] {o['title']} -- {o['company']} ({o['location']}) [{o['source']}]")
        print(f"    {o.get('score_reason')}\n")
