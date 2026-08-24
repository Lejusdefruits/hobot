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

# Runs one external command directly (the "&" call operator), letting its
# own output flow straight to the console -- pip/venv's normal install
# progress, visible as it happens.
#
# This used to run through Start-Process with redirected output and a
# Write-Progress spinner, for a quieter, prettier terminal: nothing printed
# unless a step actually failed, then a real progress bar in the meantime.
# Dropped after it proved unreliable on a real Windows machine: creating the
# venv with a real (non-Microsoft-Store) Python, invoked by its own fully
# resolved absolute path, still silently failed through that combination --
# empty exit code, nothing captured -- while the exact same command,
# run directly, worked every single time. Whatever the precise reason (a few
# real, documented Start-Process/redirection gotchas were fixed along the
# way and still didn't account for it), reliability wins over a progress
# bar: this is plain, boring, and every install step here has already been
# proven to behave correctly when invoked exactly this way.
function Invoke-Step {
    param([string]$Message, [string]$FilePath, [string[]]$ArgumentList)
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -eq 0) {
        Write-Done $Message
    } else {
        if ($UseColor) { Write-Host "  failed " -ForegroundColor Red -NoNewline } else { Write-Host "  failed " -NoNewline }
        Write-Host "$Message (exit code $LASTEXITCODE, see the output above)"
        exit 1
    }
}

try {
    Write-Step 1 5 "Checking for Python 3.10+"
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Write-Failure "python not found -- install Python 3.10+ from python.org first (check 'Add python.exe to PATH' during install)."
        exit 1
    }
    if ($python.Source -like "*WindowsApps*") {
        # The Microsoft Store's Python is an app execution alias, not a
        # real .exe -- it runs code fine (the version check right below
        # this still passes), but it's sandboxed in ways that make venv
        # creation fail, often with no useful error message at all (see
        # Invoke-Step's own fallback for that symptom). Catching it here,
        # before wasting time on a clone/pip install that would just hit
        # the same wall two steps later with a much more confusing failure.
        Write-Failure "python resolves to the Microsoft Store version ($($python.Source)) -- that one is sandboxed and often can't create a working virtual environment."
        Write-Host "Install Python from https://python.org instead (check 'Add python.exe to PATH' during install), open a new PowerShell window, and run this again."
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
        # $python.Source (the absolute path step 1 already resolved and
        # validated above), not the bare "python" name: Start-Process's own
        # -FilePath resolution for a bare command name doesn't necessarily
        # match PowerShell's own (the "&" call operator used for the version
        # check two lines above) -- confirmed on a real machine with both a
        # real python.org install and the Microsoft Store's alias stub on
        # PATH: typing "python" directly, and "& python", both correctly
        # picked the real install, but Start-Process -FilePath "python" here
        # did not, silently producing an empty exit code and no output
        # instead of a working venv. The explicit path sidesteps the
        # ambiguity entirely.
        Invoke-Step "creating .venv" $python.Source @("-m", "venv", ".venv")
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
