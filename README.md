# BetaAPI

**FastAPI YouTube stream API + Telegram API-key bot**  
Built for music bots, lightweight deployments, and simple plan-based access control.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/TEAM-ISTKHAR/Api)
[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new)

BetaAPI resolves YouTube URLs or search queries and returns metadata plus a direct stream URL. The companion Telegram bot creates API keys, tracks usage, handles plan upgrades, and provides a small admin workflow.

> **Important:** direct media URLs are temporary and can expire. Use them immediately instead of storing them. Only use this project for content you are allowed to access and process.

## What is included

- FastAPI server with OpenAPI docs at `/docs`
- `/stream`, `/song`, `/api/stream`, `/ytdl`, `/search`, `/info`, and `/formats`
- API-key authentication with daily and per-minute plan limits
- SQLite usage, payment-request, ticket, audit, and metadata-cache tables
- Telegram bot for free keys, renewals, upgrades, support, payments, and admin actions
- YouTube client fallback, direct networking, cookies, PO token, and TTL caching support
- Heroku `Procfile` + `app.json` with one always-on Basic process running the API and bot together
- Railway health checks and restart policy
- Docker Compose setup with persistent SQLite volume
- VPS systemd units and Nginx reverse-proxy config

## 🚀 DEPLOY GUIDE (VPS) — copy/paste commands

### 1. Pehli baar install (Ubuntu 20.04+/Debian, root ya sudo)

```bash
# zip VPS pe upload karo (scp / WinSCP), phir:
cd ~ && unzip -o Api-main-fixed.zip && cd Api-main
sudo bash deploy/install.sh
sudo nano /opt/betaapi/.env
```

`.env` mein ye 4 cheezein zaroor bharo (ADMIN_KEY install script khud bana deta hai):

```dotenv
TELEGRAM_BOT_TOKEN=123456:xxxx        # @BotFather se
ADMIN_IDS=123456789                   # tumhara Telegram numeric ID
APP_URL=http://YOUR_VPS_IP:8000       # ya https://api.yourdomain.com
BOT_URL=https://t.me/YourBotUsername
```

Phir start karo:

```bash
sudo systemctl start betaapi
sudo systemctl status betaapi --no-pager
curl http://127.0.0.1:8000/healthz        # {"status":"ok",...} aana chahiye
```

Firewall mein port kholo (agar ufw use karte ho):

```bash
sudo ufw allow 8000/tcp        # ya 80/443 agar nginx lagaya
```

### 2. API key lo aur test karo

Telegram pe apne bot ko `/start` bhejo → key milegi. Phir:

```bash
cd ~/Api-main
bash scripts/selftest.sh YOUR_API_KEY
# last line: DOWNLOAD OK  → sab sahi hai
```

### 3. Daily commands

```bash
sudo systemctl restart betaapi          # restart
sudo systemctl stop betaapi             # stop
sudo journalctl -u betaapi -f           # live logs
sudo journalctl -u betaapi -n 200 --no-pager
sudo nano /opt/betaapi/.env && sudo systemctl restart betaapi   # config change
```

### 4. Update (YouTube error aaye to SABSE PEHLE ye)

```bash
sudo bash ~/Api-main/deploy/update.sh      # yt-dlp update + restart
```

Naya code zip aaye to:

```bash
cd ~ && unzip -o Api-main-fixed.zip && cd Api-main
sudo bash deploy/install.sh                # .env aur database safe rehta hai
sudo systemctl restart betaapi
```

(Weekly yt-dlp auto-update cron install script khud laga deta hai.)

### 5. Domain + HTTPS (optional, recommended)

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx
sudo cp /opt/betaapi/deploy/nginx.conf /etc/nginx/sites-available/betaapi
sudo sed -i 's/api.example.com/api.YOURDOMAIN.com/' /etc/nginx/sites-available/betaapi
sudo ln -sf /etc/nginx/sites-available/betaapi /etc/nginx/sites-enabled/betaapi
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d api.YOURDOMAIN.com
# phir .env mein APP_URL=https://api.YOURDOMAIN.com  → systemctl restart betaapi
```

### 6. Docker se (alternative)

```bash
cd ~/Api-main && cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```

### 7. Sizzu / music bot ko connect karo

Bot ke `.env` mein:

```dotenv
BETAAPI_URL=http://YOUR_VPS_IP:8000     # ya https://api.YOURDOMAIN.com
BETAAPI_KEY=BetaAPIxxxxxxxxxxxxxxxx     # bot /start se mili key
```

Is repo ki `youtube.py` ko bot ke `platforms/Youtube.py` ki jagah rakho, bot restart karo.
Bot dusre server pe ho to bhi chalega: `youtube.py` pehle direct download try karti hai, fail/empty ho to `GET /download` se API server ke through leti hai.

### 8. Problem → Solution

| Error | Fix |
| --- | --- |
| `Sign in to confirm you're not a bot` / 403 / 503 `ip_blocked` | `update.sh` chalao → fresh YouTube cookies (`YTDLP_COOKIE_FILE=/opt/betaapi/cookies.txt`) → ya `PROXY_LIST=http://user:pass@host:port` |
| `downloaded file is empty` | Bot ka `youtube.py` isi repo wala lagao (auto `/download` fallback) + `update.sh` |
| `Invalid or expired API key` (401) | Bot se `/mykey`, `.env` mein sahi `BETAAPI_KEY` |
| `Daily limit reached` (429) | Plan upgrade, ya admin se `/upgrade` |
| Telegram bot reply nahi de raha | `journalctl -u betaapi -n 100` → `TELEGRAM_BOT_TOKEN` check |
| Port 8000 bahar se nahi khulta | `ufw allow 8000/tcp` + provider firewall |

Cookies file ke liye: browser mein YouTube login (alag account use karo) → "Get cookies.txt LOCALLY" extension → file `/opt/betaapi/cookies.txt` pe copy → `sudo chown betaapi:betaapi /opt/betaapi/cookies.txt` → restart.

## Architecture

| Process | Command | Purpose |
| --- | --- | --- |
| `web` / `app` | `python start.py` | HTTP API + Telegram polling bot |

The API and bot start together through one supervised process. If either side exits, the launcher stops cleanly so Heroku, Railway, Docker, or systemd can restart the complete service.

## Quick start

```bash
git clone https://github.com/TEAM-ISTKHAR/Api.git
cd Api

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill .env before starting

python start.py
```

Open `http://localhost:8000/docs`. The API root and `/healthz` do not require a key; stream and account endpoints do.

Start both the API and Telegram bot together:

```bash
source .venv/bin/activate
python start.py
```

## Configuration

> **Heroku networking:** Proxy routing is disabled to prevent proxy DNS failures. The service uses direct YouTube requests with retries and client fallbacks.


Copy `.env.example` to `.env`. Never commit `.env`, Telegram tokens, cookies, proxy credentials, UPI/bank details, or API keys.

Required for a complete deployment:

```dotenv
APP_URL=https://your-public-api.example.com
ADMIN_KEY=generate-a-long-random-value
TELEGRAM_BOT_TOKEN=123456:replace-me
ADMIN_IDS=123456789
```

`API_BASE_URL` is the URL used by the bot to call the API. URLs are normalized automatically, so `api.example.com` and `https://api.example.com` both work. For a public deployment it can be the same as `APP_URL`; in Docker Compose and the VPS systemd setup it is overridden to the local API address.

Useful optional settings:

- YouTube networking: direct connections with retries; proxy routing is disabled on Heroku
- `YTDLP_COOKIE_FILE`: path to a cookies file (keep it outside Git)
- `YTDLP_PO_TOKEN`: YouTube PO token, if required by the extractor
- `DB_PATH`: SQLite file location; use persistent storage in production
- `YTDLP_WORKERS`: extraction thread count; start with `6`
- `BF_MAX_ATTEMPTS`, `BF_WINDOW_SEC`, `BF_BLOCK_SEC`: invalid-key protection

Generate a strong admin key without putting it in shell history:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## API examples

Use the header form in production:

```bash
curl -H "x-api-key: BetaAPIxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  "https://your-api.example.com/stream?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DVIDEO_ID"
```

Search and resolve a song:

```bash
curl -H "x-api-key: YOUR_KEY" \
  "https://your-api.example.com/song?query=Alan%20Walker%20Faded"
```

Useful endpoints:

| Endpoint | Auth | Description |
| --- | --- | --- |
| `GET /` | No | Service information |
| `GET /healthz` | No | Hosting-platform health probe |
| `GET /docs` | No | Swagger UI |
| `GET /plans` | No | Plan and pricing metadata |
| `GET /stream` | Yes | Universal audio stream |
| `GET /song` | Yes | Music-bot-compatible response |
| `GET /api/stream` | Yes | Audio or video via `type` |
| `GET /ytdl` | Yes | Raw yt-dlp-style response |
| `GET /search` | Yes | YouTube search |
| `GET /info` | Yes | Video metadata |
| `GET /formats` | Yes | Available formats |
| `GET /my/stats` | Yes | Current key usage |

Admin endpoints use the same `x-api-key` header with `ADMIN_KEY`.

## Heroku: Basic dynos, API + bot

The repository includes a single `web` process that runs both API and Telegram bot through `start.py`. `app.json` requests one always-on **Basic** dyno; Basic dynos are paid and do not use Eco sleeping behavior.

### Deploy from the Heroku Dashboard

1. Create a Heroku app and connect this GitHub repository.
2. In **Settings → Config Vars**, add the values from `.env.example`.
3. Deploy the `main` branch.
4. In **Resources**, verify `web` is set to `Basic` and quantity `1`.

### Deploy with Heroku CLI

```bash
heroku create your-betaapi-name
heroku stack:set heroku-24 -a your-betaapi-name

heroku config:set \
  APP_NAME=BetaAPI \
  APP_URL=https://your-betaapi-name.herokuapp.com \
  API_BASE_URL=https://your-betaapi-name.herokuapp.com \
  ADMIN_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  ADMIN_IDS="123456789" \
  -a your-betaapi-name

git push heroku main
heroku ps:scale web=1:basic -a your-betaapi-name
heroku ps -a your-betaapi-name
```

Add `TELEGRAM_BOT_TOKEN` separately in the Heroku dashboard or with `heroku config:set` from a secure terminal.

Do not paste real tokens into GitHub. Heroku cannot provide durable SQLite storage: a dyno restart or redeploy can remove `bot_data.db`. Use the Docker/VPS option or migrate the database before relying on persistent users, payments, and usage history.

## Railway: API + bot

Railway reads `railway.json`, installs the Python requirements, exposes the injected `$PORT`, checks `/healthz`, and restarts failed API processes.

1. Create one Railway service from this GitHub repository.
2. Add the required environment variables from `.env.example`. Proxy configuration is not required.
3. Railway will use `python start.py` from the `Procfile`; generate a public domain and set `APP_URL` and `API_BASE_URL` to it.
4. Keep the service health check on `/healthz`; the same process serves HTTP while the Telegram bot runs in the background.
5. Use persistent storage or an external database if you need user, payment, and usage data to survive redeploys.

## VPS: recommended Docker deployment

Docker Compose runs the API and bot together with `restart: unless-stopped` and stores SQLite in a named volume.

```bash
git clone https://github.com/TEAM-ISTKHAR/Api.git /opt/betaapi
cd /opt/betaapi
cp .env.example .env
nano .env

docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/healthz
```

Put `deploy/nginx.conf` in `/etc/nginx/sites-available/betaapi`, replace `api.example.com`, enable it, and issue TLS with Certbot:

```bash
sudo ln -s /etc/nginx/sites-available/betaapi /etc/nginx/sites-enabled/betaapi
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d api.example.com
```

## VPS: systemd deployment

For a non-Docker install:

```bash
sudo useradd --system --home /opt/betaapi --shell /usr/sbin/nologin betaapi
sudo mkdir -p /opt/betaapi /var/lib/betaapi
sudo chown -R betaapi:betaapi /opt/betaapi /var/lib/betaapi

cd /opt/betaapi
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN, ADMIN_IDS, and ADMIN_KEY

sudo cp deploy/betaapi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now betaapi
sudo systemctl status betaapi
```

The service files expect the checkout at `/opt/betaapi` and persistent SQLite at `/var/lib/betaapi/bot_data.db`.

## Telegram bot commands

- `/start` — create or show a free key
- `/mykey` — show key and plan
- `/getfile` — download the music-bot `youtube.py` helper
- `/revokekey` — revoke a leaked key and issue a replacement
- `/status` — check API health
- `/admin` — admin panel
- `/approve`, `/reject`, `/upgrade`, `/payments`, `/reply` — admin-only workflows

The included `youtube.py` helper is intended to be copied into another music bot. Its consumer project needs `aiohttp` and the dependencies used by that bot (including Pyrogram if the `YouTubeAPI` class is used).

## Production notes

- Keep one Uvicorn worker when using the bundled SQLite database.
- Use persistent disk or an external database before scaling beyond one service instance.
- Stream URLs are not permanent download links.
- Proxy routing is disabled by default to keep Heroku DNS reliable.
- Review YouTube's terms and local copyright law before operating the service commercially.
- Rotate `ADMIN_KEY` and Telegram credentials if they are ever exposed.

## License

No license is currently declared. Add a license before distributing this project publicly.

## v7 — High traffic setup

- **Cookie pool:** put one cookie file per account in `cookies/` (`python tools_cookies_convert.py cookies.json cookies/account2.txt`). Bad ones rest 30 min automatically. Check: `/admin/cookie-status`.
- **Shared cache:** set `REDIS_URL` (Railway/Heroku Redis add-on). All servers share resolved songs.
- **Auto link refresh:** stream links are renewed 5 min before YouTube expires them, so long songs never break.
- **Multi-worker:** for heavy traffic use `web: python serve.py` (API only, many workers) and run the bot as a separate `worker` process.
- **More servers:** run several copies behind one domain with the same `REDIS_URL`.

## v9 — New key style, Premium switch, easy cookies

- **Keys look like:** `SHIV255445425731BETA` — prefix sirf `SHIV` ya `ISTU` (randomly koi ek) + 12 digits + `BETA`.
  Apne naam chahiye to env: `KEY_PREFIXES=SHIV,ISTU,APNA_NAAM`, `KEY_SUFFIX=BETA`, `KEY_DIGITS=12`. Old `BetaAPI...` keys still work.
- **/premium off** (default): sab free, unlimited songs, new + old + expired keys sab chalengi.
- **/premium on**: plans, daily limits aur expiry phir se lag jaate hain. Sirf owner (ADMIN_IDS) chala sakta hai.
- **Cookies add karna (sabse aasaan):** bot ko owner account se `.txt` cookie file bhejo → bot use `cookies/` mein save kar deta hai.
  Cookie file kaise nikale: Chrome/Firefox mein naya YouTube account login → extension **"Get cookies.txt LOCALLY"** → youtube.com kholo → Export → `.txt` file bot ko bhejo. 3–4 accounts = 3–4 files.
- **Faster bot downloads:** youtube.py ab gaana 4 MB ke parallel tukdon mein laata hai (~0.5 s for 12 MB), aur API har link ko dene se pehle check karti hai ki link chalta hai.

## v11 — Proxy / Cookies auto mode + editable plans
- Sirf cookies daali → cookies use. Sirf PROXY_LIST → proxy use. Dono → proxy pehle, fail ho to cookies (backup), phir direct.
- Force karna ho: ROUTE_MODE=cookies | proxy | auto. `/admin/cookie-status` mein `route` dikhta hai.
- Owner/admin: `/setplan` (list), `/setplan pro rpm=100 rpd=10000 validity=30 price=₹399/month`, `-1` = unlimited, `/setplan reset`. Limits sirf `/premium on` par lagti hain.
- Saare variables: `.env.example`.

## v12 — Auto UPI payment
Set PAY_API_KEY. User taps Buy → bot sends a QR (valid 10 min) with plan details, an "I Have Paid" button and a payment page link. The bot checks the payment every 8 s; once it's verified, the plan starts and the user gets the key. Each order can only be activated once. If PAY_API_KEY is empty, the old UTR/manual flow is used. Prices come from /setplan price=₹199/month (the first number becomes the amount).
