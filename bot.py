import os
import time
import json
import base64
import hmac
import hashlib
import requests
import datetime
import smtplib
from email.message import EmailMessage
from typing import Optional

# ======================
# CONFIG
# ======================
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15")
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS", "20"))

KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64")

EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_TLS = os.getenv("SMTP_TLS", "true").lower() == "true"
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

BASE_URL = "https://api.kalshi.com/trade-api/v2"

print("=== BOT STARTED ===")
print(f"ENABLE_TRADING={ENABLE_TRADING}")
print(f"POLL_SECONDS={POLL_SECONDS}")
print(f"SERIES_PREFIX={SERIES_PREFIX}")

# ======================
# EMAIL
# ======================
def send_email(subject: str, body: str):
    if not EMAIL_ENABLED:
        return
    try:
        msg = EmailMessage()
        msg["From"] = SMTP_USERNAME
        msg["To"] = SMTP_USERNAME
        msg["Subject"] = subject
        msg.set_content(body)

        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        if SMTP_TLS:
            server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"EMAIL FAILED: {e}")

# ======================
# AUTH
# ======================
def load_private_key() -> Optional[bytes]:
    if not KALSHI_PRIVATE_KEY_B64:
        print("❌ Missing KALSHI_PRIVATE_KEY_PEM_BASE64")
        return None
    try:
        return base64.b64decode(KALSHI_PRIVATE_KEY_B64)
    except Exception as e:
        print(f"❌ Private key decode failed: {e}")
        return None

PRIVATE_KEY_BYTES = load_private_key()

def sign_request(timestamp: str, method: str, path: str, body: str = "") -> str:
    msg = f"{timestamp}{method}{path}{body}".encode()
    return hmac.new(PRIVATE_KEY_BYTES, msg, hashlib.sha256).hexdigest()

def kalshi_request(method: str, path: str, body: dict = None):
    if not KALSHI_API_KEY_ID or not PRIVATE_KEY_BYTES:
        raise RuntimeError("Kalshi credentials missing")

    ts = str(int(time.time() * 1000))
    body_json = json.dumps(body) if body else ""
    sig = sign_request(ts, method, path, body_json)

    headers = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }

    url = BASE_URL + path
    resp = requests.request(method, url, headers=headers, data=body_json)

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

    return resp.json()

# ======================
# MARKET HELPERS
# ======================
def current_market_ticker():
    now = datetime.datetime.utcnow().replace(second=0, microsecond=0)
    minute = (now.minute // 15) * 15
    market_time = now.replace(minute=minute)
    return f"{SERIES_PREFIX}-{market_time.strftime('%d%b%y%H%M').upper()}"

def get_orderbook(ticker):
    return kalshi_request("GET", f"/markets/{ticker}/orderbook")

def get_balance():
    data = kalshi_request("GET", "/portfolio/balance")
    return float(data["available_cash"])

# ======================
# STRATEGY
# ======================
daily_start_balance = None
daily_loss = 0.0

def check_daily_limits(balance):
    global daily_start_balance, daily_loss
    if daily_start_balance is None:
        daily_start_balance = balance
        return True

    daily_loss = max(0, daily_start_balance - balance)
    loss_pct = (daily_loss / daily_start_balance) * 100

    if loss_pct >= MAX_DAILY_LOSS_PCT:
        send_email("BOT STOPPED", f"Daily loss limit hit: {loss_pct:.2f}%")
        print("🛑 Daily loss limit reached")
        return False

    return True

def maybe_trade():
    ticker = current_market_ticker()
    print(f"CHECKING MARKET: {ticker}")

    book = get_orderbook(ticker)
    yes = sum(o["quantity"] for o in book.get("yes", []))
    no = sum(o["quantity"] for o in book.get("no", []))

    total = yes + no
    if total == 0:
        print("No liquidity yet")
        return

    dominance = max(yes, no) / total
    side = "no" if yes > no else "yes"

    if dominance < 0.95:
        print(f"Skips: dominance {dominance:.2%}")
        return

    balance = get_balance()
    if not check_daily_limits(balance):
        return

    stake = round(balance * 0.01, 2)
    print(f"TRADE SIGNAL → {side.upper()} stake=${stake}")

    if not ENABLE_TRADING:
        print("READ-ONLY MODE")
        return

    kalshi_request(
        "POST",
        "/orders",
        {
            "market_ticker": ticker,
            "side": side,
            "type": "market",
            "quantity": stake,
        },
    )

    send_email("TRADE EXECUTED", f"{ticker} → {side.upper()} ${stake}")

# ======================
# LOOP
# ======================
while True:
    try:
        maybe_trade()
    except Exception as e:
        print(f"ERROR: {e}")
        send_email("BOT ERROR", str(e))
    time.sleep(POLL_SECONDS)