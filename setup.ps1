# One-time setup on Windows. Safe to run again.
#
#   Right-click this file > "Run with PowerShell", or in PowerShell in this folder:
#     powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# Every tool runs as "python -m ...", never pip.exe or uvicorn.exe directly, because
# school and company laptops often block those programs ("An Application Control policy
# has blocked this file").

# -Yes answers every question with its default and creates no login (used by CI).
param([switch]$Yes, [string]$Company = "Acme Inc")

$ErrorActionPreference = "Continue"   # native tools (pip) print warnings; exit codes are checked instead
Set-Location -Path $PSScriptRoot

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Pause-End { if (-not $Yes) { Read-Host "Press Enter to close" | Out-Null } }
function Fail($text) { Write-Host "`n$text" -ForegroundColor Red; Pause-End; exit 1 }
function Ask($question, $default) { if ($Yes) { return $default }; $a = Read-Host $question; if ($a) { $a } else { $default } }

Step "Checking Python"
$py = $null
foreach ($candidate in @("py", "python")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $version = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]"3.11") { $py = $candidate; break }
    }
}
if (-not $py) {
    Fail "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
}
Write-Host "Using $py ($version)"

Step "Creating the app's own Python environment (.venv)"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { & $py -m venv .venv }
$python = $venvPython
& $python -c "print('ok')" *> $null
if ($LASTEXITCODE -ne 0) {
    # Some security policies block programs inside a new folder. Fall back to the main Python.
    Write-Host "The environment's Python is blocked here; installing for your user instead." -ForegroundColor Yellow
    $python = (& $py -c "import sys; print(sys.executable)")
    $userInstall = "--user"
} else { $userInstall = $null }

Step "Installing the app (a minute or two)"
& $python -m pip install --upgrade pip --quiet $userInstall
& $python -m pip install -e ".[dev]" $userInstall
if ($LASTEXITCODE -ne 0) { Fail "Installing failed. Send the red text above." }
Set-Content -Path ".python-path" -Value $python   # start.ps1 uses the same Python

Step "Settings and secrets (.env)"
& $python -m invoice_detector.cli init

Step "Your company"
$company = Ask "Company name (Enter for '$Company')" $Company
& $python -m invoice_detector.cli create-org $company

if ((Ask "Create a login now? (Y/n)" $(if ($Yes) { "n" } else { "y" })) -notmatch "^[nN]") {
    $name = Ask "Username (Enter for 'alice')" "alice"
    Write-Host "Choose a password of 12+ characters. Nothing shows while you type."
    & $python -m invoice_detector.cli create-user $company $name admin
}

if ((Ask "Load sample invoices with planted problems to try it out? (Y/n)" "y") -notmatch "^[nN]") {
    & $python -m invoice_detector.cli seed $company
}

Write-Host "`nDone. Start the app with start.ps1 (right-click > Run with PowerShell)." -ForegroundColor Green
Pause-End
