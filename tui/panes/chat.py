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
import re

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Static

from tui.modals import ConfirmModal

CLI_CHAT_THREAD_ID = os.environ.get("CLI_CHAT_THREAD_ID", "cli")

# https URLs only -- job postings, letters, and everything this agent talks
# about link out over https, never a bare "http://" worth special-casing.
_LINK_RE = re.compile(r"https://\S+")
# Trailing characters a URL picked up from surrounding prose almost never
# actually belongs to it (end of sentence, comma, a closing paren that
# opened before the URL started) -- stripped off the clickable span and
# rendered back as plain text right after it.
_LINK_TRAILING_PUNCTUATION = ").,;:!?\"'"

LINK_STYLE = Style(underline=True, color="bright_cyan")


def _message_text(content: str) -> Text:
    """Plain text with any https:// link turned into a clickable span --
    built up with Text.append() instead of parsing `content` as markup, so
    arbitrary LLM output (a job posting's own description can end up quoted
    back in a reply) can never inject a markup/action sequence of its own.
    The click handler below reads the URL back out of the span's meta dict,
    never through a parsed action string, for the same reason."""
    text = Text()
    pos = 0
    for m in _LINK_RE.finditer(content):
        if m.start() > pos:
            text.append(content[pos:m.start()])
        url = m.group(0)
        trailing = ""
        while url and url[-1] in _LINK_TRAILING_PUNCTUATION:
            trailing = url[-1] + trailing
            url = url[:-1]
        text.append(url, style=LINK_STYLE + Style(meta={"hobot_link": url}))
        if trailing:
            text.append(trailing)
        pos = m.end()
    if pos < len(content):
        text.append(content[pos:])
    return text


class ChatPane(Vertical):
    # #chat-log (VerticalScroll) never actually has focus -- the input box
    # does, always (see focus_input below and _on_reply's refocus after
    # every turn) -- so its own built-in scroll bindings never fire; there
    # was no way to scroll the log at all without a working mouse wheel
    # (which not every terminal reports). These re-implement the same
    # bindings at the pane level so they work no matter what has focus.
    # Up/down/pageup/pagedown only, not home/end: Input itself already binds
    # home/end (cursor to start/end of the text box, confirmed in Textual's
    # own source) and consumes those two before they'd ever reach here.
    BINDINGS = [
        ("up", "scroll_log('up')", "Scroll chat up"),
        ("down", "scroll_log('down')", "Scroll chat down"),
        ("pageup", "scroll_log('page_up')", "Page up"),
        ("pagedown", "scroll_log('page_down')", "Page down"),
    ]

    def focus_input(self) -> None:
        self.query_one("#chat-input", Input).focus()

    def scroll_to_end(self) -> None:
        self.query_one("#chat-log", VerticalScroll).scroll_end(animate=False)

    def action_scroll_log(self, direction: str) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        {
            "up": log.scroll_up, "down": log.scroll_down,
            "page_up": log.scroll_page_up, "page_down": log.scroll_page_down,
        }[direction](animate=False)

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="chat-log")
        with Vertical(id="pending-sends"):
            pass
        with Horizontal(classes="chat-input-row"):
            yield Input(placeholder="Ask anything -- find postings, check a score, draft a reply...", id="chat-input")
            yield Button("Send", id="send-chat", variant="primary")
            yield Button("Clear chat", id="clear-chat")

    def on_mount(self) -> None:
        from graphs.chat_agent import get_history

        # Textual's own mechanism for a chat-log-shaped widget: stays
        # scrolled to the bottom on every future content change on its own,
        # rather than this pane having to remember to call scroll_end()
        # after each mutation and hope the timing works out (the explicit
        # calls below are kept anyway, as a second, redundant path to the
        # same result -- cheap insurance, not a sign this alone was
        # insufficient).
        self.query_one("#chat-log", VerticalScroll).anchor()
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
        is_user = role == "user"
        turn = Vertical(classes=f"chat-turn {'chat-turn-user' if is_user else 'chat-turn-assistant'}")
        log.mount(turn)
        turn.mount(Static("You" if is_user else "hobot", classes="chat-role-user" if is_user else "chat-role-assistant"))
        turn.mount(Static(_message_text(content), classes="chat-message"))
        self.scroll_to_end()

    def on_click(self, event) -> None:
        # Click bubbles up from whichever Static rendered the link (see
        # _message_text) -- caught once here instead of a custom widget
        # subclass per message. event.style reflects whatever's under the
        # cursor at click time, only set on a link span (see LINK_STYLE).
        style = getattr(event, "style", None)
        url = style.meta.get("hobot_link") if style else None
        if url:
            event.stop()
            self.app.open_url(url)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._send()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send-chat":
            self._send()
        elif event.button.id == "clear-chat":
            self._confirm_clear_chat()
        elif event.button.id and event.button.id.startswith("confirm-send-"):
            self._confirm_send(event.button.id.removeprefix("confirm-send-"))
        elif event.button.id and event.button.id.startswith("dismiss-send-"):
            self._dismiss_send(event.button.id.removeprefix("dismiss-send-"))

    def _confirm_clear_chat(self) -> None:
        def handle(confirmed: bool | None) -> None:
            if confirmed:
                self._clear_chat()
        self.app.push_screen(
            ConfirmModal(
                "Clear this conversation?",
                "Deletes this chat's history for good -- also the reliable way to force a fresh "
                "answer to a status question (e.g. is the daemon running) instead of the model "
                "quoting its own earlier answer from this same conversation.",
                confirm_label="Clear", danger=True,
            ),
            handle,
        )

    def _clear_chat(self) -> None:
        from graphs.chat_agent import clear_thread
        clear_thread(CLI_CHAT_THREAD_ID)
        self.query_one("#chat-log", VerticalScroll).remove_children()
        self.notify("Chat cleared.")

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
        self.scroll_to_end()
        self.run_worker(lambda: self._ask_worker(message), thread=True, exclusive=True)

    def _ask_worker(self, message: str) -> None:
        from graphs.chat_agent import ask
        reply = ask(message, thread_id=CLI_CHAT_THREAD_ID)
        self.app.call_from_thread(self._on_reply, reply)

    def _on_reply(self, reply: str) -> None:
        indicator = self.query_one("#thinking-indicator", Static)
        indicator.remove()
        # _append_turn() already scrolls to the true bottom -- kept as a
        # separate, explicit call here too (not just relying on the one
        # inside _append_turn) so the intent (always land on the latest
        # message, input box included, the moment hobot replies) survives
        # even if _append_turn's own internal scrolling ever changes.
        self._append_turn("assistant", reply)
        self.scroll_to_end()
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
