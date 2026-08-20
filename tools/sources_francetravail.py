"""France Travail "Offres d'emploi v2" connector -- GET /partenaire/offresdemploi/v2/offres/search.

France only -- no value outside that scope. Complements La Bonne Alternance
(tools/sources_lba.py): LBA is an apprenticeship-only slice of the same
France Travail ecosystem, with a different offer pool (smaller but more
filtered) -- the two sources overlap partially, cross-source dedup
(tools/common.py::make_offer, dedup_key) absorbs the duplicates. No
credentials -> search_offers() returns an empty list, not an error.

Contract type is filtered server-side only if FRANCE_TRAVAIL_NATURE_CONTRAT
is set (.env) -- empty by default, so all contract types are included. E1,E2
= professionalization contract + apprenticeship contract (the two forms of
work-study in France); see France Travail's docs for other codes if a
different filter is needed."""
import os
from pathlib import Path

from dotenv import load_dotenv

from core.france_travail_auth import CLIENT_ID, get as ft_get
from core.locations import DEFAULT_LOCATION, resolve as resolve_location
from tools.common import make_offer

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SCOPE = "api_offresdemploiv2 o2dsoffre"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
NATURE_CONTRAT = os.environ.get("FRANCE_TRAVAIL_NATURE_CONTRAT", "")
# Documented limit on the francetravail.io portal: 10 calls/s. Safety margin
# so it's never approached (see core/france_travail_auth.py).
MAX_CALLS_PER_SECOND = 8


def search_offers(mots_cles: str = "emploi", ville: str = "Paris",
                   distance: int = 30, results_wanted: int = 20) -> list[dict]:
    if not CLIENT_ID:
        return []
    # France Travail's commune = an INSEE code, not lat/lon -- the API silently
    # ignores latitude/longitude if `commune` is absent (see core/locations.py).
    # A city unknown to the registry falls back to Paris, never an error that
    # breaks the run.
    commune = (resolve_location(ville) or DEFAULT_LOCATION)["commune_insee"]
    params = {
        "motsCles": mots_cles,
        "commune": commune,
        "distance": distance,
        "range": f"0-{max(results_wanted - 1, 0)}",
    }
    if NATURE_CONTRAT:
        params["natureContrat"] = NATURE_CONTRAT
    resp = ft_get(
        SEARCH_URL, scope=SCOPE, max_calls_per_second=MAX_CALLS_PER_SECOND,
        params=params,
    )
    if resp.status_code == 204:  # no results -- France Travail returns 204 with no body
        return []
    resp.raise_for_status()
    return [_normalize(o) for o in resp.json().get("resultats", [])]


def _normalize(o: dict) -> dict:
    lieu = o.get("lieuTravail") or {}
    salaire = (o.get("salaire") or {}).get("libelle")
    return make_offer(
        source="francetravail",
        external_id=o.get("id"),
        title=o.get("intitule"),
        company=(o.get("entreprise") or {}).get("nom"),
        location=lieu.get("libelle"),
        description=o.get("description"),
        url=(o.get("origineOffre") or {}).get("urlOrigine"),
        salary=salaire,
        posted_date=o.get("dateCreation"),
    )


if __name__ == "__main__":
    results = search_offers()
    print(f"{len(results)} France Travail results (Offres d'emploi v2)")
    for r in results[:8]:
        print(f"- {r['title']} -- {r['company']} ({r['location']})")
