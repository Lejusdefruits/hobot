"""Greenhouse / Ashby / Lever / SmartRecruiters / Workable / Rippling / Workday /
SuccessFactors connector -- per-company, not a keyword search: each of these
platforms exposes a free, unauthenticated feed of a SINGLE company's current
openings, keyed by that company's own "board slug" (visible in its public
careers page URL, e.g. boards.greenhouse.io/{slug}, jobs.ashbyhq.com/{slug},
jobs.lever.co/{slug}). There is no "search every company on Greenhouse"
endpoint -- resolve_slug() below is the best-effort bridge from a plain
company name to a working slug, by trying it directly against each API. No
API key for any of them.

Greenhouse/Ashby/Lever are hand-rolled here (each platform's raw JSON shape
parsed directly in normalize_job() below); SmartRecruiters/Workable/Rippling/
Workday/SuccessFactors instead go through ats-scrapers
(https://github.com/kalil0321/ats-scrapers), which already normalizes every
platform's raw response into one common ``Job`` shape -- no per-platform
parsing needed for those five, see the second half of normalize_job().

Workday and SuccessFactors are NOT in _GUESSABLE_PLATFORMS: both need the
company's full careers URL/hostname as their "slug", not a short guessable
name (see their own _try_workday/_try_successfactors docstrings below), so
resolve_slug()'s spelling-guess loop can't reach them -- add_company()
(core/ats_watchlist.py) still accepts them via its explicit platform/slug
override, reachable from surveiller_entreprise's own platform/slug
parameters (graphs/chat_agent.py).

Companies to check come from core/ats_watchlist.py (a static default list in
.env plus whatever's been added since through the chat tool), never a
keyword search the way the other sources in this project work -- LBA_ROME_CODES/
target_roles-style profile-driven search has no equivalent here at all.
"""
import html
import re

import requests
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers.base import get_scraper as _get_ats_scraper

TIMEOUT = 10

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"


def _strip_html(text: str | None) -> str:
    """Greenhouse's `content` field is HTML, and entity-escaped on top of
    that (a literal "&lt;p&gt;", not "<p>") -- unescape BEFORE stripping
    tags, not after, or the tag-strip regex never matches the escaped
    entities and unescaping afterward just reintroduces the raw tags
    unstripped (caught by actually checking the output against a real
    Greenhouse response, not assumed). The other two platforms already
    provide a plain-text field, nothing to do for them. A regex strip
    rather than a new HTML-parsing dependency: good enough for a job
    description, not meant to survive adversarial input."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", html.unescape(text)).strip()


def _try_greenhouse(slug: str) -> list[dict] | None:
    resp = requests.get(GREENHOUSE_URL.format(slug=slug), timeout=TIMEOUT)
    if resp.status_code != 200:
        return None
    return resp.json().get("jobs", [])


def _try_ashby(slug: str) -> list[dict] | None:
    resp = requests.get(ASHBY_URL.format(slug=slug), timeout=TIMEOUT)
    if resp.status_code != 200:
        return None
    return resp.json().get("jobs", [])


def _try_lever(slug: str) -> list[dict] | None:
    resp = requests.get(LEVER_URL.format(slug=slug), timeout=TIMEOUT)
    if resp.status_code != 200:
        return None
    return resp.json()  # bare array, unlike the other two -- see module docstring


def _try_ats_scrapers(ats: ATSType):
    """Builds a _try_* fetcher on top of ats-scrapers for one platform --
    unlike _try_greenhouse/_try_ashby/_try_lever (which distinguish a plain
    "not found" HTTP response from a network error, letting resolve_slug()
    itself catch requests.RequestException), ats-scrapers uses httpx and its
    own exception types (ScraperError, CompanyNotFoundError, ...) internally
    -- catching broadly here and returning None either way is simpler and
    safe: whether a guessed slug was wrong or the request itself failed,
    resolve_slug()'s guess loop should just move on to the next candidate."""
    def fetcher(slug: str) -> list | None:
        try:
            jobs = _get_ats_scraper(ats, slug).fetch()
        except Exception:
            return None
        return jobs

    return fetcher


_try_smartrecruiters = _try_ats_scrapers(ATSType.SMARTRECRUITERS)
_try_workable = _try_ats_scrapers(ATSType.WORKABLE)
_try_rippling = _try_ats_scrapers(ATSType.RIPPLING)


def _try_workday(slug: str) -> list | None:
    """slug here is the company's FULL careers URL
    (https://{company}.{instance}.myworkdayjobs.com/{site}), not a short
    guessable name -- see ats_scrapers.scrapers.workday's own module
    docstring for why. Never reached by resolve_slug()'s guess loop (not in
    _GUESSABLE_PLATFORMS below); only usable via an explicit platform/slug
    (add_company(), core/ats_watchlist.py)."""
    return _try_ats_scrapers(ATSType.WORKDAY)(slug)


def _try_successfactors(slug: str) -> list | None:
    """slug here is the tenant's recruiting-marketing hostname, not a short
    guessable name -- same reasoning and same guess-loop exclusion as
    _try_workday above."""
    return _try_ats_scrapers(ATSType.SUCCESSFACTORS)(slug)


_PLATFORM_FETCHERS = {
    "greenhouse": _try_greenhouse, "ashby": _try_ashby, "lever": _try_lever,
    "smartrecruiters": _try_smartrecruiters, "workable": _try_workable, "rippling": _try_rippling,
    "workday": _try_workday, "successfactors": _try_successfactors,
}

# Platforms resolve_slug() can try a spelling-guessed slug against -- see the
# module docstring for why workday/successfactors are excluded.
_GUESSABLE_PLATFORMS = {
    k: v for k, v in _PLATFORM_FETCHERS.items() if k not in ("workday", "successfactors")
}


def _candidate_slugs(company: str) -> list[str]:
    """A handful of plausible slug spellings for a company name -- the
    convention (all lowercase, spaces removed vs. hyphenated) isn't
    standardized across companies, so this tries the common ones instead of
    guessing once and giving up."""
    lowered = re.sub(r"[^a-z0-9 ]", "", company.strip().lower())
    candidates = [lowered.replace(" ", ""), "-".join(lowered.split()), lowered]
    seen: set = set()
    return [c for c in candidates if c and not (c in seen or seen.add(c))]


def resolve_slug(company: str) -> tuple[str, str] | None:
    """Tries a company name against every guessable platform
    (_GUESSABLE_PLATFORMS) with a few plausible slug spellings. Returns
    (platform, slug) for the first one that responds with a real board (a
    successful response, whether or not it currently has openings -- a
    company's board can legitimately be empty right now), or None if nothing
    matched anywhere. Best-effort: a company on an ATS not covered here, on
    Workday/SuccessFactors (which need the full URL/hostname, not a
    guessable slug -- pass that directly instead, see add_company() in
    core/ats_watchlist.py), or under a slug that doesn't match any of the
    spelling conventions tried here, won't resolve -- reported as such by
    the caller, never silently guessed at."""
    for slug in _candidate_slugs(company):
        for platform, fetch in _GUESSABLE_PLATFORMS.items():
            try:
                jobs = fetch(slug)
            except requests.RequestException:
                continue
            if jobs is not None:
                return platform, slug
    return None


def fetch_company_jobs(platform: str, slug: str) -> list[dict]:
    """Raw postings for one already-resolved (platform, slug), normalized
    into hobot's offer shape (tools.common.make_offer) -- not here, same
    separation tools/sources_lba.py and the other connectors already follow.
    Returns [] (never raises) on a transient failure -- the discovery graph's
    fetch node decides what that means for backoff, not this function."""
    fetch = _PLATFORM_FETCHERS.get(platform)
    if fetch is None:
        return []
    try:
        jobs = fetch(slug)
    except requests.RequestException:
        return []
    return jobs or []


# platform key -> hobot source tag, for the five ats-scrapers-backed
# platforms (see the module docstring) -- their Job objects are already
# fully normalized (title/company/location/description/url/posted_at) by
# ats-scrapers itself, so no per-platform field parsing is needed the way
# greenhouse/ashby/lever below still require.
_ATS_SCRAPERS_SOURCE_TAG = {
    "smartrecruiters": "ats_smartrecruiters", "workable": "ats_workable", "rippling": "ats_rippling",
    "workday": "ats_workday", "successfactors": "ats_successfactors",
}


def normalize_job(platform: str, company: str, job) -> dict:
    from tools.common import make_offer

    if platform == "greenhouse":
        return make_offer(
            source="ats_greenhouse", external_id=str(job.get("id")), title=job.get("title"),
            company=job.get("company_name") or company, location=(job.get("location") or {}).get("name"),
            description=_strip_html(job.get("content")), url=job.get("absolute_url"),
            posted_date=job.get("updated_at"),
        )
    if platform == "ashby":
        return make_offer(
            source="ats_ashby", external_id=str(job.get("id")), title=job.get("title"),
            company=company, location=job.get("location"),
            description=job.get("descriptionPlain") or "", url=job.get("jobUrl") or job.get("applyUrl"),
            posted_date=job.get("publishedAt"),
        )
    if platform == "lever":
        categories = job.get("categories") or {}
        return make_offer(
            source="ats_lever", external_id=str(job.get("id")), title=job.get("text"),
            company=company, location=categories.get("location"),
            description=job.get("descriptionPlain") or "", url=job.get("hostedUrl"),
            posted_date=str(job.get("createdAt") or ""),
        )
    if platform in _ATS_SCRAPERS_SOURCE_TAG:
        return make_offer(
            source=_ATS_SCRAPERS_SOURCE_TAG[platform], external_id=job.ats_id, title=job.title,
            company=job.company or company, location=job.location,
            description=job.description or "", url=str(job.url),
            posted_date=job.posted_at.isoformat() if job.posted_at else None,
        )
    raise ValueError(f"Unknown ATS platform: {platform}")
