"""Web search for company enrichment (cover letters, /ask lookups) -- Tavily
if TAVILY_API_KEY is set, a self-hosted SearXNG instance otherwise. Used ONLY
to enrich cover letters with real information about the company (what it
does, recent news, ...), never bulk scraping.

Tavily is a real, paid-by-credit search API: no ban risk, so it's tried
FIRST whenever configured (1000 free credits/month, see core/api_usage.py).
SearXNG is the fallback for whoever hasn't set a Tavily key -- but it queries
Google/Bing/DuckDuckGo/Qwant *on our behalf*, which carries a real ban risk
on repeated use. The anti-ban rules below (no pagination, no repeated
queries for the same company, short timeout, circuit breaker) matter for
that path specifically; Tavily doesn't need them for the same reason it
doesn't need them anywhere else client code calls it. Either way: a failed
search should never block writing the letter, so nothing here ever raises.
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from core.api_usage import has_quota, log_call
from core.circuit_breaker import compute_backoff_until, is_backed_off
from core.db import get_connection

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_TIMEOUT = int(os.environ.get("TAVILY_TIMEOUT_SECONDS", "10"))

SEARXNG_HOST = os.environ.get("SEARXNG_HOST", "http://localhost:8888")
TIMEOUT = int(os.environ.get("SEARXNG_TIMEOUT_SECONDS", "10"))
MAX_RESULTS = 3


def _log(source: str, n_found: int, error: str | None) -> None:
    backoff_until = compute_backoff_until(source, has_error=bool(error))
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO run_log (run_type, source, finished_at, n_found, n_new, errors, backoff_until)
               VALUES ('web_search', ?, datetime('now'), ?, 0, ?, ?)""",
            (source, n_found, error, backoff_until),
        )


def _normalize(results: list[dict]) -> list[dict]:
    return [{"title": r.get("title"), "url": r.get("url"), "content": r.get("content")} for r in results[:MAX_RESULTS]]


def _tavily_available() -> bool:
    return bool(TAVILY_API_KEY) and not is_backed_off("tavily")[0] and has_quota("tavily")


def _search_tavily(query: str) -> list[dict] | None:
    """None if Tavily couldn't answer this query at all (caller falls back
    to SearXNG in that case) -- a list (possibly empty) means the call
    completed, and an empty answer is a real "nothing found," not a reason
    to fall through to the bannable SearXNG path."""
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            json={"query": query, "max_results": MAX_RESULTS},
            timeout=TAVILY_TIMEOUT,
        )
    except Exception as e:
        # Never sent / no response -- no credit burned, nothing to log_call for.
        _log("tavily", 0, str(e))
        return None
    log_call("tavily")  # a response came back, credit spent whether or not the query succeeded
    try:
        resp.raise_for_status()
        results = _normalize(resp.json().get("results", []))
    except Exception as e:
        _log("tavily", 0, str(e))
        return None
    _log("tavily", len(results), None)
    return results


def _search_searxng(query: str) -> list[dict]:
    if is_backed_off("searxng")[0]:
        return []
    try:
        resp = requests.get(
            f"{SEARXNG_HOST}/search",
            params={"q": query, "format": "json", "categories": "general"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        results = _normalize(resp.json().get("results", []))
        _log("searxng", len(results), None)
        return results
    except Exception as e:
        _log("searxng", 0, str(e))
        return []


def search(query: str) -> list[dict]:
    """One query, raw results (title/url/content). Empty list if nothing
    found or on failure -- never an exception. Tavily first when configured
    and usable; SearXNG only as a fallback (unconfigured/backed-off/quota-
    exhausted Tavily, or a Tavily call that actually failed)."""
    if not query.strip():
        return []
    if _tavily_available():
        results = _search_tavily(query)
        if results is not None:
            return results
    return _search_searxng(query)


def search_company(company: str, hint: str = "") -> str:
    """Formats a company search, ready to inject into a cover letter prompt.
    hint = optional context (job title, industry...) to narrow the query.
    Empty string if there's nothing usable."""
    company = (company or "").strip()
    if not company:
        return ""
    results = search(f"{company} {hint}".strip())
    lines = [f"- {r['title']} : {r['content']}" for r in results if r.get("content")]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Michelin Clermont-Ferrand"
    for r in search(q):
        print(f"- {r['title']}\n  {r['url']}\n  {r['content']}\n")
