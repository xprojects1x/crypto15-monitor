#!/usr/bin/env bash
# Quick health check: is it running, are the data feeds live, recent log lines, disk space, backups.
APP_DIR=/opt/crypto15
echo "== Service"
systemctl --no-pager --lines=0 status crypto15 2>/dev/null | sed -n '1,4p'
echo
echo "== Data feeds (per coin)"
if health="$(curl -fsS --max-time 3 http://127.0.0.1:8765/api/health 2>/dev/null)"; then
  HEALTH="$health" python3 - <<'PY'
import json, os
for coin, h in json.loads(os.environ["HEALTH"]).items():
    kalshi = "OK" if h["kalshi_ok"] else "NOT OK " + (h["kalshi_err"] or "")
    print(f"  {coin}: {h['feeds']}/5 exchanges live, Kalshi {kalshi}")
PY
else
  echo "  The monitor isn't answering. Recent log lines below."
fi
echo
echo "== Recent log lines"
journalctl -u crypto15 -n 8 --no-pager -o cat 2>/dev/null
echo
echo "== Timers"
systemctl list-timers 'crypto15-*' --no-pager 2>/dev/null | sed -n '1,3p'
echo
echo "== Disk"
df -h "$APP_DIR" | tail -1 | awk '{print "  " $4 " free of " $2 " (" $5 " used)"}'
du -sh "$APP_DIR/data" 2>/dev/null | awk '{print "  data folder: " $1}'
newest="$(ls -1t "$APP_DIR"/backups/*.tar.gz 2>/dev/null | head -1)"
echo "  newest backup: ${newest:-none yet}"
echo
host="$(grep -m1 -E '^[^#[:space:]].*\{$' /etc/caddy/Caddyfile 2>/dev/null | sed 's/ *{$//')"
[ -n "$host" ] && echo "== Dashboard: https://$host"
