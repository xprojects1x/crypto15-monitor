#!/usr/bin/env bash
# Starts the live dashboard inside a GitHub Codespace.
# Pulls the history the hourly GitHub Action saved, so the History page works right away.
cd "$(dirname "$0")" || exit 1
mkdir -p data
if [ ! -f data/history/windows.csv ] && git fetch --quiet --depth=1 origin history-data 2>/dev/null; then
  git archive FETCH_HEAD | tar -x -C data && echo "Loaded saved BTC, ETH and SOL history from the history-data branch."
fi
echo
echo "Starting the live dashboard. It opens in a new browser tab;"
echo "if it doesn't, open the PORTS tab below and click the globe icon next to port 8765."
echo
exec python app.py "$@"
