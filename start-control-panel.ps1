$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Project environment is missing. Installing it now..." -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot "setup-env.ps1")
}

Write-Host "Starting WeChat RPA control panel..." -ForegroundColor Cyan
Write-Host "The page will open at http://127.0.0.1:8010/" -ForegroundColor Cyan
& $venvPython (Join-Path $PSScriptRoot "rpa_control_panel.py")
