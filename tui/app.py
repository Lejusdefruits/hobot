"""hobot's terminal UI -- launched by cli.py as its own foreground process,
never inside the daemon (systemd runs daemon.py headless, with no terminal
attached for a full-screen app to take over). It talks to the exact same
SQLite database and calls the exact same functions Discord does; the only
piece that needed real cross-process wiring is the pause flag, now backed by
the daemon_flags table instead of an in-memory global (core/daemon_state.py).
Everything else (offers, applications, drafts, profile, chat, reports) is a
plain read/write against the database, same as the old web dashboard was --
no server, no auth token needed: this only ever runs as you, in your own
terminal."""
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, TabbedContent, TabPane

from tui.panes.applications import ApplicationsPane
from tui.panes.chat import ChatPane
from tui.panes.drafts import DraftsPane
from tui.panes.offers import OffersPane
from tui.panes.profile import ProfilePane
from tui.panes.reports import ReportsPane
from tui.panes.status import StatusPane


class HobotApp(App):
    TITLE = "hobot"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("f1", "show_tab('tab-status')", "Status"),
        ("f2", "show_tab('tab-offers')", "Offers"),
        ("f3", "show_tab('tab-applications')", "Applications"),
        ("f4", "show_tab('tab-drafts')", "Drafts"),
        ("f5", "show_tab('tab-profile')", "Profile"),
        ("f6", "show_tab('tab-chat')", "Chat"),
        ("f7", "show_tab('tab-reports')", "Reports"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="tab-status"):
            with TabPane("Status", id="tab-status"):
                yield StatusPane(classes="pane")
            with TabPane("Offers", id="tab-offers"):
                yield OffersPane(classes="pane")
            with TabPane("Applications", id="tab-applications"):
                yield ApplicationsPane(classes="pane")
            with TabPane("Drafts", id="tab-drafts"):
                yield DraftsPane(classes="pane")
            with TabPane("Profile", id="tab-profile"):
                yield ProfilePane(classes="pane")
            with TabPane("Chat", id="tab-chat"):
                yield ChatPane(classes="pane")
            with TabPane("Reports", id="tab-reports"):
                yield ReportsPane(classes="pane")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "tokyo-night"

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        # Focusing the chat input as soon as its tab becomes current, not
        # from ChatPane.on_mount() -- every pane mounts eagerly at startup
        # (not just the visible one), and focusing a widget in a hidden pane
        # would silently switch the active tab to reveal it.
        if event.pane is not None and event.pane.id == "tab-chat":
            self.query_one(ChatPane).focus_input()


def run() -> None:
    HobotApp().run()
