@echo off
chcp 65001 >nul
title 安装 stream-grabber 插件
cd /d "%~dp0"

echo.
echo   装插件就三步，没有安装包，浏览器直接读这个文件夹。
echo.
echo   1. 打开扩展管理页
echo        Chrome 地址栏输入  chrome://extensions
echo        Edge   地址栏输入  edge://extensions
echo   2. 右上角打开「开发者模式」
echo   3. 点「加载已解压的扩展程序」，选中这个文件夹：
echo        %~dp0extension
echo.
echo   装好之后，工具栏会出现一个蓝色下载图标。
echo   下载前记得先双击 server.bat，那个窗口最小化留着。
echo.
start "" "%~dp0extension"
pause
