$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Project environment is missing. Installing it now..." -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot "setup-env.ps1")
}

# Load the local Docker MongoDB connection without exporting secrets globally.
$mongoEnvFile = Join-Path $PSScriptRoot ".env.mongo"
if (-not $env:MONGO_URI -and (Test-Path -LiteralPath $mongoEnvFile)) {
    $mongoValues = @{}
    foreach ($line in Get-Content -LiteralPath $mongoEnvFile -Encoding UTF8) {
        $value = $line.Trim()
        if (-not $value -or $value.StartsWith("#") -or -not $value.Contains("=")) {
            continue
        }
        $name, $content = $value.Split("=", 2)
        $mongoValues[$name.Trim()] = $content.Trim()
    }
    $mongoHost = if ($mongoValues["MONGO_BIND_IP"] -eq "0.0.0.0") { "127.0.0.1" } else { $mongoValues["MONGO_BIND_IP"] }
    $mongoUser = [Uri]::EscapeDataString($mongoValues["MONGO_APP_USERNAME"])
    $mongoPassword = [Uri]::EscapeDataString($mongoValues["MONGO_APP_PASSWORD"])
    $mongoDatabase = $mongoValues["MONGO_DATABASE"]
    $env:MONGO_URI = "mongodb://" + $mongoUser + ":" + $mongoPassword + "@" + $mongoHost + ":" + $mongoValues["MONGO_PORT"] + "/" + $mongoDatabase + "?authSource=" + $mongoDatabase
    $env:MONGO_DATABASE = $mongoDatabase
    $env:MONGO_ACCOUNT_COLLECTION = "collection_target"
    $env:MONGO_ARTICLE_COLLECTION = "article"
    Write-Host "Using Docker MongoDB from .env.mongo." -ForegroundColor Green
}

# The open-source build never ships a default administrator password.
if (-not $env:CONTROL_PANEL_PASSWORD) {
    $securePassword = Read-Host "Set the control panel administrator password" -AsSecureString
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    try {
        $env:CONTROL_PANEL_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
}

$controlHost = if ($env:RPA_CONTROL_HOST) { $env:RPA_CONTROL_HOST } else { "127.0.0.1" }
$controlPort = if ($env:RPA_CONTROL_PORT) { $env:RPA_CONTROL_PORT } else { "8010" }

Write-Host "Starting WeChat RPA control panel..." -ForegroundColor Cyan
Write-Host "The page will open at http://$($controlHost):$($controlPort)/" -ForegroundColor Cyan
& $venvPython (Join-Path $PSScriptRoot "rpa_control_panel.py") --host $controlHost --port $controlPort
