#!/usr/bin/env bash
# Crypto 15-minute monitor: one-command setup for a fresh DigitalOcean droplet (Ubuntu 24.04 or 26.04).
#
#   From your GitHub repo (paste into the droplet's Console as root):
#     bash <(curl -fsSL https://raw.githubusercontent.com/YOU/REPO/main/deploy/setup_vps.sh) https://github.com/YOU/REPO
#
#   From an uploaded copy of the folder:
#     bash /opt/crypto15/deploy/setup_vps.sh
#
# What it does: installs Python and Caddy, runs the monitor as a background service that restarts itself,
# puts it behind HTTPS with a password, turns on the firewall, schedules hourly history reports and nightly
# backups, and sets up phone alerts. Safe to re-run: your data is kept and it offers to keep your settings.
set -euo pipefail

APP_DIR=/opt/crypto15
APP_USER=crypto15
ENV_FILE=/etc/crypto15.env
AUTH_FILE=/etc/crypto15.auth
REPO_URL="${1:-}"
COINS="BTC ETH SOL"

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mStopped: %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local a=""; read -r -p "   $1 " a </dev/tty || true; printf '%s' "$a"; }
ask_secret() { local a=""; read -r -s -p "   $1 " a </dev/tty || true; printf '\n' >/dev/tty; printf '%s' "$a"; }
yes_no() {   # yes_no "Question?" y|n
  local def="${2:-y}" a hint="[Y/n]"
  [ "$def" = n ] && hint="[y/N]"
  a=$(ask "$1 $hint")
  a="${a:-$def}"
  [[ "$a" =~ ^[Yy] ]]
}
as_app() { sudo -u "$APP_USER" -H "$@"; }

[ "$(id -u)" -eq 0 ] || die "please run as root (the DigitalOcean Console logs you in as root)."
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = ubuntu ] || warn "This script is tested on Ubuntu; you're on ${PRETTY_NAME:-unknown}. Continuing anyway."

# ------------------------------------------------------------------ 1. system packages
say "1/9  Installing system packages (2-3 minutes)"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
APT_OPTS=(-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
apt-get update -qq
apt-get upgrade -y -qq "${APT_OPTS[@]}" >/dev/null
apt-get install -y -qq python3 python3-venv python3-pip git curl ufw gnupg debian-keyring debian-archive-keyring \
  apt-transport-https unattended-upgrades >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
fi
ok "Python $(python3 -V | cut -d' ' -f2), $(caddy version | cut -d' ' -f1 | head -1) Caddy, firewall, automatic security updates"

# ------------------------------------------------------------------ 2. swap (helps a 1 GB droplet)
say "2/9  Memory safety net"
if swapon --show 2>/dev/null | grep -q .; then
  ok "Swap already present"
elif { [ -f /swapfile ] || fallocate -l 1G /swapfile; } && chmod 600 /swapfile && mkswap -q /swapfile >/dev/null 2>&1 \
     && swapon /swapfile 2>/dev/null; then
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ok "Added 1 GB of swap"
else
  warn "Couldn't add swap (not essential on 1 GB+ droplets)"
fi

# ------------------------------------------------------------------ 3. app user and code
say "3/9  Getting the monitor's code"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)"
if [ -n "$REPO_URL" ]; then
  if [ -d "$APP_DIR/.git" ]; then
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
    as_app git -C "$APP_DIR" pull --ff-only -q && ok "Updated code from $REPO_URL"
  else
    [ -e "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ] && mv "$APP_DIR" "${APP_DIR}.old.$(date +%s)"
    git clone -q --depth 1 "$REPO_URL" "$APP_DIR" || die "couldn't download $REPO_URL (is the repo public and the address right?)"
    ok "Downloaded code from $REPO_URL"
  fi
elif [ -f "$APP_DIR/app.py" ]; then
  ok "Using the code already in $APP_DIR"
elif [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/app.py" ]; then
  mkdir -p "$APP_DIR" && cp -a "$SRC_DIR/." "$APP_DIR/" && ok "Copied code from $SRC_DIR"
else
  die "no code found. Re-run with your GitHub address, e.g.  bash setup_vps.sh https://github.com/YOU/REPO"
fi
[ -f "$APP_DIR/app.py" ] || die "$APP_DIR doesn't contain app.py"
mkdir -p "$APP_DIR/data" "$APP_DIR/backups"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod +x "$APP_DIR"/deploy/*.sh

# ------------------------------------------------------------------ 4. python environment
say "4/9  Python environment"
as_app python3 -m venv "$APP_DIR/.venv"
as_app "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
as_app "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "Installed $(as_app "$APP_DIR/.venv/bin/python" -c 'import aiohttp; print("aiohttp", aiohttp.__version__)')"

# ------------------------------------------------------------------ 5. settings: password, address, alerts
say "5/9  Settings"
PUBLIC_IP="$(curl -fsS --max-time 3 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null \
  || curl -4fsS --max-time 5 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
DEFAULT_HOST="${PUBLIC_IP//./-}.sslip.io"
KEEP=n
if [ -f "$ENV_FILE" ] && [ -f "$AUTH_FILE" ] && yes_no "Keep your current password, address and alert settings?" y; then
  KEEP=y
  # shellcheck disable=SC1090
  . "$AUTH_FILE"
  ok "Keeping current settings (edit $ENV_FILE to change alerts later)"
fi

if [ "$KEEP" = n ]; then
  echo "   The dashboard will be protected by a username and password."
  DASH_USER="$(ask "Username [admin]:")"; DASH_USER="${DASH_USER:-admin}"
  while true; do
    P1="$(ask_secret "Password (8+ characters):")"
    P2="$(ask_secret "Same password again:")"
    [ "${#P1}" -ge 8 ] && [ "$P1" = "$P2" ] && break
    warn "Passwords didn't match or were shorter than 8 characters. Try again."
  done
  DASH_HASH="$(caddy hash-password --plaintext "$P1")"
  unset P1 P2
  echo "   Web address: without your own domain, the dashboard will be at https://$DEFAULT_HOST"
  DASH_HOST="$(ask "Your own domain (e.g. monitor.example.com, pointed at $PUBLIC_IP) or Enter to use the free address:")"
  DASH_HOST="${DASH_HOST:-$DEFAULT_HOST}"
  umask 077
  printf "DASH_USER='%s'\nDASH_HASH='%s'\nDASH_HOST='%s'\n" "$DASH_USER" "$DASH_HASH" "$DASH_HOST" > "$AUTH_FILE"

  TG_TOKEN=""; TG_CHAT=""; DISCORD=""; SMTP_HOST=""; SMTP_PORT="587"; SMTP_USER=""; SMTP_PASS=""; MAIL_TO=""
  echo
  echo "   Where should alerts go?  1) Telegram (recommended: free, instant phone notifications)"
  echo "                            2) Discord   3) Email   4) Skip for now"
  CH="$(ask "Choose 1-4 [1]:")"; CH="${CH:-1}"
  case "$CH" in
    1)
      echo "   a. In Telegram, open @BotFather, send /newbot and follow the prompts. Copy the token it gives you."
      TG_TOKEN="$(ask "Paste the bot token:")"
      echo "   b. Open your new bot in Telegram (BotFather shows a link) and send it any message, like: hi"
      ask "Press Enter once you've sent the message..." >/dev/null
      TG_CHAT="$(curl -fsS --max-time 10 "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" 2>/dev/null | python3 -c '
import json, sys
try:
    r = [u for u in json.load(sys.stdin).get("result", []) if u.get("message")]
    print(r[-1]["message"]["chat"]["id"] if r else "")
except Exception:
    print("")' || true)"
      if [ -z "$TG_CHAT" ]; then
        warn "Couldn't find your message (check the token, and that you messaged the new bot)."
        TG_CHAT="$(ask "Type your chat ID if you know it, or press Enter to skip Telegram:")"
      fi
      if [ -n "$TG_CHAT" ]; then
        curl -fsS --max-time 10 -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" -d "chat_id=${TG_CHAT}" \
          --data-urlencode "text=✅ Your crypto 15-minute monitor is connected. Alerts will arrive here." >/dev/null \
          && ok "Telegram connected: check your phone for a test message" || warn "Telegram test message failed"
      else
        TG_TOKEN=""
      fi ;;
    2)
      echo "   In Discord: Server Settings > Integrations > Webhooks > New Webhook > Copy Webhook URL."
      DISCORD="$(ask "Paste the webhook URL:")"
      [ -n "$DISCORD" ] && curl -fsS --max-time 10 -H 'Content-Type: application/json' \
        -d '{"content":"✅ Your crypto 15-minute monitor is connected."}' "$DISCORD" >/dev/null \
        && ok "Discord connected" || warn "Discord test failed" ;;
    3)
      echo "   For Gmail, create an App Password (Google Account > Security > App passwords) and use it below."
      SMTP_HOST="$(ask "SMTP server [smtp.gmail.com]:")"; SMTP_HOST="${SMTP_HOST:-smtp.gmail.com}"
      SMTP_PORT="$(ask "SMTP port [587]:")"; SMTP_PORT="${SMTP_PORT:-587}"
      SMTP_USER="$(ask "Email login:")"
      SMTP_PASS="$(ask_secret "Email (app) password:")"
      MAIL_TO="$(ask "Send alerts to [$SMTP_USER]:")"; MAIL_TO="${MAIL_TO:-$SMTP_USER}" ;;
    *) warn "No alerts for now. Run this script again any time to add them." ;;
  esac
  echo
  echo "   Optional dead-man's switch: a free https://healthchecks.io check (Period 5 min, Grace 5 min) texts or emails"
  echo "   you if this server goes silent, even if it's completely down."
  HC_URL="$(ask "Paste its ping URL, or press Enter to skip:")"
  SUMMARY="$(ask "Daily summary time, US Eastern [20:00] (or 'off'):")"; SUMMARY="${SUMMARY:-20:00}"
  [ "$SUMMARY" = off ] && SUMMARY=""
  TRADES="0"; yes_no "Also message you about every team paper trade? (can be many per day)" n && TRADES="1"
  umask 077
  cat > "$ENV_FILE" <<EOF
# Crypto 15-minute monitor settings. After editing: systemctl restart crypto15
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHAT}
DISCORD_WEBHOOK_URL=${DISCORD}
SMTP_HOST=${SMTP_HOST}
SMTP_PORT=${SMTP_PORT}
SMTP_USER=${SMTP_USER}
SMTP_PASSWORD=${SMTP_PASS}
ALERT_EMAIL_TO=${MAIL_TO}
HEALTHCHECK_URL=${HC_URL}
DAILY_SUMMARY_ET=${SUMMARY}
ALERT_TRADES=${TRADES}
EOF
  umask 022
  ok "Saved settings to $ENV_FILE (readable by root only)"
fi
chmod 600 "$ENV_FILE" "$AUTH_FILE"

# ------------------------------------------------------------------ 6. HTTPS + password (Caddy) and firewall
say "6/9  HTTPS, password and firewall"
mkdir -p /etc/caddy
cat > /etc/caddy/Caddyfile <<EOF
# Crypto 15-minute monitor: HTTPS (automatic certificate) + password in front of the app.
${DASH_HOST} {
	encode zstd gzip
	basic_auth {
		${DASH_USER} ${DASH_HASH}
	}
	reverse_proxy 127.0.0.1:8765
}
EOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || die "Caddy configuration check failed"
systemctl enable -q caddy
systemctl reload caddy 2>/dev/null || systemctl restart caddy
if ufw allow OpenSSH >/dev/null && ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null && ufw --force enable >/dev/null; then
  ok "Only SSH (22) and the web (80/443) are open; the app itself is reachable only through Caddy"
else
  warn "Couldn't turn on the firewall; the app is still only reachable through Caddy (it listens on 127.0.0.1)"
fi

# ------------------------------------------------------------------ 7. background services
say "7/9  Background services"
cat > /etc/systemd/system/crypto15.service <<EOF
[Unit]
Description=Crypto 15-minute Kalshi monitor (BTC, ETH, SOL)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/python app.py --host 127.0.0.1 --port 8765
Restart=always
RestartSec=5
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/crypto15-history.service <<EOF
[Unit]
Description=Crypto 15-minute monitor: refresh history reports
After=network-online.target

[Service]
Type=oneshot
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${APP_DIR}/deploy/refresh_history.sh
Nice=10
EOF
cat > /etc/systemd/system/crypto15-history.timer <<EOF
[Unit]
Description=Refresh history reports every hour

[Timer]
OnCalendar=*-*-* *:20:00
RandomizedDelaySec=120
Persistent=true

[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/crypto15-backup.service <<EOF
[Unit]
Description=Crypto 15-minute monitor: nightly backup of what the agents have learned

[Service]
Type=oneshot
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/deploy/backup.sh
EOF
cat > /etc/systemd/system/crypto15-backup.timer <<EOF
[Unit]
Description=Nightly backup of agent memory and paper trades

[Timer]
OnCalendar=*-*-* 03:40:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
ok "Monitor service (restarts itself), hourly history refresh, nightly backups"

# ------------------------------------------------------------------ 8. first-time head start
say "8/9  Head start from history"
if ! curl -fsS -o /dev/null --max-time 10 "https://api.elections.kalshi.com/trade-api/v2/exchange/status"; then
  warn "Can't reach Kalshi from this server right now; skipping. The hourly refresh will download history once it can."
elif [ ! -f "$APP_DIR/data/history/windows.csv" ]; then
  DAYS=""
  yes_no "Also download 30 days of Kalshi prices so the teams learn more up front? (about 20 minutes)" y && DAYS="--candles-days 30"
  systemctl stop crypto15 2>/dev/null || true
  for c in $COINS; do
    echo "   $c: downloading settled windows..."
    # shellcheck disable=SC2086
    as_app bash -c "cd '$APP_DIR' && .venv/bin/python history.py --asset $c $DAYS 2>&1 | tr '\r' '\n' | grep -E 'settled windows|available' || true"
    as_app bash -c "cd '$APP_DIR' && .venv/bin/python train_agents.py --asset $c 2>&1 | grep -E 'learned from|recent 30' || true"
  done
  ok "History downloaded and the teams have a head start"
else
  ok "History already present"
fi

# ------------------------------------------------------------------ 9. start everything
say "9/9  Starting"
systemctl enable -q --now crypto15 crypto15-history.timer crypto15-backup.timer
systemctl restart crypto15
for _ in $(seq 1 30); do
  curl -fsS -o /dev/null --max-time 2 http://127.0.0.1:8765/api/health && break
  sleep 1
done
if curl -fsS -o /dev/null --max-time 2 http://127.0.0.1:8765/api/health; then
  ok "Monitor is running"
else
  warn "Monitor didn't answer yet. See: journalctl -u crypto15 -n 50"
fi
HTTPS_OK=n
for _ in $(seq 1 12); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://${DASH_HOST}/" || true)"
  [ "$code" = 401 ] || [ "$code" = 200 ] && { HTTPS_OK=y; break; }
  sleep 5
done
[ "$HTTPS_OK" = y ] && ok "HTTPS certificate issued" || warn "HTTPS isn't ready yet; Caddy keeps trying in the background (check: journalctl -u caddy -n 30)"

cat <<EOF

  ============================================================================
   Your monitor is live:   https://${DASH_HOST}
   Username:               ${DASH_USER}
   Password:               the one you just chose

   It runs on its own, restarts itself if anything crashes, refreshes the history
   reports every hour and backs up what the agents learn every night.

   Useful commands (in this Console):
     ${APP_DIR}/deploy/status.sh      health, recent logs, disk space
     ${APP_DIR}/deploy/update.sh      install the latest version from GitHub
     nano ${ENV_FILE}               change alert settings, then: systemctl restart crypto15
     bash ${APP_DIR}/deploy/setup_vps.sh   change password or address (keeps your data)
  ============================================================================
EOF
