#!/usr/bin/env bash
# Base install for Linux and macOS -- creates the virtualenv, installs
# dependencies, and prepares .env. Everything after that (LLM, Discord,
# profile) is still a manual step; see README.md.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON=python3
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "python3 not found -- install Python 3.10+ first (python.org, or your system's package manager)." >&2
    exit 1
fi

version=$("$PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
major=${version%.*}
minor=${version#*.}
if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
    echo "Found Python $version -- 3.10 or newer is required." >&2
    exit 1
fi

if [ ! -d .venv ]; then
    echo "Creating .venv ($version)..."
    "$PYTHON" -m venv .venv
fi

echo "Installing dependencies..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example -- fill in the REQUIRED section before running anything."
else
    echo ".env already exists, left untouched."
fi

cat <<'EOF'

Base install done. Next:
  1. Open .env and fill in at least the REQUIRED section (an LLM: Ollama by
     default, or a cloud key -- see README.md).
  2. .venv/bin/python daemon.py        # discovery + Discord (if configured)
  3. .venv/bin/python cli.py           # terminal UI, in a separate terminal
EOF
