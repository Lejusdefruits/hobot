"""La Bonne Alternance (France Travail) connector -- GET /job/v1/search.

Apprenticeship only, France only -- no value outside that scope. No key ->
search_offers() returns an empty list, not an error.

The response holds two distinct lists:
- "jobs"       : published listings (France Travail partners, etc.)
- "recruiters" : companies flagged as potential apprenticeship employers but
                 with no published listing -> a spontaneous-application lead.
Both get normalized here, with a different `source` value to tell them apart
(see tools/common.py::SPONTANEOUS_LEAD_SOURCES).
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from core.locations import DEFAULT_LOCATION
from tools.common import make_offer

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

LBA_API_KEY = os.environ.get("LBA_API_KEY", "")
BASE_URL = "https://api.apprentissage.beta.gouv.fr/api"


def search_offers(romes: str | None = None, latitude: float = DEFAULT_LOCATION["lat"],
                   longitude: float = DEFAULT_LOCATION["lon"], radius: int = 30,
                   target_diploma_level: str | None = None) -> list[dict]:
    """romes: ROME code(s) (e.g. "M1805" = IT studies and development).
    target_diploma_level: "3" to "7" (3 = vocational, ... 7 = 5 years post-secondary)."""
    if not LBA_API_KEY:
        return []
    params = {"latitude": latitude, "longitude": longitude, "radius": radius}
    if romes:
        params["romes"] = romes
    if target_diploma_level:
        params["target_diploma_level"] = target_diploma_level

    resp = requests.get(
        f"{BASE_URL}/job/v1/search",
        params=params,
        headers={"authorization": f"Bearer {LBA_API_KEY}"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    offers = [_normalize_job(j) for j in data.get("jobs", [])]
    offers += [_normalize_recruiter(r) for r in data.get("recruiters", [])]
    return offers


def _contact_suffix(workplace: dict, apply_: dict) -> str:
    """Contact line to append to the description if the API provides one -- a
    minority of "spontaneous-application" recruiter entries have a phone
    number in apply.phone (make_offer/the offers schema has no dedicated
    column for an optional field available on so few offers, so it's added
    straight into the text instead of extending the schema). No LBA source
    provides an email."""
    parts = []
    if apply_.get("phone"):
        parts.append(f"Contact phone (via La Bonne Alternance): {apply_['phone']}.")
    if workplace.get("website"):
        parts.append(f"Website: {workplace['website']}.")
    return (" " + " ".join(parts)) if parts else ""


def _normalize_job(job: dict) -> dict:
    offer, workplace, apply_ = job.get("offer", {}), job.get("workplace", {}), job.get("apply", {})
    return make_offer(
        source="lba",
        external_id=job.get("identifier", {}).get("id"),
        title=offer.get("title"),
        company=workplace.get("brand") or workplace.get("name") or workplace.get("legal_name"),
        location=workplace.get("location", {}).get("address"),
        description=(offer.get("description") or "") + _contact_suffix(workplace, apply_),
        url=apply_.get("url"),
    )


def _normalize_recruiter(recruiter: dict) -> dict:
    workplace, apply_ = recruiter.get("workplace", {}), recruiter.get("apply", {})
    naf_label = workplace.get("domain", {}).get("naf", {}).get("label") or ""
    description = (
        f"Company flagged by La Bonne Alternance as a potential apprenticeship "
        f"employer ({naf_label}). No listing published: this is a spontaneous-"
        f"application lead."
    )
    return make_offer(
        source="lba_recruiter",
        external_id=recruiter.get("identifier", {}).get("id"),
        title=f"Spontaneous application -- {naf_label}" if naf_label else "Spontaneous application",
        company=workplace.get("brand") or workplace.get("name") or workplace.get("legal_name"),
        location=workplace.get("location", {}).get("address"),
        description=description + _contact_suffix(workplace, apply_),
        url=apply_.get("url"),
    )


if __name__ == "__main__":
    results = search_offers(romes="M1805")
    print(f"{len(results)} results (listings + recruiters) around Paris, ROME M1805")
    for o in results[:8]:
        print(f"- [{o['source']}] {o['title']} -- {o['company']} ({o['location']})")
