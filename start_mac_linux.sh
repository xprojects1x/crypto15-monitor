#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
PY=python3; command -v python3 >/dev/null || PY=python
$PY -m pip install -q -r requirements.txt 2>/dev/null || $PY -m pip install -q --user -r requirements.txt
( sleep 2; (command -v open >/dev/null && open http://localhost:8765) || (command -v xdg-open >/dev/null && xdg-open http://localhost:8765) ) &
exec $PY app.py "$@"
