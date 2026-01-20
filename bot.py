import os
import time
import base64
import uuid
import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode
from datetime import datetime

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding


# =====================================================
# LOGGING
# =====================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-bot")


# =====================================================
# BASE URL SPLIT (FINAL)
# =====================================================
ELECTIONS_BASE_URL = "https://api.elections.kalshi.com"
TRADING_BASE_URL   = "https://trading-api.kalshi.com"


# =====================================================
# CONFIG
# =====================================================
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()
KALSHI_SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT", "").strip()

SERIES_PREFIX = (
    os.getenv("SERIES_PREFIX", "").strip()
    or os.getenv("Series_PREFIC", "").strip()
    or os.getenv("SERIES_PREFIC", "").strip()
)

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))
POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")


# =====================================================
# AUTH HELPERS
# =====================================================
def now_utc_ts_ms() -> int:
    return int(time.time() * 1000)


def load_private_key_from_b64(b64: str):
    key_bytes = base64.b64decode(b64)
    return serialization.load_pem_private_key(key_bytes, password=None)


def sign_request(private_key, timestamp_ms: int, method: str, path: str) -> str:
    sign_str = f"{timestamp_ms}{method.upper()}{path}"
    sig = private_key.sign(
        sign_str.encode("utf-8"),
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def kalshi_headers(private_key, method: str, signed_path: str) -> Dict[str, str]:
    ts = now_utc_ts_ms()
    headers = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign_request(private_key, ts, method, signed_path),
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
    }
    if KALSHI_SUBACCOUNT:
        headers["KALSHI-SUBACCOUNT"] = KALSHI_SUBACCOUNT
    return headers


def canonical_query(params: Optional[Dict[str, Any]]) -> str:
    if not params:
        return ""
    return urlencode(sorted((k, str(v)) for k, v in params.items() if v is not None))


def _request(
    base_url: str,
    private_key,
    method: str,
    path: str,
    params=None,
    body=None,
) -> Tuple[int, Any]:
    q = canonical_query(params)
    signed_path = f"{path}?{q}" if q else path

    resp = requests.request(
        method=method,
        url=f"{base_url}{path}",
        headers=kalshi_headers(private_key, method, signed_path),
        params=params,
        json=body,
        timeout=15,
    )

    try:
        data = resp.json()
    except Exception:
        data = resp.text

    log.info("[REQ] %s %s -> %s", method, signed_path, resp.status_code)
    return resp.status_code, data


# =====================================================
# MARKET RESOLUTION (ELECTIONS)
# =====================================================
def parse_close_ms(m: Dict[str, Any]) -> Optional[int]:
    if "close_time" in m:
        try:
            dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
    return m.get("close_time_ms")


def resolve_active_market(private_key) -> str:
    code, data = _request(
        ELECTIONS_BASE_URL,
        private_key,
        "GET",
        "/trade-api/v2/markets",
        params={
            "series_ticker": SERIES_PREFIX.upper(),
            "status": "open",
            "limit": 50,
        },
    )

    if code != 200:
        raise RuntimeError(f"Market resolve failed: {data}")

    markets = data.get("markets", [])
    now_ms = now_utc_ts_ms()

    future = []
    for m in markets:
        close_ms = parse_close_ms(m)
        if close_ms and close_ms > now_ms:
            future.append((close_ms, m["ticker"]))

    if not future:
        raise RuntimeError("No future open markets found")

    future.sort()
    return future[0][1]


# =====================================================
# ORDERBOOK (ELECTIONS — FIX)
# =====================================================
def get_orderbook(private_key, ticker: str):
    code, data = _request(
        ELECTIONS_BASE_URL,
        private_key,
        "GET",
        f"/trade-api/v2/markets/{ticker}/orderbook",
    )
    if code != 200:
        raise RuntimeError(f"Orderbook error: {data}")
    return data["orderbook"]


# =====================================================
# TRADING (TRADING API)
# =====================================================
def place_yes_buy(private_key, ticker: str, price: int, count: int):
    body = {
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": count,
        "yes_price": price,
        "client_order_id": str(uuid.uuid4()),
        "post_only": POST_ONLY,
    }

    code, data = _request(
        TRADING_BASE_URL,
        private_key,
        "POST",
        "/trade-api/v2/portfolio/orders",
        body=body,
    )

    if code not in (200, 201):
        raise RuntimeError(f"Order failed: {data}")


# =====================================================
# MAIN
# =====================================================
def main():
    private_key = load_private_key_from_b64(KALSHI_PRIVATE_KEY_B64)

    log.info("[BOOT] LIVE BTC BOT — FINAL ROUTING FIX")

    active_ticker = resolve_active_market(private_key)
    log.info("[MARKET] Locked active contract: %s", active_ticker)

    while True:
        try:
            ob = get_orderbook(private_key, active_ticker)
            place_yes_buy(private_key, active_ticker, BUY_PRICE_CENTS, BASE_SIZE)
        except Exception as e:
            log.exception("[LOOPERR] %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()