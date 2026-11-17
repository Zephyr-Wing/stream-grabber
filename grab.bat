@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Pick the first interpreter that can import websocket.
rem A machine may have several Pythons; the dependency sits in only one of them.
set "PYEXE="
for %%P in ("python" "py -3") do (
  if not defined PYEXE (
    %%~P -c "import websocket" >nul 2>nul && set "PYEXE=%%~P"
  )
)

if not defined PYEXE (
  echo.
  echo [X] No Python with websocket-client found.
  echo     Install it with the same interpreter that runs grab.py:
  echo       python -m pip install -r requirements.txt
  echo       py -3 -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

%PYEXE% grab.py %*
echo.
pause
