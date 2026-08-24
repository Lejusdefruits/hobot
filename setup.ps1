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

# NO_COLOR is the same convention the terminal UI itself already honors
# (README's Accessibility section) -- respected here too. Presence, not
# truthiness: an empty NO_COLOR still counts, per the convention's own spec.
$UseColor = ($null -eq $env:NO_COLOR)

function Write-Step {
    param([int]$Number, [int]$Total, [string]$Message)
    Write-Host ""
    if ($UseColor) {
        Write-Host "[$Number/$Total] $Message" -ForegroundColor White
    } else {
        Write-Host "[$Number/$Total] $Message"
    }
}

function Write-Done {
    param([string]$Message)
    if ($UseColor) {
        Write-Host "  done " -ForegroundColor Green -NoNewline
    } else {
        Write-Host "  done " -NoNewline
    }
    Write-Host $Message
}

# Write-Error, not this, under $ErrorActionPreference = "Stop" throws a full
# terminating exception (a multi-line stack trace: "At line:X char:Y",
# "+ CategoryInfo", "+ FullyQualifiedErrorId") instead of the clean one-line
# message it looks like it'd print -- every intentional failure in this
# script goes through this plus an explicit exit instead, so a missing
# dependency reads as a clean stop, not a crash.
function Write-Failure {
    param([string]$Message)
    if ($UseColor) {
        Write-Host "error: " -ForegroundColor Red -NoNewline
    } else {
        Write-Host "error: " -NoNewline
    }
    Write-Host $Message
}

# Runs one external command with a real progress bar in front of it (no
# percentage to report -- pip doesn't expose one -- so this just keeps it
# visibly moving instead of a blank line during a slow resolve/download).
# Output is captured and only shown back if the command actually fails.
function Invoke-Step {
    param([string]$Message, [string]$FilePath, [string[]]$ArgumentList)
    $outLog = [System.IO.Path]::GetTempFileName()
    $errLog = [System.IO.Path]::GetTempFileName()
    $proc = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -NoNewWindow -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $pct = 0
    while (-not $proc.HasExited) {
        $pct = ($pct + 4) % 100
        Write-Progress -Activity $Message -Status "working" -PercentComplete $pct
        Start-Sleep -Milliseconds 150
    }
    Write-Progress -Activity $Message -Completed
    if ($proc.ExitCode -eq 0) {
        Write-Done $Message
    } else {
        if ($UseColor) { Write-Host "  failed " -ForegroundColor Red -NoNewline } else { Write-Host "  failed " -NoNewline }
        Write-Host $Message
        Get-Content $outLog, $errLog -ErrorAction SilentlyContinue | Write-Host
        Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue
        exit 1
    }
    Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue
}

try {
    Write-Step 1 5 "Checking for Python 3.10+"
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Write-Failure "python not found -- install Python 3.10+ from python.org first (check 'Add python.exe to PATH' during install)."
        exit 1
    }
    $versionOutput = & python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
    $major, $minor = $versionOutput.Split(".")
    if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 10)) {
        Write-Failure "found Python $versionOutput -- 3.10 or newer is required."
        exit 1
    }
    Write-Done "Python $versionOutput"

    Write-Step 2 5 "Setting up a virtual environment"
    Write-Host "Keeps hobot's dependencies out of your system Python -- lives in .\.venv, only used from inside this project." -ForegroundColor DarkGray
    if (Test-Path .venv) {
        Write-Host "  .venv already exists, left as is." -ForegroundColor DarkGray
    } else {
        Invoke-Step "creating .venv" python @("-m", "venv", ".venv")
    }

    Write-Step 3 5 "Installing dependencies"
    Write-Host "Everything in requirements.txt -- discord.py, textual, langgraph, and the rest." -ForegroundColor DarkGray
    # python -m pip, not pip.exe directly -- upgrading pip by running pip.exe
    # means replacing that same .exe while Windows still has it open, which can
    # fail outright there; going through python.exe sidesteps it.
    Invoke-Step "upgrading pip" .venv\Scripts\python.exe @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-Step "installing from requirements.txt" .venv\Scripts\pip.exe @("install", "-r", "requirements.txt")

    Write-Step 4 5 "Preparing .env"
    if (Test-Path .env) {
        Write-Host "  .env already exists, left untouched." -ForegroundColor DarkGray
    } else {
        Copy-Item .env.example .env
        Write-Done "copied .env.example to .env"
    }

    Write-Step 5 5 "Guided setup"
    $Guided = $false
    if (-not [Console]::IsInputRedirected) {
        & .venv\Scripts\python.exe scripts\install_wizard.py
        $Guided = $true
    } else {
        Write-Host "  non-interactive install, skipping -- edit .env by hand (see README.md)." -ForegroundColor DarkGray
    }

    $Rule = "-" * 60
    Write-Host ""
    Write-Host $Rule -ForegroundColor White
    Write-Host "Base install done." -ForegroundColor White
    Write-Host $Rule -ForegroundColor White
    Write-Host "Next:"
    if ($Guided) {
        Write-Host "  1. Review .env if you want to double check or change anything -- the"
        Write-Host "     guided setup above already covers the LLM and most optional features."
    } else {
        Write-Host "  1. Open .env and fill in the REQUIRED section at the top -- an LLM,"
        Write-Host "     Ollama by default (nothing to pay for) or a cloud key. See README.md"
        Write-Host "     for the rest (Discord, French job sources, mail monitoring...), all"
        Write-Host "     optional and off until you fill in their own section."
    }
    Write-Host "  2. .venv\Scripts\python.exe daemon.py    # discovery + Discord, if configured"
    Write-Host "  3. .venv\Scripts\python.exe cli.py       # terminal UI, in a second window"
}
catch {
    Write-Host ""
    Write-Failure $_.Exception.Message
    Write-Host "See README.md, or open an issue at https://github.com/Lejusdefruits/hobot/issues if this looks like a bug."
    exit 1
}
