"""
database.py
-----------
SQLite manager for BetaAPI — commercial grade.

Tables:
  users            - registered Telegram users
  api_keys         - API keys with plan + expiry
  usage_logs       - per-request tracking (rate-limit source of truth)
  tickets          - support tickets
  feedback         - user feedback
  payment_requests - commercial plan purchase requests

Thread-safety: WAL journal + check_same_thread=False.
"""

import sqlite3
import os
import secrets
import string
import logging
from datetime import datetime, timedelta
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH           = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db"),
)
KEY_VALIDITY_DAYS = 30          # 30-day keys for paid plans, 28 for free


# ---------------------------------------------------------------------------
# Plans  — commercial pricing
# ---------------------------------------------------------------------------

PLANS = {
    "free": {
        "name":     "Free",
        "rpd":      50,
        "rpm":      5,
        "price":    "Free",
        "validity": 28,     # days
        "features": ["50 requests/day", "5 req/min", "Audio only"],
    },
    "basic": {
        "name":     "Basic",
        "rpd":      500,
        "rpm":      20,
        "price":    "₹99/month",
        "validity": 30,
        "features": ["500 requests/day", "20 req/min", "Audio + Video"],
    },
    "pro": {
        "name":     "Pro",
        "rpd":      5000,
        "rpm":      60,
        "price":    "₹299/month",
        "validity": 30,
        "features": ["5000 requests/day", "60 req/min", "Audio + Video", "Priority support"],
    },
    "ultra": {
        "name":     "Ultra",
        "rpd":      -1,
        "rpm":      200,
        "price":    "₹699/month",
        "validity": 30,
        "features": ["Unlimited requests/day", "200 req/min", "Audio + Video",
                     "Priority support", "Dedicated assistance"],
    },
}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA cache_size=-8000;")
    conn.execute("PRAGMA busy_timeout=15000;")   # 8 MB page cache
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT    DEFAULT '',
                first_name  TEXT    DEFAULT '',
                joined_at   TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                key         TEXT    PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                plan        TEXT    DEFAULT 'free',
                created_at  TEXT    DEFAULT (datetime('now')),
                expires_at  TEXT    NOT NULL,
                is_active   INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS usage_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                api_key     TEXT    NOT NULL,
                endpoint    TEXT    NOT NULL,
                query       TEXT    DEFAULT '',
                media_type  TEXT    DEFAULT 'audio',
                timestamp   TEXT    DEFAULT (datetime('now')),
                success     INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                message     TEXT    NOT NULL,
                status      TEXT    DEFAULT 'open',
                created_at  TEXT    DEFAULT (datetime('now')),
                reply       TEXT
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                message     TEXT    NOT NULL,
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS payment_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                plan        TEXT    NOT NULL,
                status      TEXT    DEFAULT 'pending',
                utr         TEXT    DEFAULT '',
                amount      TEXT    DEFAULT '',
                created_at  TEXT    DEFAULT (datetime('now')),
                resolved_at TEXT,
                note        TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS key_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                api_key     TEXT    NOT NULL,
                event       TEXT    NOT NULL,
                ip          TEXT    DEFAULT '',
                note        TEXT    DEFAULT '',
                timestamp   TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS video_cache (
                video_id     TEXT    PRIMARY KEY,
                url          TEXT    NOT NULL,
                title        TEXT    DEFAULT '',
                uploader     TEXT    DEFAULT '',
                duration_sec INTEGER DEFAULT 0,
                thumbnail    TEXT    DEFAULT '',
                upload_date  TEXT    DEFAULT '',
                view_count   INTEGER DEFAULT 0,
                like_count   INTEGER DEFAULT 0,
                description  TEXT    DEFAULT '',
                is_live      INTEGER DEFAULT 0,
                cached_at    TEXT    DEFAULT (datetime('now')),
                hit_count    INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_usage_key_time  ON usage_logs    (api_key, timestamp);
            CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_logs    (user_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_keys_user       ON api_keys      (user_id, is_active);
            CREATE INDEX IF NOT EXISTS idx_pay_user        ON payment_requests (user_id, status);
            CREATE INDEX IF NOT EXISTS idx_audit_key       ON key_audit_log (api_key, timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_ip        ON key_audit_log (ip, timestamp);
        """)
    logger.info("DB init OK.")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def upsert_user(user_id: int, username: str, first_name: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name
        """, (user_id, username or "", first_name or ""))


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def get_all_users() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()


def count_users() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

# Sirf SHIV ya ISTU — randomly koi ek. Apne naam chahiye to env mein
# KEY_PREFIXES=SHIV,ISTU,APNA_NAAM daal do.
KEY_PREFIXES = [x.strip().upper() for x in os.getenv(
    "KEY_PREFIXES",
    "SHIV,ISTU",
).split(",") if x.strip()]
KEY_SUFFIX = os.getenv("KEY_SUFFIX", "BETA").strip().upper() or "BETA"
KEY_DIGITS = max(9, int(os.getenv("KEY_DIGITS", "12")))


def _gen_key() -> str:
    """e.g. SHIV255445425731BETA  ->  <random name><digits>BETA"""
    while True:
        prefix = secrets.choice(KEY_PREFIXES)
        digits = "".join(secrets.choice(string.digits) for _ in range(KEY_DIGITS))
        key = f"{prefix}{digits}{KEY_SUFFIX}"
        with get_conn() as conn:
            if not conn.execute("SELECT 1 FROM api_keys WHERE key = ?", (key,)).fetchone():
                return key


# ---------------------------------------------------------------------------
# Premium mode  (/premium on | /premium off) — OFF = everything free & unlimited
# ---------------------------------------------------------------------------

_premium_cache = {"v": None, "t": 0.0}


def _ensure_settings():
    with get_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")


def is_premium_mode() -> bool:
    import time as _t
    if _premium_cache["v"] is not None and _t.time() - _premium_cache["t"] < 5:
        return _premium_cache["v"]
    _ensure_settings()
    with get_conn() as conn:
        row = conn.execute("SELECT v FROM settings WHERE k = 'premium'").fetchone()
    val = bool(row and row["v"] == "on")
    _premium_cache.update(v=val, t=_t.time())
    return val


def set_premium_mode(on: bool):
    _ensure_settings()
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (k, v) VALUES ('premium', ?)",
                     ("on" if on else "off",))
    _premium_cache.update(v=on, t=0.0)


# ── Owner-editable plans (/setplan) — saved in DB, all workers reload in 5s ──
import json as _json
_plans_cache = {"t": 0.0}
import copy as _copy
_DEFAULT_PLANS = _copy.deepcopy(PLANS)
_EDITABLE = {"rpm": int, "rpd": int, "validity": int, "price": str, "name": str}


def refresh_plans(force: bool = False):
    import time as _t
    if not force and _t.time() - _plans_cache["t"] < 5:
        return
    _plans_cache["t"] = _t.time()
    _ensure_settings()
    with get_conn() as conn:
        row = conn.execute("SELECT v FROM settings WHERE k = 'plans'").fetchone()
    if not row:
        return
    for pid, cfg in _json.loads(row["v"]).items():
        PLANS.setdefault(pid, {"name": pid.title(), "rpd": 50, "rpm": 5,
                               "price": "Free", "validity": 30, "features": []})
        PLANS[pid].update(cfg)
        r, m = PLANS[pid]["rpd"], PLANS[pid]["rpm"]
        PLANS[pid]["features"] = [("Unlimited" if r == -1 else str(r)) + " requests/day",
                                  ("Unlimited" if m == -1 else str(m)) + " req/min", "Audio + Video"]


def set_plan(plan: str, **fields):
    """Update a plan's limits. -1 = unlimited. New plan names are created."""
    plan = plan.lower()
    clean = {k: _EDITABLE[k](v) for k, v in fields.items() if k in _EDITABLE and v is not None}
    _ensure_settings()
    with get_conn() as conn:
        row = conn.execute("SELECT v FROM settings WHERE k = 'plans'").fetchone()
        data = _json.loads(row["v"]) if row else {}
        data.setdefault(plan, {}).update(clean)
        conn.execute("INSERT OR REPLACE INTO settings (k, v) VALUES ('plans', ?)", (_json.dumps(data),))
    refresh_plans(force=True)
    return PLANS[plan]


def reset_plans():
    _ensure_settings()
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE k = 'plans'")
    PLANS.clear(); PLANS.update(_copy.deepcopy(_DEFAULT_PLANS))


def _validity_days(plan: str) -> int:
    return PLANS.get(plan, PLANS["free"])["validity"]


def get_active_key(user_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM api_keys
            WHERE user_id = ?
              AND is_active = 1
              AND datetime(expires_at) > datetime('now')
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,)).fetchone()


def create_key(user_id: int, plan: str = "free") -> str:
    if plan not in PLANS:
        plan = "free"
    validity   = _validity_days(plan)
    key        = _gen_key()
    expires_at = (datetime.utcnow() + timedelta(days=validity)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("UPDATE api_keys SET is_active = 0 WHERE user_id = ?", (user_id,))
        conn.execute("""
            INSERT INTO api_keys (key, user_id, plan, expires_at)
            VALUES (?, ?, ?, ?)
        """, (key, user_id, plan, expires_at))
    return key


def renew_key(user_id: int) -> Optional[str]:
    row = get_active_key(user_id)
    if not row:
        return None
    validity    = _validity_days(row["plan"])
    new_expires = (datetime.utcnow() + timedelta(days=validity)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("UPDATE api_keys SET expires_at = ? WHERE key = ?",
                     (new_expires, row["key"]))
    return row["key"]


def revoke_key(user_id: int) -> str:
    row  = get_active_key(user_id)
    plan = row["plan"] if row else "free"
    with get_conn() as conn:
        conn.execute("UPDATE api_keys SET is_active = 0 WHERE user_id = ?", (user_id,))
    return create_key(user_id, plan)


def upgrade_plan(user_id: int, new_plan: str) -> str:
    if new_plan not in PLANS:
        raise ValueError(f"Invalid plan '{new_plan}'. Valid: {', '.join(PLANS)}")
    with get_conn() as conn:
        conn.execute("UPDATE api_keys SET is_active = 0 WHERE user_id = ?", (user_id,))
    return create_key(user_id, new_plan)


def validate_key(api_key: str) -> Optional[sqlite3.Row]:
    if not api_key:
        return None
    if not is_premium_mode():
        # Free mode: every key ever issued works (new, old, expired, revoked-by-renewal).
        with get_conn() as conn:
            return conn.execute("SELECT * FROM api_keys WHERE key = ? ORDER BY is_active DESC LIMIT 1",
                                (api_key,)).fetchone()
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM api_keys
            WHERE key = ?
              AND is_active = 1
              AND datetime(expires_at) > datetime('now')
        """, (api_key,)).fetchone()


def days_remaining(user_id: int) -> int:
    row = get_active_key(user_id)
    if not row:
        return 0
    try:
        exp   = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
        delta = exp - datetime.utcnow()
        return max(0, delta.days)
    except Exception:
        return 0


def days_remaining_by_key(api_key: str) -> int:
    row = validate_key(api_key)
    if not row:
        return 0
    try:
        exp   = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
        delta = exp - datetime.utcnow()
        return max(0, delta.days)
    except Exception:
        return 0


def get_expiring_keys(within_days: int = 3) -> list:
    """Return all active keys expiring within N days (for warning notifications)."""
    cutoff = (datetime.utcnow() + timedelta(days=within_days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        return conn.execute("""
            SELECT k.*, u.first_name, u.username
            FROM api_keys k
            JOIN users u ON k.user_id = u.user_id
            WHERE k.is_active = 1
              AND datetime(k.expires_at) > datetime('now')
              AND datetime(k.expires_at) <= datetime(?)
        """, (cutoff,)).fetchall()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def check_rate_limit(api_key: str) -> dict:
    refresh_plans()
    key_row = validate_key(api_key)
    if not key_row:
        return {"allowed": False, "reason": "invalid_key", "used": 0, "limit": 0, "plan": "free"}

    plan     = key_row["plan"]
    if not is_premium_mode():
        return {"allowed": True, "used": 0, "limit": -1, "plan": plan}
    plan_cfg = PLANS.get(plan, PLANS["free"])
    rpd      = plan_cfg["rpd"]
    rpm      = plan_cfg["rpm"]

    with get_conn() as conn:
        used_today = conn.execute("""
            SELECT COUNT(*) AS cnt FROM usage_logs
            WHERE api_key = ? AND timestamp >= date('now') AND success = 1
        """, (api_key,)).fetchone()["cnt"]

        used_min = conn.execute("""
            SELECT COUNT(*) AS cnt FROM usage_logs
            WHERE api_key = ?
              AND timestamp >= datetime('now', '-1 minute')
              AND success = 1
        """, (api_key,)).fetchone()["cnt"]

    if rpm != -1 and used_min >= rpm:
        return {"allowed": False, "reason": "rpm_exceeded",
                "used": used_today, "limit": rpd, "plan": plan}

    if rpd != -1 and used_today >= rpd:
        return {"allowed": False, "reason": "rpd_exceeded",
                "used": used_today, "limit": rpd, "plan": plan}

    return {"allowed": True, "used": used_today, "limit": rpd, "plan": plan}


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------

def log_usage(user_id: int, api_key: str, endpoint: str, query: str,
              success: bool = True, media_type: str = "audio"):
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO usage_logs (user_id, api_key, endpoint, query, success, media_type)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, api_key, endpoint, query or "", int(success), media_type))
    except Exception as e:
        logger.warning(f"log_usage: {e}")


def get_usage_stats(user_id: int) -> dict:
    with get_conn() as conn:
        def _c(sql, p=()):
            return conn.execute(sql, p).fetchone()["cnt"]

        total       = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=?", (user_id,))
        today       = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=? AND timestamp >= date('now')", (user_id,))
        today_audio = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=? AND timestamp >= date('now') AND media_type='audio'", (user_id,))
        today_video = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=? AND timestamp >= date('now') AND media_type='video'", (user_id,))
        total_audio = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=? AND media_type='audio'", (user_id,))
        total_video = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=? AND media_type='video'", (user_id,))
        last        = conn.execute("SELECT timestamp FROM usage_logs WHERE user_id=? ORDER BY timestamp DESC LIMIT 1", (user_id,)).fetchone()

    return {
        "total": total, "today": today,
        "today_audio": today_audio, "today_video": today_video,
        "total_audio": total_audio, "total_video": total_video,
        "last_used": last["timestamp"] if last else "Never",
    }


def get_admin_stats() -> dict:
    with get_conn() as conn:
        total_users    = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        active_keys    = conn.execute("SELECT COUNT(*) AS c FROM api_keys WHERE is_active=1 AND datetime(expires_at)>datetime('now')").fetchone()["c"]
        total_requests = conn.execute("SELECT COUNT(*) AS c FROM usage_logs").fetchone()["c"]
        today_requests = conn.execute("SELECT COUNT(*) AS c FROM usage_logs WHERE timestamp >= date('now')").fetchone()["c"]
        open_tickets   = conn.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='open'").fetchone()["c"]
        pending_pay    = conn.execute("SELECT COUNT(*) AS c FROM payment_requests WHERE status='pending'").fetchone()["c"]

        plan_counts = {p: conn.execute("""
            SELECT COUNT(*) AS c FROM api_keys
            WHERE plan=? AND is_active=1 AND datetime(expires_at)>datetime('now')
        """, (p,)).fetchone()["c"] for p in PLANS}

    return {
        "total_users":    total_users,
        "active_keys":    active_keys,
        "total_requests": total_requests,
        "today_requests": today_requests,
        "open_tickets":   open_tickets,
        "pending_payments": pending_pay,
        "plan_counts":    plan_counts,
    }


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def create_ticket(user_id: int, message: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO tickets (user_id, message) VALUES (?, ?)", (user_id, message)
        ).lastrowid


def get_open_tickets() -> list:
    with get_conn() as conn:
        return conn.execute("""
            SELECT t.*, u.first_name, u.username
            FROM tickets t JOIN users u ON t.user_id = u.user_id
            WHERE t.status = 'open' ORDER BY t.created_at ASC
        """).fetchall()


def close_ticket(ticket_id: int, reply: str):
    with get_conn() as conn:
        conn.execute("UPDATE tickets SET status='closed', reply=? WHERE id=?",
                     (reply, ticket_id))


def get_user_tickets(user_id: int) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tickets WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
            (user_id,)
        ).fetchall()


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

def save_feedback(user_id: int, message: str):
    with get_conn() as conn:
        conn.execute("INSERT INTO feedback (user_id, message) VALUES (?, ?)",
                     (user_id, message))


# ---------------------------------------------------------------------------
# Payment requests
# ---------------------------------------------------------------------------

def create_payment_request(user_id: int, plan: str, utr: str = "", amount: str = "") -> int:
    with get_conn() as conn:
        return conn.execute("""
            INSERT INTO payment_requests (user_id, plan, utr, amount)
            VALUES (?, ?, ?, ?)
        """, (user_id, plan, utr, amount)).lastrowid


def get_pending_payments() -> list:
    with get_conn() as conn:
        return conn.execute("""
            SELECT p.*, u.first_name, u.username
            FROM payment_requests p JOIN users u ON p.user_id = u.user_id
            WHERE p.status = 'pending'
            ORDER BY p.created_at ASC
        """).fetchall()


def resolve_payment(payment_id: int, status: str = "approved", note: str = ""):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("""
            UPDATE payment_requests
            SET status=?, resolved_at=?, note=?
            WHERE id=?
        """, (status, now, note, payment_id))


def get_user_payments(user_id: int) -> list:
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM payment_requests
            WHERE user_id=? ORDER BY created_at DESC LIMIT 5
        """, (user_id,)).fetchall()


# ---------------------------------------------------------------------------
# Auto init
# ---------------------------------------------------------------------------
init_db()


# ---------------------------------------------------------------------------
# Key audit log
# ---------------------------------------------------------------------------

def log_key_audit(user_id: int, api_key: str, event: str,
                  ip: str = "", note: str = ""):
    """
    Log key lifecycle events:
      events: created | user_revoke | admin_revoke | expired | invalid_attempt
    """
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO key_audit_log (user_id, api_key, event, ip, note)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, api_key, event, ip[:100], note[:300]))
    except Exception as e:
        logger.warning(f"log_key_audit failed: {e}")


def get_key_audit(user_id: int, limit: int = 20) -> list:
    """Last N audit events for a user."""
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM key_audit_log
            WHERE user_id = ?
            ORDER BY timestamp DESC LIMIT ?
        """, (user_id, limit)).fetchall()


# ---------------------------------------------------------------------------
# Brute-force tracking (in-memory — fast, no DB hit per request)
# ---------------------------------------------------------------------------

import threading
import time as _time
from collections import defaultdict

_bf_lock    = threading.Lock()
# ip → {"count": int, "window_start": float}
_bf_tracker: dict = defaultdict(lambda: {"count": 0, "window_start": 0.0})

BF_MAX_ATTEMPTS = int(os.getenv("BF_MAX_ATTEMPTS", "10"))   # fails before block
BF_WINDOW_SEC   = int(os.getenv("BF_WINDOW_SEC",   "60"))   # rolling window
BF_BLOCK_SEC    = int(os.getenv("BF_BLOCK_SEC",    "300"))  # block duration (5 min)

# ip → block_until timestamp
_bf_blocked: dict = {}


def is_ip_blocked(ip: str) -> bool:
    """True if IP is currently in block period."""
    with _bf_lock:
        until = _bf_blocked.get(ip, 0)
        if _time.time() < until:
            return True
        if until:                           # block expired — clean up
            del _bf_blocked[ip]
            _bf_tracker.pop(ip, None)
        return False


def record_invalid_attempt(ip: str) -> dict:
    """
    Record one failed auth attempt for this IP.
    Returns {"blocked": bool, "attempts": int, "block_until": float|None}
    """
    now = _time.time()
    with _bf_lock:
        rec = _bf_tracker[ip]
        # Reset window if it has expired
        if now - rec["window_start"] > BF_WINDOW_SEC:
            rec["count"]        = 0
            rec["window_start"] = now
        rec["count"] += 1

        if rec["count"] >= BF_MAX_ATTEMPTS:
            block_until          = now + BF_BLOCK_SEC
            _bf_blocked[ip]      = block_until
            _bf_tracker.pop(ip, None)   # reset counter after blocking
            return {"blocked": True, "attempts": rec["count"],
                    "block_until": block_until}

    return {"blocked": False, "attempts": rec["count"], "block_until": None}


def reset_attempts(ip: str):
    """Call after a successful auth to clear failed counter."""
    with _bf_lock:
        _bf_tracker.pop(ip, None)
        _bf_blocked.pop(ip, None)


def get_bf_stats() -> dict:
    """Admin: current brute-force state."""
    now = _time.time()
    with _bf_lock:
        active_blocks = {
            ip: round(until - now, 1)
            for ip, until in _bf_blocked.items()
            if until > now
        }
        pending = {
            ip: rec["count"]
            for ip, rec in _bf_tracker.items()
        }
    return {"active_blocks": active_blocks, "pending_attempts": pending}


# ---------------------------------------------------------------------------
# Video metadata cache (persistent DB)
# ---------------------------------------------------------------------------
# Stream URLs expire quickly (1-6 hrs) — we only cache metadata.
# Stream URL is always fetched fresh from yt-dlp.
# ---------------------------------------------------------------------------

import hashlib

# How long to keep metadata in DB (default 7 days)
VIDEO_CACHE_TTL_DAYS = int(os.getenv("VIDEO_CACHE_TTL_DAYS", "7"))


def _url_to_video_id(url: str) -> str:
    """Extract YouTube video ID from URL, or hash the string if not a URL."""
    # Standard YouTube URL
    if "v=" in url:
        return url.split("v=")[-1].split("&")[0].strip()
    # Short URL youtu.be/VIDEO_ID
    if "youtu.be/" in url:
        return url.split("youtu.be/")[-1].split("?")[0].strip()
    # If already an 11-char ID
    if len(url) == 11 and url.replace("-", "").replace("_", "").isalnum():
        return url
    # For search queries — use MD5 hash as key
    return "q_" + hashlib.md5(url.lower().strip().encode()).hexdigest()[:16]


def cache_video_metadata(url: str, info: dict):
    """
    Save video metadata to DB.
    Called after every successful yt-dlp extraction.
    info dict = what get_video_info() returns.
    """
    video_id = _url_to_video_id(url)
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO video_cache
                    (video_id, url, title, uploader, duration_sec,
                     thumbnail, upload_date, view_count, like_count,
                     description, is_live, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(video_id) DO UPDATE SET
                    title        = excluded.title,
                    uploader     = excluded.uploader,
                    duration_sec = excluded.duration_sec,
                    thumbnail    = excluded.thumbnail,
                    upload_date  = excluded.upload_date,
                    view_count   = excluded.view_count,
                    like_count   = excluded.like_count,
                    description  = excluded.description,
                    is_live      = excluded.is_live,
                    cached_at    = datetime('now')
            """, (
                video_id,
                url,
                info.get("title", ""),
                info.get("uploader", ""),
                int(info.get("duration_seconds") or 0),
                info.get("thumbnail", ""),
                info.get("upload_date", ""),
                int(info.get("view_count") or 0),
                int(info.get("like_count") or 0),
                (info.get("description") or "")[:500],
                int(info.get("is_live", False)),
            ))
    except Exception as e:
        logger.warning(f"cache_video_metadata failed: {e}")


def get_cached_video_metadata(url: str) -> Optional[dict]:
    """
    Get video metadata from DB cache.
    Returns dict (same format as get_video_info) or None if not cached / expired.
    """
    video_id = _url_to_video_id(url)
    try:
        with get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM video_cache
                WHERE video_id = ?
                  AND datetime(cached_at) > datetime('now', ?)
            """, (video_id, f"-{VIDEO_CACHE_TTL_DAYS} days")).fetchone()

            if not row:
                return None

            # Increment hit counter
            conn.execute(
                "UPDATE video_cache SET hit_count = hit_count + 1 WHERE video_id = ?",
                (video_id,)
            )

        return {
            "id":               row["video_id"],
            "title":            row["title"],
            "uploader":         row["uploader"],
            "duration_seconds": row["duration_sec"],
            "thumbnail":        row["thumbnail"],
            "upload_date":      row["upload_date"],
            "view_count":       row["view_count"],
            "like_count":       row["like_count"],
            "description":      row["description"],
            "is_live":          bool(row["is_live"]),
            "webpage_url":      row["url"],
            "channel_url":      None,
            "tags":             [],
            "_from_db_cache":   True,   # helpful for debugging
        }
    except Exception as e:
        logger.warning(f"get_cached_video_metadata failed: {e}")
        return None


def get_video_cache_stats() -> dict:
    """Admin: how many videos are cached and total cache hits."""
    try:
        with get_conn() as conn:
            total  = conn.execute("SELECT COUNT(*) AS c FROM video_cache").fetchone()["c"]
            hits   = conn.execute("SELECT SUM(hit_count) AS s FROM video_cache").fetchone()["s"] or 0
            fresh  = conn.execute(f"""
                SELECT COUNT(*) AS c FROM video_cache
                WHERE datetime(cached_at) > datetime('now', '-{VIDEO_CACHE_TTL_DAYS} days')
            """).fetchone()["c"]
            top5   = conn.execute("""
                SELECT video_id, title, hit_count FROM video_cache
                ORDER BY hit_count DESC LIMIT 5
            """).fetchall()
        return {
            "total_cached":  total,
            "fresh_entries": fresh,
            "total_hits":    hits,
            "top_videos":    [{"id": r["video_id"], "title": r["title"],
                               "hits": r["hit_count"]} for r in top5],
        }
    except Exception as e:
        logger.warning(f"get_video_cache_stats: {e}")
        return {}


def clear_video_cache(older_than_days: int = None):
    """
    Clear video cache.
    older_than_days=None → clear everything
    older_than_days=7    → clear entries older than 7 days
    """
    try:
        with get_conn() as conn:
            if older_than_days is None:
                conn.execute("DELETE FROM video_cache")
            else:
                conn.execute(
                    "DELETE FROM video_cache WHERE datetime(cached_at) <= datetime('now', ?)",
                    (f"-{older_than_days} days",)
                )
    except Exception as e:
        logger.warning(f"clear_video_cache: {e}")
