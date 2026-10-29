@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
for %%C in (BTC ETH SOL) do (
  echo === %%C ===
  %PY% history.py --asset %%C
  %PY% validate.py --asset %%C %*
)
if exist data\history\report.html start "" data\history\report.html
pause
