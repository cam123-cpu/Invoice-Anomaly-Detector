# Start the app on Windows: right-click > "Run with PowerShell", or
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
# Then open http://127.0.0.1:8000. Close this window (or press Ctrl+C) to stop it.

# -NoBrowser: don't open a browser window (used by CI).
param([switch]$NoBrowser)

$ErrorActionPreference = "Continue"   # native tools (pip) print warnings; exit codes are checked instead
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".python-path")) {
    Write-Host "Run setup.ps1 first." -ForegroundColor Red
    Read-Host "Press Enter to close"; exit 1
}
$python = (Get-Content ".python-path" -TotalCount 1).Trim()

$port = 8000
while (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { $port++ }

Write-Host "Starting on http://127.0.0.1:$port  (close this window to stop)" -ForegroundColor Green
if (-not $NoBrowser) { Start-Job -ScriptBlock { param($p) Start-Sleep 3; Start-Process "http://127.0.0.1:$p" } -ArgumentList $port | Out-Null }
& $python -m uvicorn invoice_detector.api:create_app --factory --host 127.0.0.1 --port $port
if (-not $NoBrowser) { Read-Host "The app stopped. Press Enter to close" | Out-Null }
