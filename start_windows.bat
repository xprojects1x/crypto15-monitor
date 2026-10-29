@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install it from https://www.python.org/downloads/ and tick "Add Python to PATH".
  pause
  exit /b 1
)
echo Installing requirements...
%PY% -m pip install -q -r requirements.txt
start "" http://localhost:8765
%PY% app.py %*
pause
