"""Flags a posting that looks like it might be a "ghost job" -- kept open
indefinitely to build a talent pipeline or as free advertising, with no real,
current intent to hire for it. Two cheap, deterministic signals, computed on
read every time (same pattern as core/circuit_breaker.py::is_backed_off, not
stored on the row): how long it's been open, and whether its own text uses
the stock phrasing a company reaches for when a posting isn't tied to an
actual current opening. Neither signal is proof by itself, and either one
alone is enough to flag -- this is a hint worth a second look, shown as one,
never used to auto-exclude or otherwise change what happens to a posting.
"""
import os
from datetime import datetime

DAYS_THRESHOLD = int(os.environ.get("GHOST_JOB_DAYS_THRESHOLD", "45"))

# Phrases a posting tied to a real, current opening rarely needs -- they're
# how a company signals "not actually hiring for this specific role right
# now" without saying so outright. Matched case-insensitively, substring.
EVERGREEN_PHRASES = (
    "always looking for talented", "always accepting applications",
    "keep your resume on file", "keep your cv on file", "talent pool",
    "talent community", "no immediate opening", "for future opportunities",
    "we are constantly looking", "continuously hiring", "banque de cv",
    "candidature spontanée bienvenue", "vivier de candidats",
)


def check_ghost_job(description: str | None, first_seen_at: str | None) -> tuple[bool, str | None]:
    """Returns (is_possible_ghost, reason) -- reason is None when not
    flagged, otherwise a short human-readable explanation combining
    whichever signal(s) fired. Never raises: an unparseable timestamp or a
    missing description just skips that one signal rather than failing the
    whole check, since this is advisory information, not something anything
    else depends on succeeding."""
    reasons = []

    if first_seen_at:
        try:
            first_seen = datetime.fromisoformat(first_seen_at)
            days_open = (datetime.now() - first_seen).days
            if days_open >= DAYS_THRESHOLD:
                reasons.append(f"open {days_open} days")
        except ValueError:
            pass

    if description:
        lowered = description.lower()
        hit = next((p for p in EVERGREEN_PHRASES if p in lowered), None)
        if hit:
            reasons.append(f'contains "{hit}"')

    if not reasons:
        return False, None
    return True, "possible ghost job (" + ", ".join(reasons) + ")"
