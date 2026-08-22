"""Checks that a generated PDF (a tailored CV, a cover letter) is actually
ATS-readable -- has a real, extractable text layer, not just glyphs that
look right on screen while an Applicant Tracking System's own text
extraction sees little or nothing. Advisory only: never raises, and never
blocks a document from being saved -- a false positive here must not stop
letter drafting or CV tailoring, the same convention tailor_cv() itself
already documents ("never raises past this boundary").

MIN_CHARS is deliberately a low floor, not a quality bar: tools/cv_tailor.py's
pdf_image branch only ever makes ONE paragraph on an otherwise
still-image-only page newly extractable (see its module docstring and the
README's "CV tailoring" section) -- checking for a full page of extractable
text there would misfire on a document working exactly as designed. This
only catches the catastrophic case: near-empty extraction, meaning the PDF
is entirely image-only or a font substitution silently failed."""
import logging
from pathlib import Path

log = logging.getLogger("ats_check")

MIN_CHARS = 200


def extract_pdf_text(pdf_path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


def check_ats_readable(pdf_path: Path, min_chars: int = MIN_CHARS) -> tuple[bool, str]:
    """Returns (ok, reason). ok=False means this PDF likely won't survive an
    ATS's text extraction -- worth a second look, not proof it's broken (a
    scanner-specific quirk could still produce a false negative here, and a
    deliberately partial edit -- see the module docstring -- is not one)."""
    try:
        text = extract_pdf_text(pdf_path)
    except Exception as e:
        return False, f"could not read the PDF back ({e})"
    if len(text) < min_chars:
        return False, f"only {len(text)} character(s) of extractable text"
    return True, f"{len(text)} character(s) extracted"


def log_if_not_ats_readable(pdf_path: Path, context: str) -> bool:
    """Best-effort wrapper for callers that just want this checked and
    logged, never raised -- see the module docstring on why a check failure
    here must stay a warning, never an exception."""
    try:
        ok, reason = check_ats_readable(pdf_path)
        if not ok:
            log.warning("ATS check failed for %s (%s): %s", pdf_path, context, reason)
        return ok
    except Exception as e:
        log.warning("ATS check itself failed for %s (%s): %s", pdf_path, context, e)
        return False
