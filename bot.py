import os
import time
import json
import base64
import uuid
import logging
from typing import Any, Dict, Optional, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo  # Python 3.9+

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding


# ============================================================
# LOGGING
# ============================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")


# ============================================================
# BASE URLS (Kalshi migration reality)
# - We will use elections as PRIMARY for reads+write since it consistently 200s.
# - We keep trading-api as fallback for reads.
# ============================================================

READ_BASE = "https://api.elections.kalshi.com"
READ_FALLBACK_BASE = "https://trading-api.kalshi.com"
WRITE_BASE = "https://api.elections.kalshi.com"


# ============================================================
# ENV
# ============================================================

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()
KALSHI_SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT", "").strip()

# For this market family: KXBTC15M
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "").strip()  # ex: KXBTC15m or KXBTC15M

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))
POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")

if not KALSHI_KEY_ID:
    raise RuntimeError("Missing KALSHI_KEY_ID")
if not KALSHI_PRIVATE_KEY_B64:
    raise RuntimeError("Missing KALSHI_PRIVATE_KEY_B64")
if not SERIES_PREFIX:
    raise RuntimeError("Missing SERIES_PREFIX (set in Render)")

SERIES_PREFIX = SERIES_PREFIX.upper()


# ============================================================
# AUTH
# ============================================================

def now_ms() -> int:
    return int(time.time() * 1000)

def load_private_key_from_b64(b64: str):
    key_bytes = base64.b64decode(b64)
    return serialization.load_pem_private_key(key_bytes, password=None)

PRIVATE_KEY = load_private_key_from_b64(KALSHI_PRIVATE_KEY_B64)
log.info("[BOOT] Private key loaded OK (b64)")

def sign_request(method: str, signed_path: str, body_json: str) -> Tuple[str, str]:
    """
    Signature payload: timestamp + METHOD + signed_path + body_json
    - signed_path MUST include query string if present.
    - body_json must be '' for GET.
    """
    ts = str(now_ms())
    payload = f"{ts}{method.upper()}{signed_path}{body_json}".encode("utf-8")
    sig = PRIVATE_KEY.sign(payload, asy_padding.PKCS1v15(), hashes.SHA256())
    return ts, base64.b64encode(sig).decode("utf-8")

def auth_headers(method: str, signed_path: str, body_json: str) -> Dict[str, str]:
    ts, sig = sign_request(method, signed_path, body_json)
    h = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }
    if KALSHI_SUBACCOUNT:
        # keep your existing behavior
        h["KALSHI-ACCESS-SUBACCOUNT"] = KALSHI_SUBACCOUNT
    return h


# ============================================================
# HTTP
# ============================================================

def _get_json(base: str, path: str, signed_path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{base}{path}"
    r = requests.get(url, headers=auth_headers("GET", signed_path, ""), params=params, timeout=10)
    log.info("[REQ] GET %s -> %s", signed_path, r.status_code)
    r.raise_for_status()
    return r.json()

def kalshi_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    if params:
        # stable ordering for signing
        items = sorted((k, str(v)) for k, v in params.items() if v is not None)
        qs = "&".join(f"{k}={requests.utils.quote(v, safe='')}" for k, v in items)
        signed_path = f"{path}?{qs}"
    else:
        signed_path = path

    # primary
    try:
        return _get_json(READ_BASE, path, signed_path, params=params)
    except Exception:
        # fallback
        return _get_json(READ_FALLBACK_BASE, path, signed_path, params=params)

def kalshi_post(path: str, body: dict) -> Any:
    body_json = json.dumps(body, separators=(",", ":"))
    url = f"{WRITE_BASE}{path}"

    r = requests.post(url, headers=auth_headers("POST", path, body_json), data=body_json, timeout=10)
    log.info("[REQ] POST %s -> %s", path, r.status_code)

    try:
        data = r.json()
    except Exception:
        data = r.text

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Order failed: {data}")

    return data


# ============================================================
# TICKER COMPUTATION (YOUR LINK FORMAT)
# Format: {SERIES}-{YY}{MON}{DD}{HH}{MM}
# Example: KXBTC15M-26JAN201645  == 2026-01-20 16:45 (America/New_York)
# ============================================================

MONTH_ABBR = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
ET = ZoneInfo("America/New_York")

def floor_to_15m(dt: datetime) -> datetime:
    m = (dt.minute // 15) * 15
    return dt.replace(minute=m, second=0, microsecond=0)

def market_ticker_for_now() -> str:
    now_et = datetime.now(ET)
    slot = floor_to_15m(now_et)
    yy = slot.year % 100
    mon = MONTH_ABBR[slot.month - 1]
    dd = slot.day
    hh = slot.hour
    mm = slot.minute
    return f"{SERIES_PREFIX}-{yy:02d}{mon}{dd:02d}{hh:02d}{mm:02d}"


# ============================================================
# ORDERBOOK / POSITIONS
# ============================================================

def get_best_yes_ask(ticker: str) -> Optional[int]:
    resp = kalshi_get(f"/trade-api/v2/markets/{ticker}/orderbook")
    asks = resp.get("orderbook", {}).get("yes", {}).get("asks", [])
    if not asks:
        return None
    # robust key handling
    p = asks[0].get("price_cents")
    if p is None:
        p = asks[0].get("price")
    if p is None:
        p = asks[0].get("yes_price")
    return int(p) if p is not None else None

def has_position(ticker: str) -> bool:
    resp = kalshi_get("/trade-api/v2/portfolio/positions")
    for p in resp.get("positions", []):
        if p.get("ticker") == ticker and int(p.get("position", 0)) != 0:
            return True
    return False


# ============================================================
# TRADING (WRITE PATH)
# ============================================================

def place_yes_buy(ticker: str, price: int, count: int) -> Any:
    body = {
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": int(count),
        "yes_price": int(price),
        "post_only": bool(POST_ONLY),
        "client_order_id": str(uuid.uuid4()),
    }
    # migrated write path (NO /trade-api)
    return kalshi_post("/v2/portfolio/orders", body)


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    log.info("[BOOT] LIVE BTC 15M BOT STARTED")
    log.info("[BOOT] SERIES_PREFIX=%s POLL_SECONDS=%.2f BUY_PRICE_CENTS=%d BASE_SIZE=%d POST_ONLY=%s",
             SERIES_PREFIX, POLL_SECONDS, BUY_PRICE_CENTS, BASE_SIZE, POST_ONLY)

    active_ticker = None

    while True:
        try:
            computed = market_ticker_for_now()
            if computed != active_ticker:
                active_ticker = computed
                log.info("[MARKET] Switched active ticker -> %s", active_ticker)

            # One-open-contract behavior via position check
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
            log.exception("[LOOPERR] %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()