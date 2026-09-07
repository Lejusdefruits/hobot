"""Postings list -- mirrors the web dashboard's /offers (offers.html): same
core.queries.list_offers() call, every open posting, best-scored first.
Enter (or a click) on a row opens tui/modals.py::OfferDetailScreen for the
actions (mark applied, exclude, tailor CV, edit the letter). The Age column
is days since first_seen_at, colored green for an offer found today -- a
quick way to spot what's actually new versus the same backlog seen before,
which the Ghost?/check_ghost_job() column this replaced didn't answer at all
(that's still shown as a warning in the detail screen, just not useful as a
whole extra column here).

"Show unscored" swaps the same table over to core.queries.list_unscored_offers()
-- the backlog score_node (graphs/discovery_graph.py) hasn't reached yet, in
the same oldest-first order it'll actually process them in. "Score now" runs
that scoring step immediately (graphs.discovery_graph.run_scoring_now(), same
one lancer_scoring uses through /ask) instead of waiting for the next
scheduled discovery run."""
from datetime import datetime

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Static

from tui.modals import OfferDetailScreen

SCORED_COLUMNS = ("ID", "Score", "Title", "Company", "Location", "Letter", "Age")
UNSCORED_COLUMNS = ("ID", "Title", "Company", "Location", "Source", "Found")

# Matches tui/panes/chat.py's LINK_STYLE -- a plain Rich color, not a
# Textual CSS $token: DataTable cells render whatever Rich renderable they're
# given, outside the CSS engine that resolves theme tokens.
NEW_OFFER_STYLE = Style(color="bright_green", bold=True)


def _offer_age_days(first_seen_at: str | None) -> int | None:
    """Days since first_seen_at, or None if it's missing/unparseable.
    first_seen_at is written via SQLite's own datetime('now'), which is UTC
    -- comparing against datetime.now() (local time) would skew this by the
    local UTC offset, same fix as tools/ghost_job.py::check_ghost_job."""
    if not first_seen_at:
        return None
    try:
        first_seen = datetime.fromisoformat(first_seen_at)
    except ValueError:
        return None
    return (datetime.utcnow() - first_seen).days


def _format_age(days: int | None) -> Text:
    if days is None:
        return Text("?")
    if days == 0:
        return Text("Today", style=NEW_OFFER_STYLE)
    label = "1 day" if days == 1 else f"{days} days"
    return Text(label)


class OffersPane(Vertical):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._showing_unscored = False

    def compose(self) -> ComposeResult:
        yield Static("Best open postings (Enter for detail, actions inside)", id="offers-hint", classes="hint")
        with Horizontal(classes="button-row"):
            # active_effect_duration=0: Button's default 0.2s "clicked" flash
            # (the -active CSS class) makes it silently ignore a second click
            # that lands inside that window -- confirmed live, every other
            # quick click on "Show unscored/scored" did nothing. Neither
            # button needs the flash badly enough to trade responsiveness
            # for it.
            toggle_btn = Button("Show unscored", id="toggle-unscored")
            toggle_btn.active_effect_duration = 0
            score_btn = Button("Score now", id="score-now")
            score_btn.active_effect_duration = 0
            yield toggle_btn
            yield score_btn
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

        self.query_one("#offers-hint", Static).update("Best open postings (Enter for detail, actions inside)")
        for row in queries.list_offers(limit=None):
            table.add_row(
                str(row["id"]), str(row["score"]), row["title"] or "", row["company"] or "",
                row["location"] or "", "yes" if row["has_dossier"] else "no",
                _format_age(_offer_age_days(row["first_seen_at"])),
                key=str(row["id"]),
            )

    def _fill_unscored(self, table: DataTable) -> None:
        from core import queries
        from tools.common import offer_type_label

        self.query_one("#offers-hint", Static).update(
            "Waiting to be scored, oldest first (Enter for detail) -- \"Score now\" scores this backlog."
        )
        for row in queries.list_unscored_offers(limit=None):
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
        # OfferDetailScreen already renders "not scored" for score IS NULL
        # and every action there (mark applied, exclude, tailor CV...) works
        # the same regardless of score -- no reason Enter should be a no-op
        # just because the row came from the unscored view.
        offer_id = int(event.row_key.value)

        def handle(result: str | None) -> None:
            if result == "changed":
                self.refresh_offers()

        self.app.push_screen(OfferDetailScreen(offer_id), handle)
