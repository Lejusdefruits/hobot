"""Snov.io connector -- fallback for when Hunter.io is used up or returns nothing.

Same principle as Hunter: emails already collected/verified by Snov.io across
the public web, never scraped by us. Free plan: 50 credits/month, 1 credit
per domain search regardless of how many results come back.

OAuth2 client_credentials auth (the access_token is valid for 1h, the
exchange itself doesn't cost a credit) -- cached in process memory to avoid
an unnecessary HTTP request on every call.
"""
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from core.api_usage import has_quota, log_call

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

USER_ID = os.environ.get("SNOV_USER_ID", "")
API_SECRET = os.environ.get("SNOV_API_SECRET", "")
BASE_URL = "https://api.snov.io"
TIMEOUT = 15

_token: str | None = None
_token_expires_at: float = 0.0


def _get_token() -> str | None:
    global _token, _token_expires_at
    if not USER_ID or not API_SECRET:
        return None
    if _token and time.time() < _token_expires_at:
        return _token
    resp = requests.post(
        f"{BASE_URL}/v1/oauth/access_token",
        data={"grant_type": "client_credentials", "client_id": USER_ID, "client_secret": API_SECRET},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    _token = data["access_token"]
    _token_expires_at = time.time() + data.get("expires_in", 3600) - 60  # safety margin
    return _token


def domain_search(domain: str, limit: int = 10) -> list[dict]:
    """Known emails for a domain. Empty list if nothing found, the domain is
    invalid, quota is used up, or the API isn't configured -- never an
    exception that propagates to the caller (same philosophy as
    sources_hunter.domain_search)."""
    if not domain:
        return []
    if not has_quota("snov"):
        return []
    try:
        token = _get_token()
        if not token:
            return []
        resp = requests.get(
            f"{BASE_URL}/v2/domain-emails-with-info",
            params={"access_token": token, "domain": domain, "type": "all", "limit": limit},
            timeout=TIMEOUT,
        )
        log_call("snov")
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            return []
        results = []
        for e in payload.get("data", []):
            if not e.get("email"):
                continue
            results.append({
                "email": e["email"],
                "prenom": e.get("first_name"),
                "nom": e.get("last_name"),
                "poste": e.get("position"),
                "type": "personal" if e.get("type") == "prospect" else "generic",
                "verifie": e.get("status"),  # "verified" / "notVerified" from the API
            })
        return results
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    domaine = sys.argv[1] if len(sys.argv) > 1 else "michelin.com"
    for e in domain_search(domaine):
        label = f"{e['prenom'] or ''} {e['nom'] or ''}".strip() or "(generic)"
        print(f"- {e['email']} -- {label} ({e['poste'] or '?'}) -- {e['verifie']}")
