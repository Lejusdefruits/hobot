"""Reads the real OS clipboard -- Textual's own Ctrl+V binding only reads
its own in-app clipboard (whatever was last copied *from within* the
running app via Ctrl+C, empty otherwise), never the actual system
clipboard, so pasting something copied from a browser or password manager
into the terminal UI silently pastes nothing. tui/widgets.py's
PasteableInput calls this first and only falls back to Textual's own
behavior if it comes back empty.

Same fail-open shape as core/browser.py: any failure (tool missing, no
display, no permission, timeout) returns None rather than raising, so a
clipboard-read problem never crashes the terminal UI -- worst case, Ctrl+V
falls back to pasting nothing, same as today.
"""
import shutil
import subprocess
import sys


def _read(cmd: list[str]) -> str | None:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def read_system_clipboard() -> str | None:
    try:
        if sys.platform == "darwin":
            return _read(["pbpaste"])
        if sys.platform == "win32":
            return _read(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"])
        # Linux has no single standard clipboard tool -- try whichever is
        # actually installed, Wayland first since wl-clipboard is the
        # modern default on most current distros.
        if shutil.which("wl-paste"):
            return _read(["wl-paste", "--no-newline"])
        if shutil.which("xclip"):
            return _read(["xclip", "-selection", "clipboard", "-o"])
        if shutil.which("xsel"):
            return _read(["xsel", "--clipboard", "--output"])
    except Exception:
        return None
    return None
