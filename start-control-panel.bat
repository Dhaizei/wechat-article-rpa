@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-control-panel.ps1"
if errorlevel 1 (
  echo.
  echo Control panel failed. Review the error above.
  pause
)
