@echo off
chcp 65001 >nul
title stream-grabber
cd /d "%~dp0"

set "PYEXE=python"
where py >nul 2>nul
if %errorlevel%==0 set "PYEXE=py -3"

%PYEXE% server.py %*
if not errorlevel 1 goto end
echo.
echo 服务没起来，看上面几行的报错。
pause
:end
