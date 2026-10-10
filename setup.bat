@echo off
setlocal
cd /d "%~dp0"
echo [setup] Creating virtual environment .venv ...
python -m venv .venv || goto :err
echo [setup] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :err
echo [setup] Installing dependencies (requests, cryptography, pycryptodome) ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
echo.
echo [setup] Done. Try:
echo     ".venv\Scripts\python.exe" checkin.py qoder claim --dry-run
echo     ".venv\Scripts\python.exe" checkin.py all
goto :eof
:err
echo [setup] FAILED. Read the messages above.
exit /b 1
