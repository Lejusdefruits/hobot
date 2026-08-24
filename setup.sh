#!/usr/bin/env bash
# Base install for Linux and macOS -- creates the virtualenv, installs
# dependencies, and prepares .env. Everything after that (LLM, Discord,
# profile) is still a manual step; see README.md.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Color only where it'll actually help -- a real terminal, and not turned
# off. NO_COLOR is the same convention the terminal UI itself already
# honors (README's Accessibility section), so this script respects it too.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$(tput bold) GREEN=$(tput setaf 2) RED=$(tput setaf 1) DIM=$(tput dim) RESET=$(tput sgr0)
else
    BOLD="" GREEN="" RED="" DIM="" RESET=""
fi
INTERACTIVE=0
[ -t 1 ] && INTERACTIVE=1

TOTAL_STEPS=5
STEP=0

step() {
    STEP=$((STEP + 1))
    printf '\n%s[%s/%s] %s%s\n' "$BOLD" "$STEP" "$TOTAL_STEPS" "$1" "$RESET"
}

fail() {
    printf '%serror:%s %s\n' "$RED" "$RESET" "$1" >&2
    exit 1
}

# Runs one command in the background with a spinner in front of it, so a
# slow step (pip resolving/downloading a couple dozen packages) still shows
# something is happening instead of sitting on a blank line. Output is kept
# out of the way while it runs and only printed back out if the command
# actually fails -- the goal is "visibly working," not a wall of pip's own
# per-package log on a normal, successful run.
run() {
    local msg=$1; shift
    local log; log=$(mktemp)
    "$@" >"$log" 2>&1 &
    local pid=$!
    if [ "$INTERACTIVE" = 1 ]; then
        local frames='-\|/' i=0
        while kill -0 "$pid" 2>/dev/null; do
            i=$(( (i + 1) % 4 ))
            printf '\r  %s %s' "${frames:$i:1}" "$msg"
            sleep 0.1
        done
    else
        printf '  %s ...\n' "$msg"
    fi
    local prefix=""
    [ "$INTERACTIVE" = 1 ] && prefix='\r'
    if wait "$pid"; then
        printf "${prefix}  %s%s%s %s\n" "$GREEN" "done" "$RESET" "$msg"
    else
        printf "${prefix}  %s%s%s %s\n" "$RED" "failed" "$RESET" "$msg"
        cat "$log" >&2
        rm -f "$log"
        exit 1
    fi
    rm -f "$log"
}

step "Checking for Python 3.10+"
PYTHON=python3
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    fail "python3 not found -- install Python 3.10+ first (python.org, or your system's package manager)."
fi
version=$("$PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
major=${version%.*}
minor=${version#*.}
if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
    fail "found Python $version -- 3.10 or newer is required."
fi
printf '  %s%s%s Python %s\n' "$GREEN" "found" "$RESET" "$version"

step "Setting up a virtual environment"
echo "${DIM}Keeps hobot's dependencies out of your system Python -- lives in ./.venv, only used from inside this project.${RESET}"
if [ -d .venv ]; then
    echo "  ${DIM}.venv already exists, left as is.${RESET}"
else
    run "creating .venv" "$PYTHON" -m venv .venv
fi

step "Installing dependencies"
echo "${DIM}Everything in requirements.txt -- discord.py, textual, langgraph, and the rest.${RESET}"
run "upgrading pip" .venv/bin/pip install --upgrade pip
run "installing from requirements.txt" .venv/bin/pip install -r requirements.txt

step "Preparing .env"
if [ -f .env ]; then
    echo "  ${DIM}.env already exists, left untouched.${RESET}"
else
    cp .env.example .env
    printf '  %s%s%s copied .env.example to .env\n' "$GREEN" "done" "$RESET"
fi

step "Guided setup"
if [ -t 0 ]; then
    .venv/bin/python scripts/install_wizard.py
else
    echo "  ${DIM}non-interactive install, skipping -- edit .env by hand (see README.md).${RESET}"
fi

cat <<EOF

${BOLD}Base install done.${RESET} Next:
  1. Open .env and fill in the REQUIRED section at the top -- an LLM,
     Ollama by default (nothing to pay for) or a cloud key. See README.md
     for the rest (Discord, French job sources, mail monitoring...), all
     optional and off until you fill in their own section.
  2. .venv/bin/python daemon.py    # discovery + Discord, if configured
  3. .venv/bin/python cli.py       # terminal UI, in a second terminal
EOF
