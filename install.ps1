# One-line install for Windows -- clones the repo, then hands off to
# setup.ps1 for the venv/dependencies/.env part. Meant to be run straight
# from GitHub, no local checkout needed first, from PowerShell:
#
#   irm https://raw.githubusercontent.com/Lejusdefruits/hobot/main/install.ps1 | iex
#
# Set $env:HOBOT_DIR to clone somewhere other than .\hobot.

$ErrorActionPreference = "Stop"

$UseColor = ($null -eq $env:NO_COLOR)

function Write-Failure {
    param([string]$Message)
    if ($UseColor) {
        Write-Host "error: " -ForegroundColor Red -NoNewline
    } else {
        Write-Host "error: " -NoNewline
    }
    Write-Host $Message
}

try {
    $repoUrl = "https://github.com/Lejusdefruits/hobot.git"
    $targetDir = if ($env:HOBOT_DIR) { $env:HOBOT_DIR } else { "hobot" }

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Failure "git not found -- install it first (git-scm.com, or 'winget install Git.Git')."
        exit 1
    }

    if (Test-Path $targetDir) {
        Write-Failure "$targetDir already exists -- remove it, or set `$env:HOBOT_DIR to clone somewhere else."
        exit 1
    }

    Write-Host "Cloning hobot into .\$targetDir" -ForegroundColor White
    git clone --depth 1 $repoUrl $targetDir
    if ($LASTEXITCODE -ne 0) {
        Write-Failure "git clone failed (see the error above) -- check the URL and your network connection."
        exit 1
    }
    Set-Location $targetDir

    Write-Host ""
    Write-Host "Repo cloned -- handing off to setup.ps1 for the venv, dependencies, and .env."
    # -ExecutionPolicy Bypass, not a bare "& .\setup.ps1": this script itself
    # only ran because irm | iex evaluates code in the current session,
    # sidestepping the execution-policy check entirely -- but that check
    # very much applies to running a real .ps1 file freshly written to disk,
    # which is exactly what setup.ps1 is at this point (the "running scripts
    # is disabled on this system" error). Bypass here is scoped to this one
    # child process, not a system-wide policy change.
    & powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
    $setupExitCode = $LASTEXITCODE
    if ($setupExitCode -ne 0) {
        # setup.ps1 already explained itself above (it has the same clean
        # Write-Failure pattern) -- this is a second, guaranteed-last line
        # so the final thing on screen is always calm and actionable, even
        # in the one-in-a-thousand case where whatever printed above wasn't
        # setup.ps1's own clean message (a genuinely unexpected native
        # PowerShell error, which -- unlike a Write-Host message -- can't be
        # guaranteed not to happen from here).
        Write-Host ""
        Write-Failure "setup.ps1 didn't finish (see the output above for why)."
        Write-Host "See README.md, or open an issue at https://github.com/Lejusdefruits/hobot/issues if this looks like a bug."
    }
    exit $setupExitCode
}
catch {
    Write-Host ""
    Write-Failure $_.Exception.Message
    Write-Host "See README.md, or open an issue at https://github.com/Lejusdefruits/hobot/issues if this looks like a bug."
    exit 1
}
