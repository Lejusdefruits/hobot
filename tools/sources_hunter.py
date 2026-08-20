"""Hunter.io connector (verified email search by domain).

Emails already collected and verified by Hunter across the public web
(sources cited, with a last-seen date), never scraped by us -- a way to find
a real contact without automating any visit to a social network or third-
party site. Free plan: 50 searches/month, 1 credit per call regardless of
how many results come back.
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_KEY = os.environ.get("HUNTER_API_KEY", "")
BASE_URL = "https://api.hunter.io/v2"
TIMEOUT = 15


def domain_search(domain: str, limit: int = 10) -> list[dict]:
    """Known emails for a domain, sorted by Hunter's own confidence score,
    highest first. Empty list if nothing found, the domain is invalid, quota
    is used up, or the API isn't configured -- never an exception that
    propagates to the caller (same philosophy as sources_pappers.dirigeants/
    web_search.search: a failed contact lookup should never block anything
    else)."""
    if not API_KEY or not domain:
        return []
    try:
        resp = requests.get(
            f"{BASE_URL}/domain-search",
            params={"domain": domain, "api_key": API_KEY, "limit": limit},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        emails = resp.json().get("data", {}).get("emails", [])
        return [
            {
                "email": e["value"],
                "prenom": e.get("first_name"),
                "nom": e.get("last_name"),
                "poste": e.get("position") or e.get("position_raw"),
                "confiance": e.get("confidence"),
                "type": e.get("type"),  # "personal" (a real person) or "generic" (e.g. contact@)
                "verifie": (e.get("verification") or {}).get("status"),
            }
            for e in emails
            if e.get("value")
        ]
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    domaine = sys.argv[1] if len(sys.argv) > 1 else "michelin.com"
    for e in domain_search(domaine):
        label = f"{e['prenom'] or ''} {e['nom'] or ''}".strip() or "(generic)"
        print(f"- {e['email']} -- {label} ({e['poste'] or '?'}) -- confidence {e['confiance']}% "
              f"-- verified: {e['verifie']}")
