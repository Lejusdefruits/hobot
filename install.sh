#!/usr/bin/env bash
# One-line install for Linux and macOS -- clones the repo, then hands off to
# setup.sh for the venv/dependencies/.env part. Meant to be run straight from
# GitHub, no local checkout needed first:
#
#   curl -fsSL https://raw.githubusercontent.com/Lejusdefruits/hobot/main/install.sh | bash
#
# Set HOBOT_DIR to clone somewhere other than ./hobot.
set -euo pipefail

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$(tput bold) RED=$(tput setaf 1) RESET=$(tput sgr0)
else
    BOLD="" RED="" RESET=""
fi

REPO_URL="https://github.com/Lejusdefruits/hobot.git"
TARGET_DIR="${HOBOT_DIR:-hobot}"

if ! command -v git >/dev/null 2>&1; then
    printf '%serror:%s git not found -- install it first (your OS'"'"'s package manager, or git-scm.com).\n' "$RED" "$RESET" >&2
    exit 1
fi

if [ -e "$TARGET_DIR" ]; then
    printf '%serror:%s %s already exists -- remove it, or set HOBOT_DIR to clone somewhere else.\n' "$RED" "$RESET" "$TARGET_DIR" >&2
    exit 1
fi

printf '%sCloning hobot into ./%s%s\n' "$BOLD" "$TARGET_DIR" "$RESET"
git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
cd "$TARGET_DIR"

echo
echo "Repo cloned -- handing off to setup.sh for the venv, dependencies, and .env."
exec ./setup.sh
