"""Minimal state shared between the scheduler (daemon.py) and the Discord
bot/agent (/pause, /resume, /status, the agent's statut_veille tool) -- a
separate module to avoid a circular import (daemon.py imports discord_bot,
not the other way around).

`scheduler` is the real APScheduler object (set by daemon.py at startup), not
a copy: daemon.py runs as the `__main__` script, so an `import daemon` from
elsewhere in the process would reload the file under a second module name
with an empty, never-started scheduler (a classic trap) -- routing through
this shared module sidesteps it entirely."""

paused = False
scheduler = None
