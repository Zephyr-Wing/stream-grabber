@echo off
setlocal
chcp 936 >nul
title 录一段 README 演示动图

set "OUT=%~dp0"
set "FF="

if exist "D:\Tools\ffmpeg\ffmpeg.exe" set "FF=D:\Tools\ffmpeg\ffmpeg.exe"
if not defined FF for /f "delims=" %%i in ('where ffmpeg 2^>nul') do if not defined FF set "FF=%%i"
if not defined FF goto NOFF

set "SEC=20"
set /p "SEC=录多少秒（直接回车 = 20）："
if not defined SEC set "SEC=20"

echo.
echo ============================================================
echo   3 秒后开始全屏录制，共 %SEC% 秒
echo   录制前请把窗口摆好：浏览器和终端各占一半，字体调大
echo   开始后按 Ctrl+C 可提前结束
echo ============================================================
echo.
timeout /t 3 >nul

"%FF%" -y -f gdigrab -framerate 30 -i desktop -t %SEC% -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p "%OUT%raw.mp4"
if errorlevel 1 goto FAIL

echo.
echo 转成 GIF ...
"%FF%" -y -i "%OUT%raw.mp4" -vf "fps=12,scale=1000:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer:bayer_scale=3" -loop 0 "%OUT%raw.gif"
if errorlevel 1 goto FAIL

echo.
echo ============================================================
echo   完成
echo   视频：%OUT%raw.mp4
echo   动图：%OUT%raw.gif
echo ============================================================
echo.
echo 如果画面里有多余区域，用这条命令裁掉（crop=宽:高:左上X:左上Y）：
echo   "%FF%" -i raw.mp4 -vf "crop=1200:700:0:0" -c:v libx264 -crf 20 trimmed.mp4
echo.
pause
exit /b 0

:NOFF
echo.
echo [X] 没找到 ffmpeg.exe
echo     放到 D:\Tools\ffmpeg\ffmpeg.exe，或装好后加入 PATH。
echo.
pause
exit /b 1

:FAIL
echo.
echo [X] 失败了，把上面的报错发出来。
echo.
pause
exit /b 1
