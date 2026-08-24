"""Shared widget customizations used across more than one pane."""
from textual.widgets import Input

from core.clipboard import read_system_clipboard


class PasteableInput(Input):
    """An Input whose Ctrl+V reads the real OS clipboard first.

    Textual's own Ctrl+V binding (Input.action_paste) only ever reads its
    own in-app clipboard -- see core/clipboard.py's docstring for why that
    silently pastes nothing for anything copied outside the app. Falls back
    to the stock behavior if the real clipboard comes back empty (nothing
    there, or the platform-specific read failed), so this never behaves
    worse than plain Input, only better."""

    def action_paste(self) -> None:
        text = read_system_clipboard()
        if not text:
            super().action_paste()
            return
        line = text.splitlines()[0] if text else text
        start, end = self.selection
        if start == end:
            self.insert_text_at_cursor(line)
        else:
            self.replace(line, start, end)
