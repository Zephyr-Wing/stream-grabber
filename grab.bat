@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE=python"
where py >nul 2>nul
if %errorlevel%==0 set "PYEXE=py -3"

%PYEXE% grab.py %*
echo.
pause
