"""Modal screens shared across panes -- a yes/no confirmation, a single-line
text prompt, and the offer detail view (the one screen complex enough, and
used from enough places, to earn its own class rather than living inline in
tui/panes/offers.py)."""
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea


class ConfirmModal(ModalScreen[bool]):
    """dismiss(True) on confirm, dismiss(False) on cancel or Escape."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: str, confirm_label: str = "Confirm", danger: bool = False):
        super().__init__()
        self._title = title
        self._body = body
        self._confirm_label = confirm_label
        self._danger = danger

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title, classes="modal-title")
            yield Static(self._body)
            with Horizontal(classes="button-row"):
                yield Button(self._confirm_label, id="confirm", variant="error" if self._danger else "primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class TextPromptModal(ModalScreen[str | None]):
    """dismiss(text) on submit, dismiss(None) on cancel or Escape."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", initial: str = ""):
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title, classes="modal-title")
            yield Input(value=self._initial, placeholder=self._placeholder, id="prompt-input")
            with Horizontal(classes="button-row"):
                yield Button("OK", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one("#prompt-input", Input).value.strip() or None)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class OfferDetailScreen(ModalScreen[str | None]):
    """Shows one posting in full (description, score reason, letter text if
    one exists) with the same actions the web dashboard's offer detail page
    had -- mark applied, exclude, tailor CV, edit+save the letter -- each
    calling the exact function its Discord/web equivalent called. Dismisses
    with a short result string the caller (tui/panes/offers.py) can show as a
    toast, or None if nothing changed."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, offer_id: int):
        super().__init__()
        self.offer_id = offer_id
        self._changed = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="offer-detail-body"):
            yield Static("Loading...", id="offer-detail-content")
            with Horizontal(classes="button-row"):
                yield Button("Mark applied", id="applied")
                yield Button("Exclude", id="exclude")
                yield Button("Tailor CV", id="tailor-cv")
                yield Button("Edit letter", id="edit-letter")
                yield Button("Open letter PDF", id="open-letter")
                yield Button("Open CV PDF", id="open-cv")
                yield Button("Close", id="close")
            yield TextArea(id="letter-editor")
            with Horizontal(classes="button-row", id="letter-save-row"):
                yield Button("Save letter", id="save-letter", variant="primary")
                yield Button("Cancel edit", id="cancel-edit")

    def on_mount(self) -> None:
        self.query_one("#letter-editor").display = False
        self.query_one("#letter-save-row").display = False
        self.refresh_detail()

    def refresh_detail(self) -> None:
        from core import queries
        from tools import common
        from tools.ats_check import check_ats_readable
        from tools.ghost_job import check_ghost_job

        row, has_dossier = queries.get_offer_detail(self.offer_id)
        content = self.query_one("#offer-detail-content", Static)
        if not row:
            content.update(f"Posting #{self.offer_id} not found.")
            return

        self._files_row = self._offer_files_row() if has_dossier else None
        lines = [
            f"#{self.offer_id} -- {row['title']}",
            f"{row['company']} -- {row['location'] or 'location unknown'}",
            "",
            f"Status: {common.STATUS_LABELS.get(row['status'], row['status'])}",
            f"Type: {common.offer_type_label(row['source'])}",
            f"Score: {row['score'] if row['score'] is not None else 'not scored'}",
            f"Last seen: {row['last_seen_at']}",
            f"URL: {row['url'] or '(none)'}",
        ]
        is_ghost, ghost_reason = check_ghost_job(row["description"], row["first_seen_at"])
        if is_ghost:
            lines.append(f"Warning: {ghost_reason}")
        if row["score_reason"]:
            lines += ["", "Score reason:", row["score_reason"]]
        lines += ["", "Description:", row["description"] or "(none)"]

        letter_text = None
        if self._files_row and self._files_row["cover_letter_path"]:
            from tools.documents import read_letter_text
            try:
                letter_text = read_letter_text(Path(self._files_row["cover_letter_path"]))
            except Exception:
                letter_text = None
        self._letter_text = letter_text
        if letter_text:
            lines += ["", "Cover letter:", letter_text]
            ok, reason = check_ats_readable(Path(self._files_row["cover_letter_path"]))
            if not ok:
                lines.append(f"Warning: letter PDF may not be ATS-readable ({reason})")
        if self._files_row and self._files_row["cv_path"]:
            ok, reason = check_ats_readable(Path(self._files_row["cv_path"]))
            if not ok:
                lines += ["", f"Warning: tailored CV PDF may not be ATS-readable ({reason})"]

        content.update("\n".join(str(x) for x in lines))
        self.query_one("#open-letter", Button).disabled = not (self._files_row and self._files_row["cover_letter_path"])
        self.query_one("#open-cv", Button).disabled = not (self._files_row and self._files_row["cv_path"])

    def _offer_files_row(self):
        from core.db import get_connection
        with get_connection() as conn:
            return conn.execute(
                "SELECT a.cover_letter_path, a.cv_path FROM applications a "
                "WHERE a.offer_id = ? AND a.cover_letter_path IS NOT NULL "
                "ORDER BY a.id DESC LIMIT 1",
                (self.offer_id,),
            ).fetchone()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "close":
            self.dismiss("changed" if self._changed else None)
        elif bid == "applied":
            self._mark_applied()
        elif bid == "exclude":
            self._exclude()
        elif bid == "tailor-cv":
            self._tailor_cv()
        elif bid == "edit-letter":
            self._start_edit()
        elif bid == "cancel-edit":
            self._end_edit()
        elif bid == "save-letter":
            self._save_letter()
        elif bid == "open-letter":
            self._open_file(self._files_row["cover_letter_path"] if self._files_row else None)
        elif bid == "open-cv":
            self._open_file(self._files_row["cv_path"] if self._files_row else None)

    def _mark_applied(self) -> None:
        from core.db import get_connection
        from tools import common
        with get_connection() as conn:
            row = conn.execute("SELECT status FROM offers WHERE id = ?", (self.offer_id,)).fetchone()
            if row and row["status"] != "applied":
                conn.execute("UPDATE offers SET status = 'applied' WHERE id = ?", (self.offer_id,))
                row_id = common.upsert_application(conn, self.offer_id, status="applied")
                conn.execute("UPDATE applications SET sent_at = datetime('now') WHERE id = ?", (row_id,))
                common.archive_cover_letter(conn, self.offer_id)
        self._changed = True
        self.refresh_detail()

    def _exclude(self) -> None:
        from core.db import get_connection
        with get_connection() as conn:
            row = conn.execute("SELECT status FROM offers WHERE id = ?", (self.offer_id,)).fetchone()
            if row and row["status"] not in ("applied", "excluded"):
                conn.execute("UPDATE offers SET status = 'excluded' WHERE id = ?", (self.offer_id,))
        self._changed = True
        self.refresh_detail()

    def _tailor_cv(self) -> None:
        # An LLM call plus, for a .docx profile, a LibreOffice subprocess
        # conversion (tools/cv_tailor.py) -- run off the UI thread so the
        # rest of the app (other panes, input) stays responsive for however
        # long that takes, same reasoning tui/panes/chat.py's _send() and
        # tui/panes/status.py's digest worker already document.
        self.notify("Tailoring CV...", timeout=3)
        self.run_worker(self._tailor_cv_worker, thread=True, exclusive=True)

    def _tailor_cv_worker(self) -> None:
        from core.db import get_connection
        from tools import common
        from tools.cv_tailor import tailor_cv
        with get_connection() as conn:
            row = conn.execute(
                "SELECT title, description FROM offers WHERE id = ?", (self.offer_id,)
            ).fetchone()
        if not row:
            return
        path = tailor_cv(self.offer_id, row["title"], row["description"])
        if path is not None:
            with get_connection() as conn:
                common.upsert_application(conn, self.offer_id, defaults={"status": "draft"}, cv_path=str(path))
            self._changed = True
        else:
            self.app.call_from_thread(self.notify, "CV tailoring failed -- check the daemon logs.", severity="error")
        self.app.call_from_thread(self.refresh_detail)

    def _start_edit(self) -> None:
        editor = self.query_one("#letter-editor", TextArea)
        editor.text = self._letter_text or ""
        editor.display = True
        self.query_one("#letter-save-row").display = True
        editor.focus()

    def _end_edit(self) -> None:
        self.query_one("#letter-editor").display = False
        self.query_one("#letter-save-row").display = False

    def _save_letter(self) -> None:
        from core.db import get_connection, get_user_profile
        from tools import common
        from tools.documents import generate_letter_pdf

        contenu = self.query_one("#letter-editor", TextArea).text
        profile = get_user_profile() or {}
        path = generate_letter_pdf(self.offer_id, contenu, full_name=profile.get("full_name"))
        with get_connection() as conn:
            common.upsert_application(conn, self.offer_id, defaults={"status": "draft"}, cover_letter_path=str(path))
        self._changed = True
        self._end_edit()
        self.refresh_detail()

    def _open_file(self, path: str | None) -> None:
        if not path or not Path(path).exists():
            return
        import os
        import subprocess
        import sys
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def action_close(self) -> None:
        self.dismiss("changed" if self._changed else None)
