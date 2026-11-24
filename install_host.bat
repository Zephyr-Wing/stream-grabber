@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 正在登记「启动本地服务」组件...
echo.
python -m app.native_host --install %*
if errorlevel 1 (
  echo.
  echo 登记失败。常见原因：
  echo   1. 还没有在 chrome://extensions 加载过本项目的 ext 目录
  echo   2. python 不在 PATH 里
  echo   3. 使用了不受支持的浏览器
  echo.
  pause
  exit /b 1
)
echo.
echo 登记完成，可以关掉这个窗口。回到浏览器点扩展图标即可启动服务。
pause
