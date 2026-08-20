"""Email tools -- entirely optional (see README): if no GMAIL_ACCOUNT_i is set
in .env, CREDENTIALS stays empty and daemon.py doesn't even schedule email
watching. Read access on every listed account (GMAIL_ACCOUNT_1..N), but only
GMAIL_SEND_ACCOUNT can send or create a draft -- enforced in code here, not
just documented.
"""
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv
from imap_tools import AND, MailBox

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Without an explicit timeout, a stalled IMAP connection (Gmail slowing down
# after a lot of requests) blocks the caller indefinitely without raising.
IMAP_TIMEOUT = int(os.environ.get("IMAP_TIMEOUT_SECONDS", "20"))

CREDENTIALS: dict[str, str] = {}
_i = 1
while f"GMAIL_ACCOUNT_{_i}" in os.environ:
    CREDENTIALS[os.environ[f"GMAIL_ACCOUNT_{_i}"]] = os.environ[f"GMAIL_APP_PASSWORD_{_i}"]
    _i += 1

SEND_ACCOUNT = os.environ.get("GMAIL_SEND_ACCOUNT")  # None -> sending/drafting disabled


def verifier_compte(email_compte: str) -> str:
    if email_compte not in CREDENTIALS:
        raise ValueError(f"Account {email_compte} isn't configured on the server.")
    return CREDENTIALS[email_compte]


def verifier_compte_envoi(email_compte: str) -> str:
    if not SEND_ACCOUNT:
        raise ValueError("No GMAIL_SEND_ACCOUNT set in .env -- sending/drafting is disabled.")
    if email_compte != SEND_ACCOUNT:
        raise ValueError(
            f"Send/draft refused for {email_compte}: only {SEND_ACCOUNT} "
            f"is allowed to send outgoing mail (GMAIL_SEND_ACCOUNT in .env)."
        )
    return verifier_compte(email_compte)


def lire_emails(email_compte: str, limite: int = 5) -> str:
    """Reads the most recent emails received on the given mailbox."""
    mot_de_passe = verifier_compte(email_compte)
    resultat = ""
    try:
        with MailBox('imap.gmail.com', timeout=IMAP_TIMEOUT).login(email_compte, mot_de_passe) as mailbox:
            for msg in mailbox.fetch(limit=limite, reverse=True):
                resultat += f"ID: {msg.uid}\nFrom: {msg.from_}\nSubject: {msg.subject}\nBody: {msg.text[:300]}\n---\n"
        return resultat if resultat else "No emails found."
    except Exception as e:
        return f"Error reading IMAP for {email_compte}: {e}"


def lire_emails_bruts(email_compte: str, limite: int = 10) -> list:
    """Graph-facing version: returns raw imap_tools objects (uid, from_,
    subject, text, date...) instead of a string formatted for an LLM."""
    mot_de_passe = verifier_compte(email_compte)
    with MailBox('imap.gmail.com', timeout=IMAP_TIMEOUT).login(email_compte, mot_de_passe) as mailbox:
        return list(mailbox.fetch(limit=limite, reverse=True))


def rechercher_emails(
    mots_cles: str = "",
    depuis: str = None,
    jusqua: str = None,
    comptes: list[str] = None,
    limite_par_compte: int = 20,
) -> list[dict]:
    """Server-side IMAP search (not just the most recent mails) -- mots_cles
    searches both the body and subject, depuis/jusqua in "YYYY-MM-DD" format.
    comptes=None searches every configured account. On-demand search (user-
    triggered), not automatic polling -- no anti-ban pacing concern here."""
    cibles = comptes or list(CREDENTIALS.keys())
    resultats = []
    for compte in cibles:
        try:
            mot_de_passe = verifier_compte(compte)
        except ValueError as e:
            resultats.append({"compte": compte, "erreur": str(e)})
            continue

        criteria_kwargs = {}
        if mots_cles:
            criteria_kwargs["text"] = mots_cles
        if depuis:
            criteria_kwargs["date_gte"] = datetime.strptime(depuis, "%Y-%m-%d").date()
        if jusqua:
            criteria_kwargs["date_lt"] = datetime.strptime(jusqua, "%Y-%m-%d").date()
        criteria = AND(**criteria_kwargs) if criteria_kwargs else "ALL"

        try:
            with MailBox('imap.gmail.com', timeout=IMAP_TIMEOUT).login(compte, mot_de_passe) as mailbox:
                for msg in mailbox.fetch(criteria=criteria, limit=limite_par_compte, reverse=True):
                    resultats.append({
                        "compte": compte, "uid": msg.uid, "de": msg.from_, "sujet": msg.subject,
                        "date": str(msg.date), "extrait": (msg.text or msg.html or "")[:300],
                    })
        except Exception as e:
            resultats.append({"compte": compte, "erreur": str(e)})
    return resultats


def envoyer_ou_repondre_email(
    email_compte: str,
    destinataire: str,
    sujet: str,
    contenu: str,
    message_id_original: str = None
) -> str:
    """Sends a new email or replies to an existing one."""
    mot_de_passe = verifier_compte_envoi(email_compte)

    msg = EmailMessage()
    msg['From'] = email_compte
    msg['To'] = destinataire
    msg['Subject'] = sujet
    msg.set_content(contenu)

    if message_id_original:
        msg['In-Reply-To'] = message_id_original
        msg['References'] = message_id_original

    try:
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=IMAP_TIMEOUT) as server:
            server.starttls()
            server.login(email_compte, mot_de_passe)
            server.send_message(msg)
        return f"Email sent successfully from {email_compte} to {destinataire}."
    except Exception as e:
        return f"Error during SMTP send: {e}"


def _find_drafts_folder(mailbox: MailBox) -> str:
    """Finds the real name of the Drafts folder via its IMAP \\Drafts flag,
    instead of guessing a localized name ("Drafts" in English, "Brouillons"
    in French...) that depends on the Gmail account's language."""
    for info in mailbox.folder.list():
        if "\\Drafts" in info.flags:
            return info.name
    raise RuntimeError("No Drafts folder (IMAP \\Drafts flag) found on this account.")


def creer_brouillon(email_compte: str, destinataire: str, sujet: str, contenu: str) -> str:
    """Creates a NEW draft in the Gmail mailbox without sending it. To modify
    or delete an existing draft, use modifier_brouillon/supprimer_brouillon --
    calling creer_brouillon on an already-created draft produces a duplicate
    instead of replacing it (IMAP has no "edit in place" operation).

    Relies on imap_tools.MailBox.append(), which checks the command's own
    status and raises on failure instead of returning a silent error status
    that has to be checked manually. The Drafts folder is found dynamically
    via its IMAP \\Drafts flag (robust to the account's language, see
    _find_drafts_folder)."""
    mot_de_passe = verifier_compte_envoi(email_compte)

    msg = EmailMessage()
    msg['From'] = email_compte
    msg['To'] = destinataire
    msg['Subject'] = sujet
    msg.set_content(contenu)

    try:
        with MailBox('imap.gmail.com', timeout=IMAP_TIMEOUT).login(email_compte, mot_de_passe) as mailbox:
            dossier_brouillons = _find_drafts_folder(mailbox)
            mailbox.append(msg.as_bytes(), folder=dossier_brouillons)
        return f"Draft saved successfully on {email_compte} (folder {dossier_brouillons})."
    except Exception as e:
        return f"Error creating draft: {e}"


def lister_brouillons(email_compte: str = None, limite: int = 20) -> list[dict]:
    """Lists existing drafts (uid, recipient, subject, preview, date) on
    email_compte (defaults to SEND_ACCOUNT, the only account drafts get
    created on) -- needed to find the right uid before modifying or deleting
    a specific draft. Returns an empty list (no exception) if the folder is
    empty."""
    email_compte = email_compte or SEND_ACCOUNT
    mot_de_passe = verifier_compte_envoi(email_compte)
    with MailBox('imap.gmail.com', timeout=IMAP_TIMEOUT).login(email_compte, mot_de_passe) as mailbox:
        dossier_brouillons = _find_drafts_folder(mailbox)
        mailbox.folder.set(dossier_brouillons)
        return [
            {
                "uid": msg.uid, "destinataire": msg.to, "sujet": msg.subject,
                "apercu": (msg.text or msg.html or "")[:200], "date": str(msg.date),
            }
            for msg in mailbox.fetch(limit=limite, reverse=True)
        ]


def supprimer_brouillon(uid: str, email_compte: str = None) -> str:
    """Permanently deletes a draft by its uid (obtained via lister_brouillons).
    Irreversible on the IMAP side (STORE \\Deleted + EXPUNGE)."""
    email_compte = email_compte or SEND_ACCOUNT
    mot_de_passe = verifier_compte_envoi(email_compte)
    try:
        with MailBox('imap.gmail.com', timeout=IMAP_TIMEOUT).login(email_compte, mot_de_passe) as mailbox:
            dossier_brouillons = _find_drafts_folder(mailbox)
            mailbox.folder.set(dossier_brouillons)
            result = mailbox.delete(uid)
        if result is None:
            return f"No draft with uid {uid} (nothing to delete)."
        return f"Draft {uid} deleted on {email_compte}."
    except Exception as e:
        return f"Error deleting draft: {e}"


def modifier_brouillon(uid: str, destinataire: str, sujet: str, contenu: str, email_compte: str = None) -> str:
    """Replaces the content of an existing draft (uid obtained via
    lister_brouillons) -- IMAP has no way to edit a message in place, so this
    deletes the old draft THEN creates a new one with the updated content. If
    the deletion succeeds but the creation fails, the original draft is
    already gone -- the caller then needs to retry the creation (the error
    message says so), not just retry modifier_brouillon with the same uid
    (it no longer exists)."""
    email_compte = email_compte or SEND_ACCOUNT
    suppression = supprimer_brouillon(uid, email_compte)
    if suppression.startswith("Error"):
        return f"Update cancelled, the old draft couldn't be deleted: {suppression}"
    creation = creer_brouillon(email_compte, destinataire, sujet, contenu)
    if creation.startswith("Error"):
        return (f"Old draft deleted but recreating it failed: {creation}. "
                f"Call creer_brouillon again to recreate it (not modifier_brouillon, the old uid no longer exists).")
    return f"Draft {uid} replaced. {creation}"


def tester_les_comptes() -> None:
    print("\n" + "=" * 50)
    print(" STARTING GMAIL ACCOUNT TESTS ".center(50, "="))
    print("=" * 50 + "\n")

    for email_compte, mot_de_passe in CREDENTIALS.items():
        print(f"[test] Connecting for: {email_compte}...")
        try:
            with MailBox('imap.gmail.com', timeout=IMAP_TIMEOUT).login(email_compte, mot_de_passe) as mailbox:
                mails = list(mailbox.fetch(limit=1, reverse=True))
                print("[OK] Authentication successful.")
                if mails:
                    dernier_mail = mails[0]
                    print("     Most recent email:")
                    print(f"       From:    {dernier_mail.from_}")
                    print(f"       Subject: {dernier_mail.subject}\n")
                else:
                    print("     Inbox is empty.\n")
        except Exception as e:
            print("[ERROR] Couldn't connect.")
            print(f"        Detail: {e}\n")

    print("=" * 50)
    print(" TESTS DONE ".center(50, "="))
    print("=" * 50 + "\n")


if __name__ == "__main__":
    tester_les_comptes()
