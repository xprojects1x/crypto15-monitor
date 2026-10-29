#!/usr/bin/env bash
# Install the latest version from GitHub and restart. Your data, settings and agent memory are kept.
set -euo pipefail
APP_DIR=/opt/crypto15
APP_USER=crypto15
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
cd "$APP_DIR"
if [ -d .git ]; then
  sudo -u "$APP_USER" -H git pull --ff-only
else
  echo "This copy didn't come from GitHub; upload the new files into $APP_DIR, then run this again."
fi
sudo -u "$APP_USER" -H "$APP_DIR/.venv/bin/pip" install -q -r requirements.txt
chmod +x deploy/*.sh
sudo -u "$APP_USER" -H "$APP_DIR/deploy/backup.sh"
systemctl restart crypto15
sleep 3
systemctl --no-pager --lines=0 status crypto15 | head -5
echo "Updated. Your agents' memory was backed up first (see $APP_DIR/backups)."
