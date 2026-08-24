"""Interactive guided setup -- run after setup.sh/setup.ps1 have already
created .env from .env.example. Walks through the machine's hardware
suitability for the local model, then asks which optional features to turn
on one at a time (what each is for, taken straight from .env.example's own
comments, with an offer to open that feature's signup page), then an
autostart offer -- and writes only the answered keys back into .env,
leaving every other line (comments, banners, untouched values) byte-for-byte
as-is.

Never runs unattended: exits immediately if stdin isn't a real terminal, so
a piped/non-interactive install (curl | bash, CI) just skips this step, same
as setup.sh's own guard around invoking it in the first place.

Run directly: .venv/bin/python scripts/install_wizard.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if not sys.stdin.isatty():
    sys.exit(0)

from core import autostart, browser, hardware  # noqa: E402

ENV_EXAMPLE = ROOT / ".env.example"
ENV_FILE = ROOT / ".env"

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"
# Older Windows consoles (plain conhost, pre-VT) print raw escape codes as
# literal garbage instead of interpreting them -- setup.ps1's own output uses
# PowerShell's native -ForegroundColor for this reason. Simplest safe choice
# here is no color on Windows at all, same as the existing NO_COLOR/non-tty case.
if not sys.stdout.isatty() or sys.platform == "win32":
    BOLD = DIM = GREEN = YELLOW = RESET = ""


def _heading(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} [{hint}] ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes", "o", "oui"):
            return True
        if answer in ("n", "no", "non"):
            return False
        print("  please answer y or n.")


def _ask_text(prompt: str, secret: bool = False) -> str:
    if secret:
        import getpass
        return getpass.getpass(f"{prompt} (leave blank to skip): ").strip()
    return input(f"{prompt} (leave blank to skip): ").strip()


def _offer_browser(url: str | None) -> None:
    if not url:
        return
    if _ask_yes_no(f"  Open {url} in your browser now?", default=True):
        if not browser.open_url(url):
            print(f"  {YELLOW}couldn't launch a browser -- open it yourself: {url}{RESET}")


# --- .env.example parsing -----------------------------------------------
# Attaches to each KEY the contiguous "#"-prefixed comment block directly
# above it (reset on a blank line or a "# ====" section banner) -- this is
# what feeds the wizard's own explanation text and signup-link offers, so
# the copy shown here can never drift from what's already documented in the
# file itself.

_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_URL_RE = re.compile(r"https?://\S+")


def parse_env_example() -> dict[str, dict]:
    records: dict[str, dict] = {}
    pending: list[str] = []
    for raw_line in ENV_EXAMPLE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("# ===="):
            pending = []
            continue
        if line.startswith("#"):
            pending.append(line.lstrip("#").strip())
            continue
        match = _KEY_RE.match(line)
        if match:
            key, default = match.group(1), match.group(2)
            comment = " ".join(p for p in pending if p)
            url_match = _URL_RE.search(comment)
            records[key] = {
                "comment": comment,
                "url": url_match.group(0).rstrip(".,)") if url_match else None,
                "default": default,
            }
            pending = []
    return records


# --- feature definitions --------------------------------------------------
# Each feature's explanation/URL is pulled from its FIRST listed var's
# .env.example comment (parse_env_example above) -- only the grouping
# (which vars belong together, and whether a var is a secret) is decided
# here, not the wording.

FEATURES = [
    {"id": "discord", "title": "Discord bot",
     "vars": ["DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"], "secrets": {"DISCORD_BOT_TOKEN"}},
    {"id": "lba", "title": "La Bonne Alternance (apprenticeship offers, France)",
     "vars": ["LBA_API_KEY"], "secrets": {"LBA_API_KEY"}},
    {"id": "adzuna", "title": "Adzuna (general-purpose job search)",
     "vars": ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"], "secrets": {"ADZUNA_APP_KEY"}},
    {"id": "france_travail", "title": "France Travail Connect (Offres d'emploi + La Bonne Boite)",
     "vars": ["FRANCE_TRAVAIL_CLIENT_ID", "FRANCE_TRAVAIL_CLIENT_SECRET"],
     "secrets": {"FRANCE_TRAVAIL_CLIENT_SECRET"}},
    {"id": "ats", "title": "ATS watchlist (Greenhouse/Ashby/Lever, by company name)",
     "vars": ["ATS_WATCHLIST"], "secrets": set()},
    {"id": "email", "title": "Email monitoring (Gmail)",
     "vars": ["GMAIL_ACCOUNT_1", "GMAIL_APP_PASSWORD_1", "GMAIL_SEND_ACCOUNT"],
     "secrets": {"GMAIL_APP_PASSWORD_1"}},
    {"id": "tavily", "title": "Tavily (web search for letters/contacts)",
     "vars": ["TAVILY_API_KEY"], "secrets": {"TAVILY_API_KEY"}},
    {"id": "pappers", "title": "Pappers (French company registry)",
     "vars": ["PAPPERS_API_TOKEN"], "secrets": {"PAPPERS_API_TOKEN"}},
    {"id": "hunter", "title": "Hunter.io (verified emails by domain)",
     "vars": ["HUNTER_API_KEY"], "secrets": {"HUNTER_API_KEY"}},
    {"id": "snov", "title": "Snov.io (email finder fallback)",
     "vars": ["SNOV_USER_ID", "SNOV_API_SECRET"], "secrets": {"SNOV_API_SECRET"}},
    {"id": "cv", "title": "CV file (for tailoring and profile extraction)",
     "vars": ["CV_PATH"], "secrets": set()},
]


def ask_feature(records: dict[str, dict], feature: dict) -> dict[str, str]:
    lead_var = feature["vars"][0]
    comment = records.get(lead_var, {}).get("comment", "")
    url = records.get(lead_var, {}).get("url")

    _heading(feature["title"])
    if comment:
        print(f"  {DIM}{comment}{RESET}")
    if not _ask_yes_no("  Configure this now?", default=False):
        return {}

    _offer_browser(url)
    answers: dict[str, str] = {}
    for var in feature["vars"]:
        value = _ask_text(f"  {var}", secret=var in feature["secrets"])
        if value:
            answers[var] = value
    if not answers:
        print(f"  {DIM}nothing entered, skipping.{RESET}")
    return answers


# --- hardware + LLM provider ---------------------------------------------

def ask_hardware() -> dict[str, str]:
    _heading("Checking whether this machine can run the local model comfortably")
    verdict = hardware.assess_install()
    for reason in verdict["reasons"]:
        print(f"  {DIM}{reason}{RESET}")

    if verdict["tier"] == "comfortable":
        print(f"  {GREEN}looks good -- Ollama should run fine.{RESET}")
        return {}
    if verdict["tier"] == "unknown":
        print(f"  {YELLOW}couldn't read system memory -- skipping this check.{RESET}")
        return {}

    print(f"  {YELLOW}this machine may struggle with the default local model.{RESET}")
    groq_console_url = "https://console.groq.com"
    if not _ask_yes_no(
        "  Use a free cloud API (Groq -- only needs a Google account) instead of Ollama?",
        default=True,
    ):
        print(f"  {DIM}keeping Ollama -- it may just run slower.{RESET}")
        return {}

    _offer_browser(groq_console_url)
    api_key = _ask_text("  OPENAI_API_KEY (from console.groq.com)", secret=True)
    if not api_key:
        print(f"  {DIM}no key entered, keeping Ollama.{RESET}")
        return {}
    answers = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": api_key,
               "OPENAI_BASE_URL": "https://api.groq.com/openai/v1"}
    print(f"  {GREEN}set to use Groq via the OpenAI-compatible endpoint.{RESET}")
    return answers


# --- autostart --------------------------------------------------------

def ask_autostart() -> None:
    if not autostart.is_supported():
        return
    _heading("Autostart")
    already = autostart.is_configured()
    note = "already configured -- this refreshes it" if already else "not configured yet"
    print(f"  {DIM}({note}) target: {autostart.describe_target()}{RESET}")
    if not _ask_yes_no("  Start the hobot daemon automatically when you log in?", default=False):
        return
    enable_linger = False
    if sys.platform == "linux":
        enable_linger = _ask_yes_no(
            "  Also survive a reboot with nobody logged in yet (loginctl enable-linger)?",
            default=False,
        )
    try:
        autostart.configure(enable_linger=enable_linger)
        print(f"  {GREEN}done.{RESET}")
    except Exception as e:
        print(f"  {YELLOW}couldn't configure autostart: {e}{RESET}")
        print(f"  {DIM}see README.md's \"Running it continuously\" section to do it by hand.{RESET}")


# --- .env writing ----------------------------------------------------

def write_env(answers: dict[str, str]) -> None:
    if not answers:
        return
    lines = ENV_FILE.read_text().splitlines(keepends=True)
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        match = _KEY_RE.match(stripped)
        if match and match.group(1) in answers:
            key = match.group(1)
            newline = "\n" if line.endswith("\n") else ""
            out.append(f"{key}={answers[key]}{newline}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in answers.items():
        if key not in seen:
            out.append(f"{key}={value}\n")
    tmp = ENV_FILE.with_suffix(".env.tmp") if ENV_FILE.suffix else ENV_FILE.with_name(".env.tmp")
    tmp.write_text("".join(out))
    tmp.replace(ENV_FILE)


def main() -> None:
    if not ENV_FILE.exists():
        print("No .env found -- run ./setup.sh (or setup.ps1) first.")
        return

    print(f"{BOLD}Guided setup{RESET}")
    print(f"{DIM}Answer what you want configured now -- everything else stays as-is in .env,")
    print(f"and can always be filled in later by editing the file directly.{RESET}")

    if not _ask_yes_no("\nRun guided setup now?", default=True):
        print(f"{DIM}Skipping -- edit .env by hand whenever you're ready (see README.md).{RESET}")
        return

    records = parse_env_example()
    answers: dict[str, str] = {}

    answers.update(ask_hardware())

    _heading("Optional features")
    print(f"{DIM}Each one is independent -- skip anything you don't need right now.{RESET}")
    for feature in FEATURES:
        answers.update(ask_feature(records, feature))

    write_env(answers)

    configured = sorted(k for k in answers if k not in ("LLM_PROVIDER", "OPENAI_BASE_URL"))
    if configured:
        _heading("Saved to .env")
        for key in configured:
            print(f"  {GREEN}set{RESET} {key}")

    ask_autostart()

    print(f"\n{BOLD}Guided setup done.{RESET} {DIM}Review .env any time -- nothing here is final.{RESET}")


if __name__ == "__main__":
    main()
