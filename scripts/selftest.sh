#!/usr/bin/env bash
# Usage: bash scripts/selftest.sh YOUR_API_KEY [http://127.0.0.1:8000]
KEY="${1:?usage: selftest.sh API_KEY [BASE_URL]}"; BASE="${2:-http://127.0.0.1:8000}"
H="x-api-key: $KEY"
echo "1) health";  curl -fsS "$BASE/healthz"; echo
echo "2) search";  curl -fsS -H "$H" "$BASE/search?q=faded%20alan%20walker&limit=1" | head -c 400; echo
echo "3) stream";  curl -fsS -H "$H" "$BASE/stream?query=faded%20alan%20walker" | head -c 400; echo
echo "4) download (first 100KB)"
curl -sS -H "$H" "$BASE/download?query=faded%20alan%20walker&type=audio" 2>/dev/null | head -c 102400 > /tmp/selftest.bin
SZ=$(stat -c %s /tmp/selftest.bin); echo "got $SZ bytes"; [ "$SZ" -gt 10000 ] && echo "DOWNLOAD OK" || echo "DOWNLOAD FAILED"
