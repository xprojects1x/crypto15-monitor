#!/usr/bin/env bash
# Nightly: save everything the agents have learned, paper trades, settings and history (not the big tick logs).
# Keeps the newest 14 backups in backups/. To restore one:
#   systemctl stop crypto15 && tar -xzf /opt/crypto15/backups/<file>.tar.gz -C /opt/crypto15 && systemctl start crypto15
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
name="backups/crypto15-$(date -u +%Y%m%d-%H%M).tar.gz"
find data -type f ! -name 'ticks_*' -print0 | tar --null -czf "$name" --files-from=-
ls -1t backups/crypto15-*.tar.gz | tail -n +15 | xargs -r rm -f
echo "Saved $name ($(du -h "$name" | cut -f1))"
