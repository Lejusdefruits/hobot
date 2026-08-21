"""Dashboard pane -- core.queries.get_status_summary(), the pause toggle, an
on-demand weekly digest (daemon._build_weekly_digest/_run_weekly_digest,
shown in full below the buttons rather than left to show up flattened into
a single row of the Notifications report), and the reset danger zone. No
live scheduler introspection here (see core/daemon_state.py's module
docstring on why `paused` is DB-backed but `scheduler` isn't) -- "next run"
is shown as the configured cron window instead of a jittered timestamp,
which is what these env vars actually guarantee anyway."""
import os

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

from tui.modals import ConfirmModal

DISCOVERY_HOURS_WEEKDAY = os.environ.get("DISCOVERY_HOURS_WEEKDAY", "9,11,13,15,17")
DISCOVERY_HOURS_WEEKEND = os.environ.get("DISCOVERY_HOURS_WEEKEND", "10,16")
DISCOVERY_JITTER_MIN = os.environ.get("JOBSPY_JITTER_MIN", "90")
EMAIL_INTERVAL_MIN = os.environ.get("EMAIL_POLL_INTERVAL_MIN", "20")
EMAIL_JITTER_MIN = os.environ.get("EMAIL_POLL_JITTER_MIN", "5")
WEEKLY_DIGEST_DAY = os.environ.get("WEEKLY_DIGEST_DAY", "mon")
WEEKLY_DIGEST_HOUR = os.environ.get("WEEKLY_DIGEST_HOUR", "9")
EMAIL_CONFIGURED = bool(os.environ.get("GMAIL_ACCOUNT_1"))


class StatusPane(VerticalScroll):
    """Auto-refreshes every 30s while visible so pause/resume flipped from
    Discord (or a scheduled run finishing) shows up without a manual
    refresh."""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="card-row"):
            yield Static(id="card-state", classes="card")
            yield Static(id="card-backlog", classes="card")
            yield Static(id="card-best", classes="card")
        yield Static("Discovery", classes="section-title")
        with Vertical(classes="section"):
            yield Static(id="discovery-info")
        yield Static("Mail watching", classes="section-title")
        with Vertical(classes="section"):
            yield Static(id="email-info")
        with Horizontal(classes="button-row"):
            yield Button("Pause", id="toggle-pause")
            yield Button("Run digest now", id="run-digest")
            yield Button("Refresh", id="refresh")
            yield Button("Reset everything", id="reset", classes="danger-btn")
        yield Static("Weekly digest", classes="section-title")
        with Vertical(classes="section"):
            yield Static(id="digest-text", markup=False)

    def on_mount(self) -> None:
        self.refresh_status()
        self._load_last_digest()
        self.set_interval(30, self.refresh_status)

    def refresh_status(self) -> None:
        from core import daemon_state, queries

        summary = queries.get_status_summary()
        last_discovery, last_email = summary["last_discovery"], summary["last_email"]
        backlog, best = summary["backlog"], summary["best"]
        paused = daemon_state.is_paused()

        state_card = self.query_one("#card-state", Static)
        state_card.update(
            f"[b]State[/b]\n[{'$warning' if paused else '$success'}]"
            f"{'Paused' if paused else 'Active'}[/]"
        )
        self.query_one("#toggle-pause", Button).label = "Resume" if paused else "Pause"

        self.query_one("#card-backlog", Static).update(f"[b]Scoring queue[/b]\n{backlog} unscored")

        best_text = f"[{best['score']}] {best['title']} -- {best['company']}" if best else "(none yet)"
        self.query_one("#card-best", Static).update(f"[b]Best open posting[/b]\n{best_text}")

        if last_discovery:
            discovery_text = (
                f"Last run: {last_discovery['finished_at']} "
                f"({last_discovery['source']}, {last_discovery['n_new']} new / {last_discovery['n_found']} found)\n"
            )
        else:
            discovery_text = "Last run: never\n"
        discovery_text += (
            f"Schedule: weekdays {DISCOVERY_HOURS_WEEKDAY}h, weekends {DISCOVERY_HOURS_WEEKEND}h "
            f"(+/-{DISCOVERY_JITTER_MIN}min jitter)"
        )
        self.query_one("#discovery-info", Static).update(discovery_text)

        if not EMAIL_CONFIGURED:
            email_text = "Not configured (no GMAIL_ACCOUNT_1 in .env)."
        elif last_email:
            email_text = (
                f"Last run: {last_email['finished_at']} "
                f"({last_email['n_new']} new)\n"
                f"Schedule: every {EMAIL_INTERVAL_MIN}min (+/-{EMAIL_JITTER_MIN}min jitter)"
            )
        else:
            email_text = f"Last run: never\nSchedule: every {EMAIL_INTERVAL_MIN}min (+/-{EMAIL_JITTER_MIN}min jitter)"
        self.query_one("#email-info", Static).update(email_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "refresh":
            self.refresh_status()
        elif bid == "toggle-pause":
            self._toggle_pause()
        elif bid == "run-digest":
            self._run_digest()
        elif bid == "reset":
            self._confirm_reset()

    def _toggle_pause(self) -> None:
        from core import daemon_state
        daemon_state.set_paused(not daemon_state.is_paused())
        self.refresh_status()

    def _load_last_digest(self) -> None:
        """Shows the most recently sent digest on mount, read back from the
        notifications table -- so the interface has something to show even
        before you've clicked "Run digest now" this session (the scheduled
        job writes the same row)."""
        from core import queries

        for row in queries.get_notifications(limit=20):
            if row["kind"] == "digest":
                self._show_digest(row["message"], sent_at=row["created_at"])
                return
        self._show_digest(None)

    def _show_digest(self, text: str | None, sent_at: str | None = None) -> None:
        widget = self.query_one("#digest-text", Static)
        if text is None:
            widget.update(
                f"Nothing sent yet. Runs automatically {WEEKLY_DIGEST_DAY} at {WEEKLY_DIGEST_HOUR}h, "
                f"or click \"Run digest now\" above."
            )
            return
        header = f"Sent {sent_at}\n\n" if sent_at else ""
        widget.update(header + text)

    def _run_digest(self) -> None:
        self.notify("Building the weekly digest...", timeout=3)
        self.run_worker(self._run_digest_worker, thread=True, exclusive=True)

    def _run_digest_worker(self) -> None:
        import daemon as daemon_module
        text = daemon_module._run_weekly_digest()
        if text is None:
            self.app.call_from_thread(self.notify, "Nothing to report this week -- no digest sent.")
            return
        self.app.call_from_thread(self._show_digest, text, "just now")
        self.app.call_from_thread(
            self.notify, "Digest sent (desktop notification + Discord if the daemon is running) -- see below.",
        )

    def _confirm_reset(self) -> None:
        def handle(confirmed: bool | None) -> None:
            if confirmed:
                self._do_reset()
        self.app.push_screen(
            ConfirmModal(
                "Reset everything?",
                "Wipes every posting, application, draft record, contact, profile and chat memory. "
                "Generated CVs and letters on disk are deleted too. This cannot be undone.",
                confirm_label="Wipe everything",
                danger=True,
            ),
            handle,
        )

    def _do_reset(self) -> None:
        from core import queries
        from graphs.chat_agent import wipe_memory

        queries.wipe_database()
        queries.wipe_files()
        wipe_memory()
        self.notify("Database and files wiped.")
        self.refresh_status()
