#!/usr/bin/env bash
# Connects the monitor to Kalshi's live data stream with your Kalshi API key.
# The key is used only to sign the connection; the monitor reads market data and never places orders.
#
#   bash /opt/crypto15/deploy/kalshi_key.sh            add or replace the key
#   bash /opt/crypto15/deploy/kalshi_key.sh --remove   stop using it (back to once-a-second prices)
set -euo pipefail
APP_DIR=/opt/crypto15
APP_USER=crypto15
ENV_FILE=/etc/crypto15.env
KEY_FILE=/etc/crypto15-kalshi.pem

[ "$(id -u)" -eq 0 ] || { echo "Please run this as root (the Droplet Console logs you in as root)."; exit 1; }
[ -f "$ENV_FILE" ] || { echo "No $ENV_FILE found. Run deploy/setup_vps.sh first."; exit 1; }

set_env() {   # set_env NAME VALUE  (replace the line, or add it)
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  fi
}

if [ "${1:-}" = "--remove" ]; then
  set_env KALSHI_API_KEY_ID ""
  set_env KALSHI_PRIVATE_KEY_PATH ""
  rm -f "$KEY_FILE"
  systemctl restart crypto15
  echo "Removed. The monitor is back to fetching Kalshi's prices once a second."
  exit 0
fi

echo
echo "Kalshi API key setup"
echo "--------------------"
read -r -p "1) Paste your Kalshi API Key ID and press Enter: " KEY_ID
KEY_ID="$(echo "$KEY_ID" | tr -d '[:space:]')"
[ -n "$KEY_ID" ] || { echo "No Key ID entered."; exit 1; }

echo
echo "2) Paste your private key: everything from the -----BEGIN line to the -----END line."
echo "   (Right-click > Paste, or Ctrl+Shift+V.) It stays hidden on screen and finishes by itself after the -----END line."
PEM=""
while IFS= read -rs line; do
  line="${line%$'\r'}"
  [ -z "$PEM" ] && [ -z "$line" ] && continue
  PEM+="$line"$'\n'
  [[ "$line" == -----END* ]] && break
done
echo "   Received $(printf '%s' "$PEM" | wc -l) lines."
[[ "$PEM" == -----BEGIN* ]] || { echo "That didn't look like a private key (it should start with -----BEGIN)."; exit 1; }

TMP="$(mktemp)"
chmod 600 "$TMP"
printf '%s' "$PEM" > "$TMP"
chown "$APP_USER" "$TMP"
unset PEM

echo
echo "3) Testing the key against Kalshi's live stream (up to 20 seconds)..."
if ! sudo -u "$APP_USER" -H "$APP_DIR/.venv/bin/python" "$APP_DIR/kalshi_ws.py" --test --key-id "$KEY_ID" --key "$TMP"; then
  rm -f "$TMP"
  echo
  echo "The key didn't work, so nothing was changed. Check that the Key ID and private key belong together"
  echo "(they're shown together when you create the key on Kalshi), then run this again."
  exit 1
fi

install -o root -g "$APP_USER" -m 640 "$TMP" "$KEY_FILE"
rm -f "$TMP"
set_env KALSHI_API_KEY_ID "$KEY_ID"
set_env KALSHI_PRIVATE_KEY_PATH "$KEY_FILE"
chmod 600 "$ENV_FILE"
systemctl restart crypto15
echo
echo "Done. The monitor now uses Kalshi's live order book and trades, and the official CF Benchmarks index."
echo "The key is stored in $KEY_FILE (readable only by root and the monitor). To stop using it:"
echo "  bash $APP_DIR/deploy/kalshi_key.sh --remove"
