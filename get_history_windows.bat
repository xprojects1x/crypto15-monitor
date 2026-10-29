@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo.
echo Downloads every settled Kalshi 15-minute window for BTC, ETH and SOL and builds a report for each.
echo Add --candles-days 30 to also pull Kalshi's prices before each close (slower, resumable).
echo.
for %%C in (BTC ETH SOL) do %PY% history.py --asset %%C %*
if exist data\history\report.html start "" data\history\report.html
pause
