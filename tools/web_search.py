"""Web search via a self-hosted SearXNG instance -- used ONLY to enrich cover
letters with real information about the company (what it does, recent news,
...), never bulk scraping.

Anti-ban: SearXNG queries Google/Bing/DuckDuckGo/Qwant on our behalf. No
pagination, no repeated queries for the same company (one letter = one
search), a short timeout, and the same per-source circuit breaker as the
other connectors (core/circuit_breaker.py): if a query fails, back off
instead of pushing harder -- never an exception that propagates to the
caller, a failed search should never block writing the letter.
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from core.circuit_breaker import compute_backoff_until, is_backed_off
from core.db import get_connection

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SEARXNG_HOST = os.environ.get("SEARXNG_HOST", "http://localhost:8888")
TIMEOUT = int(os.environ.get("SEARXNG_TIMEOUT_SECONDS", "10"))
MAX_RESULTS = 3

SOURCE = "searxng"


def _log(n_found: int, error: str | None) -> None:
    backoff_until = compute_backoff_until(SOURCE, has_error=bool(error))
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO run_log (run_type, source, finished_at, n_found, n_new, errors, backoff_until)
               VALUES ('web_search', ?, datetime('now'), ?, 0, ?, ?)""",
            (SOURCE, n_found, error, backoff_until),
        )


def search(query: str) -> list[dict]:
    """One SearXNG query, raw results (title/url/content). Empty list if
    nothing found, SearXNG is backed off (circuit breaker), or on failure --
    never an exception."""
    if not query.strip():
        return []
    backed_off, _until = is_backed_off(SOURCE)
    if backed_off:
        return []
    try:
        resp = requests.get(
            f"{SEARXNG_HOST}/search",
            params={"q": query, "format": "json", "categories": "general"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])[:MAX_RESULTS]
        _log(len(results), None)
        return [
            {"title": r.get("title"), "url": r.get("url"), "content": r.get("content")}
            for r in results
        ]
    except Exception as e:
        _log(0, str(e))
        return []


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
