import os
import time
import json
import base64
import uuid
import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

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
# API BASES
# ============================================================

READ_BASE  = "https://trading-api.kalshi.com"
WRITE_BASE = "https://api.elections.kalshi.com"
# Fallback for reads during migration
READ_FALLBACK_BASE = "https://api.elections.kalshi.com"


# ============================================================
# ENV
# ============================================================

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()
KALSHI_SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT", "").strip()

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "").strip()  # must be set in Render

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))
POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")


if not SERIES_PREFIX:
    raise RuntimeError("SERIES_PREFIX env var is required (set it in Render).")
if not KALSHI_KEY_ID:
    raise RuntimeError("Missing KALSHI_KEY_ID")
if not KALSHI_PRIVATE_KEY_B64:
    raise RuntimeError("Missing KALSHI_PRIVATE_KEY_B64")


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

def sign_request(private_key, method: str, signed_path: str, body_json: str) -> Tuple[str, str]:
    """
    Kalshi signature payload: timestamp + METHOD + signed_path + body_json
    - signed_path MUST include query string if present.
    - body_json is '' for GET
    """
    ts = str(now_ms())
    payload = f"{ts}{method.upper()}{signed_path}{body_json}".encode("utf-8")
    sig = private_key.sign(
        payload,
        asy_padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return ts, base64.b64encode(sig).decode("utf-8")

def auth_headers(method: str, signed_path: str, body_json: str) -> Dict[str, str]:
    ts, sig = sign_request(PRIVATE_KEY, method, signed_path, body_json)
    h = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }
    if KALSHI_SUBACCOUNT:
        # Keep your existing subaccount behavior
        h["KALSHI-ACCESS-SUBACCOUNT"] = KALSHI_SUBACCOUNT
    return h

def canonical_query(params: Optional[Dict[str, Any]]) -> str:
    if not params:
        return ""
    # stable ordering for signing
    return urlencode(sorted((k, str(v)) for k, v in params.items() if v is not None))


# ============================================================
# HTTP HELPERS (FIXED)
# ============================================================

def kalshi_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    q = canonical_query(params)
    signed_path = f"{path}?{q}" if q else path
    body_json = ""  # GET has empty body

    # First try READ_BASE
    url = f"{READ_BASE}{path}"
    r = requests.get(url, headers=auth_headers("GET", signed_path, body_json), params=params, timeout=10)
    log.info("[REQ] GET %s -> %s", signed_path, r.status_code)

    # If trading-api rejects, fallback to elections for reads
    if r.status_code in (401, 403, 404):
        url2 = f"{READ_FALLBACK_BASE}{path}"
        r2 = requests.get(url2, headers=auth_headers("GET", signed_path, body_json), params=params, timeout=10)
        log.info("[REQ] GET(fallback) %s -> %s", signed_path, r2.status_code)
        r = r2

    r.raise_for_status()
    return r.json()

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
# MARKET RESOLUTION (ONCE)
# ============================================================

def resolve_active_market() -> Optional[str]:
    resp = kalshi_get(
        "/trade-api/v2/markets",
        params={"series_ticker": SERIES_PREFIX, "status": "open", "limit": 50},
    )

    markets = resp.get("markets", [])
    for m in markets:
        if m.get("status") == "open":
            return m.get("ticker")
    return None


# ============================================================
# ORDERBOOK
# ============================================================

def get_best_yes_ask(ticker: str) -> Optional[int]:
    resp = kalshi_get(f"/trade-api/v2/markets/{ticker}/orderbook")
    asks = resp.get("orderbook", {}).get("yes", {}).get("asks", [])
    if not asks:
        return None
    # Your earlier logs showed price_cents
    return int(asks[0].get("price_cents") or asks[0].get("price") or asks[0].get("yes_price"))


# ============================================================
# POSITIONS (READ)
# ============================================================

def has_position(ticker: str) -> bool:
    resp = kalshi_get("/trade-api/v2/portfolio/positions")
    for p in resp.get("positions", []):
        if p.get("ticker") == ticker and int(p.get("position", 0)) != 0:
            return True
    return False


# ============================================================
# TRADING (WRITE PATH MIGRATED)
# ============================================================

def place_yes_buy(ticker: str, price: int, count: int):
    order = {
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": int(count),
        "yes_price": int(price),
        "post_only": bool(POST_ONLY),
        "client_order_id": str(uuid.uuid4()),
    }
    # migrated write path (no /trade-api)
    return kalshi_post("/v2/portfolio/orders", order)


# ============================================================
# MAIN
# ============================================================

def main():
    log.info("[BOOT] LIVE BTC BOT STARTED")
    log.info("[BOOT] SERIES_PREFIX=%s", SERIES_PREFIX)

    active_ticker = resolve_active_market()
    if not active_ticker:
        log.error("[MARKET] No active market found for series %s", SERIES_PREFIX)
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
            log.exception("[LOOPERR] %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()