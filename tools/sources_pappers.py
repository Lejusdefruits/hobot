"""Pappers connector (the official legal registry for French companies).

Goal: provide a REAL, verified contact (a legal representative, name +
official role) for an application or a follow-up, instead of guessing an HR
name or automating any visit to a social network. Official quota-limited API,
France only.

Two calls: /v2/recherche to find the SIREN from a company name, /v2/entreprise
for the full record (the list of legal representatives sits under the
"representants" key in the response).
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from core.api_usage import has_quota, log_call

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_TOKEN = os.environ.get("PAPPERS_API_TOKEN", "")
BASE_URL = "https://api.pappers.fr/v2"
TIMEOUT = 15


def rechercher_entreprise(nom: str) -> dict | None:
    """Search by name, returns the first result (siren + basic info) or None
    if nothing found. One Pappers credit -- see dirigeants()'s quota check,
    the only real caller."""
    if not API_TOKEN:
        return None
    resp = requests.get(
        f"{BASE_URL}/recherche",
        params={"api_token": API_TOKEN, "q": nom, "longueur": 1},
        timeout=TIMEOUT,
    )
    log_call("pappers")
    resp.raise_for_status()
    resultats = resp.json().get("resultats", [])
    return resultats[0] if resultats else None


def fiche_entreprise(siren: str) -> dict:
    """Full record (including legal representatives) for a given SIREN. A
    second Pappers credit, same accounting note as rechercher_entreprise."""
    resp = requests.get(
        f"{BASE_URL}/entreprise",
        params={"api_token": API_TOKEN, "siren": siren},
        timeout=TIMEOUT,
    )
    log_call("pappers")
    resp.raise_for_status()
    return resp.json()


def dirigeants(nom_entreprise: str) -> list[dict]:
    """Name + role of a company's legal representatives (president, manager,
    CEO...) -- real contacts pulled from the official registry, never
    guessed. Empty list if the company can't be identified, has no published
    representative, the API isn't configured, or the monthly quota (see
    core/api_usage.py) is already used up -- never an exception that
    propagates to the caller, a failed contact lookup should never block
    anything else (same philosophy as web_search.search_company)."""
    if not API_TOKEN or not nom_entreprise:
        return []
    if not has_quota("pappers"):
        return []
    try:
        base = rechercher_entreprise(nom_entreprise)
        if not base:
            return []
        fiche = fiche_entreprise(base["siren"])
        return [
            {"nom": r["nom_complet"], "role": r.get("qualite") or "legal representative"}
            for r in (fiche.get("representants") or [])
            if r.get("nom_complet")
        ]
    except Exception:
        return []


def verifier_sante(nom_entreprise: str) -> dict | None:
    """Public health signals for a company -- not a judgment, facts pulled
    from the official registry: ceased activity, an ongoing insolvency
    procedure (redressement/liquidation judiciaire), declared headcount.
    Lets a caller avoid drafting a cover letter for a company that doesn't
    really exist anymore. None if the company can't be identified or the API
    isn't configured -- never an exception propagating to the caller, same
    philosophy as dirigeants(). Same quota gate as dirigeants(): this is
    meant to run automatically (see tools/common.py::company_health_check),
    so it must never spend a credit past the monthly cap."""
    if not API_TOKEN or not nom_entreprise:
        return None
    if not has_quota("pappers"):
        return None
    try:
        base = rechercher_entreprise(nom_entreprise)
        if not base:
            return None
        fiche = fiche_entreprise(base["siren"])
        return {
            "cessee": bool(fiche.get("entreprise_cessee")),
            "procedure_collective": bool(fiche.get("procedure_collective_en_cours")),
            "effectif": fiche.get("effectif"),
        }
    except Exception:
        return None


def alerte_texte(sante: dict | None) -> str | None:
    """Short summary of verifier_sante()'s warning signals, or None if
    there's nothing concerning (or nothing to check at all)."""
    if not sante:
        return None
    alertes = []
    if sante.get("cessee"):
        alertes.append("company has ceased activity")
    if sante.get("procedure_collective"):
        alertes.append("ongoing insolvency procedure (redressement/liquidation)")
    return "; ".join(alertes) if alertes else None


if __name__ == "__main__":
    import sys
    nom = " ".join(sys.argv[1:]) or "Michelin"
    for d in dirigeants(nom):
        print(f"- {d['nom']} ({d['role']})")
    print("Health:", verifier_sante(nom))
