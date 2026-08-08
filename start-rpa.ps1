$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Project environment is missing. Installing it now..." -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot "setup-env.ps1")
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outputDir = Join-Path $PSScriptRoot "output\mongo-$stamp"

Write-Host "Confirm that WeChat is logged in and the Search window is visible." -ForegroundColor Yellow
Write-Host "Do not use the mouse or keyboard while the RPA is running." -ForegroundColor Yellow
Write-Host "Output directory: $outputDir" -ForegroundColor Cyan
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".env")) {
    Write-Host "Project configuration: .env (loaded by Python)" -ForegroundColor Cyan
} else {
    Write-Host "Project configuration: .env not found; built-in defaults/system variables will be used." -ForegroundColor Yellow
}

$arguments = @(
    "-u", (Join-Path $PSScriptRoot "wechat_visual_rpa.py"),
    "--run-search-accounts",
    "--live",
    "--accounts-from-mongo",
    "--write-mongo",
    "--metrics", "share",
    "--window-layout", "auto",
    "--max-articles", "20",
    "--output-dir", $outputDir,
    "--export-jsonl", (Join-Path $outputDir "articles.jsonl"),
    "--export-csv", (Join-Path $outputDir "articles.csv")
)

& $venvPython @arguments
if ($LASTEXITCODE -ne 0) {
    throw "The collector exited with code $LASTEXITCODE. Check run.log in the output directory."
}
