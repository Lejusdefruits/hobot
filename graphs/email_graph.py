"""email_graph -- watches the configured mailboxes (GMAIL_ACCOUNT_1..N in .env).

Optional: if no account is configured, daemon.py doesn't even schedule this
graph (see tools/email_tools.py::CREDENTIALS, filled dynamically from .env).

fetch_new (every account, sequential) -> classify -> link_to_application ->
draft_reply -> log.

Anti-ban: one IMAP connection per account, one after another (never in
parallel) -- simultaneous logins would look a lot more like bot behavior than
spaced-out ones. Dedup goes through the `emails` table's UNIQUE(account, uid)
constraint, not a fragile external cursor: every run re-reads each account's
last FETCH_LIMIT mails and skips whatever's already in the database.

draft_reply only fires for mail received on GMAIL_SEND_ACCOUNT: the other
configured accounts are read-only (see tools/email_tools.py), so for mail
received elsewhere this graph classifies and logs but drafts nothing -- that
decision is left to the user.
"""
import os
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.graph import END, START, StateGraph

from core.db import get_connection, get_user_profile, init_db
from core.llm import chat_json
from tools import email_tools
from tools.notify_tools import notify_all

FETCH_LIMIT = int(os.environ.get("EMAIL_FETCH_LIMIT", "10"))

# Categories that trigger a "high priority" notification -- account security
# or an interview shouldn't wait for the next summary.
HIGH_PRIORITY_CATEGORIES = {"entretien", "securite_google"}
REPLY_CATEGORIES = {"recruteur", "entretien"}


def _log(msg: str) -> None:
    print(msg, flush=True)


class EmailState(TypedDict):
    raw_emails: list[dict]
    classified: list[dict]


def fetch_new_node(state: EmailState) -> dict:
    with get_connection() as conn:
        known = {(r["account"], r["uid"]) for r in conn.execute("SELECT account, uid FROM emails")}

    raw = []
    for account in email_tools.CREDENTIALS:
        try:
            msgs = email_tools.lire_emails_bruts(account, limite=FETCH_LIMIT)
        except Exception as e:
            _log(f"[fetch] {account} -> failed: {e}")
            continue

        n_new = 0
        for msg in msgs:
            uid = str(msg.uid)
            if (account, uid) in known:
                continue
            n_new += 1
            references = msg.headers.get("references")
            message_id = msg.headers.get("message-id")
            raw.append({
                "account": account,
                "uid": uid,
                "message_id": message_id[0] if message_id else None,
                "thread_id": references[0].split()[0] if references else (message_id[0] if message_id else None),
                "from_addr": msg.from_,
                "subject": msg.subject,
                "snippet": (msg.text or msg.html or "")[:800],
                "received_at": str(msg.date),
            })
        _log(f"[fetch] {account} -> {len(msgs)} read, {n_new} new")

    return {"raw_emails": raw}


CLASSIFY_PROMPT = """You're classifying an email received in a mailbox used for a job search.

Possible categories:
- "recruteur": a recruiter/company replying about an application.
- "entretien": proposes or confirms an interview / meeting.
- "refus": a rejection to an application.
- "securite_google": a Google security alert (login, password...).
- "spam": advertising, phishing, unsolicited.
- "newsletter": an automated job-alert digest (Indeed, LinkedIn...), a newsletter.
- "autre": none of the above.

From: {from_addr}
Subject: {subject}
Content (excerpt): {snippet}

Reply with ONLY JSON: {{"category": "...", "needs_reply": true/false}}
needs_reply = true only if "recruteur" or "entretien" AND a reply from the candidate is clearly expected."""


def classify_node(state: EmailState) -> dict:
    classified = []
    total = len(state["raw_emails"])
    for i, email in enumerate(state["raw_emails"], 1):
        try:
            result = chat_json(CLASSIFY_PROMPT.format(
                from_addr=email["from_addr"] or "",
                subject=email["subject"] or "",
                snippet=email["snippet"] or "",
            ))
            category = result.get("category", "autre")
            needs_reply = bool(result.get("needs_reply")) and category in REPLY_CATEGORIES
        except Exception as e:
            category, needs_reply = "autre", False
            _log(f"[classify] {i}/{total} - failed: {e}")

        priority = "high" if category in HIGH_PRIORITY_CATEGORIES else "normal"
        _log(f"[classify] {i}/{total} - [{category}/{priority}] {email['subject']!r} -- {email['from_addr']}")
        classified.append({**email, "category": category, "needs_reply": needs_reply})

    return {"classified": classified}


def link_to_application_node(state: EmailState) -> dict:
    """Simple matching (substring on the company name inside the sender/subject)
    -- nothing sophisticated, same "simple dedup" philosophy as elsewhere."""
    with get_connection() as conn:
        companies = [
            (r["id"], r["company"]) for r in
            conn.execute("SELECT id, company FROM offers WHERE company IS NOT NULL")
        ]
        for email in state["classified"]:
            haystack = f"{email['from_addr']} {email['subject']}".lower()
            email["linked_offer_id"] = next(
                (oid for oid, company in companies if company and company.lower() in haystack),
                None,
            )
    return {}


DRAFT_REPLY_PROMPT = """Write a professional, concise reply to this recruiting email, on behalf
of {full_name}. Reply in the same language as the email you're replying to.

Email received:
From: {from_addr}
Subject: {subject}
Content: {snippet}

Reply with ONLY JSON: {{"sujet": "Re: ...", "contenu": "the email body with an appropriate greeting and closing, signed {full_name}"}}"""


def draft_reply_node(state: EmailState) -> dict:
    """In-place mutation of email["draft_ok"] (True/False) on the dicts in
    state["classified"] -- same mechanism link_to_application_node uses for
    email["linked_offer_id"], LangGraph doesn't deep-copy the list between
    nodes here. creer_brouillon's actual result is propagated all the way to
    log_run_node (not just logged): without that, a draft that silently fails
    would still get marked status='reply_drafted' in the database."""
    profile = get_user_profile() or {}
    full_name = profile.get("full_name") or "[Your name]"
    for email in state["classified"]:
        if not email["needs_reply"]:
            continue
        if email["account"] != email_tools.SEND_ACCOUNT:
            _log(f"[draft] {email['subject']!r} needs a reply but was received on {email['account']} "
                 f"(read-only) -> no automatic draft, handle it yourself.")
            continue
        try:
            result = chat_json(DRAFT_REPLY_PROMPT.format(
                full_name=full_name,
                from_addr=email["from_addr"] or "", subject=email["subject"] or "", snippet=email["snippet"] or "",
            ))
            res = email_tools.creer_brouillon(
                email_compte=email["account"],
                destinataire=email["from_addr"],
                sujet=result.get("sujet", f"Re: {email['subject']}"),
                contenu=result.get("contenu", ""),
            )
            email["draft_ok"] = not res.startswith("Error")
            _log(f"[draft] {email['subject']!r} -> {'FAILED: ' if not email['draft_ok'] else ''}{res}")
        except Exception as e:
            email["draft_ok"] = False
            _log(f"[draft] {email['subject']!r} -> failed: {e}")
    return {}


def log_run_node(state: EmailState) -> dict:
    with get_connection() as conn:
        for email in state["classified"]:
            wants_reply = email["needs_reply"] and email["account"] == email_tools.SEND_ACCOUNT
            if wants_reply:
                status = "reply_drafted" if email.get("draft_ok") else "reply_failed"
            else:
                status = "read"
            conn.execute(
                """INSERT OR IGNORE INTO emails
                   (account, uid, message_id, thread_id, from_addr, subject, snippet, category,
                    linked_offer_id, status, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    email["account"], email["uid"], email.get("message_id"), email.get("thread_id"),
                    email["from_addr"], email["subject"], email["snippet"], email["category"],
                    email.get("linked_offer_id"), status,
                    email["received_at"],
                ),
            )
        conn.execute(
            """INSERT INTO run_log (run_type, source, finished_at, n_found, n_new, errors)
               VALUES ('email_watch', 'gmail', datetime('now'), ?, ?, NULL)""",
            (len(state["classified"]), len(state["classified"])),
        )
    _maybe_notify(state["classified"])
    return {}


def _maybe_notify(classified: list[dict]) -> None:
    """One grouped notification per run, never one per email -- a dozen Google
    security alerts back to back would spam the screen more than it would help."""
    entretiens = [e for e in classified if e["category"] == "entretien"]
    veut_reponse = [e for e in classified if e["needs_reply"] and e["account"] == email_tools.SEND_ACCOUNT]
    a_repondre = [e for e in veut_reponse if e.get("draft_ok")]
    echecs_brouillon = [e for e in veut_reponse if not e.get("draft_ok")]
    securite = [e for e in classified if e["category"] == "securite_google"]
    if not (entretiens or a_repondre or echecs_brouillon or securite):
        return

    lines = []
    if entretiens:
        lines.append(f"{len(entretiens)} interview(s): " + ", ".join(e["subject"] for e in entretiens[:3]))
    if a_repondre:
        lines.append(f"{len(a_repondre)} reply draft(s) prepared, review before sending")
    if echecs_brouillon:
        # Never stay quiet about a draft that silently failed to get created.
        lines.append(f"{len(echecs_brouillon)} reply draft(s) FAILED, needs manual handling: "
                     + ", ".join(e["subject"] for e in echecs_brouillon[:3]))
    if securite:
        lines.append(f"{len(securite)} Google security alert(s)")

    title = "hobot -- mail" + ("s" if len(classified) > 1 else "")
    notify_all(title, "\n".join(lines), urgency="critical" if (entretiens or echecs_brouillon) else "normal")


def build_graph():
    graph = StateGraph(EmailState)
    graph.add_node("fetch_new", fetch_new_node)
    graph.add_node("classify", classify_node)
    graph.add_node("link", link_to_application_node)
    graph.add_node("draft_reply", draft_reply_node)
    graph.add_node("log_run", log_run_node)

    graph.add_edge(START, "fetch_new")
    graph.add_edge("fetch_new", "classify")
    graph.add_edge("classify", "link")
    graph.add_edge("link", "draft_reply")
    graph.add_edge("draft_reply", "log_run")
    graph.add_edge("log_run", END)
    return graph.compile()


if __name__ == "__main__":
    init_db()
    app = build_graph()
    result = app.invoke({"raw_emails": [], "classified": []})
    print(f"\n{len(result['classified'])} new emails classified.")
    for e in result["classified"]:
        veut_reponse = e["needs_reply"] and e["account"] == email_tools.SEND_ACCOUNT
        flag = ""
        if veut_reponse:
            flag = " [DRAFTED]" if e.get("draft_ok") else " [DRAFT FAILED]"
        print(f"- [{e['category']}] {e['subject']!r} -- {e['from_addr']} ({e['account']}){flag}")
