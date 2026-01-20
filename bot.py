import os
import time
import base64
import uuid
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

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
# KALSHI BASES (migration reality)
# ============================================================

MARKET_BASE = "https://trading-api.kalshi.com"          # market data (orderbook, markets)
MARKET_FALLBACK_BASE = "https://api.elections.kalshi.com"  # fallback if needed
PORTFOLIO_BASE = "https://api.elections.kalshi.com"     # portfolio + trading


# ============================================================
# ENV
# ============================================================

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()
KALSHI_SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT", "").strip()

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "").strip()  # e.g. KXBTC15m

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
# AUTH (MATCH YOUR ORIGINAL WORKING STYLE)
# - PSS padding
# - sign only: timestamp + METHOD + signed_path
# ============================================================

def now_ms() -> int:
    return int(time.time() * 1000)

def load_private_key_from_b64(b64: str):
    key_bytes = base64.b64decode(b64)
    return serialization.load_pem_private_key(key_bytes, password=None)

PRIVATE_KEY = load_private_key_from_b64(KALSHI_PRIVATE_KEY_B64)
log.info("[BOOT] Private key loaded OK (b64)")

def sign_request(private_key, timestamp_ms: int, method: str, signed_path: str) -> str:
    sign_str = f"{timestamp_ms}{method.upper()}{signed_path}"
    sig = private_key.sign(
        sign_str.encode("utf-8"),
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")

def kalshi_headers(method: str, signed_path: str) -> Dict[str, str]:
    ts = now_ms()
    h = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign_request(PRIVATE_KEY, ts, method, signed_path),
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
    }
    if KALSHI_SUBACCOUNT:
        h["KALSHI-ACCESS-SUBACCOUNT"] = KALSHI_SUBACCOUNT
    return h

def signed_path(path: str, params: Optional[Dict[str, Any]] = None) -> str:
    if not params:
        return path
    # stable ordering
    items = sorted((k, str(v)) for k, v in params.items() if v is not None)
    return f"{path}?{urlencode(items)}"


# ============================================================
# HTTP HELPERS
# ============================================================

def market_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    sp = signed_path(path, params)
    url = f"{MARKET_BASE}{path}"
    r = requests.get(url, headers=kalshi_headers("GET", sp), params=params, timeout=10)
    log.info("[REQ] GET %s -> %s", sp, r.status_code)

    # fallback if trading-api refuses (migration weirdness)
    if r.status_code in (401, 403, 404):
        url2 = f"{MARKET_FALLBACK_BASE}{path}"
        r2 = requests.get(url2, headers=kalshi_headers("GET", sp), params=params, timeout=10)
        log.info("[REQ] GET(fallback) %s -> %s", sp, r2.status_code)
        r = r2

    r.raise_for_status()
    return r.json()

def portfolio_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    sp = signed_path(path, params)
    url = f"{PORTFOLIO_BASE}{path}"
    r = requests.get(url, headers=kalshi_headers("GET", sp), params=params, timeout=10)
    log.info("[REQ] GET %s -> %s", sp, r.status_code)
    r.raise_for_status()
    return r.json()

def portfolio_post(path: str, body: Dict[str, Any]) -> Any:
    sp = path  # POST signature uses path (no query)
    url = f"{PORTFOLIO_BASE}{path}"
    r = requests.post(url, headers=kalshi_headers("POST", sp), json=body, timeout=10)
    log.info("[REQ] POST %s -> %s", sp, r.status_code)
    try:
        data = r.json()
    except Exception:
        data = r.text
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Order failed: {data}")
    return data


# ============================================================
# TICKER COMPUTATION (every 15 minutes)
# Example: KXBTC15M-26JAN201645
# ============================================================

MONTH_ABBR = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
ET = ZoneInfo("America/New_York")

def floor_to_15m(dt: datetime) -> datetime:
    m = (dt.minute // 15) * 15
    return dt.replace(minute=m, second=0, microsecond=0)

def current_market_ticker() -> str:
    now_et = datetime.now(ET)
    slot = floor_to_15m(now_et)
    yy = slot.year % 100
    mon = MONTH_ABBR[slot.month - 1]
    dd = slot.day
    hh = slot.hour
    mm = slot.minute
    return f"{SERIES_PREFIX}-{yy:02d}{mon}{dd:02d}{hh:02d}{mm:02d}"


# ============================================================
# MARKET DATA
# ============================================================

def get_best_yes_ask(ticker: str) -> Optional[int]:
    resp = market_get(f"/trade-api/v2/markets/{ticker}/orderbook")
    asks = resp.get("orderbook", {}).get("yes", {}).get("asks", [])
    if not asks:
        return None
    p = asks[0].get("price_cents")
    if p is None:
        p = asks[0].get("price")
    if p is None:
        p = asks[0].get("yes_price")
    return int(p) if p is not None else None


# ============================================================
# PORTFOLIO (MIGRATED PATH — THIS IS THE FIX)
# ============================================================

def has_position(ticker: str) -> bool:
    # MIGRATED: no /trade-api here
    resp = portfolio_get("/v2/portfolio/positions")
    for p in resp.get("positions", []):
        if p.get("ticker") == ticker and int(p.get("position", 0)) != 0:
            return True
    return False


# ============================================================
# TRADING (MIGRATED PATH)
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
    return portfolio_post("/v2/portfolio/orders", body)


# ============================================================
# MAIN
# ============================================================

def main():
    log.info("[BOOT] LIVE BTC 15M BOT STARTED")
    log.info("[BOOT] SERIES_PREFIX=%s POLL_SECONDS=%.2f BUY_PRICE_CENTS=%d BASE_SIZE=%d POST_ONLY=%s",
             SERIES_PREFIX, POLL_SECONDS, BUY_PRICE_CENTS, BASE_SIZE, POST_ONLY)

    active_ticker: Optional[str] = None

    while True:
        try:
            computed = current_market_ticker()
            if computed != active_ticker:
                active_ticker = computed
                log.info("[MARKET] Switched active ticker -> %s", active_ticker)

            # one contract at a time
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