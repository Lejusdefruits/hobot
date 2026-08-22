"""Profile pane -- view the structured profile, upload a CV (pdf/docx)
through the same detect_format -> parse_cv -> save_profile ->
save_profile_source sequence (core/profile.py) the Discord /profile command
calls, or edit full_name/skills/target_roles/target_locations directly --
the same fields definir_profil/modifier_profil (graphs/chat_agent.py,
reachable through Chat/`/ask`) touch, without the LLM round-trip for a plain
"fix this field" edit. Free-text updates ("I'm looking for X in Lyon",
CV-derived experience/education) still go through Chat/`/ask`; the edit
form here never touches those.

The edit form is inline (shown/hidden in this same pane), not a modal
screen: a popup ProfileEditModal was tried first and reported completely
unresponsive to clicks in a real terminal (fields, Save, Cancel -- nothing
reachable except Escape) despite testing clean under Textual's own pilot
harness, which dispatches synthetic events directly and doesn't reproduce
whatever real terminal/mouse-reporting quirk was actually happening.
Everything here lives in the one already-reliable widget tree instead of a
separate overlay screen -- same category of widget (Input, Button) as the
Chat pane's own input row, which was never reported broken."""
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Static

from tui.modals import CvFilePickerModal


class ProfilePane(VerticalScroll):
    # A plain Vertical here doesn't scroll -- with the edit form's fields
    # added, this pane's content is taller than a classic 80x24 terminal's
    # viewport, and the overflow (target roles/locations, Save/Cancel) was
    # silently clipped below the visible area instead of reachable by
    # scrolling. StatusPane already uses VerticalScroll for the same reason.
    def compose(self) -> ComposeResult:
        yield Static(id="profile-content", classes="section")
        with Vertical(id="profile-edit-form"):
            # compact=True: a bordered Input is 3 rows tall by default --
            # ×4 fields alone was most of why this didn't fit an ordinary
            # 80x24 terminal without scrolling.
            yield Label("Full name")
            yield Input(id="profile-full-name", compact=True)
            yield Label("Skills (comma-separated)")
            yield Input(id="profile-skills", compact=True)
            yield Label("Target roles (comma-separated)")
            yield Input(id="profile-target-roles", compact=True)
            yield Label("Target locations (comma-separated)")
            yield Input(id="profile-target-locations", compact=True)
            with Horizontal(classes="button-row"):
                yield Button("Save", id="save-profile", variant="primary")
                yield Button("Cancel", id="cancel-profile-edit")
        with Horizontal(classes="button-row"):
            yield Button("Edit profile", id="edit-profile")
            yield Button("Upload CV...", id="upload-cv")
            yield Button("Refresh", id="refresh")
        yield Static(id="profile-error", classes="error-text")

    def on_mount(self) -> None:
        self.query_one("#profile-edit-form").display = False
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
            self._open_edit_form()
        elif event.button.id == "save-profile":
            self._save_profile()
        elif event.button.id == "cancel-profile-edit":
            self._close_edit_form()

    def _open_edit_form(self) -> None:
        from core.db import get_user_profile

        profile = get_user_profile() or {}
        self.query_one("#profile-full-name", Input).value = profile.get("full_name") or ""
        self.query_one("#profile-skills", Input).value = ", ".join(profile.get("skills") or [])
        self.query_one("#profile-target-roles", Input).value = ", ".join(profile.get("target_roles") or [])
        self.query_one("#profile-target-locations", Input).value = ", ".join(profile.get("target_locations") or [])
        self.query_one("#profile-error", Static).update("")
        # Hidden while editing -- it's the same four values the form now
        # shows, keeping both on screen just pushes the form (and Save/
        # Cancel) further down for no reason.
        self.query_one("#profile-content", Static).display = False
        self.query_one("#profile-edit-form").display = True
        self.query_one("#profile-full-name", Input).focus()

    def _close_edit_form(self) -> None:
        self.query_one("#profile-edit-form").display = False
        self.query_one("#profile-content", Static).display = True

    @staticmethod
    def _split(value: str) -> list[str]:
        return [v.strip() for v in value.split(",") if v.strip()]

    def _save_profile(self) -> None:
        import json

        from core.db import get_connection

        full_name = self.query_one("#profile-full-name", Input).value.strip() or None
        skills = self._split(self.query_one("#profile-skills", Input).value)
        target_roles = self._split(self.query_one("#profile-target-roles", Input).value)
        target_locations = self._split(self.query_one("#profile-target-locations", Input).value)

        with get_connection() as conn:
            conn.execute(
                # Only these four columns -- raw_text/experience/education
                # (set by a CV upload, see _upload_cv_worker below) are left
                # alone on both insert (NULL, fine -- no CV yet) and update
                # (excluded from the SET clause, so a prior CV's parsed data
                # survives a plain field edit here).
                """INSERT INTO user_profile (id, full_name, skills, target_roles, target_locations, updated_at)
                   VALUES (1, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET
                       full_name = excluded.full_name, skills = excluded.skills,
                       target_roles = excluded.target_roles, target_locations = excluded.target_locations,
                       updated_at = excluded.updated_at""",
                (
                    full_name, json.dumps(skills, ensure_ascii=False),
                    json.dumps(target_roles, ensure_ascii=False), json.dumps(target_locations, ensure_ascii=False),
                ),
            )
        self._close_edit_form()
        self.notify("Profile updated.")
        self.refresh_profile()

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
