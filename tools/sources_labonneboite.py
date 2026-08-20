"""La Bonne Boite v2 connector -- GET /partenaire/labonneboite/v2/company/.

The general-purpose equivalent of La Bonne Alternance's "recruiters"
(tools/sources_lba.py, source lba_recruiter): ranks companies by hiring
potential (ROME code + geographic area), no published listing behind it --
a lead for a spontaneous application, not a job ad.

WARNING, conditional access (documented by France Travail): subscribing to
this API on francetravail.io isn't enough, an additional manual approval is
required on France Travail's side. Until it's granted, the call returns 403
with a `Www-Authenticate: ... error="insufficient_scope"` header -- treated
here as a normal source error (surfaced in stats/run_log, circuit breaker),
never an exception that breaks the run. No credentials -> search_offers()
returns an empty list, not an error.

Endpoint/parameters (rome_codes, latitude, longitude, distance) are based on
the publicly documented v1 version (v2 has no complete public docs)."""
from pathlib import Path

from dotenv import load_dotenv

from core.france_travail_auth import CLIENT_ID, get as ft_get
from core.locations import DEFAULT_LOCATION
from tools.common import make_offer

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SCOPE = "api_labonneboitev2"
SEARCH_URL = "https://api.francetravail.io/partenaire/labonneboite/v2/company/"
# Documented limit on the francetravail.io portal: 2 calls/s (the strictest
# of the France Travail Connect APIs) -- safety margin, see core/france_travail_auth.py.
MAX_CALLS_PER_SECOND = 1.5


class AccessNotGranted(Exception):
    """Raised when France Travail hasn't approved access to this API yet
    (403 insufficient_scope) -- distinct from a real outage, so the caller
    can log a clear message instead of a generic error trace."""


def search_offers(romes: str = "M1805", latitude: float = DEFAULT_LOCATION["lat"],
                   longitude: float = DEFAULT_LOCATION["lon"], distance: int = 30) -> list[dict]:
    if not CLIENT_ID:
        return []
    resp = ft_get(
        SEARCH_URL, scope=SCOPE, max_calls_per_second=MAX_CALLS_PER_SECOND,
        params={"rome_codes": romes, "latitude": latitude, "longitude": longitude, "distance": distance},
    )
    if resp.status_code == 403:
        raise AccessNotGranted(
            "La Bonne Boite v2: access not yet approved by France Travail "
            "(subscribed on francetravail.io but manual approval is still pending)."
        )
    resp.raise_for_status()
    return [_normalize(c) for c in resp.json().get("companies", [])]


def _normalize(c: dict) -> dict:
    naf_label = c.get("naf_text") or c.get("naf") or ""
    description = (
        f"Company flagged by La Bonne Boite as a potential employer "
        f"({naf_label}). No listing published: this is a spontaneous-"
        f"application lead."
    )
    return make_offer(
        source="labonneboite",
        external_id=c.get("siret"),
        title=f"Spontaneous application -- {naf_label}" if naf_label else "Spontaneous application",
        company=c.get("name"),
        location=c.get("city") or c.get("address"),
        description=description,
        url=c.get("url"),
    )


if __name__ == "__main__":
    try:
        results = search_offers()
        print(f"{len(results)} La Bonne Boite results")
        for r in results[:8]:
            print(f"- {r['title']} -- {r['company']} ({r['location']})")
    except AccessNotGranted as e:
        print(e)
