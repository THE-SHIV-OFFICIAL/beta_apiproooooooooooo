#!/usr/bin/env bash
# Update yt-dlp (fixes most "extraction failed" / "empty file" errors) and restart.
set -e
if [ -x /opt/betaapi/.venv/bin/pip ]; then
  /opt/betaapi/.venv/bin/pip install -U "yt-dlp[default]"
  systemctl restart betaapi
else
  cd "$(dirname "$0")/.." && docker compose build --no-cache --pull && docker compose up -d
fi
echo "Updated."
