"""Profile pane -- view the structured profile plus upload a CV (pdf/docx)
through the same detect_format -> parse_cv -> save_profile ->
save_profile_source sequence (core/profile.py) the Discord /profile command
calls. Free-text profile updates ("I'm looking for X in Lyon") go through
the Chat pane instead -- an LLM round-trip doesn't belong behind a file
upload button, and definir_profil/modifier_profil (graphs/chat_agent.py)
already handle it there."""
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Static

from tui.modals import TextPromptModal


class ProfilePane(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(id="profile-content", classes="section")
        with Vertical(classes="button-row"):
            yield Button("Upload CV (pdf/docx path)", id="upload-cv")
            yield Button("Refresh", id="refresh")
        yield Static(id="profile-error", classes="error-text")

    def on_mount(self) -> None:
        self.refresh_profile()

    def refresh_profile(self) -> None:
        from core.db import get_user_profile

        profile = get_user_profile()
        content = self.query_one("#profile-content", Static)
        if not profile:
            content.update("No profile yet. Upload a CV, or describe yourself in the Chat pane.")
            return
        lines = [
            f"Name: {profile.get('full_name') or '(not set)'}",
            f"Skills: {', '.join(profile.get('skills') or []) or '(none)'}",
            f"Target roles: {', '.join(profile.get('target_roles') or []) or '(none)'}",
            f"Target locations: {', '.join(profile.get('target_locations') or []) or '(none)'}",
        ]
        content.update("\n".join(lines))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh":
            self.refresh_profile()
        elif event.button.id == "upload-cv":
            self._prompt_cv_path()

    def _prompt_cv_path(self) -> None:
        def handle(path: str | None) -> None:
            if path:
                self._upload_cv(path)
        self.app.push_screen(
            TextPromptModal("Path to your CV (.pdf or .docx)", placeholder="/home/you/cv.pdf"),
            handle,
        )

    def _upload_cv(self, path_str: str) -> None:
        from pathlib import Path

        path = Path(path_str).expanduser()
        error_widget = self.query_one("#profile-error", Static)
        if path.suffix.lower() not in (".pdf", ".docx"):
            error_widget.update("Only .pdf and .docx are supported.")
            return
        if not path.exists():
            error_widget.update(f"No such file: {path}")
            return
        error_widget.update("")
        # parse_cv is an LLM call (vision-based for an image-only CV) -- run
        # off the UI thread so the rest of the app stays responsive for
        # however long that takes, same reasoning tui/panes/chat.py's
        # _send() and tui/modals.py's CV-tailoring worker already document.
        self.notify("Reading CV...", timeout=3)
        self.run_worker(lambda: self._upload_cv_worker(path), thread=True, exclusive=True)

    def _upload_cv_worker(self, path) -> None:
        from core import profile as profile_mod

        try:
            fmt = profile_mod.detect_format(path)
            parsed = profile_mod.parse_cv(path, fmt=fmt)
            profile_mod.save_profile(parsed)
            profile_mod.save_profile_source(path, fmt)
        except Exception as e:
            self.app.call_from_thread(self.query_one("#profile-error", Static).update, f"Could not read that CV: {e}")
            return
        self.app.call_from_thread(self.notify, "Profile updated from CV.")
        self.app.call_from_thread(self.refresh_profile)
