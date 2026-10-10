@echo off
setlocal
cd /d "%~dp0"
set "DIR=%CD%"
set "TASK=AICheckinDaily"
set "TIME=%~1"
if "%TIME%"=="" set "TIME=10:05"

rem Resolve a windowless interpreter: prefer the local venv, fall back to system pythonw.
set "PYW=%DIR%\.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"

echo Registering scheduled task "%TASK%" - daily at %TIME%
echo   command: "%PYW%" "%DIR%\checkin.py" all
echo   NOTE: /IT = run only when the user is logged on (Qoder DPAPI needs an interactive session).
echo.
schtasks /Create /F /SC DAILY /ST %TIME% /TN "%TASK%" /TR "\"%PYW%\" \"%DIR%\checkin.py\" all" /IT
if errorlevel 1 (
  echo FAILED to create the task.
  exit /b 1
)
echo.
echo Done. Verify with:  schtasks /Query /TN "%TASK%" /FO LIST
echo Run now:            schtasks /Run /TN "%TASK%"
echo Remove later:       uninstall-schedule.bat
pause
