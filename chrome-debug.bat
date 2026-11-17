@echo off
setlocal
chcp 936 >nul
title Chrome（下载用）

set "PORT=9222"
set "PROF=%~dp0.chrome-profile"

set "CH=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if exist "%CH%" goto HAVE
set "CH=%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"
if exist "%CH%" goto HAVE
set "CH=%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"
if exist "%CH%" goto HAVE
echo.
echo [X] 没找到 chrome.exe
echo.
pause
exit /b 1

:HAVE
if not exist "%PROF%" mkdir "%PROF%"

netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul
if not errorlevel 1 goto ALREADY

echo 启动专用浏览器
echo   配置目录  %PROF%
echo   调试端口  %PORT%
echo.
start "" "%CH%" --user-data-dir="%PROF%" --remote-debugging-port=%PORT% --no-first-run --no-default-browser-check --disable-session-crashed-bubble
echo 第一次用：在弹出的窗口里登录你想下的网站，登录状态会一直留在这份配置里。
echo 窗口可以关，下次下载 grab.bat 会自己再启动它。
echo.
ping -n 4 127.0.0.1 >nul
exit /b 0

:ALREADY
echo 专用浏览器已经在跑（端口 %PORT% 开着）。
echo 直接双击 grab.bat 下载就行。
ping -n 3 127.0.0.1 >nul
exit /b 0
