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


RULE_WIDTH = 60


def _rule(char: str = "-") -> str:
    return char * RULE_WIDTH


def _banner(text: str) -> None:
    print(f"{BOLD}{_rule('=')}{RESET}")
    print(f"{BOLD}{text}{RESET}")
    print(f"{BOLD}{_rule('=')}{RESET}")


def _heading(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")
    print(f"{DIM}{_rule()}{RESET}")


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


def _ask_choice(prompt: str, options: list[str], default_index: int = 0) -> int:
    print(f"  {prompt}")
    for i, option in enumerate(options):
        marker = " (default)" if i == default_index else ""
        print(f"    {i + 1}. {option}{marker}")
    while True:
        raw = input(f"  > [{default_index + 1}] ").strip()
        if not raw:
            return default_index
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(f"  please enter a number from 1 to {len(options)}.")


def _mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _ask_text(prompt: str, secret: bool = False) -> str:
    if secret:
        import getpass
        # getpass shows nothing at all while typing/pasting (not even a
        # placeholder character) -- with zero feedback there's no way to
        # tell a paste actually landed, so echo back a masked confirmation
        # right after capture instead of leaving the user guessing.
        value = getpass.getpass(f"{prompt} (leave blank to skip): ").strip()
        if value:
            print(f"    {DIM}got: {_mask_secret(value)} ({len(value)} characters){RESET}")
        return value
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

# france_only: True marks a source that's useless outside the French job
# market (La Bonne Alternance and France Travail Connect only cover French
# postings; Pappers is the French legal company registry) -- ask_market()
# below gates these out entirely for a "no" answer, rather than showing a
# question about a source that could never return anything relevant. Adzuna
# is explicitly multi-country and Hunter/Snov are "any country" per their own
# .env.example comments, so none of those three are gated.
FEATURES = [
    {"id": "discord", "title": "Discord bot",
     "vars": ["DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"], "secrets": {"DISCORD_BOT_TOKEN"}},
    {"id": "lba", "title": "La Bonne Alternance (apprenticeship offers, France)",
     "vars": ["LBA_API_KEY"], "secrets": {"LBA_API_KEY"}, "france_only": True},
    {"id": "adzuna", "title": "Adzuna (general-purpose job search)",
     "vars": ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"], "secrets": {"ADZUNA_APP_KEY"}},
    {"id": "france_travail", "title": "France Travail Connect (Offres d'emploi + La Bonne Boite)",
     "vars": ["FRANCE_TRAVAIL_CLIENT_ID", "FRANCE_TRAVAIL_CLIENT_SECRET"],
     "secrets": {"FRANCE_TRAVAIL_CLIENT_SECRET"}, "france_only": True},
    {"id": "ats", "title": "ATS watchlist (Greenhouse/Ashby/Lever, by company name)",
     "vars": ["ATS_WATCHLIST"], "secrets": set()},
    {"id": "email", "title": "Email monitoring (Gmail)",
     "vars": ["GMAIL_ACCOUNT_1", "GMAIL_APP_PASSWORD_1", "GMAIL_SEND_ACCOUNT"],
     "secrets": {"GMAIL_APP_PASSWORD_1"}},
    {"id": "tavily", "title": "Tavily (web search for letters/contacts)",
     "vars": ["TAVILY_API_KEY"], "secrets": {"TAVILY_API_KEY"}},
    {"id": "pappers", "title": "Pappers (French company registry)",
     "vars": ["PAPPERS_API_TOKEN"], "secrets": {"PAPPERS_API_TOKEN"}, "france_only": True},
    {"id": "hunter", "title": "Hunter.io (verified emails by domain)",
     "vars": ["HUNTER_API_KEY"], "secrets": {"HUNTER_API_KEY"}},
    {"id": "snov", "title": "Snov.io (email finder fallback)",
     "vars": ["SNOV_USER_ID", "SNOV_API_SECRET"], "secrets": {"SNOV_API_SECRET"}},
    {"id": "cv", "title": "CV file (for tailoring and profile extraction)",
     "vars": ["CV_PATH"], "secrets": set()},
]


def ask_market() -> bool:
    return _ask_yes_no(
        "Are you job-hunting in France (or already based there)?",
        default=True,
    )


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
# Every option a machine too light for the local model could reasonably use
# instead, cheapest/easiest first -- each labeled with what it actually
# costs to get running, not just a single recommendation, so the choice is
# the user's to make with the real tradeoff in front of them.
CLOUD_LLM_OPTIONS = [
    {"label": "Groq", "note": "free, only needs a Google account",
     "url": "https://console.groq.com", "provider": "openai",
     "base_url": "https://api.groq.com/openai/v1", "key_var": "OPENAI_API_KEY"},
    {"label": "OpenAI", "note": "paid, pay-as-you-go billing required",
     "url": "https://platform.openai.com/api-keys", "provider": "openai",
     "base_url": None, "key_var": "OPENAI_API_KEY"},
    {"label": "Anthropic", "note": "paid, pay-as-you-go billing required",
     "url": "https://console.anthropic.com/settings/keys", "provider": "anthropic",
     "base_url": None, "key_var": "ANTHROPIC_API_KEY"},
]


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
    labels = [f"{o['label']} -- {o['note']}" for o in CLOUD_LLM_OPTIONS]
    labels.append("Keep Ollama anyway -- it may just run slower")
    choice = _ask_choice("Use a cloud LLM instead?", labels, default_index=0)

    if choice == len(CLOUD_LLM_OPTIONS):
        print(f"  {DIM}keeping Ollama -- it may just run slower.{RESET}")
        return {}

    option = CLOUD_LLM_OPTIONS[choice]
    _offer_browser(option["url"])
    api_key = _ask_text(f"  {option['key_var']} (from {option['url']})", secret=True)
    if not api_key:
        print(f"  {DIM}no key entered, keeping Ollama.{RESET}")
        return {}
    answers = {"LLM_PROVIDER": option["provider"], option["key_var"]: api_key}
    if option["base_url"]:
        answers["OPENAI_BASE_URL"] = option["base_url"]
    print(f"  {GREEN}set to use {option['label']}.{RESET}")
    return answers


def _describe_llm_choice(answers: dict[str, str]) -> str:
    if "LLM_PROVIDER" not in answers:
        return "Ollama (local, unchanged)"
    base_url = answers.get("OPENAI_BASE_URL", "")
    if "groq.com" in base_url:
        return "Groq (cloud)"
    return {"openai": "OpenAI (cloud)", "anthropic": "Anthropic (cloud)"}.get(
        answers["LLM_PROVIDER"], answers["LLM_PROVIDER"]
    )


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

    _banner("hobot -- guided setup")
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
    targeting_france = ask_market()
    france_only_ids = {f["id"] for f in FEATURES if f.get("france_only")}
    if not targeting_france:
        print(f"  {DIM}skipping France-only sources (La Bonne Alternance, France Travail, Pappers).{RESET}")

    for feature in FEATURES:
        if feature["id"] in france_only_ids and not targeting_france:
            continue
        answers.update(ask_feature(records, feature))

    write_env(answers)
    ask_autostart()

    _banner("Guided setup done")
    print(f"  LLM: {_describe_llm_choice(answers)}")
    configured = sorted(k for k in answers if k not in ("LLM_PROVIDER", "OPENAI_BASE_URL"))
    if configured:
        print(f"  Saved to .env: {', '.join(configured)}")
    else:
        print(f"  {DIM}nothing else saved -- everything else was skipped.{RESET}")
    print(f"  {DIM}Review .env any time -- nothing here is final.{RESET}")


if __name__ == "__main__":
    main()
