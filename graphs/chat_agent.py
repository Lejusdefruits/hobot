"""chat_agent -- the conversational agent behind /ask on Discord and the
Chat pane in the terminal UI (cli.py).

A ReAct agent (local LLM via Ollama, native tool-calling) triggered by
either interface. This is the ONLY path that can lead to a real email send,
and only after explicit confirmation:
- `creer_brouillon`: always allowed, no confirmation needed.
- `preparer_envoi_mail`: ONLY prepares, never connects to SMTP itself. Each
  interface attaches its own "Confirm send" affordance to the reply (a
  Discord button, or the terminal UI's own button next to the proposal); it's
  that human action, not the agent, that actually calls
  email_tools.envoyer_ou_repondre_email.

Conversation memory persists in checkpoints.db (SqliteSaver, thread_id =
Discord user id, or CLI_CHAT_THREAD_ID -- "cli" by default -- for the
terminal UI's single-user chat session) -- survives a process restart. The
terminal UI runs as its own separate OS process, not in-process with Discord
the way the old web dashboard did, so its connection to checkpoints.db has
its own WAL + busy_timeout (see CHECKPOINT_DB_PATH below) rather than relying
on SqliteSaver's same-process lock. PENDING_SENDS below is still RAM-only and
does NOT survive a restart -- an unconfirmed send proposal is lost if the
process restarts before it's confirmed, same as before, and stays scoped to
whichever process (daemon+Discord, or the terminal UI) originally proposed it.
"""
import json
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import trim_messages
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import create_react_agent

from core.db import get_connection, get_user_profile
from core.llm_provider import get_chat_model
from tools import common
from tools.email_tools import GMAIL_DAILY_SEND_CAP
from tools.ghost_job import check_ghost_job

# Pending send proposals waiting on a human click -- see the module docstring.
PENDING_SENDS: dict[str, dict] = {}


@tool
def rechercher_offres(mot_cle: str = "", score_min: int = 0, limite: int = 10) -> str:
    """Searches offers already in the database (not a new scrape) by keyword
    in the title/company, with an optional minimum score. Offers already
    marked 'applied' are excluded."""
    query = ("SELECT id, title, company, location, score, source, description, first_seen_at FROM offers "
              "WHERE status NOT IN ('applied', 'excluded', 'expired')")
    params: list = []
    if score_min:
        query += " AND score >= ?"
        params.append(score_min)
    if mot_cle:
        query += " AND (title LIKE ? OR company LIKE ?)"
        params += [f"%{mot_cle}%", f"%{mot_cle}%"]
    query += " ORDER BY score DESC LIMIT ?"
    params.append(min(limite, 30))  # server-side cap: the agent doesn't get to pick its own context size

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    if not rows:
        return "No offers found matching these criteria."
    lines = []
    for r in rows:
        line = (f"#{r['id']} [{r['score']}] {r['title']} -- {r['company']} ({r['location']}) -- "
                f"{common.offer_type_label(r['source'])}")
        is_ghost, _ = check_ghost_job(r["description"], r["first_seen_at"])
        if is_ghost:
            line += " -- possible ghost job"
        lines.append(line)
    return "\n".join(lines)


@tool
def chercher_offres_maintenant(mot_cle: str, ville: str = "France") -> str:
    """Runs a LIVE search (JobSpy scraping) for a one-off request outside
    scheduled discovery (e.g. a specific city or role not covered by the
    current profile) -- slower than a regular API call, reserve this for a
    real explicit request, don't chain it in a loop. Results are saved to the
    database (origin='ponctuelle', same dedup as scheduled discovery) and get
    a real offer number usable afterward with details_offre, marquer_postule,
    sauvegarder_lettre_motivation, etc. Returns number, title, company,
    location, link, and a description excerpt for each result."""
    from graphs.discovery_graph import persist_adhoc_offers
    from tools import sources_jobspy

    try:
        resultats = sources_jobspy.search_offers(search_term=mot_cle, location=ville, results_wanted=10)
    except Exception as e:
        return f"Search unavailable: {e}"

    if not resultats:
        return "Nothing found live."

    persisted = persist_adhoc_offers(resultats)

    lines = []
    for r in persisted:
        line = f"#{r['id']} -- {r.get('title')} -- {r.get('company')} ({r.get('location')})"
        if r.get("url"):
            line += f"\n{r['url']}"
        description = (r.get("description") or "").strip()
        if description:
            line += f"\n{description[:500]}" + ("..." if len(description) > 500 else "")
        lines.append(line)

    if not lines:
        return "Nothing new found (results already in the database)."
    return "\n\n".join(lines)


@tool
def offres_non_notees(limite: int = 15) -> str:
    """Lists offers still waiting to be scored (score IS NULL) -- oldest
    first, the same order lancer_scoring actually processes them in. Use
    this for a question like 'what's still waiting to be scored' or 'why
    isn't offer X showing a score yet'."""
    from core import queries

    rows = queries.list_unscored_offers(limit=min(limite, 30))
    if not rows:
        return "Nothing waiting to be scored -- every open offer already has a score."
    return "\n".join(
        f"#{r['id']} {r['title'] or '(untitled)'} -- {r['company'] or '?'} ({r['location'] or '?'}) "
        f"-- {common.offer_type_label(r['source'])}, found {r['first_seen_at']}"
        for r in rows
    )


@tool
def lancer_scoring() -> str:
    """Scores the pending backlog right now instead of waiting for the next
    scheduled discovery run -- same LLM scoring step (graphs/discovery_graph.py
    ::score_node), same per-call cap (DISCOVERY_MAX_SCORE_PER_RUN). Use this
    when the user explicitly asks to score/rescore now; it does not draft
    cover letters for anything that scores well here (that only happens as
    part of a scheduled discovery run) -- say so if a result scores above
    DISCOVERY_LETTER_SCORE_THRESHOLD, so the user knows a letter isn't
    coming until the next scheduled run."""
    from graphs.discovery_graph import run_scoring_now

    scored = run_scoring_now()
    if not scored:
        return "Nothing was waiting to be scored."
    lines = [f"Scored {len(scored)} offer(s):"]
    lines += [
        f"#{o['id']} [{o['score']}] {o['title'] or '(untitled)'} -- {o['company'] or '?'}"
        for o in sorted(scored, key=lambda o: o.get("score") or 0, reverse=True)
    ]
    return "\n".join(lines)


@tool
def strategie_recherche() -> str:
    """Shows the search keyword currently in use for each discovery source
    that takes a free-text query (Adzuna, JobSpy/Indeed, France Travail --
    LBA and La Bonne Boite search by a fixed ROME code instead, see
    LBA_ROME_CODES in graphs/discovery_graph.py), with the result count from
    its last run. There's no per-run keyword adaptation here: the keyword is
    derived straight from user_profile.target_roles (see discovery_graph.py's
    _search_terms) and stays constant until the profile changes -- this
    reports what's actually being searched, not a reasoning trail."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT r.source, r.query, r.n_found, r.finished_at
               FROM run_log r
               JOIN (SELECT source, MAX(id) AS max_id FROM run_log
                     WHERE query IS NOT NULL GROUP BY source) m
                 ON r.source = m.source AND r.id = m.max_id
               ORDER BY r.source"""
        ).fetchall()
    if not rows:
        return "No keyword recorded yet (no discovery run with a free-text query has completed)."
    return "\n".join(
        f"{common.source_label(r['source'])} -- \"{r['query']}\" "
        f"({r['n_found']} result(s) last run, {r['finished_at']})"
        for r in rows
    )


@tool
def journal_recherches(limite: int = 15) -> str:
    """History of recent scheduled discovery runs, every source together
    (query used where known, result counts, errors) -- unlike
    strategie_recherche, which only shows the LATEST run per source, this
    shows several runs in a row to judge a trend (e.g. "0 results for three
    runs straight"). Useful for a question like 'what have you searched for
    recently and what did it turn up'."""
    limite = max(1, min(limite, 40))
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT source, query, n_found, n_new, errors, finished_at
               FROM run_log WHERE run_type = 'discovery' ORDER BY id DESC LIMIT ?""",
            (limite,),
        ).fetchall()
    if not rows:
        return "No discovery run recorded yet."
    lines = []
    for r in reversed(rows):  # oldest to newest, more natural to read
        line = f"{r['finished_at'] or '?'} -- {common.source_label(r['source'])}"
        if r["query"]:
            line += f" -- \"{r['query']}\""
        line += (f" -- {r['n_found'] if r['n_found'] is not None else '?'} result(s), "
                 f"{r['n_new'] if r['n_new'] is not None else '?'} new")
        if r["errors"]:
            line += f"\n  error: {r['errors'][:200]}"
        lines.append(line)
    return "\n".join(lines)


@tool
def dernieres_notifications(limite: int = 5) -> str:
    """Shows the most recent proactive notifications actually sent to Discord
    (new postings found, important mail, weekly digest, errors) -- what the
    user has in front of them in the Discord channel but that YOU don't
    otherwise see, since these are posted straight to the channel, outside
    your per-thread conversation memory. CALL THIS TOOL FIRST as soon as the
    user refers to something seen in a notification without giving a precise
    id (e.g. "that posting", "the one you just found", "that earlier email",
    "and that alert?") -- never guess which posting/email they mean, check
    here first. Each entry lists, when known, the posting ids involved
    (usable afterward with details_offre)."""
    limite = max(1, min(limite, 20))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT kind, title, message, offer_ids, created_at FROM notifications ORDER BY id DESC LIMIT ?",
            (limite,),
        ).fetchall()
    if not rows:
        return "No notification sent yet."
    lines = []
    for r in reversed(rows):  # oldest to newest, more natural to read
        offer_ids = json.loads(r["offer_ids"]) if r["offer_ids"] else []
        line = f"[{r['created_at']}] ({r['kind']}) {r['title']}\n{r['message'][:600]}"
        if offer_ids:
            line += f"\nRelated posting(s): {', '.join('#' + str(i) for i in offer_ids)}"
        lines.append(line)
    return "\n\n".join(lines)


@tool
def mes_candidatures() -> str:
    """Lists applications already sent or marked (offers applied to), most
    recent first."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT a.status, a.sent_at, o.title, o.company FROM applications a
               JOIN offers o ON o.id = a.offer_id ORDER BY a.id DESC LIMIT 15"""
        ).fetchall()
    if not rows:
        return "No applications recorded yet."
    return "\n".join(f"[{r['status']}] {r['sent_at'] or ''} -- {r['title']} ({r['company']})" for r in rows)


@tool
def statut_veille() -> str:
    """Gives the PRECISE date/time of the last check AND the next scheduled
    one, separately for offers and for emails. Use this for any question like
    'when was the last check', 'when's the next one due', 'when did you last
    check the emails', etc."""
    with get_connection() as conn:
        last_discovery = conn.execute(
            "SELECT finished_at FROM run_log WHERE run_type='discovery' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_email = conn.execute(
            "SELECT finished_at FROM run_log WHERE run_type='email_watch' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    from core import daemon_state
    next_discovery = next_email = "unknown (daemon not running)"
    if daemon_state.scheduler is not None:
        job_d = daemon_state.scheduler.get_job("discovery")
        job_e = daemon_state.scheduler.get_job("email_watch")
        if job_d and job_d.next_run_time:
            next_discovery = job_d.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
        if job_e and job_e.next_run_time:
            next_email = job_e.next_run_time.strftime("%Y-%m-%d %H:%M:%S")

    return (
        f"OFFER watch -- last check: {last_discovery['finished_at'] if last_discovery else 'never'} "
        f"| next due: {next_discovery}\n"
        f"EMAIL watch -- last check: {last_email['finished_at'] if last_email else 'never'} "
        f"| next due: {next_email}"
    )


@tool
def details_offre(offer_id: int) -> str:
    """Fetches the full detail (including description) of an offer by its
    number, for writing a targeted cover letter or reply."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT title, company, location, description, url, score, score_reason, company_domain, "
            "status, last_seen_at, first_seen_at, source FROM offers WHERE id = ?",
            (offer_id,),
        ).fetchone()
        if not row:
            return f"No offer #{offer_id}."
        contacts = conn.execute(
            "SELECT email, nom, poste, source FROM company_contacts WHERE offer_id = ? "
            "ORDER BY confiance DESC NULLS LAST", (offer_id,),
        ).fetchall()
    # Some descriptions (JobSpy/Indeed especially) run several KB of raw
    # text -- 3000 characters is enough to write a targeted letter in most
    # cases. The full text is stored in the database (never truncated on
    # write): if this isn't enough, description_complete_offre gives the
    # rest, no need to look elsewhere.
    full_description = row["description"] or ""
    description = full_description[:3000]
    if len(full_description) > 3000:
        truncated_note = (
            f"\n[... description truncated to 3000/{len(full_description)} characters -- "
            f"use description_complete_offre({offer_id}) for the rest]"
        )
    elif full_description.rstrip().endswith(("…", "...")):
        # Some sources only provide a truncated excerpt through their own
        # API -- it's not our 3000-character limit cutting it off here,
        # there's nothing more in the database. description_complete_offre
        # can't add anything in this specific case.
        truncated_note = (
            "\n[Note: this description is already truncated AT THE SOURCE (the "
            "original API only provides an excerpt) -- the full text doesn't "
            "exist in our data, description_complete_offre won't add anything "
            "here. Only the listing's URL has the complete detail.]"
        )
    else:
        truncated_note = ""
    contacts_note = ""
    if contacts:
        contact_lines = []
        for c in contacts:
            who = c["nom"] or "generic address"
            role = f" -- {c['poste']}" if c["poste"] else ""
            email_part = f"{c['email']} -- " if c["email"] else ""
            contact_lines.append(f"  - {email_part}{who}{role} (source {c['source']})")
        contacts_note = "\nContacts already found (see company_contacts):\n" + "\n".join(contact_lines)
    elif row["company_domain"]:
        contacts_note = f"\nKnown official site: {row['company_domain']} (no contact recorded yet)"

    status_note = " -- DEAD LINK, don't suggest applying to this one" if row["status"] == "expired" else ""
    is_ghost, ghost_reason = check_ghost_job(row["description"], row["first_seen_at"])
    ghost_note = f"\nWarning: {ghost_reason} -- mention this to the user before drafting anything" if is_ghost else ""
    return (f"Title: {row['title']}\nCompany: {row['company']}\n"
            f"Type: {common.offer_type_label(row['source'])}\nLocation: {row['location']}\n"
            f"Score: {row['score']} ({row['score_reason'] or 'no justification recorded'})\n"
            f"Status: {row['status']}{status_note}{ghost_note}\n"
            f"Last confirmed online: {row['last_seen_at']}\n"
            f"URL: {row['url']}\nDescription: {description}{truncated_note}{contacts_note}")


@tool
def description_complete_offre(offer_id: int) -> str:
    """Returns the FULL (untruncated) description of an offer -- use this
    when details_offre reports a truncated description, or for any explicit
    request for the listing's full text. The text is already in the
    database (never truncated on write), no need to fetch it elsewhere."""
    with get_connection() as conn:
        row = conn.execute("SELECT title, description FROM offers WHERE id = ?", (offer_id,)).fetchone()
    if not row:
        return f"No offer #{offer_id}."
    description = row["description"] or ""
    if not description:
        return f"#{offer_id} ({row['title']}) has no description recorded."
    # A generous cap (~8000 characters) rather than truly no limit, so a
    # conversation can never saturate the model's context.
    return description[:8000]


@tool
def profil_candidat() -> str:
    """Returns the candidate's profile (name, skills, target roles, target
    locations) -- check this before writing a cover letter or a reply."""
    profile = get_user_profile()
    if not profile:
        return "No profile saved yet -- use definir_profil to create one."
    return (f"Name: {profile.get('full_name') or '(not set)'}\n"
            f"Skills: {', '.join(profile['skills'])}\n"
            f"Target roles: {', '.join(profile['target_roles'])}\n"
            f"Target locations: {', '.join(profile['target_locations'])}")


@tool
def definir_profil(texte: str) -> str:
    """Saves/replaces the candidate's profile from free text -- one sentence
    ("I'm looking for a Python dev role in Lyon") or a whole CV pasted as
    text. An alternative to PDF import (see README): no file needed, just
    describe or paste the profile directly into the conversation. Overwrites
    any existing profile -- use this for initial setup or a full rewrite, not
    a one-off tweak (see modifier_profil to add/remove a single item)."""
    from core.profile import parse_text, save_profile

    profile = parse_text(texte)
    save_profile(profile)
    return (f"Profile saved.\n"
            f"Name: {profile.get('full_name') or '(not detected)'}\n"
            f"Skills: {', '.join(profile.get('skills', [])) or '(none)'}\n"
            f"Target roles: {', '.join(profile.get('target_roles', [])) or '(none)'}\n"
            f"Target locations: {', '.join(profile.get('target_locations', [])) or '(none)'}")


@tool
def rechercher_entreprise(nom_entreprise: str, contexte: str = "") -> str:
    """Web search (self-hosted SearXNG) on a company, for writing a sharper
    cover letter (what it actually does, recent news...). contexte = the role
    title or sector, to narrow the query. ONE search is enough per letter,
    don't chain several queries on the same company. Returns an empty string
    (literally) if nothing usable turns up; never treat that as a blocking
    error, write the letter anyway with what the posting already says.
    WARNING: results can be about an unrelated company with the same name
    (a common name, different sector/city) -- only reuse a detail in the
    letter if the sector/city clearly match the posting, otherwise ignore it."""
    from tools import web_search
    return web_search.search_company(nom_entreprise, hint=contexte) or "No information found."


@tool
def surveiller_entreprise(nom_entreprise: str) -> str:
    """Adds a company to the ATS watchlist (core/ats_watchlist.py) so its
    Greenhouse/Ashby/Lever job board gets checked on every scheduled
    discovery run from now on, alongside JobSpy/LBA/Adzuna/France Travail --
    for a company known to use one of those three ATS platforms, not a
    general "look this company up" tool. Resolution is best-effort (tries a
    few common spellings of the company name against all three platforms'
    public APIs): if nothing matches, this returns a clear explanation
    rather than guessing, and the user can be told to check the company's
    careers page for the exact slug if they're sure it's on one of the
    three.

    On success, also creates a spontaneous-application lead for this company
    (same idea as La Bonne Alternance/La Bonne Boite's recruiter leads,
    tools/sources_lba.py: a placeholder posting for a company with no open
    role yet, so there's something real to act on -- mark applied, exclude,
    tailor a CV -- rather than just a name sitting in a list) and looks up a
    contact for it right away (rechercher_contacts_entreprise: Pappers +
    web search first, Hunter.io/Snov.io only as their own existing
    fallback -- unchanged, this just triggers the same tool a human would
    call by hand). The lead gets picked up by the next scheduled run for
    scoring and, if it scores well, an automatic cover letter -- nothing
    extra needed here for that."""
    from core.ats_watchlist import add_company
    ok, message = add_company(nom_entreprise)
    if not ok:
        return message

    from graphs.discovery_graph import persist_adhoc_offers
    from tools.common import make_offer

    lead = make_offer(
        source="ats_lead", external_id=None, title=f"Spontaneous interest -- {nom_entreprise}",
        company=nom_entreprise, location=None,
        description=(
            f"Added to the ATS watchlist ({nom_entreprise}). No open posting yet -- its board is "
            f"checked on every scheduled run; in the meantime this is a spontaneous-application lead."
        ),
        url=None,
    )
    persisted = persist_adhoc_offers([lead])
    if not persisted:
        return message  # watchlist add still succeeded; nothing further to report

    offer_id = persisted[0]["id"]
    contacts = rechercher_contacts_entreprise.invoke({"entreprise": nom_entreprise, "offer_id": offer_id})
    return f"{message}\n\nSpontaneous-application lead created (#{offer_id}).\n\n{contacts}"


@tool
def retirer_entreprise_suivie(nom_entreprise: str) -> str:
    """Removes a company from the ATS watchlist (surveiller_entreprise) --
    its board stops being checked on future discovery runs. Postings already
    found from it stay in the database untouched, same as excluding a
    posting doesn't delete it."""
    from core.ats_watchlist import remove_company
    return f'No longer watching "{nom_entreprise}".' if remove_company(nom_entreprise) else \
        f'"{nom_entreprise}" wasn\'t on the watchlist (check lister_entreprises_suivies for the exact name).'


@tool
def lister_entreprises_suivies() -> str:
    """Lists every company on the ATS watchlist (surveiller_entreprise) --
    which platform (Greenhouse/Ashby/Lever) each one resolved to and when it
    was added."""
    from core.ats_watchlist import list_companies
    companies = list_companies()
    if not companies:
        return "No company on the ATS watchlist yet. Add one with surveiller_entreprise."
    return "\n".join(
        f"{c['company']} ({c['platform']}/{c['slug']}, added {c['added_at']})" for c in companies
    )


@tool
def verifier_actus_levees_de_fonds() -> str:
    """Runs the Maddyness/Frenchweb funding-news check right now instead of
    waiting for its own schedule (FUNDING_CHECK_INTERVAL_DAYS, .env) --
    reads recent French tech-news headlines, and for anything that reads as
    a specific company raising funding, resolves whether it has a
    Greenhouse/Ashby/Lever board. This is slow (an LLM call per candidate
    headline, plus 3 API attempts per one that resolves) and can legitimately
    return nothing new if nothing's changed since the last run -- don't
    treat an empty result as failure. Never adds anything to the watchlist
    on its own; a candidate is proposed via a proactive notification (same
    as the scheduled run), not returned as this tool's own text, so this
    always reports how many were checked rather than which ones."""
    from tools.funding_check import run_funding_check
    with get_connection() as conn:
        before = conn.execute(
            "SELECT id FROM run_log WHERE run_type = 'funding_check' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    run_funding_check()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT n_found, n_new FROM run_log WHERE run_type = 'funding_check' "
            "AND (? IS NULL OR id != ?) ORDER BY id DESC LIMIT 1",
            (before["id"] if before else None, before["id"] if before else -1),
        ).fetchone()
    if not row:
        return "Funding check ran but logged nothing -- check the daemon logs."
    if row["n_new"] == 0:
        return f"Checked {row['n_found']} funding headline(s), nothing new to propose."
    return (f"Checked {row['n_found']} funding headline(s), proposed {row['n_new']} "
            f"compan{'y' if row['n_new'] == 1 else 'ies'} -- see the notification for names "
            f"(dernieres_notifications, or check Discord/desktop).")


@tool
def quotas_api_restants() -> str:
    """Shows this month's usage for the 3 APIs with a limited free quota
    (Adzuna: 2500 searches/month, Hunter.io and Snov.io: 50 credits/month
    each, 1 credit per domain search) -- check this before running a lot of
    one-off searches or company contact lookups in a row, to avoid finding
    out the quota's exhausted after the fact."""
    from core.api_usage import quota_summary
    lines = []
    for q in quota_summary():
        label = {"adzuna": "Adzuna", "hunter": "Hunter.io", "snov": "Snov.io"}.get(q["source"], q["source"])
        lines.append(f"{label}: {q['used']}/{q['limit']} used this month ({q['remaining']} remaining)")
    return "\n".join(lines)


_DOMAIN_SKIP = ("linkedin.com", "societe.com", "pappers.fr", "indeed.com", "google.com",
                "facebook.com", "welcometothejungle.com", "glassdoor", "gouv.fr",
                "pagesjaunes.fr", "pagesblanches", "annuaire-entreprises", "verif.com",
                "infogreffe.fr", "manageo.fr", "kompass.com")


@tool
def rechercher_contacts_entreprise(entreprise: str, ville: str = "", offer_id: int = None) -> str:
    """Looks for a REAL contact at a company (to apply or follow up on an
    application), combining three reliable sources, all optional; any not
    configured (no API key in .env) just returns an empty result without
    breaking anything:
    1. Legal representatives (Pappers, the official French company registry,
       France only), verified names. Pappers provides neither an email nor a
       phone number (a legal registry, not a contact directory).
    2. Web search (SearXNG) to identify the company's official site.
    3. Hunter.io (domain search) on that official site, with an automatic
       fallback to Snov.io if Hunter returns nothing (monthly quota
       exhausted or domain not covered): emails that are ACTUALLY VERIFIED,
       collected by these services from the public web over time, NOT us
       scraping anything, LinkedIn or otherwise. This is the most reliable
       source for a usable email; prefer these results over anything found
       through a generic web search.
    offer_id (strongly recommended whenever the search is about a specific
    posting): contacts found get SAVED TO THE DATABASE (company_contacts
    table + offers.company_domain) so they're never lost again. If contacts
    already exist for this posting, they're returned directly WITHOUT
    spending a new Hunter/Snov credit -- always pass offer_id when you have
    it, never re-run a search from scratch on the same posting.
    ABSOLUTE RULE: if neither Hunter nor Snov.io returns anything, NEVER
    guess an address (e.g. firstname.lastname@company.com), say clearly that
    no address was found and recommend applying through the posting's link
    instead of making up a contact."""
    from tools import sources_hunter, sources_pappers, sources_snov, web_search

    if offer_id:
        with get_connection() as conn:
            existing = conn.execute(
                "SELECT email, nom, poste, confiance, verifie, source FROM company_contacts "
                "WHERE offer_id = ? ORDER BY confiance DESC NULLS LAST", (offer_id,),
            ).fetchall()
        if existing:
            lines = [f"Contacts already saved for offer #{offer_id} (no new API call):"]
            for c in existing:
                who = f"{c['nom']}" if c["nom"] else "generic address"
                role = f" -- {c['poste']}" if c["poste"] else ""
                email_part = f"{c['email']} -- " if c["email"] else ""
                confidence = f", {c['confiance']}% confidence" if c["confiance"] is not None else ""
                lines.append(f"- {email_part}{who}{role} (source {c['source']}{confidence})")
            return "\n".join(lines)

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

    return "\n".join(lines)


@tool
def sauvegarder_lettre_motivation(offer_id: int, contenu: str) -> str:
    """Saves the text of a cover letter (that YOU wrote yourself in your
    reply) as a PDF in outputs/ and links it to the offer. Sends nothing
    anywhere."""
    from tools.documents import generate_letter_pdf
    profile = get_user_profile() or {}
    path = generate_letter_pdf(offer_id, contenu, full_name=profile.get("full_name"))
    with get_connection() as conn:
        common.upsert_application(conn, offer_id, defaults={"status": "draft"}, cover_letter_path=str(path))
    return f"Letter saved to {path}."


@tool
def adapter_cv(offer_id: int) -> str:
    """Generates a tailored version of the candidate's own uploaded CV for
    one specific offer -- same file, same layout, same fonts, only the
    profile/summary paragraph (and, if the CV's skills section is a plain
    text list, which real skills fill the visible slots) changes. Requires
    a CV to have been uploaded first via /profile; never invents a skill,
    project, or experience not already in the profile. Use this after
    scoring an offer highly, alongside (not instead of)
    sauvegarder_lettre_motivation."""
    from tools.cv_tailor import tailor_cv

    with get_connection() as conn:
        row = conn.execute("SELECT title, description FROM offers WHERE id = ?", (offer_id,)).fetchone()
    if not row:
        return f"No offer #{offer_id}."

    path = tailor_cv(offer_id, row["title"], row["description"])
    if path is None:
        return ("Couldn't tailor a CV for this offer -- either no CV was uploaded yet (use /profile), "
                "or the tailoring attempt failed. The cover letter isn't affected.")

    with get_connection() as conn:
        common.upsert_application(conn, offer_id, defaults={"status": "draft"}, cv_path=str(path))
    return f"Tailored CV saved to {path}."


@tool
def lire_lettre_motivation(offer_id: int) -> str:
    """Shows the TEXT of a cover letter already written for an offer
    (auto-generated above the score threshold, or via
    sauvegarder_lettre_motivation) -- use this as soon as you're asked to
    SHOW, read, or display a letter. Never just give the file path in your
    reply: read it with this tool and paste the content into your reply."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT cover_letter_path FROM applications WHERE offer_id = ? AND cover_letter_path IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (offer_id,),
        ).fetchone()
    if not row:
        return f"No letter saved for offer #{offer_id}."
    path = Path(row["cover_letter_path"])
    if not path.exists():
        return f"File {path} no longer exists on disk."
    from tools.documents import read_letter_text
    return read_letter_text(path)


@tool
def marquer_postule(offer_id: int) -> str:
    """Marks an offer as already applied to: it disappears from searches and
    /offers, and its cover letter (if any) moves to outputs/archive/ to keep
    outputs/ clean (active letters only)."""
    with get_connection() as conn:
        row = conn.execute("SELECT title, company, status FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if not row:
            return f"No offer #{offer_id}."
        if row["status"] == "applied":
            return f"#{offer_id} was already marked applied: {row['title']} -- {row['company']}."
        conn.execute("UPDATE offers SET status = 'applied' WHERE id = ?", (offer_id,))
        row_id = common.upsert_application(conn, offer_id, status="applied")
        conn.execute("UPDATE applications SET sent_at = datetime('now') WHERE id = ?", (row_id,))
        common.archive_cover_letter(conn, offer_id)
    return f"#{offer_id} marked applied: {row['title']} -- {row['company']}."


@tool
def annuler_postule(offer_id: int) -> str:
    """Undoes an 'already applied' mark made by mistake (via marquer_postule
    or /applied): the offer reappears in /offers and rechercher_offres, and
    its cover letter (if it had been archived) moves back into outputs/.
    Doesn't touch the letter's content, only the 'applied' status and its
    location."""
    with get_connection() as conn:
        row = conn.execute("SELECT title, company, status, score FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if not row:
            return f"No offer #{offer_id}."
        if row["status"] != "applied":
            return f"#{offer_id} isn't marked applied (current status: {row['status']})."
        # Same rule as inclure_offre: only 'scored' if it actually has a
        # score, otherwise 'new' so it gets picked up by the next scoring
        # run instead of being mislabeled 'scored' with score still NULL
        # (possible if it was marked applied before ever being scored).
        new_status = "scored" if row["score"] is not None else "new"
        conn.execute("UPDATE offers SET status = ? WHERE id = ?", (new_status, offer_id))
        # Reverts the row to draft rather than deleting it -- since marquer_postule
        # now updates the SAME row a letter/CV was saved to (see
        # common.upsert_application), deleting it here would orphan those file
        # paths even though the files themselves are un-archived right below.
        conn.execute(
            "UPDATE applications SET status = 'draft', sent_at = NULL WHERE offer_id = ? AND status = 'applied'",
            (offer_id,),
        )
        common.unarchive_cover_letter(conn, offer_id)
    return f"#{offer_id} is no longer marked applied: {row['title']} -- {row['company']}."


@tool
def exclure_offre(offer_id: int, raison: str = "") -> str:
    """Manually excludes an offer (e.g. the automatic training-intermediary
    filter let a listing through, or any other offer worth dropping for a
    reason of your own): it disappears from rechercher_offres, /offers, and
    /status, just like an already-applied offer. raison = an optional note,
    only used for the confirmation message, not stored in the database."""
    with get_connection() as conn:
        row = conn.execute("SELECT title, company, status FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if not row:
            return f"No offer #{offer_id}."
        if row["status"] == "applied":
            return f"#{offer_id} is already marked applied -- undo that first with annuler_postule if you want to exclude it."
        if row["status"] == "excluded":
            return f"#{offer_id} is already excluded."
        conn.execute("UPDATE offers SET status = 'excluded' WHERE id = ?", (offer_id,))
    detail = f" ({raison})" if raison else ""
    return f"#{offer_id} excluded: {row['title']} -- {row['company']}{detail}."


@tool
def inclure_offre(offer_id: int) -> str:
    """Undoes an exclusion (manual via exclure_offre, or automatic, e.g. the
    training-intermediary filter got it wrong): the offer reappears in
    searches. Goes back to 'scored' if it already has a score, otherwise
    'new' so it gets picked up by the next scoring run."""
    with get_connection() as conn:
        row = conn.execute("SELECT title, company, status, score FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if not row:
            return f"No offer #{offer_id}."
        if row["status"] != "excluded":
            return f"#{offer_id} isn't excluded (current status: {row['status']})."
        new_status = "scored" if row["score"] is not None else "new"
        conn.execute("UPDATE offers SET status = ? WHERE id = ?", (new_status, offer_id))
    return f"#{offer_id} is no longer excluded: {row['title']} -- {row['company']}."


@tool
def repartition_scores() -> str:
    """Quick overview of the database: how many offers per score tier, how
    many still waiting to be scored, without listing offers one by one.
    Useful for a question like 'how many interesting offers do I have?'."""
    with get_connection() as conn:
        unscored = conn.execute("SELECT COUNT(*) c FROM offers WHERE score IS NULL").fetchone()["c"]
        rows = conn.execute(
            "SELECT score FROM offers WHERE score IS NOT NULL AND status NOT IN ('applied', 'excluded', 'expired')"
        ).fetchall()
    scores = [r["score"] for r in rows]
    tiers = [
        ("≥ 80", sum(1 for s in scores if s >= 80)),
        ("60-79", sum(1 for s in scores if 60 <= s < 80)),
        ("40-59", sum(1 for s in scores if 40 <= s < 60)),
        ("< 40", sum(1 for s in scores if s < 40)),
    ]
    lines = [f"{label}: {n}" for label, n in tiers]
    lines.append(f"Waiting to be scored: {unscored}")
    return "\n".join(lines)


@tool
def funnel_conversion() -> str:
    """Full conversion funnel: found -> scored -> letter written -> sent ->
    reply received -> interview obtained, with the % of the previous stage
    (more telling than % of total for spotting where things stall) and the
    biggest proportional leak. Useful for 'where am I losing the most
    applications?' or a general read on the process."""
    with get_connection() as conn:
        stats = common.funnel_stats(conn)
    stages = common.funnel_stages(stats)
    lines = [f"{s['label']}: {s['count']}" + (f" ({s['pct_previous']}% of the previous stage)" if s["pct_previous"] is not None else "") for s in stages]
    lines.append("")
    lines.append(common.funnel_insight(stages))
    return "\n".join(lines)


AXES_PROGRESSION_PROMPT = """Here are the score reasons an evaluator gave for {n} recently
poorly-scored job postings (score < 50) for this candidate:

{raisons}

Identify the 2-4 gaps or mismatches that come up MOST OFTEN in these reasons (a missing
skill, wrong experience level, poor location match, wrong role type...). Ignore reasons
that only show up once. Be concrete and actionable, not a vague summary.

Reply with ONLY JSON: {{"analyse": "2-4 bullet points, one per line"}}"""


@tool
def axes_progression(limite: int = 30) -> str:
    """Analyzes the score reasons (score_reason) of the most recent poorly-
    scored postings (score < 50) to spot the gaps that come up most often --
    no new search, just a synthesis of what scoring already wrote for each
    posting. Useful for a question like 'why am I scoring poorly lately' or
    'what should I work on'."""
    from core.llm import chat_json
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT title, score, score_reason FROM offers WHERE score IS NOT NULL AND score < 50 "
            "AND score_reason IS NOT NULL ORDER BY first_seen_at DESC LIMIT ?",
            (limite,),
        ).fetchall()
    if len(rows) < 5:
        return "Not enough recently poorly-scored postings for a useful analysis (minimum 5)."
    raisons = "\n".join(f"- [{r['score']}] {r['title']} :: {r['score_reason']}" for r in rows)
    try:
        result = chat_json(AXES_PROGRESSION_PROMPT.format(n=len(rows), raisons=raisons))
        return result.get("analyse") or "No usable analysis."
    except Exception as e:
        return f"Analysis unavailable: {e}"


@tool
def brouillons_en_attente() -> str:
    """Lists reply drafts already created automatically (by scheduled
    monitoring, not by you) and not yet reviewed -- use this for 'are there
    any pending reply drafts?'."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT account, from_addr, subject, received_at FROM emails
               WHERE status = 'reply_drafted' ORDER BY id DESC LIMIT 15"""
        ).fetchall()
    if not rows:
        return "No pending reply drafts."
    return "\n".join(
        f"[{r['account']}] {r['received_at']} -- {r['from_addr']} -- {r['subject']}" for r in rows
    )


@tool
def envois_restants_aujourdhui() -> str:
    """How many more emails can be sent today before the anti-ban daily cap
    (beyond it, confirmed sends go out as a draft instead of being
    refused)."""
    with get_connection() as conn:
        sent_today = conn.execute(
            "SELECT COUNT(*) c FROM run_log WHERE run_type='email_send' AND date(started_at) = date('now')"
        ).fetchone()["c"]
    remaining = max(0, GMAIL_DAILY_SEND_CAP - sent_today)
    return f"{sent_today}/{GMAIL_DAILY_SEND_CAP} sends used today, {remaining} remaining."


@tool
def comptes_mail_suivis() -> str:
    """Lists the Gmail accounts being watched and marks which one may send/
    create a draft -- useful before calling lire_derniers_mails or to know
    which account to act on."""
    from tools import email_tools
    lines = [
        f"{compte}" + (" (send/draft allowed)" if compte == email_tools.SEND_ACCOUNT else " (read-only)")
        for compte in email_tools.CREDENTIALS
    ]
    return "\n".join(lines)


PROFIL_CHAMPS = ("skills", "target_roles", "target_locations")


@tool
def modifier_profil(champ: str, action: str, valeur: str) -> str:
    """Edits the candidate's profile (what rechercher_offres and scoring
    use). champ: 'skills', 'target_roles', or 'target_locations'. action:
    'ajouter' (add) or 'retirer' (remove). valeur: the item in question, e.g.
    champ='target_locations', action='ajouter', valeur='Lyon'."""
    if champ not in PROFIL_CHAMPS:
        return f"Invalid field '{champ}' -- use 'skills', 'target_roles', or 'target_locations'."
    if action not in ("ajouter", "retirer"):
        return f"Invalid action '{action}' -- use 'ajouter' or 'retirer'."

    with get_connection() as conn:
        row = conn.execute(f"SELECT {champ} FROM user_profile WHERE id = 1").fetchone()
        current = json.loads(row[champ]) if row and row[champ] else []
        if action == "ajouter":
            if valeur not in current:
                current.append(valeur)
        else:
            current = [v for v in current if v.lower() != valeur.lower()]
        conn.execute(
            f"UPDATE user_profile SET {champ} = ?, updated_at = datetime('now') WHERE id = 1",
            (json.dumps(current, ensure_ascii=False),),
        )
    return f"{champ} updated: {', '.join(current) or '(empty)'}"


@tool
def lire_derniers_mails(compte: str, limite: int = 5) -> str:
    """Reads the most recent mail received on an account (see /status for the
    list of watched accounts). Read-only, no side effects."""
    from tools import email_tools
    return email_tools.lire_emails(compte, min(limite, 20))


@tool
def rechercher_emails(mots_cles: str = "", depuis: str = "", jusqua: str = "", tous_les_comptes: bool = True) -> str:
    """Searches mail by keyword and/or date range (dates as YYYY-MM-DD),
    across every watched account by default, unlike lire_derniers_mails,
    which only looks at the very latest mail on ONE account. Use this for
    any request like 'find the rejection emails from June' or 'find the
    email from X'."""
    from tools import email_tools
    resultats = email_tools.rechercher_emails(
        mots_cles=mots_cles, depuis=depuis or None, jusqua=jusqua or None,
        comptes=None if tous_les_comptes else [email_tools.SEND_ACCOUNT],
    )
    found = [r for r in resultats if "erreur" not in r]
    if not found:
        return "No mail found matching these criteria."
    return "\n".join(
        f"[{r['compte']}] {r['date']} -- {r['de']} -- {r['sujet']} :: {r['extrait'][:150]}"
        for r in found
    )


@tool
def creer_brouillon(compte: str, destinataire: str, sujet: str, contenu: str) -> str:
    """Creates a NEW Gmail draft WITHOUT sending it -- always allowed
    without confirmation, this is the default path for any email reply. To
    FIX/EDIT a draft that already exists (whether you or scheduled
    monitoring created it), use modifier_brouillon_mail, NOT this tool:
    calling this again on the same subject creates a duplicate instead of
    replacing the old one (IMAP has no in-place edit)."""
    from tools import email_tools
    return email_tools.creer_brouillon(compte, destinataire, sujet, contenu)


@tool
def lister_brouillons_mail() -> str:
    """Lists existing reply drafts (uid, recipient, subject, preview) on the
    sending account. Use this BEFORE modifier_brouillon_mail or
    supprimer_brouillon_mail to find the right uid to target -- without it,
    those two tools have nothing to aim at."""
    from tools import email_tools
    try:
        drafts = email_tools.lister_brouillons()
    except Exception as e:
        return f"Error listing drafts: {e}"
    if not drafts:
        return "No pending drafts."
    return "\n".join(
        f"uid={b['uid']} -- {b['sujet']} -- to {b['destinataire']} ({b['date']})\n  {b['apercu']}"
        for b in drafts
    )


@tool
def modifier_brouillon_mail(uid: str, destinataire: str, sujet: str, contenu: str) -> str:
    """Replaces the content of an existing draft (uid from
    lister_brouillons_mail) with a new version. ALWAYS use this tool (not
    creer_brouillon) when asked to fix/edit/rephrase a draft that already
    exists, otherwise you create a duplicate instead of replacing it."""
    from tools import email_tools
    return email_tools.modifier_brouillon(uid, destinataire, sujet, contenu)


@tool
def supprimer_brouillon_mail(uid: str) -> str:
    """Permanently deletes a draft (uid from lister_brouillons_mail).
    Irreversible -- if the goal is just to fix it, use
    modifier_brouillon_mail instead."""
    from tools import email_tools
    return email_tools.supprimer_brouillon(uid)


@tool
def preparer_envoi_mail(destinataire: str, sujet: str, contenu: str) -> str:
    """Prepares the REAL send of an email (not a draft) -- use this ONLY if
    explicitly asked to send (not just reply/prepare). Sends strictly
    nothing: explicit human confirmation is required before any SMTP send,
    through whichever interface the user is on (a "Confirm send" button on
    Discord, or the same on the terminal UI's Chat pane)."""
    pending_id = str(uuid.uuid4())[:8]
    PENDING_SENDS[pending_id] = {"destinataire": destinataire, "sujet": sujet, "contenu": contenu}
    return (f"[PROPOSAL {pending_id}] Ready to send to {destinataire}, subject \"{sujet}\". "
            f"NOT SENT -- waiting on your confirmation.")


def execute_pending_send(pending_id: str) -> dict:
    """Executes a proposal from PENDING_SENDS (removing it either way), with
    the same daily anti-ban cap check as everywhere else a send can happen --
    falls back to a draft instead of a flat refusal once the cap is reached.
    NOT a @tool -- this only runs after a human confirms (a Discord button,
    or the terminal UI's own confirm button), never called by the agent
    itself. Returns {"status": one of "expired" | "draft_failed" |
    "draft_saved" | "send_failed" | "sent", "title": str, "description": str}
    -- no interface-specific content, so both map "status" to their own
    visual treatment."""
    from tools import email_tools

    payload = PENDING_SENDS.pop(pending_id, None)
    if not payload:
        return {
            "status": "expired", "title": "Proposal expired",
            "description": "This proposal is no longer valid (already sent or expired).",
        }

    with get_connection() as conn:
        sent_today = conn.execute(
            "SELECT COUNT(*) c FROM run_log WHERE run_type='email_send' AND date(started_at) = date('now')"
        ).fetchone()["c"]

    if sent_today >= GMAIL_DAILY_SEND_CAP:
        result = email_tools.creer_brouillon(
            email_compte=email_tools.SEND_ACCOUNT, destinataire=payload["destinataire"],
            sujet=payload["sujet"], contenu=payload["contenu"],
        )
        # email_tools always prefixes its errors with "Error" (a convention of
        # that module, the same check rechercher_emails uses elsewhere in this
        # file) -- checked here so a failed draft never gets reported as a success.
        failed = result.startswith("Error")
        return {
            "status": "draft_failed" if failed else "draft_saved",
            "title": "Draft failed" if failed else "Daily cap reached -- saved as draft",
            "description": result if failed else f"Daily cap of {GMAIL_DAILY_SEND_CAP} sends reached. {result}",
        }

    result = email_tools.envoyer_ou_repondre_email(
        email_compte=email_tools.SEND_ACCOUNT, destinataire=payload["destinataire"],
        sujet=payload["sujet"], contenu=payload["contenu"],
    )
    failed = result.startswith("Error")
    # Only sends that actually went out count against the daily cap -- a
    # silent SMTP failure (wrong password, network outage...) must never eat
    # into the anti-ban cap or get reported as a misleading success.
    if not failed:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO run_log (run_type, source, started_at, finished_at, n_found, n_new) "
                "VALUES ('email_send', ?, datetime('now'), datetime('now'), 1, 1)",
                (email_tools.SEND_ACCOUNT,),
            )
    return {
        "status": "send_failed" if failed else "sent",
        "title": "Send failed" if failed else "Email sent",
        "description": result,
    }


TOOLS = [
    rechercher_offres, chercher_offres_maintenant, offres_non_notees, lancer_scoring,
    strategie_recherche, journal_recherches,
    dernieres_notifications, mes_candidatures, details_offre,
    description_complete_offre, rechercher_entreprise, quotas_api_restants, rechercher_contacts_entreprise,
    surveiller_entreprise, retirer_entreprise_suivie, lister_entreprises_suivies,
    verifier_actus_levees_de_fonds,
    profil_candidat,
    definir_profil, modifier_profil, statut_veille, sauvegarder_lettre_motivation, adapter_cv, lire_lettre_motivation,
    marquer_postule, annuler_postule, exclure_offre, inclure_offre, repartition_scores, funnel_conversion,
    axes_progression, brouillons_en_attente,
    envois_restants_aujourdhui, comptes_mail_suivis, lire_derniers_mails, rechercher_emails,
    creer_brouillon, lister_brouillons_mail, modifier_brouillon_mail, supprimer_brouillon_mail,
    preparer_envoi_mail,
]

SYSTEM_PROMPT = """You are the user's job-search assistant.
You have tools to search offers already in the database, read their mail,
and draft replies and cover letters.

Non-negotiable rules:
- If no profile is saved yet (profil_candidat returns nothing) and the user
  describes what they're looking for or pastes a CV as text, use definir_profil
  to save it before any other action that depends on it (scoring, letters).
- By default, a reply to an email is a DRAFT (creer_brouillon), never a real send.
- Only use preparer_envoi_mail if the user explicitly wrote that they want to
  send the email now -- when in doubt, make a draft and ask.
- To FIX a draft that already exists (whether you made it or not):
  lister_brouillons_mail to find its uid, then modifier_brouillon_mail --
  never call creer_brouillon a second time on the same subject, that creates
  a duplicate instead of replacing it.
- A cover letter: write it yourself in your reply (not a tool that generates
  it for you), then save it with sauvegarder_lettre_motivation. Before that,
  you can call rechercher_entreprise ONCE for real information about the
  company, and rechercher_contacts_entreprise if the user wants a specific
  contact to apply to or follow up with -- no need to push further if it
  returns nothing useful.
- To SHOW a letter already written (a question like "show me the letter for
  X"), use lire_lettre_motivation and paste its text into your reply -- never
  just give the file path.
- If the user asks for a tailored CV for an offer (or you're drafting a
  letter for a strong match and a CV has been uploaded via /profile), call
  adapter_cv -- it edits their own uploaded CV in place, do not try to write
  or describe CV content yourself.
- Reply in English, concisely.
"""

# Bounded context: a long conversation (lots of tool round-trips) must never
# drift the model by saturating its context window. The full state stays in
# the checkpointer (SqliteSaver); only what's actually sent to the LLM each
# turn gets trimmed to the ~6000 most recent tokens.
MAX_CONTEXT_TOKENS = int(os.environ.get("CHAT_AGENT_MAX_CONTEXT_TOKENS", "6000"))


def _trim_history(state: dict) -> dict:
    trimmed = trim_messages(
        state["messages"], max_tokens=MAX_CONTEXT_TOKENS, strategy="last",
        token_counter="approximate", include_system=True, allow_partial=False,
    )
    return {"llm_input_messages": trimmed}


CHECKPOINT_DB_PATH = Path(__file__).resolve().parent.parent / os.environ.get(
    "HOBOT_CHECKPOINT_DB_PATH", "checkpoints.db"
)
# check_same_thread=False: shared across Discord's run_in_executor threads --
# SqliteSaver guards every access with its own internal lock, so one
# connection shared across threads in the same process is safe, just not
# across OS processes. The terminal UI (cli.py) imports this module fresh in
# its own separate process, with its own separate connection to the same
# file -- WAL + a real busy_timeout (same reasoning as core/db.py's
# get_connection()) so a chat message from Discord and one from the terminal
# UI landing at the same instant retry instead of one immediately raising
# "database is locked".
_checkpoint_conn = sqlite3.connect(str(CHECKPOINT_DB_PATH), check_same_thread=False)
_checkpoint_conn.execute("PRAGMA journal_mode=WAL")
_checkpoint_conn.execute("PRAGMA busy_timeout=10000")
_checkpointer = SqliteSaver(_checkpoint_conn)
_checkpointer.setup()  # idempotent; called explicitly here so a broken DB file fails fast at import, not on the first /ask
_agent = create_react_agent(
    model=get_chat_model(temperature=0.2),
    tools=TOOLS,
    prompt=SYSTEM_PROMPT,
    checkpointer=_checkpointer,
    pre_model_hook=_trim_history,
)


def wipe_memory() -> None:
    """Deletes every stored conversation checkpoint -- used by /reset, since
    starting clean plausibly includes forgetting old conversations too. Goes
    through the checkpointer's own locked cursor() rather than a second
    connection, so it can't race an in-flight checkpoint write from another
    thread."""
    with _checkpointer.cursor() as cur:
        cur.execute("DELETE FROM checkpoints")
        cur.execute("DELETE FROM writes")


def get_history(thread_id: str) -> list[dict]:
    """Stored conversation turns for a thread_id, oldest first, as plain
    {"role": "user"|"assistant", "content": str} dicts -- tool calls/results
    filtered out, a chat UI shows the conversation, not the agent's internal
    tool-calling steps. Used by the terminal UI's Chat pane, to replay past
    turns on mount; Discord doesn't need this, it already shows turns live in
    the channel as they happen."""
    state = _agent.get_state({"configurable": {"thread_id": thread_id}})
    if not state:
        return []
    turns = []
    for m in state.values.get("messages", []):
        role = {"human": "user", "ai": "assistant"}.get(getattr(m, "type", None))
        if role and isinstance(m.content, str) and m.content:
            turns.append({"role": role, "content": m.content})
    return turns


def ask(message: str, thread_id: str) -> str:
    """Single entry point used by discord_bot.py -- thread_id = Discord user
    id, so each person gets their own conversation memory.

    recursion_limit bounds the number of tool<->LLM round-trips: without it,
    an agent stuck looping on a tool without ever concluding would run
    forever. Set to 40 rather than something tighter: pre_model_hook adds a
    node before every model call (hook + agent + tool = ~3 steps per
    round-trip, not 2), and a local model can repeat the same tool call
    before chaining correctly -- 40 leaves room for that inefficiency without
    going back to an unbounded loop (AGENT_TIMEOUT_SECONDS stays the
    time-based safety net in discord_bot.py)."""
    try:
        result = _agent.invoke(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": thread_id}, "recursion_limit": 40},
        )
        return result["messages"][-1].content
    except Exception as e:
        return f"Error while processing the request: {e}"


if __name__ == "__main__":
    print(ask("Find me the 3 best offers in the database.", thread_id="cli-test"))
