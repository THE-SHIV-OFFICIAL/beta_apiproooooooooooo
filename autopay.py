"""Auto UPI payment gateway (BETA BOT HUB) for the Telegram bot.

Flow: user taps Buy -> bot creates an order -> sends QR (valid PAY_QR_MINUTES,
default 10) with plan details and an "I Have Paid" button -> bot checks the
gateway every few seconds -> on PAID the plan is activated and the user is told.
"""
import os, re, time, asyncio, logging
import httpx

logger = logging.getLogger("autopay")
PAY_API_URL = os.getenv("PAY_API_URL", "https://betapaymentgateway.up.railway.app").rstrip("/")
PAY_QR_MINUTES = int(os.getenv("PAY_QR_MINUTES", "10"))
PAY_POLL_SECONDS = int(os.getenv("PAY_POLL_SECONDS", "8"))


def enabled() -> bool:
    return bool(os.getenv("PAY_API_KEY", "").strip())


def _headers():
    return {"Authorization": f"Bearer {os.getenv('PAY_API_KEY', '').strip()}",
            "Content-Type": "application/json"}


def plan_amount(cfg: dict) -> float:
    """Amount from plan['amount'] or the first number inside plan['price']."""
    if cfg.get("amount"):
        return float(cfg["amount"])
    m = re.search(r"\d+(?:\.\d+)?", str(cfg.get("price", "")).replace(",", ""))
    if not m:
        raise ValueError("Plan has no price amount")
    return float(m.group())


async def create_order(amount: float, note: str, email: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{PAY_API_URL}/api/v1/payments/create", headers=_headers(),
                         json={"amount": amount, "customerEmail": email, "note": note})
    d = r.json()
    if r.status_code >= 400 or not d.get("success"):
        raise RuntimeError(d.get("message") or d.get("error") or f"Gateway error {r.status_code}")
    return d["order"]


async def order_status(order_id: str) -> str:
    """Returns PAID, PENDING or EXPIRED ('' on network error)."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{PAY_API_URL}/api/v1/payments/{order_id}", headers=_headers())
        d = r.json()
        if d.get("verified"):
            return "PAID"
        return (d.get("order") or {}).get("status", "")
    except Exception as e:
        logger.warning(f"status check failed: {e}")
        return ""


async def watch(order_id: str, deadline: float, on_paid, on_expired):
    """Poll until paid or the QR's validity window ends."""
    while time.time() < deadline:
        st = await order_status(order_id)
        if st == "PAID":
            await on_paid()
            return
        if st == "EXPIRED":
            break
        await asyncio.sleep(PAY_POLL_SECONDS)
    # last check in case the payment landed right at the end
    if await order_status(order_id) == "PAID":
        await on_paid()
    else:
        await on_expired()
