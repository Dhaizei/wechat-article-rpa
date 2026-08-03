@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-rpa.ps1"
if errorlevel 1 (
  echo.
  echo The collector failed. Check the error above and output\run.log.
  pause
  exit /b 1
)
echo.
echo Collection completed.
pause
