@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
  echo Usage:
  echo   mongodb.bat setup
  echo   mongodb.bat start
  echo   mongodb.bat status
  echo   mongodb.bat logs
  echo   mongodb.bat backup
  echo   mongodb.bat stop
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0mongodb.ps1" %*
if errorlevel 1 (
  echo.
  echo MongoDB operation failed. Review the error above.
  exit /b 1
)
