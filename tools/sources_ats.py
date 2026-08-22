"""Greenhouse / Ashby / Lever connector -- per-company, not a keyword
search: each of these three ATS platforms exposes a free, unauthenticated
JSON feed of a SINGLE company's current openings, keyed by that company's
own "board slug" (visible in its public careers page URL, e.g.
boards.greenhouse.io/{slug}, jobs.ashbyhq.com/{slug}, jobs.lever.co/{slug}).
There is no "search every company on Greenhouse" endpoint -- resolve_slug()
below is the best-effort bridge from a plain company name to a working slug
on one of the three, by trying it directly against each API. No API key for
any of the three; all three verified live while building this.

Companies to check come from core/ats_watchlist.py (a static default list in
.env plus whatever's been added since through the chat tool), never a
keyword search the way the other sources in this project work -- LBA_ROME_CODES/
target_roles-style profile-driven search has no equivalent here at all.
"""
import html
import re

import requests

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


_PLATFORM_FETCHERS = {"greenhouse": _try_greenhouse, "ashby": _try_ashby, "lever": _try_lever}


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
    """Tries a company name against all three platforms with a few plausible
    slug spellings. Returns (platform, slug) for the first one that responds
    with a real board (HTTP 200, whether or not it currently has openings --
    a company's board can legitimately be empty right now), or None if
    nothing matched anywhere. Best-effort: a company on an ATS other than
    these three, or a slug that doesn't match any of the spelling
    conventions tried here, won't resolve -- reported as such by the caller,
    never silently guessed at."""
    for slug in _candidate_slugs(company):
        for platform, fetch in _PLATFORM_FETCHERS.items():
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


def normalize_job(platform: str, company: str, job: dict) -> dict:
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
    raise ValueError(f"Unknown ATS platform: {platform}")
