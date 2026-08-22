# Base install for Windows -- creates the virtualenv, installs
# dependencies, and prepares .env. Everything after that (LLM, Discord,
# profile) is still a manual step; see README.md.
#
# Run from PowerShell: .\setup.ps1
# If PowerShell refuses to run it ("running scripts is disabled"), that's
# Windows' execution policy, not a bug here -- either run
# `powershell -ExecutionPolicy Bypass -File setup.ps1` once, or set it
# yourself: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "python not found -- install Python 3.10+ from python.org first (check 'Add python.exe to PATH' during install)."
    exit 1
}

$versionOutput = & python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
$major, $minor = $versionOutput.Split(".")
if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 10)) {
    Write-Error "Found Python $versionOutput -- 3.10 or newer is required."
    exit 1
}

if (-not (Test-Path .venv)) {
    Write-Host "Creating .venv ($versionOutput)..."
    python -m venv .venv
}

Write-Host "Installing dependencies..."
& .venv\Scripts\pip.exe install --quiet --upgrade pip
& .venv\Scripts\pip.exe install --quiet -r requirements.txt

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example -- fill in the REQUIRED section before running anything."
} else {
    Write-Host ".env already exists, left untouched."
}

Write-Host ""
Write-Host "Base install done. Next:"
Write-Host "  1. Open .env and fill in at least the REQUIRED section (an LLM: Ollama by"
Write-Host "     default, or a cloud key -- see README.md)."
Write-Host "  2. .venv\Scripts\python.exe daemon.py    # discovery + Discord (if configured)"
Write-Host "  3. .venv\Scripts\python.exe cli.py       # terminal UI, in a separate window"
