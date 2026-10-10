@echo off
setlocal
set "TASK=AICheckinDaily"
echo Removing scheduled task "%TASK%" ...
schtasks /Delete /F /TN "%TASK%"
if errorlevel 1 (
  echo Nothing to remove or task not found.
  exit /b 1
)
echo Done.
pause
