@echo off
chcp 65001 >nul
cd /d "%~dp0"

python -m app %*
if errorlevel 1 (
  echo.
  echo [X] Service failed to start. Check the message above.
  pause
)
