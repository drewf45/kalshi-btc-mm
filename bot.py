import os
import time
import json
import base64
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

# ============================================================
# LOGGING
# ============================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-bot")

# ============================================================
# ENV / CONFIG
# ============================================================

# READ vs WRITE API SPLIT (CRITICAL FIX)
READ_BASE = "https://trading-api.kalshi.com"
WRITE_BASE = "https://api.elections.kalshi.com"

KEY_ID = os.environ["KALSHI_KEY_ID"]
PRIVATE_KEY_B64 = os.environ["KALSHI_PRIVATE_KEY_B64"]
SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT", None)

SERIES_PREFIX = os.environ["SERIES_PREFIX"]      # KXBTC15m
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))

BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))
BASE_SIZE = int(os.getenv("ORDER_USD_PER_SIDE", "1"))

POST_ONLY = os.getenv("POST_ONLY", "true").lower() == "true"

# ============================================================
# HELPERS
# ============================================================

def now_ms() -> int:
    return int(time.time() * 1000)

def sign_request(private_key, method, path, body):
    ts = str(now_ms())
    payload = f"{ts}{method}{path}{body}".encode()
    sig = private_key.sign(
        payload,
        asy_padding.PKCS1v15(),
        hashes.SHA256()
    )
    return ts, base64.b64encode(sig).decode()

def load_private_key():
    key_bytes = base64.b64decode(PRIVATE_KEY_B64)
    return serialization.load_pem_private_key(key_bytes, password=None)

PRIVATE_KEY = load_private_key()
log.info("[BOOT] Private key loaded OK (b64)")

# ============================================================
# HTTP WRAPPERS
# ============================================================

def kalshi_get(path):
    url = READ_BASE + path
    headers = {
        "KALSHI-ACCESS-KEY": KEY_ID,
    }
    r = requests.get(url, headers=headers, timeout=10)
    log.info(f"[REQ] GET {path} -> {r.status_code}")
    r.raise_for_status()
    return r.json()

def kalshi_post(path, body):
    body_json = json.dumps(body)
    ts, sig = sign_request(PRIVATE_KEY, "POST", path, body_json)

    headers = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }

    if SUBACCOUNT:
        headers["KALSHI-ACCESS-SUBACCOUNT"] = SUBACCOUNT

    url = WRITE_BASE + path
    r = requests.post(url, headers=headers, data=body_json, timeout=10)
    log.info(f"[REQ] POST {path} -> {r.status_code}")

    data = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"Order failed: {data}")
    return data

# ============================================================
# MARKET RESOLUTION (ONCE)
# ============================================================

def resolve_active_market() -> Optional[str]:
    markets = kalshi_get(
        f"/trade-api/v2/markets?series_ticker={SERIES_PREFIX}&status=open&limit=50"
    )["markets"]

    for m in markets:
        if m["status"] == "open":
            return m["ticker"]

    return None

# ============================================================
# ORDERBOOK
# ============================================================

def get_yes_best_ask(ticker: str) -> Optional[int]:
    ob = kalshi_get(f"/trade-api/v2/markets/{ticker}/orderbook")
    asks = ob.get("orderbook", {}).get("yes", {}).get("asks", [])
    if not asks:
        return None
    return int(asks[0]["price"])

# ============================================================
# TRADING
# ============================================================

def place_yes_buy(ticker: str, price: int, size: int):
    order = {
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "price": price,
        "count": size,
        "post_only": POST_ONLY,
    }
    return kalshi_post("/trade-api/v2/portfolio/orders", order)

def has_open_position(ticker: str) -> bool:
    pos = kalshi_get("/trade-api/v2/portfolio/positions")
    for p in pos.get("positions", []):
        if p["ticker"] == ticker and p["position"] != 0:
            return True
    return False

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    log.info("[BOOT] BOT STARTED")
    log.info(f"[BOOT] READ_BASE={READ_BASE}")
    log.info(f"[BOOT] WRITE_BASE={WRITE_BASE}")
    log.info(f"[BOOT] SERIES_PREFIX={SERIES_PREFIX}")

    active_ticker = resolve_active_market()
    if not active_ticker:
        log.warning("No active market found")
        return

    log.info(f"[MARKET] Active ticker: {active_ticker}")

    while True:
        try:
            if has_open_position(active_ticker):
                time.sleep(POLL_SECONDS)
                continue

            best_ask = get_yes_best_ask(active_ticker)
            if best_ask is None:
                time.sleep(POLL_SECONDS)
                continue

            if best_ask <= BUY_PRICE_CENTS:
                place_yes_buy(active_ticker, BUY_PRICE_CENTS, BASE_SIZE)

        except Exception as e:
            log.error(f"[LOOPERR] {e}")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()