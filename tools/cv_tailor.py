"""Per-offer CV tailoring: edits the candidate's own uploaded CV in place --
same file, same layout, same fonts, same colors -- rather than regenerating
a document from a template. Only two things ever change: the profile/summary
paragraph, and (if the CV's skills section is a plain text list, not an
icon/pill layout) which of the candidate's real skills fill the visible
slots. Nothing else -- name, contact info, dates, job titles, company names,
degree names, images, colors, section order -- is ever touched. Never
invents a skill, a project, or an experience not already in user_profile.

Dispatches on user_profile.cv_format:
- pdf_text: the CV has a real, extractable text layer. Redact the target
  text region and reinsert the new text with matched font/size/color -- the
  common, highest-fidelity case.
- docx: the CV is a Word document. Renders through a Jinja2 tag inserted
  into the source once (see _ensure_docx_template), then docxtpl fills it
  per offer and LibreOffice converts the result to PDF.
- pdf_image: the CV is a flattened raster export (common for Canva "PDF for
  print" downloads) with zero extractable text. True in-place editing is
  impossible here -- there's no text object to redact. The realistic,
  honest version of this feature for such a CV is: locate the summary
  paragraph's region with one narrowly-scoped vision call, cover it, and
  insert the new text as a REAL PDF text object (not repainted pixels) --
  the point of tailoring is ATS keyword matching, and an ATS parser gets
  nothing from pixels. This branch cannot promise the same visual fidelity
  as the other two and says so in its own return value.
"""
import os
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from pathlib import Path

import pymupdf as fitz

from core.db import get_connection
from core.llm import chat_json
from core.profile import PROFILE_DIR
from tools.common import normalize_text

OUTPUT_DIR = Path(__file__).resolve().parent.parent / os.environ.get("HOBOT_OUTPUT_DIR", "outputs")
FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# A rewritten summary paragraph must stay close to the original length -- a
# CV is laid out for a fixed page, a longer paragraph pushes everything else
# out of position. LLMs unreliably ignore a length instruction in the prompt
# alone (confirmed in practice, not theoretical), so this is also enforced
# programmatically as a hard fallback, never prompt-only.
SUMMARY_MAX_CHARS = int(os.environ.get("CV_TAILOR_SUMMARY_MAX_CHARS", "500"))

SUMMARY_TAILOR_PROMPT = """You are rewriting the "profile" or "summary" paragraph at the top of a
candidate's CV for ONE specific job posting, without ever inventing anything.

Current summary (never stray from the facts it contains):
{original}

Candidate's real skills: {skills}
Candidate's real experience: {experience}
Candidate's real education: {education}

Target posting:
- Title: {title}
- Description: {description}

Rewrite this paragraph (same tone, same language as the current summary) highlighting whichever
REAL elements above best fit this posting, echoing the posting's own wording where that's honest
to do. NEVER invent a skill, project, experience, or degree that isn't listed above -- a
paragraph that stays close to the original beats an inaccurate one.

STRICT LIMIT: {max_chars} characters maximum, spaces included -- the original is {original_chars}
characters, do not exceed it. If you have to choose, a shorter, cleaner paragraph beats a
complete but too-long one.

Reply with ONLY JSON: {{"summary": "..."}}"""

SKILL_PICK_PROMPT = """A candidate's CV lists these skills, some visible on the CV page, some only
known from their fuller profile (added later, not yet on the visible page):

Visible on the CV right now: {visible}
Known but not currently visible: {hidden}

Target posting:
- Title: {title}
- Description: {description}

Pick UP TO {max_picks} skill(s) from the "known but not currently visible" list that are
genuinely relevant to this posting and worth surfacing -- never invent a new one, never pick
one that isn't in that list verbatim. If none of them fit, return an empty list.

Reply with ONLY JSON: {{"skills": ["..."]}}"""


def _full_profile(conn) -> dict:
    import json
    row = conn.execute(
        "SELECT skills, experience, education, cv_source_path, cv_format FROM user_profile WHERE id = 1"
    ).fetchone()
    if not row:
        return {}
    return {
        "skills": json.loads(row["skills"] or "[]"),
        "experience": json.loads(row["experience"] or "[]"),
        "education": json.loads(row["education"] or "[]"),
        "cv_source_path": row["cv_source_path"],
        "cv_format": row["cv_format"],
    }


def _truncate_to_sentence(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[: end + 1] if end > max_chars * 0.5 else cut.rstrip() + "…"


def _tailor_summary_text(original: str, profile: dict, title: str, description: str) -> str:
    """One LLM call, budget enforced both in the prompt and programmatically
    afterward -- never trust the prompt instruction alone. Falls back to the
    untouched original on any failure or empty result, same as this project's
    other best-effort generation steps (a failed rewrite must never block the
    rest of the pipeline)."""
    try:
        result = chat_json(SUMMARY_TAILOR_PROMPT.format(
            original=original,
            max_chars=SUMMARY_MAX_CHARS,
            original_chars=len(original),
            skills=", ".join(profile.get("skills", [])) or "(none)",
            experience="; ".join(
                f"{e.get('poste')} at {e.get('entreprise')} ({e.get('periode')}): {e.get('resume')}"
                for e in profile.get("experience", [])
            ) or "(none)",
            education="; ".join(
                f"{e.get('diplome')} -- {e.get('etablissement')} ({e.get('periode')})"
                for e in profile.get("education", [])
            ) or "(none)",
            title=title or "", description=(description or "")[:2000],
        ))
        summary = (result.get("summary") or "").strip()
        return _truncate_to_sentence(summary, SUMMARY_MAX_CHARS) if summary else original
    except Exception:
        return original


def _pick_hidden_skills(visible: list[str], profile_skills: list[str], title: str, description: str,
                         max_picks: int = 2) -> list[str]:
    """Skills already in user_profile.skills (e.g. added later through chat
    via modifier_profil) that aren't on the CV's visible list yet -- the
    actual valuable move here, since reordering what's already visible has
    close to no real ATS effect (text extraction doesn't weight by visual
    position). Never returns anything not verbatim in `profile_skills`."""
    visible_norm = {normalize_text(v) for v in visible}
    hidden = [s for s in profile_skills if normalize_text(s) not in visible_norm]
    if not hidden:
        return []
    try:
        result = chat_json(SKILL_PICK_PROMPT.format(
            visible=", ".join(visible) or "(none)", hidden=", ".join(hidden),
            title=title or "", description=(description or "")[:1000], max_picks=max_picks,
        ))
        picked = result.get("skills") or []
        return [s for s in picked if s in hidden][:max_picks]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# pdf_text branch
# ---------------------------------------------------------------------------

_SIMPLE_FONTS = {
    "helv": FONTS_DIR / "Montserrat-Regular.ttf",
    "hebo": FONTS_DIR / "Montserrat-Bold.ttf",
}


def _page_lines(page) -> list[dict]:
    """One entry per visual LINE, reading order (two-column aware) -- the
    granularity _locate_summary_lines/_locate_skill_lines both need: a
    redaction boundary has to land exactly on a real line's own edge, never
    blurred by however PyMuPDF happened to group lines into a block on a
    given CV export (see _locate_summary_lines's docstring for why
    block-level grouping broke)."""
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            spans = ln["spans"]
            if spans:
                lines.append({"bbox": ln["bbox"], "text": "".join(s["text"] for s in spans), "spans": spans})
    return sorted(lines, key=lambda ln: (ln["bbox"][0] > 250, ln["bbox"][1]))


def _font_glyphs_ok(fontfile: Path, text: str) -> bool:
    """False (never raises) if getBestCmap() finds no usable cmap table at
    all -- confirmed live and common, not an edge case: a CID-keyed font
    embedded Identity-H-encoded (the standard PDF export shape from Canva/
    web CV builders/many LaTeX toolchains) is looked up by the PDF's own
    ToUnicode CMap + CIDToGIDMap, never by a cmap table inside the font
    program itself, so extract_font() routinely hands back a font with none.
    There's no reliable way to recover per-character coverage from the
    extracted font alone in that case, so this reports "not safe to use"
    rather than guessing -- _pick_font's bundled-Montserrat fallback is the
    correct, safe outcome here, just short of the full visual fidelity a
    kept cmap would allow."""
    from fontTools.ttLib import TTFont
    try:
        cmap = TTFont(str(fontfile)).getBestCmap()
    except KeyError:
        return False
    return all(ord(c) in cmap for c in text)


def _pick_font(page, spans: list[dict], text: str) -> tuple[Path, float, tuple]:
    """Real embedded font first, checked glyph-by-glyph against the actual
    replacement text -- confirmed in practice that this fails routinely, not
    rarely, two different ways: a Canva/Word/Docs PDF export typically only
    embeds the glyphs its original content used, so a common letter the
    original text never needed (a 'w', a 'z') is often simply missing; and a
    CID-keyed font embedded Identity-H (also common, e.g. many LaTeX/web CV
    builder exports) extracts with no cmap table at all, so per-character
    coverage can't be checked at all (_font_glyphs_ok's docstring). On
    either miss, fall back to the bundled real Montserrat static instance
    (matches this project's most common real-world case) rather than
    silently guessing a base-14 font.

    Deliberately only DECIDES which font file to use here and does not
    register it on the page yet (no page.insert_font call) -- that has to
    happen after redaction, not before: apply_redactions() rebuilds the
    page's whole content stream, and a font registered before that call
    stops working (confirmed in practice, not theoretical)."""
    ref = spans[0]
    size = ref["size"]
    color_int = ref["color"]
    color = ((color_int >> 16 & 255) / 255, (color_int >> 8 & 255) / 255, (color_int & 255) / 255)

    # extract_font needs an xref, not a name -- PyMuPDF only exposes the font
    # name on the span, so go through get_fonts() to resolve it. Compared
    # with the PDF subset-tag prefix stripped on both sides ("ABCDEF+Real
    # Name", six uppercase letters per the PDF spec, added to a font
    # embedded as a subset) -- get_fonts() reports it, the span's own "font"
    # field never does, so a straight == never matched on any subsetted
    # embedded font and silently fell back to the bundled Montserrat every
    # time, confirmed live on a real CV (including this project's own
    # profile_source/original.pdf -- every tailored CV before this fix
    # rendered its rewritten paragraph in the wrong font).
    def _unsubset(name: str) -> str:
        return name.split("+", 1)[1] if len(name) > 7 and name[6] == "+" and name[:6].isupper() else name

    try:
        target = _unsubset(ref["font"])
        for f in page.get_fonts():
            if _unsubset(f[3]) == target:
                fontbuffer = page.parent.extract_font(f[0])[-1]
                tmpdir = Path(tempfile.mkdtemp())
                tmp = tmpdir / "embedded.ttf"
                tmp.write_bytes(fontbuffer)
                if _font_glyphs_ok(tmp, text):
                    # kept alive on purpose: _wrap_and_insert still needs to
                    # read this file later (phase 2) -- _tailor_pdf_text
                    # removes tmpdir once every edit is done.
                    return tmp, size, color
                shutil.rmtree(tmpdir, ignore_errors=True)
                break
    except Exception:
        pass

    # bundled fallback -- always covers the full glyph set we ship it for
    bold = "bold" in ref["font"].lower() or "semibold" in ref["font"].lower()
    fallback = FONTS_DIR / ("Montserrat-Bold.ttf" if bold else "Montserrat-Regular.ttf")
    return fallback, size, color


def _sample_background(page, bbox: fitz.Rect) -> tuple[int, int, int]:
    """Several sample points at a few different offsets around the block,
    keep the MOST COMMON color rather than an average -- an average gets
    dragged off by a single point that lands on a neighboring element (a
    colored badge's edge sitting right above a paragraph, confirmed on a
    real CV to produce a visibly wrong fill otherwise)."""
    votes = []
    for dy in (-10, -8, -6, bbox.height + 6, bbox.height + 8, bbox.height + 10):
        y = bbox.y0 + dy
        if y < 0 or y > page.rect.height:
            continue
        for dx in (0, bbox.width * 0.3, bbox.width * 0.6, bbox.width * 0.9):
            x = bbox.x0 + dx
            if x + 1 > page.rect.width:
                continue
            pix = page.get_pixmap(clip=fitz.Rect(x, y, x + 1, y + 1))
            if pix.samples:
                votes.append((pix.samples[0], pix.samples[1], pix.samples[2]))
    return Counter(votes).most_common(1)[0][0] if votes else (255, 255, 255)


def _wrap_lines(font: fitz.Font, fontsize: float, width: float, text: str) -> list[str]:
    """Word-wraps `text` to `width` -- measuring against the exact loaded
    font object is what actually matters here, page.get_text_length() only
    has accurate metrics for the base-14/CJK built-ins, not an arbitrary
    embedded or bundled font. Shared between _wrap_and_insert (the real
    insertion) and _fit_to_box (a dry-run measurement before committing to
    an edit) so the two can never disagree on how many lines something
    wraps to."""
    words = text.split(" ")
    lines, current = [], ""
    for w in words:
        trial = (current + " " + w).strip()
        if font.text_length(trial, fontsize) <= width:
            current = trial
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _max_lines(fontsize: float, bbox: fitz.Rect) -> int:
    return max(1, int(bbox.height / (fontsize * 1.3)))


def _fits_box(fontfile: Path, fontsize: float, bbox: fitz.Rect, text: str) -> bool:
    font = fitz.Font(fontfile=str(fontfile))
    return len(_wrap_lines(font, fontsize, bbox.width, text)) <= _max_lines(fontsize, bbox)


def _fit_to_box(fontfile: Path, fontsize: float, bbox: fitz.Rect, text: str) -> str:
    """Shrinks `text` (by whole sentences first, then hard truncation) until
    it wraps to no more lines than `bbox` has room for -- SUMMARY_MAX_CHARS
    caps total length, but a box's actual capacity depends on how many
    CHARACTERS PER LINE its width allows, which varies with the font and the
    words themselves. Confirmed live: a rewritten paragraph well under the
    character cap still wrapped to one more line than the original occupied
    and visually collided with whatever followed it on the page -- the same
    "never trust the prompt/an estimate alone, verify against the real
    rendering" principle as everywhere else in this module.

    Only for the summary paragraph -- shortening BY WHOLE SENTENCES is a
    reasonable thing to ask of prose. It is NOT used for a skill-line edit:
    cutting a keyword/skill name down to fit doesn't produce a shorter
    skill, it produces a mangled fragment (see _tailor_pdf_text's phase-1
    loop, which drops a skill edit that doesn't fit instead of ever calling
    this). Returns the original text unchanged if it already fits, so this
    is a no-op in the common case."""
    font = fitz.Font(fontfile=str(fontfile))
    max_lines = _max_lines(fontsize, bbox)
    if len(_wrap_lines(font, fontsize, bbox.width, text)) <= max_lines:
        return text
    shrunk = text
    for target in (400, 300, 220, 160, 110, 70):
        shrunk = _truncate_to_sentence(text, target)
        if len(_wrap_lines(font, fontsize, bbox.width, shrunk)) <= max_lines:
            return shrunk
    return shrunk  # smallest attempted -- still too long is better than an infinite loop


def _wrap_and_insert(page, bbox: fitz.Rect, text: str, fontfile: Path, fontsize: float, color: tuple) -> None:
    """Registers the font and inserts the text -- must run AFTER any
    redaction on this page (see _pick_font's docstring)."""
    font = fitz.Font(fontfile=str(fontfile))
    fontname = f"cvtailor-{uuid.uuid4().hex[:8]}"
    page.insert_font(fontname=fontname, fontfile=str(fontfile))

    lines = _wrap_lines(font, fontsize, bbox.width, text)
    line_height = fontsize * 1.3
    y = bbox.y0 + fontsize * 0.9
    for line in lines:
        page.insert_text((bbox.x0, y), line, fontname=fontname, fontsize=fontsize, color=color)
        y += line_height


LOCATE_SUMMARY_LINES_PROMPT = """This is a candidate's CV, extracted as numbered lines (the numbers are
only for you to reference back -- they aren't part of the actual CV):

{numbered_lines}

Find the professional summary/profile paragraph: the short introductory blurb about the
candidate, in full sentences, usually near the top. NOT its section heading if that heading
sits on its own line, NOT a bullet list, NOT the skills/experience/education sections.

Reply with ONLY JSON: {{"start_line": <int>, "end_line": <int>}} -- inclusive, exact line numbers
from the list above, covering only that paragraph's own lines. If there's no such paragraph on
this CV, reply {{"start_line": null, "end_line": null}}."""

# A genuine summary paragraph is always short -- a wider range from the LLM
# almost certainly means it swept up more than just that paragraph (a
# section heading merged into the same PyMuPDF block/run as its own content
# is exactly what fooled the previous, layout-only heuristic here into
# grabbing the entire rest of the CV, confirmed live -- see git history).
# Checked programmatically rather than trusted from the prompt alone, same
# principle as SUMMARY_MAX_CHARS below: a bad range must never reach
# redaction.
MAX_SUMMARY_LINES = 12


def _looks_like_heading(text: str) -> bool:
    text = text.strip()
    return bool(text) and text.isupper() and len(text.split()) <= 4


def _locate_summary_lines(lines: list[dict]) -> tuple[int, int] | None:
    """Where the actual summary paragraph is, asked of the LLM instead of
    inferred from block/gap/alignment heuristics: reading the CV like a
    human generalizes to whatever layout a given user's own CV happens to
    use, rather than another position-based rule tuned against one example
    CV and broken by the next one. Validated below regardless -- a
    hallucinated or over-wide range must never reach redaction."""
    numbered = "\n".join(f"[{i}] {ln['text']}" for i, ln in enumerate(lines))
    try:
        result = chat_json(LOCATE_SUMMARY_LINES_PROMPT.format(numbered_lines=numbered))
        start, end = result.get("start_line"), result.get("end_line")
        if start is None or end is None:
            return None
        start, end = int(start), int(end)
    except Exception:
        return None
    if not (0 <= start <= end < len(lines)) or end - start >= MAX_SUMMARY_LINES:
        return None
    if any(_looks_like_heading(ln["text"]) for ln in lines[start:end + 1]):
        return None  # the range itself reads as spanning into a heading -- reject, don't risk it
    return start, end


def _tailor_pdf_text(doc: fitz.Document, profile: dict, title: str, description: str) -> None:
    """Two full passes across every edit, not one pass per edit: every
    add_redact_annot() has to happen before the single apply_redactions()
    call, and every font registration + insert_text() has to happen after
    it. Interleaving (redact this zone, insert its text, redact the NEXT
    zone, insert...) was tried and confirmed broken -- a later
    apply_redactions() rebuilds the page's whole content stream and
    corrupts text inserted by an earlier one, not just breaks its own font."""
    page = doc[0]
    edits: list[dict] = []  # each: bbox, new_text, spans

    lines = _page_lines(page)
    located = _locate_summary_lines(lines)
    if located:
        start, end = located
        run = lines[start:end + 1]
        summary_original = " ".join(ln["text"] for ln in run)
        x0 = min(ln["bbox"][0] for ln in run)
        x1 = max(ln["bbox"][2] for ln in run)
        y1 = max(ln["bbox"][3] for ln in run)
        # y0: apply_redactions() removes a text span if the redaction rect
        # intersects it AT ALL, not just the pixels inside it -- and a real
        # PDF's own line bboxes routinely overlap their neighbor's by a
        # pixel or so (font-metric padding, confirmed live), so padding
        # upward the way the top-of-column case safely can risks deleting
        # the untouched line right above (a heading, most often) instead of
        # just the paragraph being replaced. Split the gap with that line
        # when there is one in the same column; only pad outward at the
        # very top of a column, where there's nothing above to protect.
        prev = lines[start - 1] if start > 0 else None
        if prev is not None and abs(prev["bbox"][0] - run[0]["bbox"][0]) <= 5:
            y0 = (prev["bbox"][3] + run[0]["bbox"][1]) / 2
        else:
            y0 = run[0]["bbox"][1] - 2
        new_summary = _tailor_summary_text(summary_original, profile, title, description)
        if new_summary != summary_original:
            edits.append({
                "bbox": fitz.Rect(x0 - 1, y0, x1 + 1, y1 + 2),
                "text": new_summary, "spans": run[0]["spans"], "truncatable": True,
            })

    # skill lines: swap the text of each currently-visible skill line for a
    # better-matching real skill the candidate has but that isn't shown yet
    # -- never touching count/position/formatting, one line in, one line out.
    # truncatable=False: unlike the summary paragraph, a skill name that
    # doesn't fit on its one line can't be shortened into a sensible
    # shorter skill -- the phase-1 loop below drops the swap entirely rather
    # than inserting a mangled fragment or letting it overflow onto whatever
    # skill line comes next (confirmed live: "Négociation immobilière"
    # replacing the shorter "Estimation de biens" wrapped to a second line
    # and visibly overlapped the skill line below it).
    skill_groups = _locate_skill_lines(lines)
    visible_skills = [" ".join(lines[i]["text"] for i in range(s, e + 1)) for s, e in skill_groups]
    hidden_picks = _pick_hidden_skills(visible_skills, profile.get("skills", []), title, description,
                                        max_picks=min(2, len(visible_skills)))
    for (s, e), new_skill in zip(skill_groups, hidden_picks):
        group = lines[s:e + 1]
        bbox = fitz.Rect(
            min(ln["bbox"][0] for ln in group), min(ln["bbox"][1] for ln in group),
            max(ln["bbox"][2] for ln in group), max(ln["bbox"][3] for ln in group),
        )
        edits.append({"bbox": bbox, "text": new_skill, "spans": group[0]["spans"], "truncatable": False})

    if not edits:
        return

    # phase 1: decide fonts/backgrounds, drop anything that won't fit its
    # box, THEN redact everything that's left -- from the page's original
    # state throughout, nothing here has touched the page yet.
    resolved = []
    for e in edits:
        fontfile, fontsize, color = _pick_font(page, e["spans"], e["text"])
        if e["truncatable"]:
            text = _fit_to_box(fontfile, fontsize, e["bbox"], e["text"])
        elif _fits_box(fontfile, fontsize, e["bbox"], e["text"]):
            text = e["text"]
        else:
            if FONTS_DIR not in fontfile.parents:
                shutil.rmtree(fontfile.parent, ignore_errors=True)
            continue  # doesn't fit and can't be shortened sensibly -- leave this one untouched
        resolved.append({**e, "text": text, "fontfile": fontfile, "fontsize": fontsize, "color": color})

    for e in resolved:
        e["bg"] = _sample_background(page, e["bbox"])
        page.add_redact_annot(e["bbox"], fill=tuple(c / 255 for c in e["bg"]))
    if resolved:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    # phase 2: register fonts and insert all the new text, only now
    for e in resolved:
        _wrap_and_insert(page, e["bbox"], e["text"], e["fontfile"], e["fontsize"], e["color"])

    # Clean up any per-edit temp dir _pick_font extracted an embedded font
    # into (kept alive until _wrap_and_insert read it just above) -- never
    # the bundled Montserrat fallback, which lives under FONTS_DIR.
    for e in resolved:
        if FONTS_DIR not in e["fontfile"].parents:
            shutil.rmtree(e["fontfile"].parent, ignore_errors=True)


LOCATE_SKILLS_PROMPT = """This is a candidate's CV, extracted as numbered lines (the numbers are
only for you to reference back -- they aren't part of the actual CV):

{numbered_lines}

Find the skills/competencies list, IF each individual skill has its own dedicated spot (its own
bullet, line, or pill) that could be swapped for a different single skill without touching
anything else. Some entries wrap onto more than one line -- group those together as ONE entry.

Do NOT match a list where several skills are run together separated by commas on the same
line(s) under one category label (e.g. "Langages: Python, C, Java" or "IA & LLM -- LangChain,
LangGraph, DSPy...") -- there's no single item to isolate there, replacing the whole line would
silently delete every other skill it lists. Reply null for that case.

Reply with ONLY JSON: {{"skills": [[start_line, end_line], ...]}} -- one [start_line, end_line]
pair per entry (inclusive line numbers from the list above, a single-line entry is [n, n]), in
the order they appear, EXCLUDING the section's own heading line. If there's no such list on this
CV (including the comma-separated case above), reply {{"skills": null}}."""

MAX_SKILL_ENTRIES = 30
MAX_SKILL_ENTRY_LINES = 3


def _locate_skill_lines(lines: list[dict]) -> list[tuple[int, int]]:
    """LLM-driven replacement for a position/case heuristic that anchored on
    an all-caps header then swept short, single-line blocks after it --
    confirmed live on a real CV that a skill entry wrapping onto a second
    line (PyMuPDF gives each wrapped LINE its own block on some CV exports,
    same underlying inconsistency _locate_summary_lines's docstring
    describes) came back as two independent "skills", one swap landing on
    each fragment -- two unrelated replacement skills then rendered as one
    garbled, overlapping bullet. Grouping wrapped lines into one entry needs
    the model to actually read the list, not just measure gaps -- validated
    programmatically below regardless, same as everywhere else in this
    module: entries must be short, non-overlapping, in bounds, and none can
    read as a heading, or the whole result is rejected rather than risking
    a bad group."""
    numbered = "\n".join(f"[{i}] {ln['text']}" for i, ln in enumerate(lines))
    try:
        result = chat_json(LOCATE_SKILLS_PROMPT.format(numbered_lines=numbered))
        raw = result.get("skills")
        if not raw:
            return []
        pairs = [(int(p[0]), int(p[1])) for p in raw]
    except Exception:
        return []

    if len(pairs) > MAX_SKILL_ENTRIES:
        return []
    prev_end = -1
    for start, end in pairs:
        if not (prev_end < start <= end < len(lines)) or end - start >= MAX_SKILL_ENTRY_LINES:
            return []
        entry_text = " ".join(lines[i]["text"] for i in range(start, end + 1))
        if any(_looks_like_heading(lines[i]["text"]) for i in range(start, end + 1)):
            return []
        # A comma/semicolon is the clearest sign this "entry" is actually
        # several skills run together under one category label, not one
        # atomic skill -- rejected here even though the prompt already asks
        # for this, same "don't trust the model alone" reasoning as
        # everywhere else: confirmed live, a category line like "IA & LLM --
        # LangChain, LangGraph, DSPy..." still came back once as a single
        # "entry" and a swap silently deleted every skill it listed but one.
        if "," in entry_text or ";" in entry_text:
            return []
        prev_end = end
    return pairs


# ---------------------------------------------------------------------------
# docx branch
# ---------------------------------------------------------------------------

def _find_docx_summary_paragraph(doc):
    """The longest paragraph over 80 characters -- one shared definition of
    "the summary paragraph" for the .docx branch, used both to place the
    {{ cv_summary }} tag (_ensure_docx_template) and to show the LLM the
    original text to rewrite (_tailor_docx). These used to be two separate
    heuristics (longest vs. first paragraph over 80 chars) that could each
    pick a different paragraph on a CV where a later paragraph (e.g. an
    experience bullet) runs longer than the actual summary -- silently
    tailoring text that isn't the text actually shown in the output."""
    target = max(doc.paragraphs, key=lambda p: len(p.text) if len(p.text) > 80 else 0, default=None)
    return target if target is not None and len(target.text) > 80 else None


def _ensure_docx_template(source_path: Path) -> Path:
    """One-time setup per uploaded .docx: find the paragraph that looks like
    the summary (see _find_docx_summary_paragraph) and replace its text with
    a {{ cv_summary }} Jinja tag, saved as a separate template file -- the
    original upload stays untouched for reference, docxtpl renders fresh
    copies from the template from then on. docxtpl (not raw python-docx)
    specifically because Word/Docs routinely split one visually uniform
    sentence across several `run` objects even without a formatting change;
    docxtpl's tag rendering handles that run-splitting itself instead of
    this project re-solving it."""
    from docx import Document as _Document

    template_path = PROFILE_DIR / "template.docx"
    if template_path.exists():
        return template_path

    doc = _Document(str(source_path))
    target = _find_docx_summary_paragraph(doc)
    if target is None:
        raise ValueError("no summary-like paragraph found in the .docx CV")
    for run in target.runs:
        run.text = ""
    target.runs[0].text = "{{ cv_summary }}"
    doc.save(str(template_path))
    return template_path


def _tailor_docx(profile: dict, title: str, description: str, offer_id: int) -> Path:
    from docxtpl import DocxTemplate

    source_path = Path(profile["cv_source_path"])
    template_path = _ensure_docx_template(source_path)

    from docx import Document as _Document
    target = _find_docx_summary_paragraph(_Document(str(source_path)))
    original_summary = target.text if target is not None else ""

    new_summary = _tailor_summary_text(original_summary, profile, title, description)

    tpl = DocxTemplate(str(template_path))
    tpl.render({"cv_summary": new_summary})
    out_dir = OUTPUT_DIR / str(offer_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered_docx = out_dir / "CV.docx"
    tpl.save(str(rendered_docx))

    _convert_docx_to_pdf(rendered_docx, out_dir)
    rendered_docx.unlink(missing_ok=True)
    return out_dir / "CV.pdf"


def _convert_docx_to_pdf(docx_path: Path, out_dir: Path) -> None:
    """LibreOffice headless -- a real, new system dependency, not something
    pip installs (see README). A unique --env:UserInstallation profile per
    call avoids a known LibreOffice failure mode: concurrent headless
    invocations sharing the default profile can deadlock, and
    draft_letters_node can process several offers (each spawning its own
    conversion) in a single run.

    Path.as_uri() (not an f-string) to build the file:// URI: a naive
    f"file://{profile_dir}" breaks on Windows (backslashes never converted
    to "/", no third slash for the drive letter), silently defeating this
    profile-isolation mechanism there. The profile directory itself is
    always removed afterward -- LibreOffice creates it but never cleans it
    up, and one gets spawned per conversion (one per offer per run)."""
    profile_dir = Path(tempfile.gettempdir()) / f"hobot-soffice-{uuid.uuid4().hex}"
    try:
        subprocess.run(
            ["soffice", "--headless", f"-env:UserInstallation={profile_dir.as_uri()}",
             "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
            check=True, capture_output=True, timeout=60,
        )
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# pdf_image branch (no text layer at all)
# ---------------------------------------------------------------------------

LOCATE_SUMMARY_PROMPT = """This is a CV page image. Find the profile/summary paragraph only (the
short introductory paragraph near the top, not the work experience, education, or contact
details).

Reply with ONLY JSON: {{"found": true/false, "text": "verbatim transcription of that paragraph",
"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}}

x0/y0/x1/y1 are the paragraph's bounding box as FRACTIONS of the page width/height (0.0 to 1.0),
not pixels."""


def _locate_summary_zone(page) -> dict | None:
    pix = page.get_pixmap(dpi=150)
    result = chat_json(LOCATE_SUMMARY_PROMPT, images=[pix.tobytes("png")])
    if not result.get("found"):
        return None
    w, h = page.rect.width, page.rect.height
    bbox = fitz.Rect(result["x0"] * w, result["y0"] * h, result["x1"] * w, result["y1"] * h)
    # cheap self-correction: re-render just the claimed crop and ask for a
    # yes/no confirmation before trusting a small local model's bbox guess
    crop = page.get_pixmap(dpi=150, clip=bbox)
    confirm = chat_json(
        'Does this cropped image show a CV\'s profile/summary paragraph? Reply with ONLY JSON: {"yes": true/false}',
        images=[crop.tobytes("png")],
    )
    if not confirm.get("yes"):
        return None
    return {"bbox": bbox, "text": result.get("text", "")}


def _tailor_pdf_image(doc: fitz.Document, profile: dict, title: str, description: str) -> bool:
    """Returns True if a real, ATS-readable edit was made. Deliberately does
    NOT attempt to reconstruct the whole CV as an editable template (a much
    harder, much less scoped task than anything else in this project, and a
    regenerated template is by definition not the same file) and does NOT
    just repaint pixels (an ATS parser gets nothing from pixels -- inserting
    a real PDF text object is the part that actually makes this branch
    meaningful). Font fidelity is not attempted here; this branch already
    can't promise visual fidelity and says so in the caller's notification."""
    page = doc[0]
    zone = _locate_summary_zone(page)
    if zone is None:
        return False

    bbox = zone["bbox"]
    new_summary = _tailor_summary_text(zone["text"], profile, title, description)

    bg = _sample_background(page, bbox)
    page.draw_rect(bbox, color=None, fill=tuple(c / 255 for c in bg))
    _wrap_and_insert(page, bbox, new_summary, FONTS_DIR / "Montserrat-Regular.ttf", 10.0, (0, 0, 0))
    return True


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def cv_path(offer_id: int) -> Path:
    return OUTPUT_DIR / str(offer_id) / "CV.pdf"


def tailor_cv(offer_id: int, title: str, description: str) -> Path | None:
    """Shared entry point for both the automatic pipeline
    (graphs/discovery_graph.py::draft_letters_node) and the on-demand chat
    tool (graphs/chat_agent.py::adapter_cv). Never raises past this
    boundary -- a failed tailoring attempt must never break letter
    drafting or the scoring pipeline around it, same convention as
    tools/web_search.py::search_company returning "" on failure."""
    try:
        with get_connection() as conn:
            profile = _full_profile(conn)
        if not profile.get("cv_source_path"):
            return None

        fmt = profile["cv_format"]
        out_dir = OUTPUT_DIR / str(offer_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        from tools.ats_check import log_if_not_ats_readable

        if fmt == "docx":
            result = _tailor_docx(profile, title, description, offer_id)
            log_if_not_ats_readable(result, context=f"tailored CV (docx), offer #{offer_id}")
            return result

        doc = fitz.open(profile["cv_source_path"])
        try:
            if fmt == "pdf_text":
                _tailor_pdf_text(doc, profile, title, description)
            elif fmt == "pdf_image":
                if not _tailor_pdf_image(doc, profile, title, description):
                    return None
            else:
                return None
            out_path = cv_path(offer_id)
            doc.save(str(out_path))
            log_if_not_ats_readable(out_path, context=f"tailored CV ({fmt}), offer #{offer_id}")
            return out_path
        finally:
            doc.close()
    except Exception:
        return None
