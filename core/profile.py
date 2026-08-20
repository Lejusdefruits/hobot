"""Parses a candidate's profile into user_profile, from a CV (PDF) or free text.

Deliberately kept separate from any LangGraph graph: it runs once (or on
demand, through chat), never inside the scheduled loop. The structured
result, not the raw text, is what the graphs (scoring, letters) read back
afterward.
"""
import json
import os
import sys
from pathlib import Path

import pymupdf as fitz
from dotenv import load_dotenv
from pypdf import PdfReader

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

CV_PATH = Path(os.environ.get("CV_PATH", ""))

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


def parse_cv(pdf_path: Path = CV_PATH) -> dict:
    from core.llm import chat_json

    cv_text = extract_text(pdf_path)

    if cv_text:
        profile = chat_json(EXTRACTION_PROMPT.format(cv_text=cv_text))
    else:
        images = render_page_images(pdf_path)
        profile = chat_json(VISION_PROMPT, images=images)

    profile.setdefault("raw_text", cv_text)
    return profile


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
              "you can also set your profile directly in Discord via /demande "
              "\"definir_profil: <your text>\".")
        sys.exit(1)
    profile = parse_cv()
    save_profile(profile)
    print(f"Profile extracted from {CV_PATH} and saved to user_profile.\n")
    print("Name:", profile.get("full_name") or "(not detected)")
    print("Skills:", ", ".join(profile.get("skills", [])) or "(none)")
    print("Target roles:", ", ".join(profile.get("target_roles", [])) or "(none)")
    print("Target locations:", ", ".join(profile.get("target_locations", [])) or "(none)")
