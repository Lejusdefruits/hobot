# One-line install for Windows -- clones the repo, then hands off to
# setup.ps1 for the venv/dependencies/.env part. Meant to be run straight
# from GitHub, no local checkout needed first, from PowerShell:
#
#   irm https://raw.githubusercontent.com/Lejusdefruits/hobot/main/install.ps1 | iex
#
# Set $env:HOBOT_DIR to clone somewhere other than .\hobot.

$ErrorActionPreference = "Stop"

$repoUrl = "https://github.com/Lejusdefruits/hobot.git"
$targetDir = if ($env:HOBOT_DIR) { $env:HOBOT_DIR } else { "hobot" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "git not found -- install it first (git-scm.com, or 'winget install Git.Git')."
    exit 1
}

if (Test-Path $targetDir) {
    Write-Error "$targetDir already exists -- remove it, or set `$env:HOBOT_DIR to clone somewhere else."
    exit 1
}

Write-Host "Cloning into $targetDir..."
git clone --depth 1 $repoUrl $targetDir
Set-Location $targetDir

& .\setup.ps1
