#!/usr/bin/env bash
# One-line install for Linux and macOS -- clones the repo, then hands off to
# setup.sh for the venv/dependencies/.env part. Meant to be run straight from
# GitHub, no local checkout needed first:
#
#   curl -fsSL https://raw.githubusercontent.com/Lejusdefruits/hobot/main/install.sh | bash
#
# Set HOBOT_DIR to clone somewhere other than ./hobot.
set -euo pipefail

REPO_URL="https://github.com/Lejusdefruits/hobot.git"
TARGET_DIR="${HOBOT_DIR:-hobot}"

if ! command -v git >/dev/null 2>&1; then
    echo "git not found -- install it first (your OS's package manager, or git-scm.com)." >&2
    exit 1
fi

if [ -e "$TARGET_DIR" ]; then
    echo "$TARGET_DIR already exists -- remove it, or set HOBOT_DIR to clone somewhere else." >&2
    exit 1
fi

echo "Cloning into $TARGET_DIR..."
git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
cd "$TARGET_DIR"

exec ./setup.sh
