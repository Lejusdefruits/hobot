"""Minimal state shared between the scheduler (daemon.py) and whatever's
driving it -- Discord (/pause, /resume, /status, the agent's statut_veille
tool) and the terminal UI (cli.py) -- a separate module to avoid a circular
import (daemon.py imports discord_bot, not the other way around).

`scheduler` is the real APScheduler object (set by daemon.py at startup), not
a copy: daemon.py runs as the `__main__` script, so an `import daemon` from
elsewhere in the *same* process would reload the file under a second module
name with an empty, never-started scheduler (a classic trap) -- routing
through this shared module sidesteps it entirely. It stays a plain in-memory
attribute (no cross-process story needed): only code running inside the
daemon process itself (discord_bot.py, chat_agent.py) ever reads it, for a
live next-run-time lookup that wouldn't mean anything read from elsewhere
anyway.

`paused` is different -- the terminal UI runs as its own separate process
(you launch it by hand when you want to look at something, same as opening a
browser tab used to for the old web dashboard) and needs to flip the same
switch the daemon checks, so it's backed by the daemon_flags table
(core/db.py) instead of a module global. is_paused()/set_paused() are the
only way to touch it -- never read/write the row directly."""
from core.db import get_connection

scheduler = None

# Set by daemon.py at startup (same self-import trap as `scheduler` above, see
# the module docstring) -- lets /digest (discord_bot.py) trigger
# _run_weekly_digest on demand without ever doing `import daemon`. The
# terminal UI doesn't need this: it calls daemon._run_weekly_digest directly,
# safe from a separate process since that import never crosses the
# `if __name__ == "__main__"` guard.
run_weekly_digest_fn = None


def is_paused() -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT paused FROM daemon_flags WHERE id = 1").fetchone()
    return bool(row["paused"]) if row else False


def set_paused(value: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO daemon_flags (id, paused) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET paused = excluded.paused",
            (int(value),),
        )
