"""Postings list -- mirrors the web dashboard's /offers (offers.html): same
core.queries.list_offers() call, 25 best-scored open postings. Enter (or a
click) on a row opens tui/modals.py::OfferDetailScreen for the actions
(mark applied, exclude, tailor CV, edit the letter). The Ghost? column is
tools.ghost_job.check_ghost_job(), computed fresh on every refresh -- an
advisory hint (open a long time, or stock "keep your CV on file"-style
wording), never something that changes what happens to a posting."""
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from tui.modals import OfferDetailScreen


class OffersPane(Vertical):
    def compose(self) -> ComposeResult:
        yield Static("Best open postings (Enter for detail, actions inside)", classes="hint")
        yield DataTable(id="offers-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#offers-table", DataTable)
        table.add_columns("ID", "Score", "Title", "Company", "Location", "Letter", "Ghost?")
        self.refresh_offers()

    def refresh_offers(self) -> None:
        from core import queries
        from tools.ghost_job import check_ghost_job

        table = self.query_one("#offers-table", DataTable)
        table.clear()
        for row in queries.list_offers(limit=25):
            is_ghost, _ = check_ghost_job(row["description"], row["first_seen_at"])
            table.add_row(
                str(row["id"]), str(row["score"]), row["title"] or "", row["company"] or "",
                row["location"] or "", "yes" if row["has_dossier"] else "no",
                "yes" if is_ghost else "no",
                key=str(row["id"]),
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        offer_id = int(event.row_key.value)

        def handle(result: str | None) -> None:
            if result == "changed":
                self.refresh_offers()

        self.app.push_screen(OfferDetailScreen(offer_id), handle)
