"""Maddyness/Frenchweb RSS -- French startup-news feeds checked periodically
for a company that just raised funding, as a lead for core/ats_watchlist.py
(a company that just closed a round is a reasonable bet to be hiring soon,
even before it has an open posting) -- see tools/funding_check.py, which
turns a candidate headline into an actual watchlist proposal. Free, no key,
both verified live while building this.

Maddyness is the primary source: a general feed, but low-volume (roughly one
article a day) and startup/tech-focused, so a real funding headline is easy
to spot. Frenchweb is a wider net (dozens of items a week) with a lot more
non-funding content mixed in (sponsored posts, opinion pieces) -- the same
keyword pre-filter below is what keeps checking it affordable without an LLM
call per item.
"""
import html
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests

TIMEOUT = 10

FEEDS = {
    "maddyness": "https://www.maddyness.com/feed/",
    "frenchweb": "https://www.frenchweb.fr/feed",
}

# A rough, deterministic pre-filter before ever spending an LLM call on a
# headline -- catches both French and English funding-round phrasing. Not
# meant to be precise (a false positive here just costs one cheap LLM call
# in tools/funding_check.py, which itself has the real judgment call).
FUNDING_KEYWORDS = (
    "lève", "leve", "levée", "levee", "million", "millions", "financement",
    "tour de table", "series a", "series b", "series c", "seed",
    "valorisation", "raises", "funding", "investit", "investissement",
)


def _matches_funding_keywords(title: str) -> bool:
    lowered = title.lower()
    return any(kw in lowered for kw in FUNDING_KEYWORDS)


def _parse_item(item_xml: str, source: str) -> dict | None:
    title = re.search(r"<title>(.*?)</title>", item_xml, re.DOTALL)
    link = re.search(r"<link>(.*?)</link>", item_xml, re.DOTALL)
    pub_date = re.search(r"<pubDate>(.*?)</pubDate>", item_xml, re.DOTALL)
    if not (title and link):
        return None
    return {
        "title": html.unescape(title.group(1).strip()),
        "link": link.group(1).strip(),
        "pub_date": pub_date.group(1).strip() if pub_date else None,
        "source": source,
    }


def fetch_funding_candidates(since: datetime | None = None) -> list[dict]:
    """Every RSS item across both feeds whose title matches a funding
    keyword and (if `since` is given) was published after it. A feed that's
    unreachable is skipped, not fatal -- the other one still gets checked.
    Deterministic pre-filter only: turning a candidate headline into an
    actual company name is tools/funding_check.py's job, not this one's."""
    candidates = []
    for source, url in FEEDS.items():
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException:
            continue
        for item_xml in re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL):
            item = _parse_item(item_xml, source)
            if not item or not _matches_funding_keywords(item["title"]):
                continue
            if since and item["pub_date"]:
                try:
                    pub_dt = parsedate_to_datetime(item["pub_date"]).replace(tzinfo=None)
                    if pub_dt <= since:
                        continue
                except (TypeError, ValueError):
                    pass  # unparseable date -- keep the item rather than silently drop it
            candidates.append(item)
    return candidates
