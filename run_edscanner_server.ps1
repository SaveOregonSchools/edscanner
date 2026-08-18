$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$appPath = Join-Path $projectRoot "app.py"

$Host.UI.RawUI.WindowTitle = "EdScanner Server - close this window to stop"
Set-Location -LiteralPath $projectRoot

Write-Host "EdScanner is starting at http://127.0.0.1:8765" -ForegroundColor Cyan
Write-Host "Press Ctrl+C or close this window to stop EdScanner." -ForegroundColor Yellow
Write-Host ""

& $pythonPath $appPath
