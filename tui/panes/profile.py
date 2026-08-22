"""Profile pane -- view the structured profile, upload a CV (pdf/docx)
through the same detect_format -> parse_cv -> save_profile ->
save_profile_source sequence (core/profile.py) the Discord /profile command
calls, or edit full_name/skills/target_roles/target_locations directly
(ProfileEditModal, tui/modals.py) -- the same fields definir_profil/
modifier_profil (graphs/chat_agent.py, reachable through Chat/`/ask`) touch,
without the LLM round-trip for a plain "fix this field" edit. Free-text
updates ("I'm looking for X in Lyon", CV-derived experience/education) still
go through Chat/`/ask`; ProfileEditModal never touches those."""
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Static

from tui.modals import CvFilePickerModal, ProfileEditModal


class ProfilePane(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(id="profile-content", classes="section")
        with Vertical(classes="button-row"):
            yield Button("Edit profile...", id="edit-profile")
            yield Button("Upload CV...", id="upload-cv")
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
        elif event.button.id == "edit-profile":
            self._edit_profile()

    def _edit_profile(self) -> None:
        def handle(saved: bool) -> None:
            if saved:
                self.notify("Profile updated.")
                self.refresh_profile()
        self.app.push_screen(ProfileEditModal(), handle)

    def _prompt_cv_path(self) -> None:
        def handle(path) -> None:
            if path:
                self._upload_cv(path)
        self.app.push_screen(CvFilePickerModal(), handle)

    def _upload_cv(self, path) -> None:
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
