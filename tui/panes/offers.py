"""Postings list -- mirrors the web dashboard's /offers (offers.html): same
core.queries.list_offers() call, 25 best-scored open postings. Enter (or a
click) on a row opens tui/modals.py::OfferDetailScreen for the actions
(mark applied, exclude, tailor CV, edit the letter). The Ghost? column is
tools.ghost_job.check_ghost_job(), computed fresh on every refresh -- an
advisory hint (open a long time, or stock "keep your CV on file"-style
wording), never something that changes what happens to a posting.

"Show unscored" swaps the same table over to core.queries.list_unscored_offers()
-- the backlog score_node (graphs/discovery_graph.py) hasn't reached yet, in
the same oldest-first order it'll actually process them in. "Score now" runs
that scoring step immediately (graphs.discovery_graph.run_scoring_now(), same
one lancer_scoring uses through /ask) instead of waiting for the next
scheduled discovery run."""
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Static

from tui.modals import OfferDetailScreen

SCORED_COLUMNS = ("ID", "Score", "Title", "Company", "Location", "Letter", "Ghost?")
UNSCORED_COLUMNS = ("ID", "Title", "Company", "Location", "Source", "Found")


class OffersPane(Vertical):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._showing_unscored = False

    def compose(self) -> ComposeResult:
        yield Static("Best open postings (Enter for detail, actions inside)", id="offers-hint", classes="hint")
        with Horizontal(classes="button-row"):
            yield Button("Show unscored", id="toggle-unscored")
            yield Button("Score now", id="score-now")
        yield DataTable(id="offers-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#offers-table", DataTable)
        table.add_columns(*SCORED_COLUMNS)
        self.refresh_offers()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "toggle-unscored":
            self._showing_unscored = not self._showing_unscored
            event.button.label = "Show scored" if self._showing_unscored else "Show unscored"
            self.refresh_offers()
        elif event.button.id == "score-now":
            self._score_now()

    def refresh_offers(self) -> None:
        table = self.query_one("#offers-table", DataTable)
        table.clear(columns=True)
        if self._showing_unscored:
            table.add_columns(*UNSCORED_COLUMNS)
            self._fill_unscored(table)
        else:
            table.add_columns(*SCORED_COLUMNS)
            self._fill_scored(table)

    def _fill_scored(self, table: DataTable) -> None:
        from core import queries
        from tools.ghost_job import check_ghost_job

        self.query_one("#offers-hint", Static).update("Best open postings (Enter for detail, actions inside)")
        for row in queries.list_offers(limit=25):
            is_ghost, _ = check_ghost_job(row["description"], row["first_seen_at"])
            table.add_row(
                str(row["id"]), str(row["score"]), row["title"] or "", row["company"] or "",
                row["location"] or "", "yes" if row["has_dossier"] else "no",
                "yes" if is_ghost else "no",
                key=str(row["id"]),
            )

    def _fill_unscored(self, table: DataTable) -> None:
        from core import queries
        from tools.common import offer_type_label

        self.query_one("#offers-hint", Static).update(
            "Waiting to be scored, oldest first (Enter for detail) -- \"Score now\" scores this backlog."
        )
        for row in queries.list_unscored_offers(limit=25):
            table.add_row(
                str(row["id"]), row["title"] or "", row["company"] or "", row["location"] or "",
                offer_type_label(row["source"]), row["first_seen_at"] or "",
                key=str(row["id"]),
            )

    def _score_now(self) -> None:
        self.notify("Scoring the pending backlog...", timeout=3)
        self.run_worker(self._score_now_worker, thread=True, exclusive=True)

    def _score_now_worker(self) -> None:
        from graphs.discovery_graph import run_scoring_now

        try:
            scored = run_scoring_now()
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Scoring failed: {e}", severity="error")
            return
        message = f"Scored {len(scored)} offer(s)." if scored else "Nothing was waiting to be scored."
        self.app.call_from_thread(self.notify, message)
        self.app.call_from_thread(self.refresh_offers)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._showing_unscored:
            return  # nothing to open yet -- no score, no letter, no actions apply
        offer_id = int(event.row_key.value)

        def handle(result: str | None) -> None:
            if result == "changed":
                self.refresh_offers()

        self.app.push_screen(OfferDetailScreen(offer_id), handle)
