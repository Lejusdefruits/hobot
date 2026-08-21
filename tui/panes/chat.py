"""Chat pane -- the terminal UI's equivalent of /ask: same graphs.chat_agent
functions (ask, get_history, execute_pending_send) the Discord command calls,
against the exact same SqliteSaver-backed agent (graphs/chat_agent.py). Runs
its own fixed thread id (CLI_CHAT_THREAD_ID, default "cli") so a conversation
here never interleaves unexpectedly with a Discord user's -- point it at a
real Discord user id instead if you'd rather the two share one conversation.

ask() is a blocking LLM call with no per-request timeout (unlike Discord's
interaction-token expiry) -- run in a worker thread so the rest of the UI
(other panes, the input box) stays responsive while the model thinks."""
import os

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Static

CLI_CHAT_THREAD_ID = os.environ.get("CLI_CHAT_THREAD_ID", "cli")


class ChatPane(Vertical):
    def focus_input(self) -> None:
        self.query_one("#chat-input", Input).focus()

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="chat-log")
        with Vertical(id="pending-sends"):
            pass
        with Horizontal(classes="chat-input-row"):
            yield Input(placeholder="Ask anything -- find postings, check a score, draft a reply...", id="chat-input")
            yield Button("Send", id="send-chat", variant="primary")

    def on_mount(self) -> None:
        from graphs.chat_agent import get_history

        for turn in get_history(CLI_CHAT_THREAD_ID):
            self._append_turn(turn["role"], turn["content"])
        self._refresh_pending()
        # No auto-focus here: every pane mounts eagerly at startup (not just
        # the visible one), and focusing a widget reveals its tab -- doing
        # this unconditionally would hijack the initial tab away from Status
        # the moment the app opens. tui/app.py focuses this input instead,
        # only when the Chat tab actually becomes the active one.

    def _append_turn(self, role: str, content: str) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        role_class = "chat-role-user" if role == "user" else "chat-role-assistant"
        role_label = "You" if role == "user" else "hobot"
        turn = Vertical(classes="chat-turn")
        log.mount(turn)
        turn.mount(Static(role_label, classes=role_class))
        turn.mount(Static(content, markup=False))
        log.scroll_end(animate=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._send()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send-chat":
            self._send()
        elif event.button.id and event.button.id.startswith("confirm-send-"):
            self._confirm_send(event.button.id.removeprefix("confirm-send-"))
        elif event.button.id and event.button.id.startswith("dismiss-send-"):
            self._dismiss_send(event.button.id.removeprefix("dismiss-send-"))

    def _send(self) -> None:
        chat_input = self.query_one("#chat-input", Input)
        message = chat_input.value.strip()
        if not message:
            return
        chat_input.value = ""
        chat_input.disabled = True
        self._append_turn("user", message)
        log = self.query_one("#chat-log", VerticalScroll)
        thinking = Static("hobot is thinking...", classes="hint", id="thinking-indicator")
        log.mount(thinking)
        log.scroll_end(animate=False)
        self.run_worker(lambda: self._ask_worker(message), thread=True, exclusive=True)

    def _ask_worker(self, message: str) -> None:
        from graphs.chat_agent import ask
        reply = ask(message, thread_id=CLI_CHAT_THREAD_ID)
        self.app.call_from_thread(self._on_reply, reply)

    def _on_reply(self, reply: str) -> None:
        indicator = self.query_one("#thinking-indicator", Static)
        indicator.remove()
        self._append_turn("assistant", reply)
        self.query_one("#chat-input", Input).disabled = False
        self.query_one("#chat-input", Input).focus()
        self._refresh_pending()

    def _refresh_pending(self) -> None:
        from graphs.chat_agent import PENDING_SENDS

        container = self.query_one("#pending-sends", Vertical)
        container.remove_children()
        for pending_id, payload in PENDING_SENDS.items():
            card = Vertical(classes="pending-send")
            container.mount(card)
            card.mount(Static(
                f"Proposal {pending_id}: send to {payload['destinataire']}, subject \"{payload['sujet']}\"",
                markup=False,
            ))
            row = Horizontal(classes="button-row")
            card.mount(row)
            row.mount(Button("Confirm send", id=f"confirm-send-{pending_id}", variant="primary"))
            row.mount(Button("Dismiss", id=f"dismiss-send-{pending_id}"))

    def _confirm_send(self, pending_id: str) -> None:
        from graphs.chat_agent import execute_pending_send
        result = execute_pending_send(pending_id)
        self.notify(f"{result['title']}: {result['description']}")
        self._refresh_pending()

    def _dismiss_send(self, pending_id: str) -> None:
        from graphs.chat_agent import PENDING_SENDS
        PENDING_SENDS.pop(pending_id, None)
        self._refresh_pending()
