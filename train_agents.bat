@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo Close the dashboard (app.py) before training, so it doesn't overwrite what the teams learn here.
echo.
for %%C in (BTC ETH SOL) do (
  echo === %%C ===
  %PY% history.py --asset %%C --candles-days 30
  %PY% train_agents.py --asset %%C
)
pause
