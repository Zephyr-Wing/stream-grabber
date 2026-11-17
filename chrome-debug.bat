@echo off
setlocal
chcp 936 >nul
title 启动带调试端口的 Chrome

set "CH=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if exist "%CH%" goto FOUND
set "CH=%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"
if exist "%CH%" goto FOUND
set "CH=%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"
if exist "%CH%" goto FOUND
echo.
echo [X] 没找到 chrome.exe
echo.
pause
exit /b 1

:FOUND
tasklist | find /i "chrome.exe" >nul
if errorlevel 1 goto LAUNCH

echo.
echo [!] Chrome 正在运行。
echo     先完全退出它（右下角托盘图标右键退出），否则新起的这个不会带调试端口。
echo.
pause
exit /b 1

:LAUNCH
echo 用你的日常配置启动 Chrome，调试端口 9222，登录态都在。
start "" "%CH%" --remote-debugging-port=9222
echo.
echo 之后下载时加上 --cdp-port 9222：
echo   python grab.py 链接 --cdp-port 9222
echo.
echo 这个窗口可以关，Chrome 别关。
echo.
pause
