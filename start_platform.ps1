<#
    start_platform.ps1 - start the SWAXS Platform Hub on Windows.

    The Windows counterpart of start_platform.sh. Run it from PowerShell:

        .\start_platform.ps1
        .\start_platform.ps1 "D:\data\Auto_Run"     # pre-select a project folder

    It does what the bash script does - load .env, find the right Python, print
    the app map - plus the checks a Windows machine actually needs:
      * refuses to run on a Python too old for the pinned science stack
      * warns if the dependencies are not installed yet, and says how
      * finds the venv whether it was made by `python -m venv` or by conda

    If PowerShell blocks this file with "running scripts is disabled on this
    system", allow scripts for your own account (once):

        Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

    or run it without changing any policy:

        powershell -ExecutionPolicy Bypass -File .\start_platform.ps1
#>

param(
    [Parameter(Position = 0)]
    [string]$ProjectFolder
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($msg) { Write-Host "  $msg" }
function Write-Warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Write-Bad ($msg) { Write-Host "  x $msg" -ForegroundColor Red }

# -- .env (local, non-committed config: tokens, SMTP, Slack) ------------------
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $k, $v = $line.Split("=", 2)
            $k = $k.Trim()
            $v = $v.Trim().Trim('"').Trim("'")
            # never clobber a variable the user already set in this shell
            if ($k -and -not [Environment]::GetEnvironmentVariable($k)) {
                [Environment]::SetEnvironmentVariable($k, $v)
            }
        }
    }
    Write-Step "Loaded .env"
}

# -- AI token (same rule as the bash script) ----------------------------------
$claudeSettings = Join-Path $env:USERPROFILE ".claude\settings.json"
if ($env:ANTHROPIC_AUTH_TOKEN) {
    Write-Step "AI auth: token from environment"
} elseif ((Test-Path $claudeSettings) -and
          (Select-String -Path $claudeSettings -Pattern "ANTHROPIC_AUTH_TOKEN" -Quiet) -and
          -not (Select-String -Path $claudeSettings -Pattern "yourSlacApiKeyHere" -Quiet)) {
    Write-Step "AI auth: $claudeSettings"
} else {
    Write-Step "AI auth: no token found - the AI Assistant will be disabled."
    Write-Step "         (everything else works; see SECURITY.md to add one)"
}

# -- find Python: an activated venv/conda env first, then a local venv --------
$python = $null
if ($env:VIRTUAL_ENV) {
    $cand = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (Test-Path $cand) { $python = $cand; Write-Step "Python: active venv" }
}
if (-not $python -and $env:CONDA_PREFIX) {
    $cand = Join-Path $env:CONDA_PREFIX "python.exe"
    if (Test-Path $cand) { $python = $cand; Write-Step "Python: active conda env ($env:CONDA_DEFAULT_ENV)" }
}
if (-not $python -and (Test-Path "venv\Scripts\python.exe")) {
    $python = (Resolve-Path "venv\Scripts\python.exe").Path
    Write-Step "Python: .\venv (not activated - using it directly)"
}
if (-not $python) {
    foreach ($name in @("python", "python3", "py")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $python = $cmd.Source; Write-Step "Python: $name on PATH"; break }
    }
}
if (-not $python) {
    Write-Bad "No Python found."
    Write-Host ""
    Write-Host "  Install Python 3.11 or newer, then run QUICKSTART.md step 2:"
    Write-Host "    https://www.python.org/downloads/windows/"
    Write-Host "  (tick 'Add python.exe to PATH' in the installer)"
    exit 1
}

# -- version gate -------------------------------------------------------------
$ver = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
$major, $minor = $ver.Split(".")
if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 10)) {
    Write-Bad "Python $ver is too old - this platform needs 3.10 or newer."
    Write-Host "  (3.9 and older also have no prebuilt wheels for the science stack"
    Write-Host "   on Windows, so pip would try to compile numpy/scipy and fail.)"
    exit 1
}
Write-Step "Python $ver at $python"

# -- are the dependencies actually installed? ---------------------------------
$probe = & $python -c @"
import importlib.util as u
missing = [m for m in ('flask','numpy','scipy','matplotlib','pandas','yaml','fabio','pyFAI')
           if u.find_spec(m) is None]
print(','.join(missing))
"@ 2>$null
if ($probe) {
    Write-Bad "Missing packages: $probe"
    Write-Host ""
    Write-Host "  Install them (1-3 minutes):"
    Write-Host "    $python -m pip install -r requirements-core.txt" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  See QUICKSTART.md if that fails."
    exit 1
}

# -- project folder -----------------------------------------------------------
if ($ProjectFolder) {
    if (-not (Test-Path $ProjectFolder)) {
        Write-Bad "Project folder not found: $ProjectFolder"
        exit 1
    }
    $env:SWAXS_PROJECT = (Resolve-Path $ProjectFolder).Path
    Write-Step "Project: $env:SWAXS_PROJECT"
} else {
    Write-Step "Project: pick it in the hub UI (top-right)"
}

$hubPort = if ($env:SWAXS_HUB_PORT) { $env:SWAXS_HUB_PORT } else { "5000" }

Write-Host ""
Write-Host "  +======================================================+"
Write-Host "  |   SWAXS Platform Hub                                 |"
Write-Host "  |   -> http://localhost:$hubPort                            |"
Write-Host "  |                                                      |"
Write-Host "  |   Apps (started from the hub UI):                     |"
Write-Host "  |     5009 Calibration & Raw Prep                       |"
Write-Host "  |     5001 Reduction & Correction                       |"
Write-Host "  |     5002 Data Viewer                                  |"
Write-Host "  |     5003 Background Subtraction                       |"
Write-Host "  |     5006 Quality Gate                                 |"
Write-Host "  |     5004 Data Analysis                                |"
Write-Host "  |     5008 Nanoparticle Analyzer                        |"
Write-Host "  |     5007 Flow Synthesis (reactor)                     |"
Write-Host "  |     5005 AI Assistant                                 |"
Write-Host "  |                                                      |"
Write-Host "  |   Press  Ctrl-C  to stop the hub AND its apps          |"
Write-Host "  +======================================================+"
Write-Host ""

# UTF-8 so the log emoji and units (mu, Angstrom) don't crash the console
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

& $python "hub\app.py"
