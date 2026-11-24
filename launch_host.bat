@echo off
rem Native Messaging host launcher.
rem Chrome starts this file; stdout must carry protocol frames only,
rem so everything else is silenced. pythonw keeps a console from appearing.
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
where pythonw >nul 2>&1
if not errorlevel 1 (
  pythonw -m app.native_host
  goto :eof
)
python -m app.native_host
