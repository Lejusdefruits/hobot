"""Adzuna connector -- GET /v1/api/jobs/{country}/search/{page}.

Adzuna covers multiple countries (the `country` parameter, "fr" by default
here). No key -> search_offers() returns an empty list, not an error."""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from core.api_usage import has_quota, log_call
from tools.common import make_offer

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

APP_ID = os.environ.get("ADZUNA_APP_ID", "")
APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
BASE_URL = "https://api.adzuna.com/v1/api/jobs"
# An agent running continuously only asks for recent listings (Adzuna supports
# this filter server-side, no need to redo it ourselves).
MAX_DAYS_OLD = int(os.environ.get("ADZUNA_MAX_DAYS_OLD", "6"))


def search_offers(what: str = "emploi", where: str = "Paris",
                   country: str = "fr", results_per_page: int = 15, page: int = 1,
                   max_days_old: int | None = None) -> list[dict]:
    if not APP_ID or not APP_KEY:
        return []
    # Free monthly quota (see core/api_usage.py) -- checked before the call
    # rather than discovered through an API refusal.
    if not has_quota("adzuna"):
        raise RuntimeError("Adzuna monthly quota reached -- call not attempted.")
    resp = requests.get(
        f"{BASE_URL}/{country}/search/{page}",
        params={
            "app_id": APP_ID,
            "app_key": APP_KEY,
            "what": what,
            "where": where,
            "results_per_page": results_per_page,
            "max_days_old": max_days_old if max_days_old is not None else MAX_DAYS_OLD,
            "content-type": "application/json",
        },
        timeout=15,
    )
    log_call("adzuna")
    resp.raise_for_status()
    return [_normalize(r) for r in resp.json().get("results", [])]


def _normalize(r: dict) -> dict:
    salary = None
    if r.get("salary_min") or r.get("salary_max"):
        salary = f"{int(r.get('salary_min') or 0)}-{int(r.get('salary_max') or 0)}€"
    return make_offer(
        source="adzuna",
        external_id=r.get("id"),
        title=r.get("title"),
        company=(r.get("company") or {}).get("display_name"),
        location=(r.get("location") or {}).get("display_name"),
        description=r.get("description"),
        url=r.get("redirect_url"),
        salary=salary,
        posted_date=r.get("created"),
    )


if __name__ == "__main__":
    results = search_offers()
    print(f"{len(results)} Adzuna results")
    for o in results[:8]:
        print(f"- {o['title']} -- {o['company']} ({o['location']})")
