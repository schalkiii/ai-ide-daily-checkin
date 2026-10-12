@echo off
setlocal
cd /d "%~dp0"
set "T=%~1"
if "%T%"=="" set "T=10:05"

echo Registering scheduled task "AICheckinDaily" - daily at %T%
echo   mode: run WHETHER OR NOT the user is logged on (stored credential; DPAPI still works)
echo   see install-schedule.ps1 for the DPAPI rationale and caveats
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-schedule.ps1" -Time "%T%"
if errorlevel 1 (
  echo FAILED to create the task.
  pause
  exit /b 1
)
pause
