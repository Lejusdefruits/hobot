"""JobSpy connector -- multi-site scraping, no API key required.

JOBSPY_SITES (.env) controls which sites get queried. Indeed remains the most
reliable default. Glassdoor resists scraping particularly well -- JobSpy's
own location-resolution request against it 400s ("location not parsed"),
confirmed not a hobot-side config mistake: upstream issue
[#279](https://github.com/speedyapply/JobSpy/issues/279) (open since 2025-05)
and a real, minimal fix already proposed in
[#384](https://github.com/speedyapply/JobSpy/pull/384) (a stray trailing
slash in get_glassdoor_url doubling up with the endpoint path it's appended
to). Verified that fix live (2026-08-26) though: even with the double-slash
corrected, the request now gets a straight HTTP 403 from a Cloudflare
"Attention Required" challenge page -- Glassdoor is separately blocking via
WAF, so #384 is a legitimate bug fix but doesn't by itself restore results
here. "google" was tested live (2026-08-21, re-confirmed 2026-08-26): it
returns 0 results on every query tried, French and English, including
JobSpy's own example query verbatim. Confirmed this time as Google's own
automated-traffic fallback response (an `emsg=SG_REL` degraded-access shell
page, not a real search results page -- no job data present at all, not
even in a form a fixed selector could extract), not a stale parser --
matches JobSpy's own [#284](https://github.com/speedyapply/JobSpy/issues/284)
(open since 2025-06, same symptom independently reported by five other
users), no open PR against it. Left as a supported value (site_name accepts
it) in case it starts working again, but don't expect results from either
today -- both are anti-automation blocking on the site's side at this point,
not something a JobSpy code change can fix. "linkedin" was also tested live
the same day and DOES work --
real results, no CAPTCHA -- but JobSpy's own upstream docs warn it rate-limits
hard (around page 10) without a paid proxy, the same wall Glassdoor already
hit here. Adding it to JOBSPY_SITES is a real, accepted trade-off: more
volume now, real risk it stops working later without any special handling
needed (a source going quiet just means fewer results from it, same as any
other JobSpy site failing).

Cadence: see DISCOVERY_HOURS_WEEKDAY/WEEKEND and JOBSPY_JITTER_MIN in .env
(spaced out more than the official APIs -- higher ban risk than a plain API
call).
"""
import math
import os
from pathlib import Path

from dotenv import load_dotenv
from jobspy import scrape_jobs

from tools.common import make_offer

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SITES = [s.strip() for s in os.environ.get("JOBSPY_SITES", "indeed").split(",") if s.strip()]
COUNTRY = os.environ.get("JOBSPY_COUNTRY", "France")
RESULTS_PER_RUN = int(os.environ.get("JOBSPY_RESULTS_PER_RUN", "25"))
# An agent running continuously only wants recent listings, not the whole
# history re-read on every check. Set a bit wider than the gap between two
# scheduled runs so a missed run doesn't lose an offer.
HOURS_OLD = int(os.environ.get("JOBSPY_HOURS_OLD", "48"))


def search_offers(search_term: str = "emploi", location: str = "France",
                   hours_old: int | None = None, results_wanted: int | None = None) -> list[dict]:
    df = scrape_jobs(
        site_name=SITES,
        search_term=search_term,
        location=location,
        country_indeed=COUNTRY.lower(),
        results_wanted=results_wanted or RESULTS_PER_RUN,
        hours_old=hours_old if hours_old is not None else HOURS_OLD,
    )
    if df is None or df.empty:
        return []
    return [_normalize(row) for row in df.to_dict("records")]


def _clean_amount(value) -> float | None:
    """pandas turns a missing numeric column into float('nan'), not None --
    and bool(nan) is True in Python, so a plain truthiness/`or` check treats
    a missing amount as present and stringifies it as the literal "nan"."""
    try:
        return None if value is None or math.isnan(value) else float(value)
    except TypeError:
        return None


def _normalize(row: dict) -> dict:
    salary = None
    min_amount, max_amount = _clean_amount(row.get("min_amount")), _clean_amount(row.get("max_amount"))
    if min_amount or max_amount:
        currency = row.get("currency") or ""
        currency = "" if isinstance(currency, float) and math.isnan(currency) else currency
        salary = f"{min_amount or 0:.0f}-{max_amount or 0:.0f}{currency}"
    site = row.get("site") or (SITES[0] if len(SITES) == 1 else "jobspy")
    return make_offer(
        source=f"jobspy_{site}",
        external_id=row.get("id"),
        title=row.get("title"),
        company=row.get("company"),
        location=row.get("location"),
        description=row.get("description"),
        url=row.get("job_url"),
        salary=salary,
        posted_date=str(row.get("date_posted") or ""),
    )


if __name__ == "__main__":
    results = search_offers()
    print(f"{len(results)} JobSpy results ({', '.join(SITES)})")
    for o in results[:10]:
        print(f"- {o['title']} -- {o['company']} ({o['location']})")
