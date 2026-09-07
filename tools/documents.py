"""PDF rendering for cover letters -- Typst (@preview/modernpro-coverletter).

One PDF file per offer (outputs/{offer_id}/) instead of a flat .txt: when
actually applying, a file named "Cover_Letter.pdf" makes a much better
impression as an attachment than a technical id.

hobot supplies the letter data (profile, recipient, the letter body itself)
as a small prelude of #let bindings, then a .typ template consumes them
however it wants and hobot appends the actual body text as plain paragraphs
below it. templates/lettre.typ (bundled, @preview/modernpro-coverletter) is
the default for anyone who clones this repo and never sets
COVER_LETTER_TEMPLATE_PATH (.env) -- point that at your own .typ file (same
bindings, same body-appended-after contract, see that file's own header
comment) to use a different design instead.

The letter body itself is never trusted as Typst markup: it comes from an
LLM (or a human editing it in the terminal UI), so every character that
means something to Typst's parser gets escaped before being written into
the .typ source -- same reasoning the old Jinja2 autoescape had for HTML,
back when this rendered through WeasyPrint instead."""
import os
import re
from datetime import date
from pathlib import Path

import typst

from core.db import get_connection, get_user_profile
from tools.common import company_label

# Letters are written in whichever language matches the job posting
# (LETTER_PROMPT, graphs/discovery_graph.py), French most of the time given
# hobot's own market -- but the template's own date formatting always spells
# the month out in English (Typst has no locale support here), which read as
# a language mismatch in an otherwise-French letter. Detected from the
# letter text itself rather than assumed, so an English posting still gets
# an English date. Deliberately not `date.today().strftime("%B")`: that
# reads the HOST MACHINE's locale, not the letter's language -- deterministic
# either way regardless of what's installed on whoever's machine runs this.
_FR_MONTHS = ("janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet",
              "aout", "septembre", "octobre", "novembre", "decembre")
_EN_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December")
_FR_WORDS = {"le", "la", "les", "et", "de", "des", "du", "vous", "nous", "votre",
             "notre", "etre", "avec", "pour", "que", "dans", "poste", "candidature",
             "entreprise", "monsieur", "madame", "cordialement", "salutations"}
_EN_WORDS = {"the", "and", "of", "you", "your", "our", "with", "for", "that", "in",
             "position", "application", "company", "dear", "sincerely", "regards"}


def _strip_accents(text: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _is_french(text: str) -> bool:
    words = re.findall(r"[a-z]+", _strip_accents(text.lower()))
    fr = sum(1 for w in words if w in _FR_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    return fr >= en  # a tie defaults to French, hobot's primary market


def _display_date(is_french: bool) -> str:
    today = date.today()
    months = _FR_MONTHS if is_french else _EN_MONTHS
    return f"{today.day} {months[today.month - 1]} {today.year}"

OUTPUT_DIR = Path(__file__).resolve().parent.parent / os.environ.get("HOBOT_OUTPUT_DIR", "outputs")
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
DEFAULT_TEMPLATE_PATH = TEMPLATES_DIR / "lettre.typ"


def offer_output_dir(offer_id: int) -> Path:
    return OUTPUT_DIR / str(offer_id)


def letter_path(offer_id: int) -> Path:
    return offer_output_dir(offer_id) / "Cover_Letter.pdf"


def _typst_str(value: str | None) -> str:
    """A Typst string literal, or the `none` literal for a missing value --
    never a bare Python None interpolated into the source, which would break
    the compile instead of just leaving that field blank."""
    if not value:
        return "none"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


def _typst_contacts(contacts: list[tuple[str, str]]) -> str:
    if not contacts:
        return "()"
    items = ", ".join(f"(text: {_typst_str(text)}, link: {_typst_str(link)})" for text, link in contacts)
    return f"({items},)"


# Typst markup's own special characters -- escaped so the letter body (never
# authored with Typst in mind) renders as the plain prose it is instead of
# being parsed as formatting commands, section headings, or a raw expression.
_MARKUP_ESCAPE = str.maketrans({c: "\\" + c for c in "\\#*_`<>@$[]"})


def _strip_signed_name(lettre: str, full_name: str) -> str:
    """Drops a trailing line that's just the signature (`full_name`) -- the
    template renders its own bold name after the letter body (render-closing
    in modernpro-coverletter), so leaving the LLM's own copy in place would
    sign the letter twice. Deliberately narrow: only the exact trailing name
    line goes, never the closing phrase before it ("Sincerely,", "Je vous
    prie d'agreer...") -- that phrase's wording depends on the letter's own
    language (chosen to match the job posting, English or French or
    otherwise) and isn't something to guess/localize here; left as the last
    line of body prose, it reads naturally right before the template's own
    signature line either way."""
    lines = lettre.rstrip().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip().casefold() == full_name.strip().casefold():
        lines.pop()
    return "\n".join(lines).rstrip()


def _body_markup(lettre: str) -> str:
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", lettre)]
    return "\n\n".join(p.translate(_MARKUP_ESCAPE) for p in paragraphs if p)


def generate_letter_pdf(
    offer_id: int, lettre: str, full_name: str | None = None, company_override: str | None = None,
) -> Path:
    """company_override: a company name the letter-writing LLM read out of
    the offer's own description when offers.company was empty (see
    LETTER_PROMPT's "entreprise_detectee", graphs/discovery_graph.py) --
    used here ONLY when the structured company column is itself empty, so a
    known, structured name is never second-guessed by a free-text read.
    Without this, the recipient header fell back to company_label's own
    "(company withheld)" even when the LLM had already confidently named
    the company in the letter body it just wrote -- confirmed live on offer
    #267 (France Travail: company column empty, but the description opens
    by naming the employer), where the header and body ended up
    contradicting each other."""
    with get_connection() as conn:
        offer = conn.execute(
            "SELECT company, title, location FROM offers WHERE id = ?", (offer_id,)
        ).fetchone()
    profile = get_user_profile() or {}
    name = full_name or profile.get("full_name") or "[Your name]"
    role = (profile.get("target_roles") or [None])[0]
    address = ", ".join(profile.get("target_locations") or []) or None
    send_account = os.environ.get("GMAIL_SEND_ACCOUNT") or None
    contacts = [(send_account, f"mailto:{send_account}")] if send_account else []
    recipient_company = (offer["company"] if offer else None) or company_override

    prelude = "\n".join((
        f"#let hobot_name = {_typst_str(name)}",
        f"#let hobot_role = {_typst_str(role)}",
        f"#let hobot_address = {_typst_str(address)}",
        f"#let hobot_contacts = {_typst_contacts(contacts)}",
        f"#let hobot_recipient_name = {_typst_str(company_label(recipient_company))}",
        f"#let hobot_recipient_address = {_typst_str(offer['location'] if offer else None)}",
        f"#let hobot_subject = {_typst_str(offer['title'] if offer else None)}",
        f"#let hobot_date = {_typst_str(_display_date(_is_french(lettre)))}",
    ))

    template_path = Path(os.environ.get("COVER_LETTER_TEMPLATE_PATH") or DEFAULT_TEMPLATE_PATH)
    template_source = template_path.read_text(encoding="utf-8")
    body = _body_markup(_strip_signed_name(lettre, name))
    typ_source = f"{prelude}\n\n{template_source}\n\n{body}\n"

    out_dir = offer_output_dir(offer_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    typ_path = out_dir / "Cover_Letter.typ"
    typ_path.write_text(typ_source, encoding="utf-8")

    path = letter_path(offer_id)
    typst.compile(str(typ_path), output=str(path))

    from tools.ats_check import log_if_not_ats_readable
    log_if_not_ats_readable(path, context=f"cover letter, offer #{offer_id}")
    return path


def read_letter_text(path: Path) -> str:
    """Reads back the text of an already-generated letter -- handles both an
    old .txt (before PDF rendering existed) and a current .pdf, so upgrading
    doesn't break reading letters already on disk."""
    if path.suffix.lower() != ".pdf":
        return path.read_text(encoding="utf-8")
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


if __name__ == "__main__":
    path = generate_letter_pdf(
        999999, "Dear Sir or Madam,\n\nTest text.\n\nBest regards,\nFirst Last", full_name="First Last",
    )
    print("Letter:", path)
