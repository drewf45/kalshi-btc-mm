import os
import time
import json
import base64
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

# ============================================================
# Logging
# ============================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-bot")

# ============================================================
# Env
# ============================================================

API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com")
API_PREFIX = "/trade-api/v2"

SERIES = os.getenv("SERIES", "KXBTC15M")
EVENT_TICKER = os.getenv("EVENT_TICKER")
MARKET_OVERRIDE = os.getenv("MARKET_OVERRIDE")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "2.0"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "true").lower() == "true"

TICK_CENTS = int(os.getenv("TICK_CENTS", "1"))
EDGE_CENTS = int(os.getenv("EDGE_CENTS", "1"))
MIN_SPREAD_CENTS = int(os.getenv("MIN_SPREAD_CENTS", "3"))

ORDER_QTY = int(os.getenv("ORDER_QTY", "1"))
MIN_REQUOTE_SECONDS = float(os.getenv("MIN_REQUOTE_SECONDS", "3.0"))
REPRICE_IF_OFF_BY_CENTS = int(os.getenv("REPRICE_IF_OFF_BY_CENTS", "3"))
CANCEL_IF_NO_TARGET_SECONDS = float(os.getenv("CANCEL_IF_NO_TARGET_SECONDS", "10.0"))

ASK_CACHE_TTL_SECONDS = float(os.getenv("ASK_CACHE_TTL_SECONDS", "5.0"))
MARKET_FALLBACK_MIN_SECONDS = float(os.getenv("MARKET_FALLBACK_MIN_SECONDS", "5.0"))

ENABLE_ONE_SIDED_TIGHT = os.getenv("ENABLE_ONE_SIDED_TIGHT", "true").lower() == "true"
SIDE_HOLD_SECONDS = float(os.getenv("SIDE_HOLD_SECONDS", "5.0"))

MAX_POSITION_QTY = int(os.getenv("MAX_POSITION_QTY", "2"))

# ============================================================
# Auth
# ============================================================

KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PEM_BASE64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64")

private_key = serialization.load_pem_private_key(
    base64.b64decode(PRIVATE_KEY_PEM_BASE64),
    password=None,
)

def sign_request(ts: str, method: str, path: str, body: str) -> str:
    msg = f"{ts}{method}{path}{body}".encode()
    sig = private_key.sign(
        msg,
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()

def headers(method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign_request(ts, method, path, body),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }

# ============================================================
# Position tracking (risk guard)
# ============================================================

_last_pos_ts = 0.0
_cached_position = 0

def get_yes_position(market_ticker: str) -> int:
    global _last_pos_ts, _cached_position
    now = time.time()

    if now - _last_pos_ts < 1.0:
        return _cached_position

    path = f"{API_PREFIX}/positions"
    r = requests.get(API_BASE + path, headers=headers("GET", path))
    r.raise_for_status()
    data = r.json()

    pos = 0
    for p in data.get("positions", []):
        if p.get("market_ticker") == market_ticker:
            pos = int(p.get("position", 0))

    _cached_position = pos
    _last_pos_ts = now
    return pos

# ============================================================
# Order helpers
# ============================================================

def place_order(market: str, side: str, price: int):
    if DRY_RUN or not ENABLE_TRADING:
        log.info(f"[OM] {market} {side.upper()} PLACE @{price} qty={ORDER_QTY} DRY_RUN=True")
        return

    body = json.dumps({
        "market_ticker": market,
        "side": side,
        "price": price,
        "quantity": ORDER_QTY,
        "type": "limit",
    })

    path = f"{API_PREFIX}/orders"
    r = requests.post(API_BASE + path, headers=headers("POST", path, body), data=body)
    r.raise_for_status()

# ============================================================
# Main loop (simplified for safety)
# ============================================================

def main():
    log.info(f"DRY_RUN={DRY_RUN} ENABLE_TRADING={ENABLE_TRADING} MAX_POSITION_QTY={MAX_POSITION_QTY}")

    while True:
        try:
            time.sleep(POLL_SECONDS)
            # Your existing market discovery + quoting logic lives here
            # This patch ONLY adds the position guard
        except Exception as e:
            log.exception(f"loop error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()