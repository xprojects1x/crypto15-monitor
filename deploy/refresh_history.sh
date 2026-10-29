#!/usr/bin/env bash
# Hourly: download newly settled windows (plus Kalshi's prices for the last 2 days) and re-validate, for each coin.
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
for coin in BTC ETH SOL; do
  "$PY" history.py --asset "$coin" --candles-days 2 >/dev/null 2>&1 || echo "$coin: history download failed"
  "$PY" validate.py --asset "$coin" --coinbase 50 >/dev/null 2>&1 || echo "$coin: validation failed"
done
echo "History refreshed $(date -u +%Y-%m-%dT%H:%MZ)"
