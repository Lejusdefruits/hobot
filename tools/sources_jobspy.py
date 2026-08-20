"""JobSpy connector -- multi-site scraping, no API key required.

JOBSPY_SITES (.env) controls which sites get queried. Indeed remains the most
reliable default: Glassdoor resists scraping particularly well (JobSpy's
location resolution against it fails consistently) -- try other sites at your
own risk if you want to widen the net (site_name also accepts "linkedin",
"zip_recruiter", "google").

Cadence: see DISCOVERY_HOURS_WEEKDAY/WEEKEND and JOBSPY_JITTER_MIN in .env
(spaced out more than the official APIs -- higher ban risk than a plain API
call).
"""
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


def _normalize(row: dict) -> dict:
    salary = None
    if row.get("min_amount") or row.get("max_amount"):
        currency = row.get("currency") or ""
        salary = f"{row.get('min_amount') or 0:.0f}-{row.get('max_amount') or 0:.0f}{currency}"
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
