import os
import time
import json
import base64
import uuid
import logging
from datetime import datetime
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
# API BASES (FINAL)
# ============================================================

READ_BASE  = "https://trading-api.kalshi.com"
WRITE_BASE = "https://api.elections.kalshi.com"


# ============================================================
# ENV
# ============================================================

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID")
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64")
KALSHI_SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT")

SERIES_PREFIX = os.getenv("SERIES_PREFIX")  # MUST be set in Render

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))
POST_ONLY = os.getenv("POST_ONLY", "true").lower() == "true"


# ============================================================
# VALIDATION
# ============================================================

if not SERIES_PREFIX:
    raise RuntimeError("SERIES_PREFIX env var is required")

if not KALSHI_KEY_ID or not KALSHI_PRIVATE_KEY_B64:
    raise RuntimeError("Missing Kalshi credentials")


# ============================================================
# AUTH
# ============================================================

def now_ms() -> int:
    return int(time.time() * 1000)

def load_private_key():
    key_bytes = base64.b64decode(KALSHI_PRIVATE_KEY_B64)
    return serialization.load_pem_private_key(key_bytes, password=None)

PRIVATE_KEY = load_private_key()
log.info("[BOOT] Private key loaded OK (b64)")

def sign_request(method: str, path: str, body: str):
    ts = str(now_ms())
    payload = f"{ts}{method}{path}{body}".encode()
    sig = PRIVATE_KEY.sign(
        payload,
        asy_padding.PKCS1v15(),
        hashes.SHA256()
    )
    return ts, base64.b64encode(sig).decode()


# ============================================================
# HTTP HELPERS
# ============================================================

def kalshi_get(path: str):
    url = READ_BASE + path
    headers = {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
    }
    r = requests.get(url, headers=headers, timeout=10)
    log.info("[REQ] GET %s -> %s", path, r.status_code)
    r.raise_for_status()
    return r.json()

def kalshi_post(path: str, body: dict):
    body_json = json.dumps(body, separators=(",", ":"))
    ts, sig = sign_request("POST", path, body_json)

    headers = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }

    if KALSHI_SUBACCOUNT:
        headers["KALSHI-ACCESS-SUBACCOUNT"] = KALSHI_SUBACCOUNT

    url = WRITE_BASE + path
    r = requests.post(url, headers=headers, data=body_json, timeout=10)
    log.info("[REQ] POST %s -> %s", path, r.status_code)

    try:
        data = r.json()
    except Exception:
        data = r.text

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Order failed: {data}")

    return data


# ============================================================
# MARKET RESOLUTION (ONCE)
# ============================================================

def resolve_active_market() -> Optional[str]:
    resp = kalshi_get(
        f"/trade-api/v2/markets?series_ticker={SERIES_PREFIX}&status=open&limit=50"
    )
    markets = resp.get("markets", [])
    for m in markets:
        if m.get("status") == "open":
            return m["ticker"]
    return None


# ============================================================
# ORDERBOOK
# ============================================================

def get_best_yes_ask(ticker: str) -> Optional[int]:
    resp = kalshi_get(f"/trade-api/v2/markets/{ticker}/orderbook")
    asks = resp.get("orderbook", {}).get("yes", {}).get("asks", [])
    if not asks:
        return None
    return int(asks[0]["price_cents"])


# ============================================================
# PORTFOLIO
# ============================================================

def has_position(ticker: str) -> bool:
    resp = kalshi_get("/trade-api/v2/portfolio/positions")
    for p in resp.get("positions", []):
        if p["ticker"] == ticker and p["position"] != 0:
            return True
    return False


# ============================================================
# TRADING (WRITE — FIXED PATH)
# ============================================================

def place_yes_buy(ticker: str, price: int, count: int):
    order = {
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": count,
        "yes_price": price,
        "post_only": POST_ONLY,
        "client_order_id": str(uuid.uuid4()),
    }
    return kalshi_post("/v2/portfolio/orders", order)


# ============================================================
# MAIN
# ============================================================

def main():
    log.info("[BOOT] LIVE BTC BOT STARTED")
    log.info("[BOOT] SERIES_PREFIX=%s", SERIES_PREFIX)

    active_ticker = resolve_active_market()
    if not active_ticker:
        log.error("No active market found")
        return

    log.info("[MARKET] Active contract: %s", active_ticker)

    while True:
        try:
            if has_position(active_ticker):
                time.sleep(POLL_SECONDS)
                continue

            best_ask = get_best_yes_ask(active_ticker)
            if best_ask is None:
                time.sleep(POLL_SECONDS)
                continue

            if best_ask <= BUY_PRICE_CENTS:
                place_yes_buy(active_ticker, BUY_PRICE_CENTS, BASE_SIZE)

        except Exception as e:
            log.error("[LOOPERR] %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()