#!/usr/bin/env bash
# BetaAPI one-shot VPS installer (Ubuntu/Debian). Run as root:
#   sudo bash deploy/install.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash deploy/install.sh"; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/betaapi
DATA=/var/lib/betaapi

echo "==> Installing system packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip unzip curl ca-certificates rsync

echo "==> Installing Deno (JS runtime for yt-dlp)"
if ! command -v deno >/dev/null; then
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y
fi
deno --version | head -1

echo "==> Copying app to $APP"
id betaapi >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin betaapi
mkdir -p "$APP" "$DATA"
rsync -a --exclude '.venv' --exclude '.env' --exclude '__pycache__' "$SRC"/ "$APP"/

echo "==> Python venv + requirements"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install -r "$APP/requirements.txt"

if [ ! -f "$APP/.env" ]; then
  cp "$APP/.env.example" "$APP/.env"
  ADMIN=$("$APP/.venv/bin/python" -c "import secrets;print(secrets.token_urlsafe(32))")
  sed -i "s|^ADMIN_KEY=.*|ADMIN_KEY=$ADMIN|" "$APP/.env"
  sed -i "s|^DB_PATH=.*|DB_PATH=$DATA/bot_data.db|" "$APP/.env"
  sed -i "s|^API_BASE_URL=.*|API_BASE_URL=http://127.0.0.1:8000|" "$APP/.env"
  echo
  echo "!! Created $APP/.env — edit it now:"
  echo "   nano $APP/.env   (set TELEGRAM_BOT_TOKEN, ADMIN_IDS, APP_URL)"
  NEED_EDIT=1
fi

chown -R betaapi:betaapi "$APP" "$DATA"
chmod 600 "$APP/.env"

echo "==> systemd service"
cp "$APP/deploy/betaapi.service" /etc/systemd/system/betaapi.service
systemctl daemon-reload
systemctl enable betaapi

echo "==> Weekly yt-dlp auto-update (YouTube breaks old versions often)"
cat > /etc/cron.d/betaapi-ytdlp <<CRON
17 4 * * 1 root $APP/.venv/bin/pip install -U "yt-dlp[default]" >/var/log/betaapi-update.log 2>&1 && systemctl restart betaapi
CRON

if [ "${NEED_EDIT:-0}" = "1" ]; then
  echo
  echo "Next: nano $APP/.env  &&  systemctl start betaapi"
else
  systemctl restart betaapi
  echo "Started. Check: systemctl status betaapi ; curl http://127.0.0.1:8000/healthz"
fi
