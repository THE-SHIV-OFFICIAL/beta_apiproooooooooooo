"""
tgbot.py
--------
BetaAPI Commercial Telegram Bot v5.0

Features:
  - /start     → auto key + welcome
  - /mykey     → quick key display
  - /status    → API server status check
  - /upgrade   → admin: upgrade user plan
  - /reply     → admin: reply to ticket
  - /payments  → admin: pending payment requests
  - /approve   → admin: approve payment & upgrade
  - /reject    → admin: reject payment
  - /broadcast → admin: send message to all users
  - /admin     → admin panel
  - Inline keyboard: View Key, Usage, Plans, Upgrade, Ticket, Feedback, Help
  - Payment flow with UPI/bank details
  - Expiry warnings (sent at key creation + 3 days before expiry)
"""

import os
import logging
import asyncio
import re
import time
import httpx
import pytz

from datetime import datetime
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest

import database as db
import autopay

# ── Config ───────────────────────────────────────────────────────────────────
def _normalise_base_url(raw: str, default: str = "") -> str:
    value = (raw or "").strip().rstrip("/")
    if not value:
        return default
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value:
        scheme = "http" if value.startswith(("localhost", "127.", "0.0.0.0")) else "https"
        value = scheme + "://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Base URL must include a valid HTTP(S) host.")
    return value.rstrip("/")

BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOT_URL       = _normalise_base_url(os.getenv("BOT_URL", ""))
APP_URL       = _normalise_base_url(os.getenv("APP_URL", "http://localhost:8000"), "http://localhost:8000")
API_BASE_URL  = _normalise_base_url(os.getenv("API_BASE_URL", ""), APP_URL)
BOT_NAME      = os.getenv("BOT_NAME", "BetaAPI")
SUPPORT_GROUP = _normalise_base_url(os.getenv("SUPPORT_GROUP", ""), "https://t.me/your_support_group")
CHANNEL       = _normalise_base_url(os.getenv("CHANNEL", ""), "https://t.me/your_channel")
ADMIN_IDS     = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip().isdigit()]

# Payment details — fill in .env
UPI_ID        = os.getenv("UPI_ID", "yourname@upi")
UPI_NAME      = os.getenv("UPI_NAME", "BetaAPI")
BANK_NAME     = os.getenv("BANK_NAME", "")
BANK_ACCOUNT  = os.getenv("BANK_ACCOUNT", "")
BANK_IFSC     = os.getenv("BANK_IFSC", "")

if not BOT_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


# ── Keyboards ────────────────────────────────────────────────────────────────

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 My API Key",       callback_data="view_key")],
        [InlineKeyboardButton("📊 My Usage",         callback_data="my_usage")],
        [InlineKeyboardButton("💎 Plans & Pricing",  callback_data="plans")],
        [
            InlineKeyboardButton("🎫 Support",  callback_data="support_ticket"),
            InlineKeyboardButton("💬 Feedback", callback_data="feedback"),
        ],
        [InlineKeyboardButton("❓ Help & Docs",      callback_data="help")],
        [
            InlineKeyboardButton("💬 Support Group", url=SUPPORT_GROUP),
            InlineKeyboardButton("📢 Channel",       url=CHANNEL),
        ],
    ])


def key_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Renew (+30 days)",      callback_data="renew_key")],
        [InlineKeyboardButton("🔁 Revoke & New Key",      callback_data="revoke_key")],
        [InlineKeyboardButton("⬆️ Upgrade Plan",          callback_data="upgrade_request")],
        [InlineKeyboardButton("🔙 Main Menu",             callback_data="back_main")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")]
    ])


def plans_kb() -> InlineKeyboardMarkup:
    db.refresh_plans()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⚡ Buy Basic  — {db.PLANS['basic']['price']}", callback_data="buy_basic")],
        [InlineKeyboardButton(f"🚀 Buy Pro    — {db.PLANS['pro']['price']}",   callback_data="buy_pro")],
        [InlineKeyboardButton(f"👑 Buy Ultra  — {db.PLANS['ultra']['price']}", callback_data="buy_ultra")],
        [InlineKeyboardButton("🔙 Main Menu",            callback_data="back_main")],
    ])


def test_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵 Audio", callback_data="test_audio"),
            InlineKeyboardButton("🎬 Video", callback_data="test_video"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="back_main")],
    ])


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",           callback_data="admin_stats")],
        [InlineKeyboardButton("💰 Pending Payments", callback_data="admin_payments")],
        [InlineKeyboardButton("🎫 Open Tickets",    callback_data="admin_tickets")],
        [InlineKeyboardButton("📢 Broadcast",       callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 Main Menu",       callback_data="back_main")],
    ])


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def fmt_dur(seconds) -> str:
    if not seconds:
        return "N/A"
    try:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return "N/A"


def fmt_ist(dt_str: str) -> str:
    try:
        dt  = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        ist = pytz.utc.localize(dt).astimezone(IST)
        return ist.strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return str(dt_str)[:16] if dt_str else "N/A"


def _ensure_key(user_id: int):
    row = db.get_active_key(user_id)
    if not row:
        db.create_key(user_id, "free")
        row = db.get_active_key(user_id)
    return row


async def safe_send(bot, chat_id: int, text: str, **kw):
    try:
        await bot.send_message(chat_id, text, **kw)
    except TelegramError as e:
        logger.warning(f"safe_send to {chat_id}: {e}")
    except Exception as e:
        logger.warning(f"safe_send unexpected {chat_id}: {e}")


async def safe_edit(query, text: str, **kw):
    try:
        await query.edit_message_text(text, **kw)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"safe_edit: {e}")
    except Exception as e:
        logger.warning(f"safe_edit: {e}")


def _key_card(key_row, user_id: int) -> str:
    if not key_row:
        return "❌ No active key."
    try:
        days     = db.days_remaining(user_id)
        stats    = db.get_usage_stats(user_id)
        plan     = key_row["plan"]
        cfg      = db.PLANS.get(plan, db.PLANS["free"])
        rpd      = cfg["rpd"]
        lim      = "Unlimited" if rpd == -1 else f"{rpd:,}"
        warn     = " ⚠️ Expiring soon!" if days <= 3 else ""

        return (
            f"🔑 *API Key Details*\n\n"
            f"Key: `{key_row['key']}`\n"
            f"Plan: *{plan.upper()}* — {cfg['price']}\n"
            f"Status: 🟢 Active{warn}\n"
            f"Expires: {fmt_ist(key_row['expires_at'])} ({days}d left)\n\n"
            f"*Today:*  {stats['today']} reqs  |  🎵 {stats['today_audio']}  🎬 {stats['today_video']}\n"
            f"*Total:*  {stats['total']} reqs  |  🎵 {stats['total_audio']}  🎬 {stats['total_video']}\n\n"
            f"Daily limit: {lim} req/day  •  {cfg['rpm']} req/min"
        )
    except Exception as e:
        logger.warning(f"_key_card: {e}")
        return f"🔑 Key: `{key_row['key']}`\n_Stats unavailable._"


def _plans_text() -> str:
    icons = {"free": "🆓", "basic": "⚡", "pro": "🚀", "ultra": "👑"}
    lines = [f"💎 *{BOT_NAME} — Plans & Pricing*\n"]
    for pid, p in db.PLANS.items():
        rpd = "Unlimited" if p["rpd"] == -1 else f"{p['rpd']:,}"
        feats = "  •  ".join(p.get("features", []))
        lines.append(
            f"{icons.get(pid,'•')} *{p['name']}*  —  {p['price']}\n"
            f"  {feats}\n"
        )
    lines += [
        "\n💳 *How to buy:*",
        "1️⃣ Tap a plan button below",
        "2️⃣ Send payment to our UPI",
        "3️⃣ Share UTR/screenshot",
        "4️⃣ Get upgraded instantly ✅",
    ]
    return "\n".join(lines)


def _payment_info(plan: str) -> str:
    cfg    = db.PLANS.get(plan, db.PLANS["basic"])
    amount = cfg["price"]
    lines  = [
        f"💳 *Payment for {cfg['name']} Plan*\n",
        f"Amount: *{amount}*",
        f"Validity: *{cfg['validity']} days*\n",
        f"*Pay via UPI:*",
        f"`{UPI_ID}`",
        f"Name: {UPI_NAME}\n",
    ]
    if BANK_ACCOUNT:
        lines += [
            "*Bank Transfer:*",
            f"Account: `{BANK_ACCOUNT}`",
            f"IFSC: `{BANK_IFSC}`",
            f"Bank: {BANK_NAME}\n",
        ]
    lines += [
        "✅ *After payment:*",
        "Reply with your *UTR number* or *payment screenshot*.",
        "Your plan will be activated within 15 minutes.",
        "",
        "⚠️ _Do NOT close this chat after paying._",
    ]
    return "\n".join(lines)



# ── Auto payment (QR, valid PAY_QR_MINUTES) ─────────────────────────────────

def _fmt_lim(v):
    return "Unlimited" if v == -1 else f"{v:,}"


def _order_done(order_id: str) -> bool:
    """Atomically mark an order as processed. True = already processed."""
    db._ensure_settings()
    with db.get_conn() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO settings (k, v) VALUES (?, 'done')",
                           (f"order_{order_id}",))
        return cur.rowcount == 0


async def _activate_order(bot, uid: int, plan: str, order_id: str, chat_id: int, msg_id: int):
    if _order_done(order_id):
        return
    new_key = db.upgrade_plan(uid, plan)
    cfg = db.PLANS[plan]
    try:
        await bot.edit_message_caption(chat_id, msg_id, caption=(
            f"✅ Payment verified\n\nOrder: {order_id}\nPlan: {cfg['name']}"))
    except Exception:
        pass
    await safe_send(bot, uid,
        f"🎉 *Payment verified! Your {cfg['name']} plan has started.*\n\n"
        f"🔑 API key: `{new_key}`\n"
        f"📈 {_fmt_lim(cfg['rpd'])} requests/day  •  {_fmt_lim(cfg['rpm'])} requests/min\n"
        f"📅 Valid for {cfg['validity']} days\n"
        f"🧾 Order ID: `{order_id}`\n\n"
        f"Thank you for your purchase! Use /mykey anytime to see your key.",
        parse_mode=ParseMode.MARKDOWN)
    for admin_id in ADMIN_IDS:
        await safe_send(bot, admin_id,
            f"💰 Auto payment received\nUser: {uid}\nPlan: {plan}\nOrder: {order_id}")


async def _start_autopay(q, ctx, uid: int, plan: str):
    cfg = db.PLANS[plan]
    try:
        amount = autopay.plan_amount(cfg)
        order = await autopay.create_order(amount, f"{cfg['name']} plan - user {uid}",
                                           f"tg{uid}@telegram.user")
    except Exception as e:
        logger.error(f"autopay create: {e}")
        await safe_edit(q, "❌ Could not create the payment right now. Please try again in a minute.",
                        reply_markup=back_kb())
        return
    oid = order["orderId"]
    minutes = autopay.PAY_QR_MINUTES
    deadline = time.time() + minutes * 60
    caption = (
        f"💳 *{cfg['name']} Plan — Payment*\n\n"
        f"📦 *Plan details*\n"
        f"• {_fmt_lim(cfg['rpd'])} requests/day\n"
        f"• {_fmt_lim(cfg['rpm'])} requests/min\n"
        f"• Audio + Video streaming\n"
        f"• Validity: {cfg['validity']} days\n\n"
        f"💰 *Pay exactly: ₹{order['payAmount']}*\n"
        f"_(a few paise are added so we can match your payment automatically)_\n\n"
        f"📷 Scan this QR with any UPI app (GPay, PhonePe, Paytm…)\n"
        f"⏳ This QR is valid for *{minutes} minutes* only.\n"
        f"🧾 Order ID: `{oid}`\n\n"
        f"Your plan starts automatically once the payment is verified."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I Have Paid", callback_data=f"paid_{plan}_{oid}")],
        [InlineKeyboardButton("🔗 Open Payment Page", url=order["payUrl"])],
        [InlineKeyboardButton("❌ Cancel", callback_data="back_main")],
    ])
    msg = await q.message.reply_photo(photo=order["qrImageUrl"], caption=caption,
                                      parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    ctx.bot_data.setdefault("orders", {})[oid] = {
        "uid": uid, "plan": plan, "deadline": deadline,
        "chat": msg.chat_id, "msg": msg.message_id}

    async def paid():
        await _activate_order(ctx.bot, uid, plan, oid, msg.chat_id, msg.message_id)

    async def expired():
        if _order_done(oid):   # paid meanwhile, or already handled
            return
        try:
            await ctx.bot.edit_message_caption(msg.chat_id, msg.message_id, caption=(
                f"⌛ This QR has expired (valid for {minutes} minutes).\n"
                f"Order: {oid}\n\nDo not pay on this QR. Tap /start → Plans to get a new one."))
        except Exception:
            pass

    asyncio.create_task(autopay.watch(oid, deadline, paid, expired))


async def _check_api_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            t0   = time.time()
            resp = await client.get(f"{API_BASE_URL}/")
            ms   = round((time.time() - t0) * 1000)
            if resp.status_code == 200:
                return {"ok": True, "ms": ms}
            return {"ok": False, "ms": ms, "code": resp.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name)
    key_row  = _ensure_key(user.id)
    days     = db.days_remaining(user.id)
    plan     = key_row["plan"]
    plan_cfg = db.PLANS.get(plan, db.PLANS["free"])
    rpd      = plan_cfg["rpd"]
    lim      = "Unlimited" if rpd == -1 else f"{rpd:,}"
    api_url  = APP_URL or API_BASE_URL

    msg = (
        f"👋 *Welcome to {BOT_NAME}!*\n\n"
        f"YouTube Stream API — connect it to your music bot and play songs in voice chat.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 *Your API Key:*\n"
        f"`{key_row['key']}`\n\n"
        f"🌐 *API Endpoint:*\n"
        f"`{api_url}`\n\n"
        f"📦 Plan: *{plan.upper()}* — {plan_cfg['price']}\n"
        f"📊 Limit: *{lim} req/day*  •  {plan_cfg['rpm']} req/min\n"
        f"⏳ Expires in: *{days} days*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*🚀 Music Bot Setup (3 steps):*\n\n"
        f"*Step 1* — Download `betaapi.py`:\n"
        f"Send /getfile to get the file\n\n"
        f"*Step 2* — Add to your bot's `.env`:\n"
        f"`BETAAPI_URL={api_url}`\n"
        f"`BETAAPI_KEY={key_row['key']}`\n\n"
        f"*Step 3* — In your music bot:\n"
        f"`from betaapi import song`\n"
        f"`data = await song(query)`\n"
        f"`stream_url = data['streamLink']`\n\n"
        f"📖 Full docs: {api_url}/docs\n"
        f"❓ Help: /getfile for betaapi.py"
    )
    await update.message.reply_text(
        msg, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb()
    )


# ── /getfile ─────────────────────────────────────────────────────────────────

async def cmd_getfile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Send youtube.py (drop-in replacement) + betaapi.py (direct client).
    Keys are NOT embedded in files — shown only in caption.
    """
    user    = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name)
    key_row = _ensure_key(user.id)
    api_url = APP_URL or API_BASE_URL

    import io, pathlib
    base_dir = pathlib.Path(__file__).parent

    # ── 1. Send youtube.py — the drop-in replacement ──────────────────────
    yt_src = base_dir / "youtube.py"
    if yt_src.exists():
        yt_bytes = yt_src.read_bytes()
    else:
        yt_bytes = b"# youtube.py not found on server"

    yt_obj      = io.BytesIO(yt_bytes)
    yt_obj.name = "youtube.py"

    yt_caption = (
        f"🎵 *youtube.py — Drop-in Replacement*\n\n"
        f"📌 *Place this file in your music bot folder*\n"
        f"_(replace your existing youtube.py file)_\n\n"
        f"*Your credentials:*\n"
        f"🔑 Key: `{key_row['key']}`\n"
        f"🌐 URL: `{api_url}`\n\n"
        f"*Setup (add these values to your .env file):*\n"
        f"```\n"
        f"BETAAPI_URL={api_url}\n"
        f"BETAAPI_KEY={key_row['key']}\n"
        f"```\n\n"
        f"✅ That is all — no other changes are needed\n"
        f"⚠️ Add `.env` to `.gitignore` and never commit your credentials."
    )

    await update.message.reply_document(
        document=yt_obj,
        filename="youtube.py",
        caption=yt_caption,
        parse_mode=ParseMode.MARKDOWN,
    )




# ── /revokekey — user can revoke their own key ─────────────────────────────

async def cmd_revokekey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Allow a user to instantly revoke a leaked or compromised key."""
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name)
    old_row = db.get_active_key(user.id)
    if not old_row:
        await update.message.reply_text(
            "❌ No active key found.", reply_markup=back_kb()
        )
        return
    old_key = old_row["key"]
    new_key = db.revoke_key(user.id)     # deactivates old, creates new same plan
    # Log the revoke event
    db.log_key_audit(user.id, old_key, "user_revoke",
                     ip="telegram", note="User self-revoked via /revokekey")
    await update.message.reply_text(
        f"🔁 *Key Revoked & Replaced!*\n\n"
        f"Old key: `{old_key[:20]}...` ❌ (dead)\n\n"
        f"New key: `{new_key}`\n\n"
        f"⚠️ Update your `.env` file with the new key.\n"
        f"Old key will not work anymore.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )


# ── /revokeuser <uid> — admin can revoke any user key ───────────────────────

async def cmd_revokeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: force revoke any user's key (abuse/compromise)."""
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/revokeuser <user_id> [reason]`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(args[0])
        reason    = " ".join(args[1:]) if len(args) > 1 else "Admin revoked"
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return

    old_row = db.get_active_key(target_id)
    if not old_row:
        await update.message.reply_text(f"❌ No active key for user `{target_id}`.",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    old_key = old_row["key"]
    new_key = db.revoke_key(target_id)
    db.log_key_audit(target_id, old_key, "admin_revoke",
                     ip="admin", note=reason)
    await safe_send(
        ctx.bot, target_id,
        f"⚠️ *Security Notice*\n\n"
        f"Your API key was revoked by admin.\n"
        f"Reason: {reason}\n\n"
        f"New key issued:\n`{new_key}`\n\n"
        f"Update your `.env` file immediately.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.message.reply_text(
        f"✅ User `{target_id}` key revoked.\n"
        f"Old: `{old_key[:20]}...`\nNew: `{new_key}`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /mykey ────────────────────────────────────────────────────────────────────

async def cmd_mykey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name)
    key_row = _ensure_key(user.id)
    await update.message.reply_text(
        _key_card(key_row, user.id),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=key_menu_kb(),
    )


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Checking API status...")
    s   = await _check_api_status()
    if s.get("ok"):
        text = (
            f"✅ *{BOT_NAME} API — Online*\n\n"
            f"Response time: `{s['ms']} ms`\n"
            f"Endpoint: `{API_BASE_URL}`\n\n"
            f"_All systems operational._"
        )
    else:
        err = s.get("error") or f"HTTP {s.get('code', '?')}"
        text = (
            f"❌ *API Status — Issue Detected*\n\n"
            f"Error: `{err}`\n"
            f"Endpoint: `{API_BASE_URL}`\n\n"
            f"_Please contact support._"
        )
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())


# ── /admin ────────────────────────────────────────────────────────────────────

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Access denied.")
        return
    stats = db.get_admin_stats()
    text  = (
        f"👑 *Admin Panel — {BOT_NAME}*\n\n"
        f"👥 Users: `{stats['total_users']}`\n"
        f"🔑 Active Keys: `{stats['active_keys']}`\n"
        f"📈 Requests Today: `{stats['today_requests']}`\n"
        f"💰 Pending Payments: `{stats['pending_payments']}`\n"
        f"🎫 Open Tickets: `{stats['open_tickets']}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_kb())


# ── /premium on|off  (owner) ──────────────────────────────────────────────────

async def cmd_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Owner only.")
        return
    arg = (ctx.args[0].lower() if ctx.args else "")
    if arg not in ("on", "off"):
        state = "ON (paid plans active)" if db.is_premium_mode() else "OFF (sab free & unlimited)"
        await update.message.reply_text(f"Premium mode: {state}\nUse: /premium on  ya  /premium off")
        return
    db.set_premium_mode(arg == "on")
    if arg == "on":
        await update.message.reply_text("✅ Premium ON — ab plans aur limits lagengi.")
    else:
        await update.message.reply_text("✅ Premium OFF — sab users unlimited, new + old sab keys chalengi.")


# ── /setplan  (owner/admin) — plan ki limit/price badlo ───────────────────────
# /setplan <plan> rpm=<n> rpd=<n> validity=<days> price=<text>   (-1 = unlimited)
# /setplan reset   -> default plans wapas

async def cmd_setplan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Owner/Admin only.")
        return
    db.refresh_plans(force=True)
    if not ctx.args:
        lines = ["📋 Current plans (premium " + ("ON" if db.is_premium_mode() else "OFF") + "):"]
        for pid, p in db.PLANS.items():
            rpd = "unlimited" if p["rpd"] == -1 else p["rpd"]
            rpm = "unlimited" if p["rpm"] == -1 else p["rpm"]
            lines.append(f"• {pid}: {rpm}/min, {rpd}/day, {p['validity']} days, {p['price']}")
        lines.append("\nUse: /setplan pro rpm=100 rpd=10000 validity=30 price=₹399/month")
        lines.append("-1 = unlimited  •  /setplan reset = defaults")
        await update.message.reply_text("\n".join(lines))
        return
    if ctx.args[0].lower() == "reset":
        db.reset_plans()
        await update.message.reply_text("✅ Plans reset — default limits wapas.")
        return
    plan, fields = ctx.args[0].lower(), {}
    for a in ctx.args[1:]:
        if "=" not in a:
            continue
        k, v = a.split("=", 1)
        fields[k.lower()] = v
    if not fields:
        await update.message.reply_text("❌ Kuch set nahi kiya. Example: /setplan basic rpm=30 rpd=1000")
        return
    try:
        cfg = db.set_plan(plan, **fields)
    except ValueError:
        await update.message.reply_text("❌ rpm/rpd/validity number hone chahiye.")
        return
    await update.message.reply_text(
        f"✅ Plan '{plan}' updated: {cfg['rpm']}/min, {cfg['rpd']}/day, "
        f"{cfg['validity']} days, {cfg['price']}\n(Limits tabhi lagengi jab /premium on ho.)")


# ── Owner sends a cookies .txt file → saved to cookies/ ───────────────────────

async def on_cookie_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        return
    data = bytes(await (await doc.get_file()).download_as_bytearray())
    text = data.decode("utf-8", "ignore")
    if ".youtube.com" not in text:
        await update.message.reply_text("❌ Yeh YouTube cookies file nahi lagti (Netscape cookies.txt bhejo).")
        return
    lines = [l for l in text.splitlines()
             if l.startswith("#") or not l.strip() or "youtube.com" in l or "google.com" in l]
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies")
    os.makedirs(d, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", doc.file_name)
    with open(os.path.join(d, name), "w") as f:
        f.write("\n".join(lines) + "\n")
    n = len([x for x in os.listdir(d) if x.endswith(".txt")])
    await update.message.reply_text(f"✅ Cookie saved: {name}\nTotal cookie accounts: {n}")


# ── /upgrade <uid> <plan> ─────────────────────────────────────────────────────

async def cmd_upgrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/upgrade <user_id> <plan>`\nPlans: `free` `basic` `pro` `ultra`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        tid      = int(args[0])
        new_plan = args[1].lower()
        new_key  = db.upgrade_plan(tid, new_plan)
        cfg      = db.PLANS[new_plan]
        rpd_str  = "Unlimited" if cfg["rpd"] == -1 else f"{cfg['rpd']:,}"

        await safe_send(
            ctx.bot, tid,
            f"🎉 *Plan Upgraded!*\n\n"
            f"Your plan is now: *{cfg['name']}* ({cfg['price']})\n\n"
            f"🔑 New key:\n`{new_key}`\n\n"
            f"📈 {rpd_str} requests/day  •  {cfg['rpm']} req/min\n"
            f"⏳ Valid for {cfg['validity']} days\n\n"
            f"_Old key has been revoked. Start using the new key now!_",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text(
            f"✅ User `{tid}` → *{new_plan}* plan.\nNew key: `{new_key}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
    except Exception as e:
        logger.error(f"cmd_upgrade: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {e}")


# ── /approve <payment_id> ─────────────────────────────────────────────────────

async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Usage: `/approve <payment_id>`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    try:
        pay_id = int(args[0])
        with db.get_conn() as conn:
            pay = conn.execute(
                "SELECT * FROM payment_requests WHERE id=?", (pay_id,)
            ).fetchone()
        if not pay:
            await update.message.reply_text(f"❌ Payment #{pay_id} not found.")
            return

        new_key = db.upgrade_plan(pay["user_id"], pay["plan"])
        db.resolve_payment(pay_id, "approved")
        cfg     = db.PLANS[pay["plan"]]
        rpd_str = "Unlimited" if cfg["rpd"] == -1 else f"{cfg['rpd']:,}"

        await safe_send(
            ctx.bot, pay["user_id"],
            f"🎉 *Payment Approved!*\n\n"
            f"Plan: *{cfg['name']}* ({cfg['price']})\n\n"
            f"🔑 Your new API key:\n`{new_key}`\n\n"
            f"📈 {rpd_str} req/day  •  {cfg['rpm']} req/min\n"
            f"⏳ Valid: {cfg['validity']} days\n\n"
            f"Thank you for choosing {BOT_NAME}! 🙏",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text(
            f"✅ Payment #{pay_id} approved. User `{pay['user_id']}` upgraded to *{pay['plan']}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"cmd_approve: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {e}")


# ── /reject <payment_id> [reason] ────────────────────────────────────────────

async def cmd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Usage: `/reject <payment_id> [reason]`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    try:
        pay_id = int(args[0])
        reason = " ".join(args[1:]) if len(args) > 1 else "Payment could not be verified."
        with db.get_conn() as conn:
            pay = conn.execute(
                "SELECT * FROM payment_requests WHERE id=?", (pay_id,)
            ).fetchone()
        if not pay:
            await update.message.reply_text(f"❌ Payment #{pay_id} not found.")
            return
        db.resolve_payment(pay_id, "rejected", reason)
        await safe_send(
            ctx.bot, pay["user_id"],
            f"❌ *Payment Not Approved*\n\n"
            f"Reason: {reason}\n\n"
            f"Please contact support: {SUPPORT_GROUP}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text(f"✅ Payment #{pay_id} rejected.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


# ── /payments ─────────────────────────────────────────────────────────────────

async def cmd_payments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    pending = db.get_pending_payments()
    if not pending:
        await update.message.reply_text("✅ No pending payments.")
        return
    text = "💰 *Pending Payments*\n\n"
    for p in pending[:15]:
        uname = f"@{p['username']}" if p["username"] else p["first_name"]
        text += (
            f"*#{p['id']}*  {uname} (`{p['user_id']}`)\n"
            f"Plan: `{p['plan']}`  •  UTR: `{p['utr'] or 'N/A'}`\n"
            f"Date: {str(p['created_at'])[:16]}\n"
            f"`/approve {p['id']}`  or  `/reject {p['id']}`\n\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── /reply <tid> <msg> ────────────────────────────────────────────────────────

async def cmd_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: `/reply <ticket_id> <message>`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    try:
        tid       = int(args[0])
        reply_msg = " ".join(args[1:])
    except ValueError:
        await update.message.reply_text("❌ ticket_id must be a number.")
        return

    db.close_ticket(tid, reply_msg)
    with db.get_conn() as conn:
        ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if ticket:
        await safe_send(
            ctx.bot, ticket["user_id"],
            f"🎫 *Support Reply — Ticket #{tid}*\n\n{reply_msg}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text(f"✅ Replied to ticket #{tid}.")
    else:
        await update.message.reply_text(f"❌ Ticket #{tid} not found.")


# ── Callbacks ─────────────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    data    = q.data or ""
    if not data.startswith("paid_"):
        await q.answer()
    user    = q.from_user
    uid     = user.id

    db.upsert_user(uid, user.username, user.first_name)

    # ── back ──────────────────────────────────────────────────────────────
    if data == "back_main":
        ctx.user_data.pop("awaiting", None)
        key_row = _ensure_key(uid)
        await safe_edit(
            q,
            f"👋 *{BOT_NAME}* — Main Menu\n\n"
            f"🔑 Key: `{key_row['key']}`\n"
            f"Plan: *{key_row['plan'].upper()}*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(),
        )

    # ── view key ──────────────────────────────────────────────────────────
    elif data == "view_key":
        key_row = _ensure_key(uid)
        await safe_edit(q, _key_card(key_row, uid),
                        parse_mode=ParseMode.MARKDOWN, reply_markup=key_menu_kb())

    # ── renew ─────────────────────────────────────────────────────────────
    elif data == "renew_key":
        result = db.renew_key(uid)
        if not result:
            db.create_key(uid, "free")
        key_row = db.get_active_key(uid)
        await safe_edit(
            q,
            f"✅ *Key Renewed!*\n\n" + _key_card(key_row, uid),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=key_menu_kb(),
        )

    # ── revoke ────────────────────────────────────────────────────────────
    elif data == "revoke_key":
        db.revoke_key(uid)
        key_row = db.get_active_key(uid)
        await safe_edit(
            q,
            f"🔁 *New key issued!*\n\n" + _key_card(key_row, uid),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=key_menu_kb(),
        )

    # ── plans ─────────────────────────────────────────────────────────────
    elif data == "plans":
        await safe_edit(q, _plans_text(),
                        parse_mode=ParseMode.MARKDOWN, reply_markup=plans_kb())

    # ── buy plan (commercial flow) ────────────────────────────────────────
    elif data.startswith("paid_"):
        _, plan, oid = data.split("_", 2)
        info = ctx.bot_data.get("orders", {}).get(oid)
        if info and time.time() > info["deadline"]:
            await q.answer("⌛ This QR has expired. Please create a new payment.", show_alert=True)
            return
        st = await autopay.order_status(oid)
        if st == "PAID":
            await q.answer("✅ Payment verified!")
            await _activate_order(ctx.bot, uid, plan, oid, q.message.chat_id, q.message.message_id)
        elif st == "EXPIRED":
            await q.answer("⌛ This payment has expired. Please create a new one.", show_alert=True)
        else:
            await q.answer("⏳ Payment not received yet. It can take up to a minute — "
                           "we will message you automatically as soon as it is verified.",
                           show_alert=True)
        return

    elif data in ("buy_basic", "buy_pro", "buy_ultra"):
        plan = data.replace("buy_", "")
        if autopay.enabled():
            await _start_autopay(q, ctx, uid, plan)
            return
        ctx.user_data["buying_plan"] = plan
        ctx.user_data["awaiting"]    = f"utr_{plan}"
        await safe_edit(
            q,
            _payment_info(plan),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
            ]),
        )

    # ── upgrade request (free → sends request to admin) ───────────────────
    elif data == "upgrade_request":
        key_row      = db.get_active_key(uid)
        current_plan = key_row["plan"] if key_row else "free"
        uname        = f"@{user.username}" if user.username else user.first_name
        for admin_id in ADMIN_IDS:
            await safe_send(
                ctx.bot, admin_id,
                f"⬆️ *Upgrade Request*\n\n"
                f"User: {uname}\nID: `{uid}`\n"
                f"Current: `{current_plan}`\n\n"
                f"`/upgrade {uid} basic`\n"
                f"`/upgrade {uid} pro`\n"
                f"`/upgrade {uid} ultra`",
                parse_mode=ParseMode.MARKDOWN,
            )
        await safe_edit(
            q,
            f"⬆️ *Request Sent!*\n\n"
            f"Admin will contact you shortly.\n"
            f"Or tap *Plans & Pricing* to pay directly.\n\n"
            f"Support: {SUPPORT_GROUP}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )

    # ── usage ─────────────────────────────────────────────────────────────
    elif data == "my_usage":
        key_row  = _ensure_key(uid)
        stats    = db.get_usage_stats(uid)
        days     = db.days_remaining(uid)
        plan     = key_row["plan"]
        cfg      = db.PLANS.get(plan, db.PLANS["free"])
        rpd      = cfg["rpd"]
        lim      = "Unlimited" if rpd == -1 else f"{rpd:,}"
        rem      = "Unlimited" if rpd == -1 else str(max(0, rpd - stats["today"]))

        await safe_edit(
            q,
            f"📊 *Usage Stats*\n\n"
            f"Plan: *{plan.upper()}* — {cfg['price']}\n"
            f"Key: `{key_row['key'][:20]}...`\n"
            f"Expires in: *{days} days*\n\n"
            f"*Today:*\n"
            f"  Used: {stats['today']} / {lim}\n"
            f"  Remaining: {rem}\n"
            f"  🎵 Audio: {stats['today_audio']}  🎬 Video: {stats['today_video']}\n\n"
            f"*All-Time:*\n"
            f"  Total: {stats['total']}\n"
            f"  🎵 Audio: {stats['total_audio']}  🎬 Video: {stats['total_video']}\n\n"
            f"Last used: {stats['last_used']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )

    # ── help ──────────────────────────────────────────────────────────────
    elif data == "help":
        await safe_edit(
            q,
            f"❓ *{BOT_NAME} — Quick Guide*\n\n"
            f"*1. Get your API key*\n"
            f"   Tap 🔑 My API Key — auto-generated, valid 28 days.\n\n"
            f"*2. Use the API*\n"
            f"   `GET {API_BASE_URL}/stream?url=YT_URL&api_key=YOUR_KEY`\n\n"
            f"*3. Endpoints*\n"
            f"   `/stream` — Universal audio stream\n"
            f"   `/song` — AnonXMusic/Yukki style\n"
            f"   `/api/stream` — Audio or video\n"
            f"   `/search` — YouTube search\n"
            f"   `/info` — Video metadata\n\n"
            f"*4. Full Docs*\n"
            f"   {APP_URL}/docs\n\n"
            f"*5. Renew / Replace key*\n"
            f"   🔄 Renew extends by 30 days.\n"
            f"   🔁 Revoke issues a brand-new key.\n\n"
            f"*6. Upgrade*\n"
            f"   Tap 💎 Plans & Pricing for paid options.\n\n"
            f"⚠️ Never share your API key publicly.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )

    # ── support ticket ────────────────────────────────────────────────────
    elif data == "support_ticket":
        ctx.user_data["awaiting"] = "ticket"
        await safe_edit(
            q,
            "🎫 *Support Ticket*\n\nDescribe your issue in detail:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
            ]),
        )

    # ── feedback ──────────────────────────────────────────────────────────
    elif data == "feedback":
        ctx.user_data["awaiting"] = "feedback"
        await safe_edit(
            q,
            "💬 *Feedback*\n\nShare your thoughts or suggestions:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
            ]),
        )

    # ── admin: stats ──────────────────────────────────────────────────────
    elif data == "admin_stats":
        if not is_admin(uid):
            await q.answer("❌ Access denied.", show_alert=True)
            return
        s  = db.get_admin_stats()
        pc = s["plan_counts"]
        await safe_edit(
            q,
            f"📊 *Admin Stats — {BOT_NAME}*\n\n"
            f"👥 Total Users     : `{s['total_users']}`\n"
            f"🔑 Active Keys     : `{s['active_keys']}`\n"
            f"📈 Today Requests  : `{s['today_requests']}`\n"
            f"📊 Total Requests  : `{s['total_requests']}`\n"
            f"💰 Pending Payments: `{s['pending_payments']}`\n"
            f"🎫 Open Tickets    : `{s['open_tickets']}`\n\n"
            f"*Plan Distribution:*\n"
            f"🆓 Free  : `{pc.get('free',0)}`\n"
            f"⚡ Basic : `{pc.get('basic',0)}`\n"
            f"🚀 Pro   : `{pc.get('pro',0)}`\n"
            f"👑 Ultra : `{pc.get('ultra',0)}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_kb(),
        )

    # ── admin: pending payments ───────────────────────────────────────────
    elif data == "admin_payments":
        if not is_admin(uid):
            await q.answer("❌ Access denied.", show_alert=True)
            return
        pending = db.get_pending_payments()
        if not pending:
            await safe_edit(q, "✅ No pending payments.", reply_markup=admin_kb())
            return
        text = "💰 *Pending Payments*\n\n"
        for p in pending[:10]:
            uname = f"@{p['username']}" if p["username"] else p["first_name"]
            text += (
                f"*#{p['id']}*  {uname} (`{p['user_id']}`)\n"
                f"Plan: `{p['plan']}`  UTR: `{p['utr'] or 'N/A'}`\n"
                f"`/approve {p['id']}`  `/reject {p['id']}`\n\n"
            )
        await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_kb())

    # ── admin: tickets ────────────────────────────────────────────────────
    elif data == "admin_tickets":
        if not is_admin(uid):
            await q.answer("❌ Access denied.", show_alert=True)
            return
        tickets = db.get_open_tickets()
        if not tickets:
            await safe_edit(q, "✅ No open tickets.", reply_markup=admin_kb())
            return
        text = "🎫 *Open Tickets*\n\n"
        for t in tickets[:10]:
            uname = t["username"] or t["first_name"] or str(t["user_id"])
            text += (
                f"*#{t['id']}*  @{uname}\n"
                f"`{str(t['message'])[:100]}`\n"
                f"`/reply {t['id']} <message>`\n\n"
            )
        await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_kb())

    # ── admin: broadcast ──────────────────────────────────────────────────
    elif data == "admin_broadcast":
        if not is_admin(uid):
            await q.answer("❌ Access denied.", show_alert=True)
            return
        ctx.user_data["awaiting"] = "broadcast"
        await safe_edit(
            q,
            "📢 *Broadcast*\n\nType your message (supports Markdown):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
            ]),
        )

    # ── test stream ───────────────────────────────────────────────────────
    elif data in ("test_audio", "test_video"):
        media_type = "audio" if data == "test_audio" else "video"
        test_url   = ctx.user_data.get("test_url", "")
        if not test_url:
            await safe_edit(q, "❌ No URL. Send a YouTube link first.", reply_markup=back_kb())
            return

        key_row = db.get_active_key(uid)
        if not key_row:
            await safe_edit(q, "❌ No active key.", reply_markup=back_kb())
            return

        await safe_edit(q, f"⏳ Fetching {media_type} stream...")

        try:
            is_url  = test_url.startswith("http")
            params  = {"url" if is_url else "query": test_url, "type": media_type}
            headers = {"x-api-key": key_row["key"]}
            t0      = time.time()
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.get(f"{API_BASE_URL}/api/stream",
                                        params=params, headers=headers)
                resp.raise_for_status()
                result = resp.json()
            elapsed = round(time.time() - t0, 2)

            stream_url = result.get("stream_url") or result.get("direct_url", "")
            text = (
                f"✅ *Stream Ready* `({elapsed}s)`\n\n"
                f"*{result.get('title','Unknown')}*\n"
                f"👤 {result.get('uploader','')}\n"
                f"⏱ {fmt_dur(result.get('duration',0))}\n\n"
                f"📦 Format: `{result.get('ext','')}`\n"
                f"💎 Plan: `{result.get('plan','free')}`\n\n"
                f"🔗 Stream URL:\n`{stream_url[:180]}...`"
            )
            thumb = result.get("thumbnail", "")
            if thumb:
                try:
                    thumb = _normalise_base_url(thumb)
                except ValueError:
                    thumb = ""
            if thumb:
                try:
                    await q.message.reply_photo(photo=thumb, caption=text,
                                                parse_mode=ParseMode.MARKDOWN,
                                                reply_markup=back_kb())
                    await q.message.delete()
                    return
                except Exception:
                    pass
            await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())

        except httpx.HTTPStatusError as e:
            err = str(e)
            try:
                d = e.response.json().get("detail", {})
                err = d.get("message", err) if isinstance(d, dict) else str(d)
            except Exception:
                pass
            await safe_edit(q, f"❌ *API Error*\n\n`{err[:300]}`",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        except httpx.TimeoutException:
            await safe_edit(q, "❌ *Timeout.* Extraction took too long. Try again.",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        except Exception as e:
            logger.error(f"test_stream: {e}", exc_info=True)
            await safe_edit(q, f"❌ Error: `{str(e)[:200]}`",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())


# ── Message handler ───────────────────────────────────────────────────────────

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user     = update.effective_user
    uid      = user.id
    text     = update.message.text.strip()
    awaiting = ctx.user_data.get("awaiting", "")

    db.upsert_user(uid, user.username, user.first_name)

    # ── UTR / payment screenshot ──────────────────────────────────────────
    if awaiting and awaiting.startswith("utr_"):
        plan = awaiting.replace("utr_", "")
        ctx.user_data.pop("awaiting", None)
        cfg    = db.PLANS.get(plan, db.PLANS["basic"])
        pay_id = db.create_payment_request(uid, plan, utr=text[:100])
        uname  = f"@{user.username}" if user.username else user.first_name

        for admin_id in ADMIN_IDS:
            await safe_send(
                ctx.bot, admin_id,
                f"💰 *New Payment Request #{pay_id}*\n\n"
                f"User: {uname} (`{uid}`)\n"
                f"Plan: *{cfg['name']}* — {cfg['price']}\n"
                f"UTR/Note: `{text[:200]}`\n\n"
                f"`/approve {pay_id}`  or  `/reject {pay_id} <reason>`",
                parse_mode=ParseMode.MARKDOWN,
            )
        await update.message.reply_text(
            f"✅ *Payment Request Submitted!*\n\n"
            f"Plan: *{cfg['name']}*\n"
            f"Reference ID: `#{pay_id}`\n\n"
            f"Admin will verify and activate within 15 minutes.\n"
            f"For instant support: {SUPPORT_GROUP}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return

    # ── Support ticket ────────────────────────────────────────────────────
    if awaiting == "ticket":
        ctx.user_data.pop("awaiting", None)
        tid   = db.create_ticket(uid, text)
        uname = f"@{user.username}" if user.username else user.first_name
        for admin_id in ADMIN_IDS:
            await safe_send(
                ctx.bot, admin_id,
                f"🎫 *Ticket #{tid}*\n\nFrom: {uname} (`{uid}`)\n\n`{text[:500]}`\n\n"
                f"`/reply {tid} <message>`",
                parse_mode=ParseMode.MARKDOWN,
            )
        await update.message.reply_text(
            f"✅ *Ticket #{tid} submitted!*\n\nWe'll reply here directly.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return

    # ── Feedback ──────────────────────────────────────────────────────────
    if awaiting == "feedback":
        ctx.user_data.pop("awaiting", None)
        db.save_feedback(uid, text)
        uname = f"@{user.username}" if user.username else user.first_name
        for admin_id in ADMIN_IDS:
            await safe_send(
                ctx.bot, admin_id,
                f"💬 *Feedback*\nFrom: {uname} (`{uid}`)\n\n`{text[:500]}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        await update.message.reply_text(
            "💬 *Thank you for the feedback!*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return

    # ── Admin broadcast ───────────────────────────────────────────────────
    if awaiting == "broadcast" and is_admin(uid):
        ctx.user_data.pop("awaiting", None)
        users  = db.get_all_users()
        sent = failed = 0
        for u in users:
            try:
                await ctx.bot.send_message(
                    u["user_id"], text, parse_mode=ParseMode.MARKDOWN
                )
                sent += 1
                await asyncio.sleep(0.04)
            except Exception:
                failed += 1
        await update.message.reply_text(
            f"📢 Broadcast done.\n✅ Sent: {sent}  ❌ Failed: {failed}",
            reply_markup=admin_kb(),
        )
        return

    # ── YouTube URL / ID / search query ───────────────────────────────────
    is_yt_url = "youtube.com" in text or "youtu.be" in text
    is_yt_id  = len(text) == 11 and text.replace("-", "").replace("_", "").isalnum()

    if is_yt_url or is_yt_id:
        if is_yt_url:
            try:
                resolved = _normalise_base_url(text)
            except ValueError:
                resolved = text
        else:
            resolved = f"https://www.youtube.com/watch?v={text}"
        ctx.user_data["test_url"] = resolved
        await update.message.reply_text(
            f"🎬 *Test API*\n\n`{resolved}`\n\nChoose stream type:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=test_type_kb(),
        )
        return

    if len(text) >= 3 and not text.startswith("/"):
        ctx.user_data["test_url"] = text
        await update.message.reply_text(
            f"🔍 *Search & Test*\n\n`{text}`\n\nChoose stream type:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=test_type_kb(),
        )
        return

    # ── Default ───────────────────────────────────────────────────────────
    key_row = _ensure_key(uid)
    await update.message.reply_text(
        f"👋 *{BOT_NAME}*\n\n"
        f"🔑 Key: `{key_row['key']}`\n"
        f"Plan: *{key_row['plan'].upper()}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


# ── Bot commands menu ─────────────────────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("start",      "Start & get your API key"),
    BotCommand("mykey",      "View your current API key"),
    BotCommand("getfile",    "Download youtube.py for your music bot"),
    BotCommand("revokekey",  "🔒 Key leaked? Revoke & get new one instantly"),
    BotCommand("status",     "Check API server status"),
    BotCommand("help",       "How to use the API"),
    BotCommand("admin",      "Admin panel (admin only)"),
    BotCommand("upgrade",    "Upgrade user plan (admin only)"),
    BotCommand("revokeuser", "Force revoke user key (admin only)"),
    BotCommand("approve",    "Approve payment (admin only)"),
    BotCommand("reject",     "Reject payment (admin only)"),
    BotCommand("payments",   "Pending payments (admin only)"),
    BotCommand("reply",      "Reply to support ticket (admin only)"),
    BotCommand("premium",    "Premium on/off (owner only)"),
    BotCommand("setplan",    "Plan limits/price badlo (owner)"),
]


# ── Runner ────────────────────────────────────────────────────────────────────

async def run_bot():
    logger.info(f"Starting {BOT_NAME} Bot | API: {API_BASE_URL}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("mykey",      cmd_mykey))
    app.add_handler(CommandHandler("getfile",    cmd_getfile))
    app.add_handler(CommandHandler("revokekey",  cmd_revokekey))
    app.add_handler(CommandHandler("revokeuser", cmd_revokeuser))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("help",     cmd_start))
    app.add_handler(CommandHandler("admin",    cmd_admin))
    app.add_handler(CommandHandler("upgrade",  cmd_upgrade))
    app.add_handler(CommandHandler("approve",  cmd_approve))
    app.add_handler(CommandHandler("reject",   cmd_reject))
    app.add_handler(CommandHandler("payments", cmd_payments))
    app.add_handler(CommandHandler("reply",    cmd_reply))
    app.add_handler(CommandHandler("premium",  cmd_premium))
    app.add_handler(CommandHandler("setplan",  cmd_setplan))
    app.add_handler(MessageHandler(filters.Document.ALL, on_cookie_file))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    async with app:
        await app.bot.set_my_commands(BOT_COMMANDS)
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        logger.info(f"{BOT_NAME} Bot running.")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
