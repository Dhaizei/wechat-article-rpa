$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Project environment is missing. Installing it now..." -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot "setup-env.ps1")
}

# 开源版本不提供默认管理员密码；首次启动时仅在当前进程内安全地读取。
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
Write-Host "The page will open at http://${controlHost}:${controlPort}/" -ForegroundColor Cyan
& $venvPython (Join-Path $PSScriptRoot "rpa_control_panel.py") --host $controlHost --port $controlPort
