"""Gmail drafts -- mirrors the web dashboard's /drafts: same
tools.email_tools calls and the same daily anti-ban send cap check as
/drafts and its SendDraftButton/DeleteDraftButton on Discord --
tools.email_tools.send_existing_draft_with_cap() is the shared function
both this pane and SendDraftButton call, so the cap logic itself only lives
in one place (it used to be copied here independently, a real drift risk a
review caught). Both send and delete go through a confirmation modal first
-- irreversible IMAP-side actions, same reasoning as the offer detail
screen's mark-applied not needing one (that's a local DB flip) versus this
(a real email leaves the outbox, or a draft is gone for good). Both also run
in a worker thread, not on the UI thread directly -- IMAP over the network
is exactly the kind of blocking call that would otherwise freeze the whole
app for its duration, same reasoning tui/panes/chat.py's _send() documents."""
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, DataTable, Static

from tui.modals import ConfirmModal


class DraftsPane(Vertical):
    def compose(self) -> ComposeResult:
        yield Static("Gmail drafts", classes="hint")
        yield DataTable(id="drafts-table", cursor_type="row", zebra_stripes=True)
        yield Static(id="drafts-error", classes="error-text")
        with Vertical(classes="button-row"):
            yield Button("Send selected", id="send")
            yield Button("Delete selected", id="delete")
            yield Button("Refresh", id="refresh")

    def on_mount(self) -> None:
        table = self.query_one("#drafts-table", DataTable)
        table.add_columns("UID", "To", "Subject", "Preview", "Date")
        self.refresh_drafts()

    def refresh_drafts(self) -> None:
        # The actual IMAP login+fetch runs in a worker thread (see
        # _fetch_drafts_worker) -- same reasoning as _do_send/_do_delete
        # below: a blocking network call here would freeze the whole app,
        # including on the very first mount.
        self.run_worker(self._fetch_drafts_worker, thread=True, exclusive=True)

    def _fetch_drafts_worker(self) -> None:
        from textual.worker import get_current_worker

        from tools import email_tools
        try:
            drafts = email_tools.lister_brouillons()
            error = None
        except Exception as e:
            drafts = []
            error = f"Could not list drafts: {e}"
        # exclusive=True only cancels the asyncio Task wrapping this thread's
        # future -- it can't stop the OS thread once lister_brouillons() is
        # already running. A superseded fetch (e.g. a quick double-click on
        # Refresh, or a delete's own refresh_drafts() overlapping an earlier
        # one still in flight) must not clobber the table with results a
        # newer call already replaced -- checked here, right before the only
        # point that matters, instead of trying to interrupt the IMAP call.
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._apply_drafts, drafts, error)

    def _apply_drafts(self, drafts: list, error: str | None) -> None:
        table = self.query_one("#drafts-table", DataTable)
        table.clear()
        self.query_one("#drafts-error", Static).update(error or "")
        for d in drafts:
            table.add_row(
                d["uid"], d["destinataire"] or "", d["sujet"] or "",
                (d["apercu"] or "").replace("\n", " ")[:60], d["date"] or "",
                key=d["uid"],
            )

    def _selected_uid(self) -> str | None:
        table = self.query_one("#drafts-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return row_key.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "refresh":
            self.refresh_drafts()
        elif bid == "send":
            self._confirm_send()
        elif bid == "delete":
            self._confirm_delete()

    def _confirm_send(self) -> None:
        uid = self._selected_uid()
        if not uid:
            self.notify("Select a draft first.", severity="warning")
            return

        def handle(confirmed: bool | None) -> None:
            if confirmed:
                self._do_send(uid)
        self.app.push_screen(
            ConfirmModal("Send this draft?", f"Sends draft {uid} as-is, for real.", confirm_label="Send"),
            handle,
        )

    def _do_send(self, uid: str) -> None:
        self.run_worker(lambda: self._send_worker(uid), thread=True, exclusive=True)

    def _send_worker(self, uid: str) -> None:
        from tools.email_tools import send_existing_draft_with_cap

        result = send_existing_draft_with_cap(uid)
        if result["status"] != "sent":
            severity = "warning" if result["status"] == "capped" else "error"
            self.app.call_from_thread(self.notify, result["message"], severity=severity)
            return
        self.app.call_from_thread(self.notify, "Draft sent.")
        self.app.call_from_thread(self.refresh_drafts)

    def _confirm_delete(self) -> None:
        uid = self._selected_uid()
        if not uid:
            self.notify("Select a draft first.", severity="warning")
            return

        def handle(confirmed: bool | None) -> None:
            if confirmed:
                self._do_delete(uid)
        self.app.push_screen(
            ConfirmModal("Delete this draft?", f"Permanently deletes draft {uid}.", confirm_label="Delete", danger=True),
            handle,
        )

    def _do_delete(self, uid: str) -> None:
        self.run_worker(lambda: self._delete_worker(uid), thread=True, exclusive=True)

    def _delete_worker(self, uid: str) -> None:
        from tools import email_tools
        # supprimer_brouillon() never raises -- it returns a message string
        # either way, "Error deleting draft: ..." / "No draft with uid ..."
        # on failure, so those two have to be checked for explicitly instead
        # of assuming success.
        result = email_tools.supprimer_brouillon(uid)
        if result.startswith("Error") or result.startswith("No draft"):
            self.app.call_from_thread(self.notify, result, severity="error")
        else:
            self.app.call_from_thread(self.notify, "Draft deleted.")
        self.app.call_from_thread(self.refresh_drafts)
