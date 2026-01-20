import os
import json
import time
import base64
import uuid
import logging
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode
from datetime import datetime, timezone

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding


# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Config
# -----------------------------
BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com")

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

if not KALSHI_KEY_ID or not KALSHI_PRIVATE_KEY_B64:
    raise RuntimeError("Missing Kalshi API credentials")

# ✅ MICRO-CHANGE: rolling series identifier
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "").strip()  # e.g. KXBTC15m
if not SERIES_PREFIX:
    raise RuntimeError("Missing env var: SERIES_PREFIX")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
RESOLVE_EVERY_SECONDS = int(os.getenv("RESOLVE_EVERY_SECONDS", "20"))

BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))
POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")

LOG_SPREAD = os.getenv("LOG_SPREAD", "true").lower() in ("1", "true", "yes", "y")


# -----------------------------
# Helpers
# -----------------------------
def now_utc_ts_ms() -> int:
    return int(time.time() * 1000)


def load_private_key_from_b64(b64: str):
    key_bytes = base64.b64decode(b64)
    return serialization.load_pem_private_key(key_bytes, password=None)


PRIVATE_KEY = load_private_key_from_b64(KALSHI_PRIVATE_KEY_B64)


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


def kalshi_headers(method: str, path: str) -> Dict[str, str]:
    ts = now_utc_ts_ms()
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign_request(PRIVATE_KEY, ts, method, path),
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
    }


def request(method: str, path: str, params=None, body=None):
    signed_path = f"{path}?{urlencode(params)}" if params else path
    resp = requests.request(
        method=method,
        url=f"{BASE_URL}{path}",
        headers=kalshi_headers(method, signed_path),
        params=params,
        json=body,
        timeout=15,
    )
    return resp.status_code, resp.json()


# -----------------------------
# Market resolution (MICRO-CHANGE)
# -----------------------------
def list_markets(limit=200, cursor=None):
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    code, data = request("GET", "/trade-api/v2/markets", params=params)
    if code != 200:
        raise RuntimeError(data)
    return data.get("markets", []), data.get("cursor")


def resolve_active_ticker(series_prefix: str) -> Optional[str]:
    now_ms = now_utc_ts_ms()
    best: Optional[Tuple[int, str]] = None  # (delta_ms, ticker)

    cursor = None
    for _ in range(5):
        markets, cursor = list_markets(cursor=cursor)
        for m in markets:
            t = m.get("ticker", "")
            if not t.startswith(series_prefix):
                continue

            close_iso = m.get("close_time")
            if not close_iso:
                continue

            try:
                dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
                close_ms = int(dt.timestamp() * 1000)
            except Exception:
                continue

            delta = close_ms - now_ms
            if delta <= 0:
                continue

            if best is None or delta < best[0]:
                best = (delta, t)

        if not cursor:
            break

    return best[1] if best else None


# -----------------------------
# Trading
# -----------------------------
def get_orderbook(ticker: str):
    code, data = request("GET", f"/trade-api/v2/markets/{ticker}/orderbook")
    if code != 200:
        raise RuntimeError(data)
    return data["orderbook"]


def parse_yes_best(orderbook):
    y = orderbook.get("yes", {})
    bids = y.get("bids", [])
    asks = y.get("asks", [])
    best_bid = max(bids, key=lambda x: x["price_cents"]) if bids else None
    best_ask = min(asks, key=lambda x: x["price_cents"]) if asks else None
    return best_bid, best_ask


def create_order_yes_buy(ticker, price, count):
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
    code, data = request("POST", "/trade-api/v2/portfolio/orders", body=body)
    if code not in (200, 201):
        raise RuntimeError(data)


# -----------------------------
# Main loop
# -----------------------------
def main():
    active_ticker = None
    last_resolve = 0

    while True:
        try:
            now = time.time()
            if active_ticker is None or now - last_resolve > RESOLVE_EVERY_SECONDS:
                new_ticker = resolve_active_ticker(SERIES_PREFIX)
                if new_ticker and new_ticker != active_ticker:
                    log.info("[MARKET] Switched active ticker -> %s", new_ticker)
                    active_ticker = new_ticker
                last_resolve = now

            if not active_ticker:
                log.warning("[MARKET] No active ticker resolved yet")
                time.sleep(2)
                continue

            ob = get_orderbook(active_ticker)
            bid, ask = parse_yes_best(ob)

            if bid and ask and LOG_SPREAD:
                spread = ask["price_cents"] - bid["price_cents"]
                log.info(
                    "[SPREAD] %s YES bid=%dc ask=%dc spread=%dc",
                    active_ticker,
                    bid["price_cents"],
                    ask["price_cents"],
                    spread,
                )

            # still blind order (unchanged on purpose)
            create_order_yes_buy(active_ticker, BUY_PRICE_CENTS, BASE_SIZE)

        except Exception as e:
            log.exception("[LOOPERR] %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()