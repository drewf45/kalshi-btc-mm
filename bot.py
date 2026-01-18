import os
import time
import json
import base64
import hashlib
import hmac
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict

# =========================
# CONFIG
# =========================
API_BASE = "https://api.kalshi.com/trade-api/v2"

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64")

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "False").lower() == "true"

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

SERIES_PREFIX = "KXBTC15M"

MAX_DAILY_LOSS_PCT = 0.20      # 20%
TRADE_RISK_PCT = 0.01          # 1%
IMBALANCE_THRESHOLD = 0.95     # 95%

# =========================
# GLOBAL STATE
# =========================
daily_start_equity = None
daily_pnl = 0.0
last_day = None

# =========================
# UTILS
# =========================
def log(msg):
    print(msg, flush=True)

def utc_now():
    return datetime.now(timezone.utc)

def base64_decode_key():
    raw = base64.b64decode(PRIVATE_KEY_B64)
    return raw

# =========================
# AUTH SIGNING (CORRECT)
# =========================
def sign_request(method: str, path: str, body: str, timestamp: str) -> str:
    msg = f"{timestamp}{method}{path}{body}".encode()
    key = base64_decode_key()
    return base64.b64encode(
        hmac.new(key, msg, hashlib.sha256).digest()
    ).decode()

def auth_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    ts = str(int(time.time()))
    sig = sign_request(method, path, body, ts)
    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json"
    }

# =========================
# API WRAPPER
# =========================
def kalshi_request(method, path, payload=None):
    body = json.dumps(payload) if payload else ""
    headers = auth_headers(method, path, body)
    url = API_BASE + path

    resp = requests.request(method, url, headers=headers, data=body)

    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} {resp.text}")

    return resp.json()

# =========================
# MARKET SELECTION
# =========================
def current_15m_market():
    now = utc_now().replace(second=0, microsecond=0)
    minute = (now.minute // 15) * 15
    bucket = now.replace(minute=minute)

    market_time = bucket.strftime("%d%b%y%H%M").upper()
    ticker = f"{SERIES_PREFIX}-{market_time}"

    return ticker

# =========================
# ACCOUNT
# =========================
def fetch_balance():
    data = kalshi_request("GET", "/portfolio/balance")
    # API returns number, not dict
    return float(data["balance"])

# =========================
# MARKET DATA
# =========================
def fetch_market(ticker):
    data = kalshi_request("GET", f"/markets/{ticker}")
    return data["market"]

def orderbook(market):
    yes = market["yes_asks"][0]["price"] if market["yes_asks"] else None
    no = market["no_asks"][0]["price"] if market["no_asks"] else None
    return yes, no

def imbalance(market):
    yes_vol = market["volume_yes"]
    no_vol = market["volume_no"]
    total = yes_vol + no_vol
    if total == 0:
        return None
    return yes_vol / total

# =========================
# TRADING LOGIC
# =========================
def place_order(ticker, side, price, quantity):
    if not ENABLE_TRADING:
        log("READ-ONLY MODE: trade skipped")
        return

    payload = {
        "ticker": ticker,
        "side": side,
        "type": "limit",
        "price": price,
        "quantity": quantity
    }

    kalshi_request("POST", "/orders", payload)
    log(f"ORDER PLACED: {side} {quantity} @ {price}")

# =========================
# MAIN LOOP
# =========================
def main():
    global daily_start_equity, daily_pnl, last_day

    log("=== BOT STARTED ===")
    log(f"ENABLE_TRADING={ENABLE_TRADING}")
    log(f"POLL_SECONDS={POLL_SECONDS}")
    log(f"SERIES_PREFIX={SERIES_PREFIX}")

    while True:
        try:
            now = utc_now().date()

            if last_day != now:
                last_day = now
                daily_start_equity = fetch_balance()
                daily_pnl = 0.0
                log(f"New trading day. Equity={daily_start_equity}")

            balance = fetch_balance()
            daily_pnl = balance - daily_start_equity

            if daily_pnl <= -daily_start_equity * MAX_DAILY_LOSS_PCT:
                log("DAILY LOSS LIMIT HIT — STOPPING TRADES")
                time.sleep(POLL_SECONDS)
                continue

            ticker = current_15m_market()
            log(f"CHECKING MARKET: {ticker}")

            market = fetch_market(ticker)

            imb = imbalance(market)
            if imb is None:
                log("No liquidity yet")
                time.sleep(POLL_SECONDS)
                continue

            yes_price, no_price = orderbook(market)

            trade_size = int((balance * TRADE_RISK_PCT) / 1)

            if imb >= IMBALANCE_THRESHOLD and no_price:
                place_order(ticker, "no", no_price, trade_size)

            elif imb <= (1 - IMBALANCE_THRESHOLD) and yes_price:
                place_order(ticker, "yes", yes_price, trade_size)
            else:
                log("No edge")

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(POLL_SECONDS)

# =========================
# ENTRY
# =========================
if __name__ == "__main__":
    main()