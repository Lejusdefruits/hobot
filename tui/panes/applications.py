"""Applications list -- mirrors the web dashboard's /applications: same
core.queries.list_applications() call. Read-only, same as it was on the web
(mutations happen from the Offers pane's detail screen or Discord)."""
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, DataTable, Static

from tools import common


class ApplicationsPane(Vertical):
    def compose(self) -> ComposeResult:
        yield Static("Applications (most recent first)", classes="hint")
        yield DataTable(id="applications-table", cursor_type="row", zebra_stripes=True)
        yield Button("Refresh", id="refresh")

    def on_mount(self) -> None:
        table = self.query_one("#applications-table", DataTable)
        table.add_columns("ID", "Title", "Company", "Status", "Sent at")
        self.refresh_applications()

    def refresh_applications(self) -> None:
        from core import queries

        table = self.query_one("#applications-table", DataTable)
        table.clear()
        for row in queries.list_applications(limit=30):
            table.add_row(
                str(row["id"]), row["title"] or "", row["company"] or "",
                common.STATUS_LABELS.get(row["status"], row["status"]),
                row["sent_at"] or "-",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh":
            self.refresh_applications()
