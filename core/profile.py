"""Parses a candidate's profile into user_profile, from a CV (PDF or .docx) or
free text.

Deliberately kept separate from any LangGraph graph: it runs once (or on
demand, through chat), never inside the scheduled loop. The structured
result, not the raw text, is what the graphs (scoring, letters) read back
afterward.
"""
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Literal

import pymupdf as fitz
from dotenv import load_dotenv
from docx import Document
from pypdf import PdfReader

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# None (not Path("")) when unset: Path("") normalizes to Path('.'), the cwd,
# which is always truthy and whose .exists() is always True -- that silently
# defeated the "CV_PATH isn't set" guard below on the default, empty .env.
_cv_path_raw = os.environ.get("CV_PATH", "").strip()
CV_PATH = Path(_cv_path_raw) if _cv_path_raw else None
PROFILE_DIR = Path(__file__).resolve().parent.parent / os.environ.get("HOBOT_PROFILE_DIR", "profile_source")

CvFormat = Literal["pdf_text", "pdf_image", "docx"]

# Below this many detected items across skills/target_roles/target_locations,
# /profile follows up with clarifying questions instead of silently accepting
# a thin profile -- see detect_gaps().
MIN_SKILLS_BEFORE_GAP = 2

_SCHEMA = """{{
  "full_name": "candidate's first + last name, or null if absent from the text",
  "skills": ["technical skill 1", "..."],
  "experience": [{{"poste": "...", "entreprise": "...", "periode": "...", "resume": "..."}}],
  "education": [{{"diplome": "...", "etablissement": "...", "periode": "..."}}],
  "target_roles": ["target job title 1", "..."],
  "target_locations": ["target city or area 1", "..."]
}}"""

EXTRACTION_PROMPT = f"""You're reading raw text extracted from a CV PDF (layout is lost, text is sometimes out of order).
Return ONLY a valid JSON object, no surrounding text, with exactly these keys:

{_SCHEMA}

Infer target_roles and target_locations from the CV's context (education, experience, stated
target role) even if they aren't spelled out verbatim. If a piece of information is missing,
use an empty list (or null for full_name).

CV:
---
{{cv_text}}
---
"""

VISION_PROMPT = f"""You're looking at an image of a CV page (layout may use columns or graphical blocks,
this is a CV with no text layer, read directly from the image).
Return ONLY a valid JSON object, no surrounding text, with exactly these keys
(plus "raw_text": a raw transcription of all visible text, in natural reading order):

{_SCHEMA}

Infer target_roles and target_locations from the CV's context (education, experience, stated
target role) even if they aren't spelled out verbatim. If a piece of information is missing,
use an empty list (or null for full_name).
"""

# Alternative to a CV: the user describes what they're looking for in plain
# language ("I'm a Python developer, 3 years of experience, looking for a role
# in Lyon...") instead of providing a PDF -- same output schema, prompt tuned
# for a short free-text input rather than a structured document.
TEXT_PROFILE_PROMPT = f"""A candidate describes their profile and/or the role they're looking for in plain language.
Return ONLY a valid JSON object, no surrounding text, with exactly these keys:

{_SCHEMA}

Infer what you can from the text (skills, experience mentioned, target role and location)
even if it isn't spelled out verbatim -- the text can be as short as one sentence or as
detailed as a full CV. If a piece of information is missing, use an empty list (or null
for full_name) rather than making something up.

Candidate's text:
---
{{texte}}
---
"""


def extract_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


def extract_docx_text(docx_path: Path) -> str:
    doc = Document(str(docx_path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def render_page_images(pdf_path: Path, dpi: int = 200) -> list[bytes]:
    """CV with no text layer (a Canva/design export) -> rasterize it and use
    the model's vision capability instead of adding an OCR dependency."""
    doc = fitz.open(str(pdf_path))
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    try:
        return [page.get_pixmap(matrix=matrix).tobytes("png") for page in doc]
    finally:
        doc.close()


def detect_format(path: Path) -> CvFormat:
    if path.suffix.lower() == ".docx":
        return "docx"
    return "pdf_text" if extract_text(path) else "pdf_image"


def parse_cv(path: Path | None = CV_PATH, fmt: CvFormat | None = None) -> dict:
    """Dispatches on the CV's actual format -- a real text layer (PDF or
    .docx) goes straight to the LLM as text, a flattened/rasterized PDF
    (no text layer at all, common for a Canva-style export) falls back to
    reading the page images with the model's vision capability."""
    from core.llm import chat_json

    fmt = fmt or detect_format(path)

    if fmt == "docx":
        cv_text = extract_docx_text(path)
        profile = chat_json(EXTRACTION_PROMPT.format(cv_text=cv_text))
    elif fmt == "pdf_text":
        cv_text = extract_text(path)
        profile = chat_json(EXTRACTION_PROMPT.format(cv_text=cv_text))
    else:
        cv_text = ""
        images = render_page_images(path)
        profile = chat_json(VISION_PROMPT, images=images)

    profile.setdefault("raw_text", cv_text)
    return profile


def detect_gaps(profile: dict) -> list[str]:
    """Deterministic, no LLM call -- what's thin or missing in an extracted
    profile, for /profile to follow up on. Kept as plain checks on purpose:
    reliably noticing what's missing from a JSON blob and phrasing good
    questions about it in one step asks more of a small local model than it
    can be trusted with (see the follow-up-questions flow in
    tools/discord_bot.py) -- computing the gaps here removes that risk, the
    model's only job afterward is to phrase them."""
    gaps = []
    if not profile.get("target_locations"):
        gaps.append("no target location detected")
    target_roles = profile.get("target_roles") or []
    if not target_roles or (len(target_roles) == 1 and len(target_roles[0].split()) <= 1):
        gaps.append("target role is missing or too vague")
    if len(profile.get("skills") or []) < MIN_SKILLS_BEFORE_GAP:
        gaps.append("very few skills detected")
    return gaps


def save_profile_source(path: Path, fmt: CvFormat) -> Path:
    """Copies the uploaded original into PROFILE_DIR and records where it
    lives -- the tailoring engine (tools/cv_tailor.py) edits THIS file, not
    a re-derived template, so it has to survive past the /profile command's
    own temp download. Requires save_profile() to have already created the
    user_profile row (id=1)."""
    from core.db import get_connection

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    dest = PROFILE_DIR / f"original{path.suffix.lower()}"
    shutil.copyfile(path, dest)
    with get_connection() as conn:
        conn.execute(
            "UPDATE user_profile SET cv_source_path = ?, cv_format = ?, cv_uploaded_at = datetime('now') "
            "WHERE id = 1",
            (str(dest), fmt),
        )
    return dest


def parse_text(texte: str) -> dict:
    """CV-free intake: free text (one sentence or several paragraphs)
    describing the profile/target role, extracted with the same output
    schema as a CV. Used by chat (see graphs/chat_agent.py::definir_profil)."""
    from core.llm import chat_json

    profile = chat_json(TEXT_PROFILE_PROMPT.format(texte=texte))
    profile.setdefault("raw_text", texte)
    return profile


def save_profile(profile: dict) -> None:
    from core.db import get_connection, init_db

    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_profile (id, full_name, raw_text, skills, experience, education, target_roles, target_locations, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                full_name=excluded.full_name, raw_text=excluded.raw_text, skills=excluded.skills,
                experience=excluded.experience, education=excluded.education,
                target_roles=excluded.target_roles, target_locations=excluded.target_locations,
                updated_at=excluded.updated_at
            """,
            (
                profile.get("full_name"),
                profile.get("raw_text", ""),
                json.dumps(profile.get("skills", []), ensure_ascii=False),
                json.dumps(profile.get("experience", []), ensure_ascii=False),
                json.dumps(profile.get("education", []), ensure_ascii=False),
                json.dumps(profile.get("target_roles", []), ensure_ascii=False),
                json.dumps(profile.get("target_locations", []), ensure_ascii=False),
            ),
        )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if not CV_PATH or not CV_PATH.exists():
        print("CV_PATH isn't set or the file doesn't exist (see .env) -- "
              "you can also set your profile directly in Discord via /ask "
              "\"definir_profil: <your text>\".")
        sys.exit(1)
    fmt = detect_format(CV_PATH)
    profile = parse_cv(CV_PATH, fmt=fmt)
    save_profile(profile)
    save_profile_source(CV_PATH, fmt)
    print(f"Profile extracted from {CV_PATH} ({fmt}) and saved to user_profile.\n")
    print("Name:", profile.get("full_name") or "(not detected)")
    print("Skills:", ", ".join(profile.get("skills", [])) or "(none)")
    print("Target roles:", ", ".join(profile.get("target_roles", [])) or "(none)")
    print("Target locations:", ", ".join(profile.get("target_locations", [])) or "(none)")
    gaps = detect_gaps(profile)
    if gaps:
        print("\nStill thin:", ", ".join(gaps))
