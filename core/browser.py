"""Single shared "open a URL in the user's browser" helper -- used by the
terminal UI (an offer's posting link) and by scripts/install_wizard.py (an
API's signup page). webbrowser.open() itself can both return False (no
browser found) and raise (a genuinely broken environment) depending on the
platform, so both have to be handled the same way every place this is
called; this is that one place, rather than two independent copies of the
same try/except drifting apart over time."""


def open_url(url: str) -> bool:
    """True if a browser was actually launched. False (never raises) if
    webbrowser reports no browser was found or the attempt otherwise failed
    -- the caller decides how to tell the user (a Textual notify(), a plain
    print(), ...), this function only ever reports success/failure."""
    if not url:
        return False
    import webbrowser
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False
