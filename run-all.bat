@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" checkin.py all %*
echo.
echo Exit code: %ERRORLEVEL%   (details written to checkin.log)
pause
