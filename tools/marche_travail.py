"""France Travail "Marche du travail v1" client -- NOT an offer source. Only
used as diagnostic context for choose_keywords_node (graphs/discovery_graph.py):
a market-tension indicator per profession/zone, to help tell a probably-bad
keyword apart from a genuinely tight market for that profession/zone.

The exact OAuth2 scope name for this API could not be reliably found (not
from the public docs, nor by guessing against the real API -- several dozen
variants tried, all rejected as invalid_scope). Unlike La Bonne Boite (a
recognized scope, access just not approved yet -- insufficient_scope), here
the scope itself isn't recognized at all: the exact name is visible via the
"eye" icon on the "Marche du travail v1" line at https://francetravail.io
(the project's account there), to set in FRANCE_TRAVAIL_MARCHE_SCOPE (.env)
once found -- as long as that variable is empty, tension_indicator() always
returns None without ever calling the API, same as any other source that's
simply not configured."""
import os
from pathlib import Path

from dotenv import load_dotenv

from core.france_travail_auth import get as ft_get

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SCOPE = os.environ.get("FRANCE_TRAVAIL_MARCHE_SCOPE", "")
# Base to confirm alongside the scope -- placeholder consistent with the URL
# scheme of the two other already-verified France Travail Connect APIs.
BASE_URL = "https://api.francetravail.io/partenaire/infotravail/v1"
# Documented limit on the francetravail.io portal (project account): 10 calls/s.
MAX_CALLS_PER_SECOND = 8

_warned = False


def tension_indicator(rome_code: str, zone_libelle: str) -> str | None:
    """Short text summary of the market tension for this profession/zone, or
    None if unavailable (scope not configured, network failure, nothing
    found) -- never raises, same defensive philosophy as tools/web_search.py."""
    global _warned
    if not SCOPE:
        if not _warned:
            print("[marche_travail] FRANCE_TRAVAIL_MARCHE_SCOPE not configured -- tension indicator disabled.", flush=True)
            _warned = True
        return None
    try:
        resp = ft_get(f"{BASE_URL}/indicateurs", scope=SCOPE, max_calls_per_second=MAX_CALLS_PER_SECOND,
                       params={"codeRome": rome_code, "zone": zone_libelle})
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get("libelleTension") or data.get("commentaire") or None
    except Exception:
        return None


if __name__ == "__main__":
    print(tension_indicator("M1805", "Paris") or "Unavailable (see the module docstring).")
