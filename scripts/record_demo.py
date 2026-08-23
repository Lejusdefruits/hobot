"""Regenerates assets/demo.gif from a live run of the actual TUI (tui/app.py)
against fabricated data in a throwaway db -- never hobot.db or checkpoints.db,
which may hold real job-search data (see the HOBOT_DB_PATH /
HOBOT_CHECKPOINT_DB_PATH / HOBOT_OUTPUT_DIR overrides below, all read by
core/db.py, graphs/chat_agent.py, and tools/documents.py respectively).

Textual's App.export_screenshot() gives one SVG frame per moment; each is
rasterized to PNG and ffmpeg assembles the sequence into a paletted GIF.
Run with the project's own .venv (needs textual + the rest of hobot's
dependencies already in requirements.txt):

    .venv/bin/python scripts/record_demo.py

Two things beyond requirements.txt, neither added to it since nothing at
runtime needs them: ffmpeg (must already be on PATH) and cairosvg (installed
here into a throwaway venv, not .venv, and discarded after the run).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if shutil.which("ffmpeg") is None:
    sys.exit("ffmpeg not found on PATH -- install it first.")

WORK = Path(tempfile.mkdtemp(prefix="hobot-demo-"))
FRAMES = WORK / "frames"
FRAMES.mkdir()

os.environ["HOBOT_DB_PATH"] = str(WORK / "demo.db")
os.environ["HOBOT_CHECKPOINT_DB_PATH"] = str(WORK / "demo-checkpoints.db")
os.environ["HOBOT_OUTPUT_DIR"] = str(WORK / "outputs")

FRAME_SIZE = (104, 34)
TARGET_WIDTH = 760
GIF_FPS = 10


def seed() -> int:
    """Fabricated profile/offers/letter -- nothing here is real applicant
    data. Returns the id of the offer a letter gets generated for."""
    from core.db import get_connection, init_db

    init_db()
    now = datetime.now(timezone.utc)

    def fmt(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")  # matches sqlite's own datetime('now') format

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_profile (id, full_name, skills, target_roles, target_locations, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            (
                "Alex Martin",
                json.dumps(["Python", "FastAPI", "PostgreSQL", "Docker", "React"]),
                json.dumps(["Backend developer", "Full-stack developer"]),
                json.dumps(["Lyon", "Remote"]),
                fmt(now),
            ),
        )
        conn.execute(
            "INSERT INTO daemon_flags (id, paused, heartbeat_at, discord_status) VALUES (1, 0, ?, 'connected')",
            (fmt(now),),
        )
        conn.execute(
            "INSERT INTO run_log (run_type, source, started_at, finished_at, n_found, n_new, query) "
            "VALUES ('discovery', 'jobspy', ?, ?, 34, 6, 'backend developer')",
            (fmt(now - timedelta(minutes=12)), fmt(now - timedelta(minutes=9))),
        )

        offers = [
            ("Backend Developer (Python)", "Northwind Analytics", "Lyon, France", 91,
             "Strong overlap on Python/FastAPI/PostgreSQL, target city matches, seniority matches.",
             "Team of 12 building a data platform for retail clients. Python/FastAPI backend, "
             "PostgreSQL, Docker, some Kubernetes exposure appreciated but not required."),
            ("Backend Developer (Django)", "Solenne Health", "Lyon, France", 88,
             "Same target role, target city, and most required skills already in the profile.",
             "Healthtech scale-up, Django REST framework, PostgreSQL, strong test culture."),
            ("Full-Stack Engineer", "Marelio", "Remote (France)", 84,
             "Good skills overlap, remote matches target locations, slightly junior on React.",
             "Small product team, Django + React, looking for someone comfortable across the stack."),
            ("Site Reliability Engineer", "Klarion Cloud", "Lyon, France", 76,
             "Backend skills transfer well, but the role leans more ops/infra than the target roles.",
             "On-call rotation, Kubernetes, Terraform, Python tooling for internal platform teams."),
            ("Data Engineer", "Ferrand & Co", "Villeurbanne, France", 68,
             "Some Python overlap, but the core stack (Spark, Airflow) isn't in the profile.",
             "ETL pipelines for a mid-size retailer, Airflow, Spark, dbt."),
            ("Frontend Developer (React)", "Aubade Studio", "Remote (EU)", 54,
             "Role is React-only; profile is backend-leaning, so overlap is partial.",
             "Marketing sites and internal tools in React/TypeScript, fully remote."),
            ("Platform Engineer", "Vezelay Systems", "Lyon, France", None, None,
             "Internal developer platform team, Go and Python, Kubernetes-heavy."),
        ]
        top_offer_id = None
        for i, (title, company, location, score, reason, desc) in enumerate(offers):
            first_seen = fmt(now - timedelta(hours=i * 3 + 1))
            cur = conn.execute(
                "INSERT INTO offers (source, url_hash, dedup_key, url, title, company, location, "
                "description, score, score_reason, status, origin, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'veille', ?, ?)",
                (
                    "jobspy", f"demo-hash-{i}", f"{company.lower()}|{title.lower()}",
                    "https://example.invalid/job/" + str(i), title, company, location, desc, score, reason,
                    "scored" if score is not None else "new", first_seen, first_seen,
                ),
            )
            if i == 0:
                top_offer_id = cur.lastrowid

    from tools.documents import generate_letter_pdf

    letter_text = (
        "Dear Hiring Team,\n\n"
        "I'm reaching out about the Backend Developer position at Northwind Analytics. "
        "I've spent the past few years building FastAPI services on top of PostgreSQL, "
        "including the Docker-based deployment pipeline for a small internal platform team, "
        "which lines up closely with what you're describing.\n\n"
        "I'd welcome the chance to talk more.\n\nBest regards,"
    )
    path = generate_letter_pdf(top_offer_id, letter_text, full_name="Alex Martin")
    from core.db import get_connection as gc
    with gc() as conn:
        conn.execute(
            "INSERT INTO applications (offer_id, cover_letter_path, status) VALUES (?, ?, 'draft')",
            (top_offer_id, str(path)),
        )
    return top_offer_id


async def record() -> list[tuple[Path, float]]:
    """Returns [(svg_path, hold_seconds), ...] in display order."""
    from textual.widgets import DataTable

    from tui.app import HobotApp

    moments: list[tuple[Path, float]] = []

    def snap(app: HobotApp, hold: float) -> None:
        svg_path = FRAMES / f"frame_{len(moments):03d}.svg"
        svg_path.write_text(app.export_screenshot())
        moments.append((svg_path, hold))

    app = HobotApp()
    async with app.run_test(size=FRAME_SIZE) as pilot:
        await pilot.pause(0.3)
        snap(app, 2.4)  # Status: daemon "running", discovery schedule

        await pilot.press("f2")
        await pilot.pause(0.3)
        snap(app, 2.2)  # Offers: scored postings, best first

        # action_show_tab() clears focus on every switch (tui/app.py) and
        # OffersPane doesn't reclaim it the way ChatPane does -- without this,
        # the arrow/enter presses below land nowhere.
        app.query_one("#offers-table", DataTable).focus()
        await pilot.pause(0.1)

        # Row 1 (already the cursor's resting spot) is the one seeded with a
        # generated cover letter -- open it straight away rather than moving
        # off it.
        await pilot.press("enter")
        await pilot.pause(0.4)
        snap(app, 3.0)  # Offer detail: description, score reason, generated letter

        await pilot.press("escape")
        await pilot.press("f6")
        await pilot.pause(0.3)
        snap(app, 1.0)  # Chat: empty input

        message = "find me more roles like this one"
        for i, ch in enumerate(message):
            await pilot.press("space" if ch == " " else ch)
            if i % 2 == 1 or i == len(message) - 1:
                snap(app, 0.2)  # snapshot every other keystroke -- still reads as live typing, half the frames
        await pilot.pause(0.3)
        snap(app, 2.0)  # Chat: typed question, not sent (no LLM call)

    return moments


def make_rasterizer_venv() -> Path:
    venv_dir = WORK / "raster-venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    pip = venv_dir / "bin" / "pip"
    subprocess.run([str(pip), "install", "--quiet", "cairosvg"], check=True)
    return venv_dir / "bin" / "python"


def rasterize(python: Path, moments: list[tuple[Path, float]]) -> list[tuple[Path, float]]:
    script = (
        "import sys, cairosvg\n"
        "svg_path, png_path = sys.argv[1], sys.argv[2]\n"
        f"cairosvg.svg2png(url=svg_path, write_to=png_path, output_width={TARGET_WIDTH})\n"
    )
    script_path = WORK / "raster.py"
    script_path.write_text(script)

    pngs = []
    for svg_path, hold in moments:
        png_path = svg_path.with_suffix(".png")
        subprocess.run([str(python), str(script_path), str(svg_path), str(png_path)], check=True)
        pngs.append((png_path, hold))
    return pngs


def assemble_gif(pngs: list[tuple[Path, float]], out_path: Path) -> None:
    concat_path = WORK / "concat.txt"
    lines = []
    for png_path, hold in pngs:
        lines.append(f"file '{png_path}'")
        lines.append(f"duration {hold}")
    lines.append(f"file '{pngs[-1][0]}'")  # ffmpeg ignores the last entry's duration otherwise
    concat_path.write_text("\n".join(lines))

    palette_path = WORK / "palette.png"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path),
         "-vf", f"fps={GIF_FPS},palettegen=stats_mode=diff", str(palette_path)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path), "-i", str(palette_path),
         "-lavfi", f"fps={GIF_FPS},paletteuse=dither=none", "-loop", "0", str(out_path)],
        check=True, capture_output=True,
    )


def main() -> None:
    seed()
    moments = asyncio.run(record())
    python = make_rasterizer_venv()
    pngs = rasterize(python, moments)
    out_path = ROOT / "assets" / "demo.gif"
    assemble_gif(pngs, out_path)
    shutil.rmtree(WORK)
    size_kb = out_path.stat().st_size / 1024
    print(f"wrote {out_path} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
