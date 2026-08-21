"""cli.py -- the terminal UI entry point. A separate process from daemon.py
on purpose: daemon.py is meant to run headless under systemd with no
terminal attached, while this needs a real one to draw into. Run it whenever
you want to look at what the daemon (or a headless-only install) has found --
`python cli.py` in any terminal, no separate service, no port to open.

Loads .env and makes sure the schema is current the same way daemon.py does,
so a first run of the UI before the daemon has ever started still works
against a freshly created, correctly shaped database.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from core.db import init_db
from tui.app import run

if __name__ == "__main__":
    init_db()
    run()
