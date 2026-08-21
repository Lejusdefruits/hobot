"""Reports pane -- mirrors the web dashboard's /sources /quotas /log
/strategy /notifications /gaps /funnel /breakdown, each a straight read
reusing the exact function its Discord/web equivalent called
(core/queries.py, core/api_usage.py, core/circuit_breaker.py, tools/common.py,
graphs/chat_agent.py). No mutations here, same as the web version -- except
Gaps, which is an LLM call (axes_progression) and only runs when asked for
via its own button, never on tab mount."""
import json

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, DataTable, Static, TabbedContent, TabPane


class ReportsPane(Vertical):
    def compose(self) -> ComposeResult:
        with TabbedContent():
            with TabPane("Sources", id="tab-sources"):
                yield DataTable(id="sources-table", zebra_stripes=True)
            with TabPane("Quotas", id="tab-quotas"):
                yield DataTable(id="quotas-table", zebra_stripes=True)
            with TabPane("Search log", id="tab-log"):
                yield DataTable(id="log-table", zebra_stripes=True)
            with TabPane("Strategy", id="tab-strategy"):
                yield DataTable(id="strategy-table", zebra_stripes=True)
            with TabPane("Notifications", id="tab-notifications"):
                yield DataTable(id="notifications-table", cursor_type="row", zebra_stripes=True)
                yield Static(
                    "Select a row to read it in full.", id="notification-detail",
                    classes="section", markup=False,
                )
            with TabPane("Funnel", id="tab-funnel"):
                yield DataTable(id="funnel-table", zebra_stripes=True)
                yield Static(id="funnel-insight", classes="section")
            with TabPane("Score breakdown", id="tab-breakdown"):
                yield DataTable(id="breakdown-table", zebra_stripes=True)
            with TabPane("Gaps", id="tab-gaps"):
                yield Button("Run analysis", id="run-gaps")
                yield Static(id="gaps-result", classes="section")

    def on_mount(self) -> None:
        self._notification_detail: dict[str, str] = {}
        self.query_one("#sources-table", DataTable).add_columns(
            "Source", "Backed off", "Until", "Last run", "Found", "New", "Errors")
        self.query_one("#quotas-table", DataTable).add_columns("Source", "Used", "Limit", "Remaining")
        self.query_one("#log-table", DataTable).add_columns(
            "Source", "Query", "Found", "New", "Errors", "Finished at")
        self.query_one("#strategy-table", DataTable).add_columns("Source", "Query", "Found", "Finished at")
        self.query_one("#notifications-table", DataTable).add_columns(
            "Kind", "Title", "Message", "Postings", "Created at")
        self.query_one("#funnel-table", DataTable).add_columns("Stage", "Count", "% of found", "% of previous")
        self.query_one("#breakdown-table", DataTable).add_columns("Score tier", "Count")
        self.refresh_reports()

    def refresh_reports(self) -> None:
        self._refresh_sources()
        self._refresh_quotas()
        self._refresh_log()
        self._refresh_strategy()
        self._refresh_notifications()
        self._refresh_funnel()
        self._refresh_breakdown()

    def _refresh_sources(self) -> None:
        from core import queries
        from core.circuit_breaker import is_backed_off
        from tools import common

        table = self.query_one("#sources-table", DataTable)
        table.clear()
        for r in queries.get_sources_status():
            backed_off, until = is_backed_off(r["source"])
            table.add_row(
                common.source_label(r["source"]), "yes" if backed_off else "no", until or "-",
                r["finished_at"] or "-", str(r["n_found"]), str(r["n_new"]), r["errors"] or "-",
            )

    def _refresh_quotas(self) -> None:
        from core.api_usage import quota_summary

        labels = {"adzuna": "Adzuna", "hunter": "Hunter.io", "snov": "Snov.io"}
        table = self.query_one("#quotas-table", DataTable)
        table.clear()
        for q in quota_summary():
            table.add_row(labels.get(q["source"], q["source"]), str(q["used"]), str(q["limit"]), str(q["remaining"]))

    def _refresh_log(self) -> None:
        from core import queries
        from tools import common

        table = self.query_one("#log-table", DataTable)
        table.clear()
        for r in reversed(queries.get_run_log(limit=20)):
            table.add_row(
                common.source_label(r["source"]), r["query"] or "-", str(r["n_found"]),
                str(r["n_new"]), r["errors"] or "-", r["finished_at"] or "-",
            )

    def _refresh_strategy(self) -> None:
        from core import queries
        from tools import common

        table = self.query_one("#strategy-table", DataTable)
        table.clear()
        for r in queries.get_strategy():
            table.add_row(common.source_label(r["source"]), r["query"] or "-", str(r["n_found"]), r["finished_at"] or "-")

    def _refresh_notifications(self) -> None:
        from core import queries

        table = self.query_one("#notifications-table", DataTable)
        table.clear()
        self._notification_detail.clear()
        for i, r in enumerate(reversed(queries.get_notifications(limit=20))):
            offer_ids = json.loads(r["offer_ids"]) if r["offer_ids"] else []
            row_key = str(i)
            self._notification_detail[row_key] = (
                f"{r['title']} ({r['kind']}, {r['created_at']})\n\n{r['message'] or ''}"
            )
            table.add_row(
                r["kind"], r["title"], (r["message"] or "").splitlines()[0][:80],
                ", ".join(str(i) for i in offer_ids) or "-", r["created_at"],
                key=row_key,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "notifications-table":
            return
        detail = self._notification_detail.get(event.row_key.value, "")
        self.query_one("#notification-detail", Static).update(detail)

    def _refresh_funnel(self) -> None:
        from core.db import get_connection
        from tools import common

        with get_connection() as conn:
            stats = common.funnel_stats(conn)
        stages = common.funnel_stages(stats)
        table = self.query_one("#funnel-table", DataTable)
        table.clear()
        for s in stages:
            table.add_row(
                s["label"], str(s["count"]), f"{s['pct_total']}%",
                f"{s['pct_previous']}%" if s["pct_previous"] is not None else "-",
            )
        self.query_one("#funnel-insight", Static).update(common.funnel_insight(stages))

    def _refresh_breakdown(self) -> None:
        from core import queries

        result = queries.get_breakdown()
        table = self.query_one("#breakdown-table", DataTable)
        table.clear()
        table.add_row("Not scored yet", str(result["unscored"]))
        for label, count in result["tiers"]:
            table.add_row(label, str(count))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-gaps":
            self._run_gaps()

    def _run_gaps(self) -> None:
        result_widget = self.query_one("#gaps-result", Static)
        result_widget.update("Analyzing...")
        self.run_worker(self._run_gaps_worker, thread=True, exclusive=True)

    def _run_gaps_worker(self) -> None:
        from graphs.chat_agent import axes_progression
        result = axes_progression.invoke({})
        self.app.call_from_thread(self.query_one("#gaps-result", Static).update, result)
