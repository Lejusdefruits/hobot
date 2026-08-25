"""Company contact research -- shared logic between the chat tool
rechercher_contacts_entreprise (graphs/chat_agent.py, /ask) and the automatic
trigger on well-scored "spontaneous application" leads (draft_letters_node,
graphs/discovery_graph.py).

Extracted into its own module rather than left in chat_agent.py and imported
from discovery_graph.py: chat_agent.py builds a LangGraph agent + LLM client
AT MODULE LEVEL (import time), so `import graphs.chat_agent` from the
scheduled pipeline would spin up a second agent/client for nothing just to
reuse one function.
"""
import re

from core.db import get_connection
from tools import common, sources_hunter, sources_pappers, sources_snov, web_search

_DOMAIN_SKIP = ("linkedin.com", "societe.com", "pappers.fr", "indeed.com", "google.com",
                "facebook.com", "welcometothejungle.com", "glassdoor", "gouv.fr",
                "pagesjaunes.fr", "pagesblanches", "annuaire-entreprises", "verif.com",
                "infogreffe.fr", "manageo.fr", "kompass.com")


def recherche_contact(entreprise: str, ville: str = "", offer_id: int | None = None) -> dict:
    """Looks for a real contact at a company (Pappers + LBA phone already in
    the database + a web search for the official site + Hunter.io/Snov.io on
    that domain). See graphs/chat_agent.py::rechercher_contacts_entreprise
    for the detail of each source -- same logic, just moved here and shaped
    as a dict so non-LLM code (draft_letters_node) can act on the counts
    instead of parsing text meant for the model.

    Returns {"text": str, "domaine": str|None, "n_dirigeants": int,
    "n_emails": int, "from_cache": bool} -- from_cache=True if offer_id
    already had contacts recorded (no API call redone, no Hunter/Snov
    credit spent)."""
    if offer_id:
        with get_connection() as conn:
            existing = conn.execute(
                "SELECT email, nom, poste, confiance, verifie, source FROM company_contacts "
                "WHERE offer_id = ? ORDER BY confiance DESC NULLS LAST", (offer_id,),
            ).fetchall()
            domain_row = conn.execute("SELECT company_domain FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if existing:
            lines = [f"Contacts already saved for offer #{offer_id} (no new API call):"]
            n_emails = 0
            for c in existing:
                who = f"{c['nom']}" if c["nom"] else "generic address"
                role = f" -- {c['poste']}" if c["poste"] else ""
                email_part = f"{c['email']} -- " if c["email"] else ""
                confidence = f", {c['confiance']}% confidence" if c["confiance"] is not None else ""
                lines.append(f"- {email_part}{who}{role} (source {c['source']}{confidence})")
                if c["email"]:
                    n_emails += 1
            return {
                "text": "\n".join(lines), "domaine": domain_row["company_domain"] if domain_row else None,
                "n_dirigeants": len(existing) - n_emails, "n_emails": n_emails, "from_cache": True,
            }

    lines = []
    reps = sources_pappers.dirigeants(entreprise)
    if reps:
        lines.append("Legal representatives (official Pappers source, verified):")
        lines += [f"- {r['nom']} ({r['role']})" for r in reps]
    else:
        lines.append("No representative found on Pappers (company not identified in the registry, "
                      "or the API isn't configured).")

    # Identify the official site (to target Hunter.io and the follow-up web
    # search, never to derive/guess an email address ourselves). Anti-homonym
    # guard: a generic company name can surface a completely unrelated
    # business as the top result just because it shares one word of the name
    # -- require a distinctive word (>=4 letters) from the company name to
    # appear in the result's title/domain, not just "the first one that
    # isn't a known directory".
    distinctive_words = [common.normalize_text(w) for w in entreprise.split() if len(w) >= 4]
    domain = None
    for r in web_search.search(f"{entreprise} {ville} official site".strip()):
        url = r.get("url") or ""
        if any(skip in url for skip in _DOMAIN_SKIP):
            continue
        haystack = common.normalize_text(f"{r.get('title') or ''} {url}")
        if distinctive_words and not any(word in haystack for word in distinctive_words):
            continue
        m = re.search(r"https?://(?:www\.)?([\w.-]+)", url)
        if m:
            domain = m.group(1)
            break

    if domain:
        lines.append(f"\nOfficial site identified: {domain}")
        emails_found = sources_hunter.domain_search(domain)
        source_tag, source_label = "hunter", "Hunter.io"
        if not emails_found:
            # Fall back to Snov.io (same idea, separate monthly quota) only
            # when Hunter returned nothing (quota exhausted or domain not
            # covered) -- not combined by default, to spare both free quotas.
            emails_found = sources_snov.domain_search(domain)
            source_tag, source_label = "snov", "Snov.io (fallback, Hunter returned nothing)"
    else:
        emails_found = []
        source_tag = source_label = ""

    if emails_found:
        lines.append(f"\nVerified emails found (source {source_label}, sorted by relevance):")
        for e in emails_found[:5]:
            who = f"{e['prenom'] or ''} {e['nom'] or ''}".strip() or "generic address"
            role = f" -- {e['poste']}" if e.get("poste") else ""
            confidence = f", {e['confiance']}% confidence" if e.get("confiance") is not None else ""
            lines.append(f"- {e['email']} -- {who}{role} (verified: {e['verifie'] or 'unverified'}{confidence})")
    elif domain:
        lines.append("\nNo email found by Hunter.io or Snov.io for this domain (quotas exhausted, "
                      "domain not covered, or genuinely no public data) -- "
                      "don't guess one instead.")
    else:
        lines.append("\nOfficial site not identified, no email search could be run.")

    # Follow-up web search (HR/hiring context) -- Hunter stays the preferred
    # source for the email itself, this is just extra context.
    query = f"{domain} hiring contact careers" if domain else \
        f"{entreprise} hiring HR LinkedIn {ville}".strip()
    web_results = web_search.search(query)
    if web_results:
        lines.append("\nPublic mentions found on the web (extra context, unverified -- "
                      "never visit/scrape any LinkedIn URL that shows up, just "
                      "mention it to the user):")
        lines += [f"- {r['title']}: {r['content']}\n  {r['url']}" for r in web_results if r.get("content")]

    # Persistence: without offer_id, a search already done would be lost
    # again on every new conversation and would have to be redone (spending
    # Hunter/Snov credit again) every time.
    if offer_id and (reps or emails_found or domain):
        with get_connection() as conn:
            if domain:
                conn.execute("UPDATE offers SET company_domain = ? WHERE id = ?", (domain, offer_id))
            for r in reps:
                conn.execute(
                    "INSERT INTO company_contacts (offer_id, nom, poste, source) VALUES (?, ?, ?, 'pappers')",
                    (offer_id, r["nom"], r["role"]),
                )
            for e in emails_found:
                full_name = f"{e.get('prenom') or ''} {e.get('nom') or ''}".strip() or None
                conn.execute(
                    """INSERT INTO company_contacts (offer_id, email, nom, poste, type, confiance, verifie, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (offer_id, e["email"], full_name, e.get("poste"), e.get("type"),
                     e.get("confiance"), e.get("verifie"), source_tag),
                )

    return {
        "text": "\n".join(lines), "domaine": domain,
        "n_dirigeants": len(reps), "n_emails": len(emails_found), "from_cache": False,
    }
