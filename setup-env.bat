@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-env.ps1"
if errorlevel 1 (
  echo.
  echo Environment setup failed. Review the error above.
  pause
  exit /b 1
)
echo.
pause
