$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPath = Join-Path $PSScriptRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$toolsPath = Join-Path $PSScriptRoot ".tools"
$localUvPath = Join-Path $toolsPath "uv\uv.exe"
$indexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple"
$pythonVersion = if ($env:RPA_PYTHON_VERSION) { $env:RPA_PYTHON_VERSION } else { "3.10" }

function Test-VenvPython {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        return $false
    }
    & $venvPython -c "import struct, sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 13) and struct.calcsize('P') * 8 == 64 else 1)" *> $null
    return $LASTEXITCODE -eq 0
}

function Resolve-UvExecutable {
    $installedUv = Get-Command uv -ErrorAction SilentlyContinue
    if ($installedUv) {
        return $installedUv.Source
    }
    if (Test-Path -LiteralPath $localUvPath) {
        return $localUvPath
    }

    # Install uv inside the project so the target computer needs neither Python nor global uv.
    Write-Host "uv was not found. Installing a project-local copy..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $toolsPath -Force | Out-Null
    $installerPath = Join-Path $toolsPath "install-uv.ps1"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/install.ps1" -OutFile $installerPath

    $previousInstallDir = $env:UV_INSTALL_DIR
    $previousNoModifyPath = $env:UV_NO_MODIFY_PATH
    try {
        $env:UV_INSTALL_DIR = Split-Path -Parent $localUvPath
        $env:UV_NO_MODIFY_PATH = "1"
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installerPath | Out-Host
    }
    finally {
        $env:UV_INSTALL_DIR = $previousInstallDir
        $env:UV_NO_MODIFY_PATH = $previousNoModifyPath
    }

    if (-not (Test-Path -LiteralPath $localUvPath)) {
        throw "uv installation failed. Verify access to https://astral.sh and try again."
    }
    return $localUvPath
}

$uvExe = Resolve-UvExecutable
Write-Host "Using uv: $uvExe" -ForegroundColor Cyan

if (-not (Test-VenvPython)) {
    if (Test-Path -LiteralPath $venvPath) {
        # Keep an invalid environment as a backup instead of deleting user files.
        $backupName = ".venv.invalid-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Rename-Item -LiteralPath $venvPath -NewName $backupName
        Write-Host "Invalid environment renamed to $backupName" -ForegroundColor Yellow
    }

    Write-Host "Installing managed 64-bit Python $pythonVersion..." -ForegroundColor Cyan
    & $uvExe python install $pythonVersion
    if ($LASTEXITCODE -ne 0) {
        throw "uv could not install 64-bit Python $pythonVersion. Review the network or proxy settings."
    }
    & $uvExe venv $venvPath --python $pythonVersion --managed-python
    if ($LASTEXITCODE -ne 0) {
        throw "uv installed Python but failed to create the virtual environment."
    }
}

if (-not (Test-VenvPython)) {
    throw "The virtual environment must use 64-bit Python 3.10, 3.11, or 3.12."
}

Write-Host "Installing project dependencies..." -ForegroundColor Cyan
Write-Host "Package index: $indexUrl" -ForegroundColor Cyan
& $uvExe pip install --python $venvPython --index-url $indexUrl --only-binary :all: -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install dependencies. Review the network, proxy, and package index settings."
}

& $venvPython -c "import cv2, pymongo, onnxruntime; from rapidocr_onnxruntime import RapidOCR; print('Environment check passed')"
if ($LASTEXITCODE -ne 0) {
    throw "Dependency verification failed. Review the error above."
}

$envExamplePath = Join-Path $PSScriptRoot ".env.example"
$envPath = Join-Path $PSScriptRoot ".env"
if ((Test-Path -LiteralPath $envExamplePath) -and -not (Test-Path -LiteralPath $envPath)) {
    # 只在首次安装时创建，绝不覆盖用户已经填写的密钥和连接配置。
    Copy-Item -LiteralPath $envExamplePath -Destination $envPath
    Write-Host "Created .env from .env.example. Edit it before starting the collector." -ForegroundColor Yellow
}

Write-Host "`nInstallation completed. Run start-rpa.bat to begin." -ForegroundColor Green
