"""PDF rendering for cover letters.

One PDF file per offer (outputs/{offer_id}/) instead of a flat .txt: when
actually applying, a file named "Cover_Letter.pdf" makes a much better
impression as an attachment than a technical id. WeasyPrint (HTML/CSS -> PDF)
+ Jinja2 (templates, auto-escaping -- the inserted text comes from an LLM,
never trusted blindly as HTML)."""
import os
from datetime import date
from pathlib import Path

import weasyprint
from jinja2 import Environment, FileSystemLoader

OUTPUT_DIR = Path(__file__).resolve().parent.parent / os.environ.get("HOBOT_OUTPUT_DIR", "outputs")
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
_CSS = (TEMPLATES_DIR / "style.css").read_text(encoding="utf-8")


def offer_output_dir(offer_id: int) -> Path:
    return OUTPUT_DIR / str(offer_id)


def letter_path(offer_id: int) -> Path:
    return offer_output_dir(offer_id) / "Cover_Letter.pdf"


def generate_letter_pdf(offer_id: int, lettre: str, full_name: str | None = None) -> Path:
    template = _jinja_env.get_template("lettre.html")
    html = template.render(
        css=_CSS, lettre=lettre,
        full_name=full_name or "[Your name]",
        date=date.today().strftime("%d/%m/%Y"),
    )
    pdf_bytes = weasyprint.HTML(string=html).write_pdf()
    path = letter_path(offer_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf_bytes)
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
        999999, "Dear Sir or Madam,\n\nTest text.\n\nBest regards,", full_name="First Last",
    )
    print("Letter:", path)
