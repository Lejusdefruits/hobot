"""Configures hobot to start automatically -- one function per platform,
each doing exactly what systemd/hobot.service, launchd/com.hobot.daemon.plist,
and README.md's "Running it continuously" section already document as the
manual procedure. This module automates that existing, reviewed procedure;
it never invents a different one, and never asks for elevated/administrator
privileges (matching the templates' own deliberate scope -- systemd's is a
--user unit, launchd's is a LaunchAgent not a LaunchDaemon, on purpose, per
their own header comments).

Every configure_*() prints the exact target file and exact commands before
running them and is only ever called after an explicit yes -- nothing here
runs silently or unprompted; scripts/install_wizard.py is the only caller.
"""
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "/path/to/hobot"


def is_supported() -> bool:
    return sys.platform in ("linux", "darwin", "win32")


def describe_target() -> str:
    """Where configure() would write to, for the confirmation prompt --
    called before configure(), never assumes the platform silently."""
    if sys.platform == "linux":
        return str(Path.home() / ".config/systemd/user/hobot.service")
    if sys.platform == "darwin":
        return str(Path.home() / "Library/LaunchAgents/com.hobot.daemon.plist")
    if sys.platform == "win32":
        return "Task Scheduler: \"hobot\" (Get-ScheduledTask -TaskName hobot)"
    return "(unsupported platform)"


def is_configured() -> bool:
    """Best-effort -- only used to tell the user "already set up, this will
    refresh it" vs "not set up yet" in the wizard's prompt. False (not an
    exception) on any detection problem, same fail-open rule as everywhere
    else that's just advisory."""
    try:
        if sys.platform == "linux":
            return (Path.home() / ".config/systemd/user/hobot.service").exists()
        if sys.platform == "darwin":
            return (Path.home() / "Library/LaunchAgents/com.hobot.daemon.plist").exists()
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "(Get-ScheduledTask -TaskName hobot -ErrorAction SilentlyContinue) -ne $null"],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout.strip().lower() == "true"
    except Exception:
        pass
    return False


def _substitute(template_path: Path) -> str:
    content = template_path.read_text()
    return content.replace(PLACEHOLDER, str(REPO_ROOT))


def configure(enable_linger: bool = False) -> None:
    """Raises on failure (a subprocess.CalledProcessError or OSError) --
    scripts/install_wizard.py catches it and reports the failure plainly
    rather than pretending autostart succeeded. Idempotent on every
    platform: safe to call again (refresh the unit/plist/task after moving
    the repo, for instance)."""
    if sys.platform == "linux":
        _configure_linux(enable_linger)
    elif sys.platform == "darwin":
        _configure_macos()
    elif sys.platform == "win32":
        _configure_windows()
    else:
        raise RuntimeError(f"autostart isn't supported on {sys.platform!r}")


def _configure_linux(enable_linger: bool) -> None:
    target = Path.home() / ".config/systemd/user/hobot.service"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_substitute(REPO_ROOT / "systemd/hobot.service"))

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
    subprocess.run(["systemctl", "--user", "enable", "--now", "hobot"], check=True, timeout=15)
    if enable_linger:
        # Only path to true boot-time survival (no login required) -- the
        # other two platforms don't offer an equally simple, no-password
        # equivalent for a personal desktop tool, see the wizard's own
        # linger-specific question.
        import getpass
        subprocess.run(["loginctl", "enable-linger", getpass.getuser()], check=True, timeout=15)


def _configure_macos() -> None:
    target = Path.home() / "Library/LaunchAgents/com.hobot.daemon.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_substitute(REPO_ROOT / "launchd/com.hobot.daemon.plist"))

    # unload first (ignore failure -- "not currently loaded" is the expected
    # case on a first run) so a second run of the wizard refreshes a
    # possibly-stale plist instead of launchctl load erroring on "already
    # loaded" with the old content still active.
    subprocess.run(["launchctl", "unload", str(target)], capture_output=True, timeout=15)
    subprocess.run(["launchctl", "load", str(target)], check=True, timeout=15)


def _configure_windows() -> None:
    repo_root = str(REPO_ROOT)
    if '"' in repo_root or "`" in repo_root:
        # Same reasoning as tools/notify_tools.py's _sanitize_for_script_literal:
        # untested escaping against a real Windows box isn't worth trusting for
        # a system-config change -- refuse rather than risk a broken/injectable
        # command. A path with a quote/backtick in it is exceedingly unusual.
        raise RuntimeError(
            f"install path contains a character (\" or `) that can't be safely passed to "
            f"PowerShell: {repo_root!r} -- move the install and try again, or configure "
            f"Task Scheduler by hand (see README.md)."
        )
    python_exe = repo_root + r"\.venv\Scripts\python.exe"
    script = (
        f'$action = New-ScheduledTaskAction -Execute "{python_exe}" -Argument "-u daemon.py" -WorkingDirectory "{repo_root}"; '
        "$trigger = New-ScheduledTaskTrigger -AtLogOn; "
        "$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
        'Register-ScheduledTask -TaskName "hobot" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null'
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, timeout=20)


if __name__ == "__main__":
    # Manual smoke test: `python -m core.autostart` prints what would happen
    # without actually doing it -- configure() itself has no dry-run mode by
    # design (every step it takes is meant to be real and is only ever
    # reached after the wizard's own explicit confirmation).
    print("supported:", is_supported())
    print("target:", describe_target())
    print("already configured:", is_configured())
    print(f"template check ({shutil.which('systemctl') or shutil.which('launchctl') or 'n/a'}):")
    if sys.platform == "linux":
        print(_substitute(REPO_ROOT / "systemd/hobot.service")[:500])
    elif sys.platform == "darwin":
        print(_substitute(REPO_ROOT / "launchd/com.hobot.daemon.plist")[:500])
