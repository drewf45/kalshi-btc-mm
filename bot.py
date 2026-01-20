import os
import json
import time
import base64
import uuid
import logging
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode
from datetime import datetime

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

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
RESOLVE_EVERY_SECONDS = int(os.getenv("RESOLVE_EVERY_SECONDS", "20"))

BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))
POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")

LOG_SPREAD = os.getenv("LOG_SPREAD", "true").lower() in ("1", "true", "yes", "y")

# ✅ MICRO-CHANGE: reduce /markets load + add 429 backoff behavior
MAX_MARKET_PAGES = int(os.getenv("MAX_MARKET_PAGES", "2"))  # was effectively 5 in code
RESOLVE_BACKOFF_SECONDS = int(os.getenv("RESOLVE_BACKOFF_SECONDS", "60"))


# -----------------------------
# Helpers
# -----------------------------
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
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign_request(private_key, ts, method, signed_path),
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
    }


def request(private_key, method: str, path: str, params=None, body=None) -> Tuple[int, Any]:
    signed_path = f"{path}?{urlencode(params)}" if params else path
    resp = requests.request(
        method=method,
        url=f"{BASE_URL}{path}",
        headers=kalshi_headers(private_key, method, signed_path),
        params=params,
        json=body,
        timeout=15,
    )
    code = resp.status_code
    try:
        data = resp.json()
    except Exception:
        data = resp.text
    log.info("[REQ] %s %s -> HTTP=%s", method, signed_path, code)
    return code, data


# -----------------------------
# Market resolution
# -----------------------------
def list_markets(private_key, limit=200, cursor=None) -> Tuple[int, List[Dict[str, Any]], Optional[str], Any]:
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor

    code, data = request(private_key, "GET", "/trade-api/v2/markets", params=params)
    if code != 200:
        return code, [], None, data

    markets = data.get("markets", [])
    next_cursor = data.get("cursor")
    if not isinstance(markets, list):
        markets = []
    return code, markets, next_cursor, data


def _parse_close_ms(m: Dict[str, Any]) -> Optional[int]:
    # Try ISO close_time first
    close_iso = m.get("close_time")
    if close_iso:
        try:
            dt = datetime.fromisoformat(str(close_iso).replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            pass

    # Try ms fields if present
    for k in ("close_time_ms", "close_ts_ms", "end_time_ms", "settlement_time_ms"):
        if m.get(k) is not None:
            try:
                return int(m[k])
            except Exception:
                pass

    return None


def resolve_active_ticker(private_key, series_prefix: str) -> Tuple[Optional[str], bool]:
    """
    Returns (ticker, hit_rate_limit)
    Micro-change:
      - case-insensitive prefix match so KXBTC15m matches KXBTC15M...
      - stop paging aggressively
      - do not throw on 429; surface it so caller backs off
    """
    if not series_prefix:
        return None, False

    want = series_prefix.strip().lower()
    now_ms = now_utc_ts_ms()

    best: Optional[Tuple[int, str]] = None  # (delta_ms, ticker)
    hit_rl = False

    cursor = None
    for _ in range(MAX_MARKET_PAGES):
        code, markets, cursor, err = list_markets(private_key, cursor=cursor)

        if code == 429:
            hit_rl = True
            log.warning("[RL] /markets rate-limited (429). Backing off.")
            break

        if code != 200:
            log.warning("[MARKETS] list failed code=%s err=%s", code, err)
            break

        for m in markets:
            if not isinstance(m, dict):
                continue
            t = str(m.get("ticker") or "").strip()
            if not t:
                continue

            # ✅ case-insensitive startswith match
            if not t.lower().startswith(want):
                continue

            close_ms = _parse_close_ms(m)
            if close_ms is None:
                # If metadata is missing, still keep a fallback candidate
                # but rank it worse than any with close time.
                delta = 10**12
            else:
                delta = close_ms - now_ms
                if delta <= 0:
                    continue

            if best is None or delta < best[0]:
                best = (delta, t)

        if not cursor:
            break

    return (best[1] if best else None), hit_rl


# -----------------------------
# Trading
# -----------------------------
def get_orderbook(private_key, ticker: str):
    code, data = request(private_key, "GET", f"/trade-api/v2/markets/{ticker}/orderbook")
    if code != 200:
        raise RuntimeError(f"Orderbook failed: {data}")
    return data.get("orderbook", {})


def parse_yes_best(orderbook):
    y = orderbook.get("yes", {})
    bids = y.get("bids", [])
    asks = y.get("asks", [])
    best_bid = max(bids, key=lambda x: x["price_cents"]) if bids else None
    best_ask = min(asks, key=lambda x: x["price_cents"]) if asks else None
    return best_bid, best_ask


def create_order_yes_buy(private_key, ticker, price, count):
    body = {
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": int(count),
        "yes_price": int(price),
        "client_order_id": str(uuid.uuid4()),
        "post_only": bool(POST_ONLY),
    }
    code, data = request(private_key, "POST", "/trade-api/v2/portfolio/orders", body=body)
    if code not in (200, 201):
        raise RuntimeError(f"Place order failed: {data}")


# -----------------------------
# Main loop
# -----------------------------
def main():
    # Accept your existing env var aliases
    series_prefix = (
        os.getenv("SERIES_PREFIX", "").strip()
        or os.getenv("Series_PREFIC", "").strip()
        or os.getenv("SERIES_PREFIC", "").strip()
    )

    if not series_prefix:
        log.error("[CONFIG] Missing series prefix. Set SERIES_PREFIX (preferred) or your existing Series_PREFIC.")
        while True:
            time.sleep(30)

    if not KALSHI_KEY_ID:
        log.error("[CONFIG] Missing KALSHI_KEY_ID")
        while True:
            time.sleep(30)

    if not KALSHI_PRIVATE_KEY_B64:
        log.error("[CONFIG] Missing KALSHI_PRIVATE_KEY_B64")
        while True:
            time.sleep(30)

    private_key = load_private_key_from_b64(KALSHI_PRIVATE_KEY_B64)

    log.info(
        "[BOOT] BASE_URL=%s SERIES_PREFIX=%s POLL_SECONDS=%.2f RESOLVE_EVERY_SECONDS=%d BUY_PRICE_CENTS=%d BASE_SIZE=%d POST_ONLY=%s MAX_MARKET_PAGES=%d RESOLVE_BACKOFF_SECONDS=%d",
        BASE_URL, series_prefix, POLL_SECONDS, RESOLVE_EVERY_SECONDS, BUY_PRICE_CENTS, BASE_SIZE, POST_ONLY, MAX_MARKET_PAGES, RESOLVE_BACKOFF_SECONDS
    )

    active_ticker = None
    next_resolve_at = 0.0

    while True:
        try:
            now = time.time()

            # Resolve ticker when needed, but respect cooldowns
            if active_ticker is None and now < next_resolve_at:
                time.sleep(1)
                continue

            if active_ticker is None or now >= next_resolve_at:
                new_ticker, hit_rl = resolve_active_ticker(private_key, series_prefix)

                if hit_rl:
                    # ✅ MICRO-CHANGE: back off hard on 429 to stop thrash
                    next_resolve_at = now + RESOLVE_BACKOFF_SECONDS
                    active_ticker = None
                    continue

                if new_ticker and new_ticker != active_ticker:
                    log.info("[MARKET] Switched active ticker -> %s", new_ticker)
                    active_ticker = new_ticker

                if not active_ticker:
                    log.warning("[MARKET] No active ticker resolved yet for prefix=%s (cooldown %ds)", series_prefix, RESOLVE_BACKOFF_SECONDS)
                    next_resolve_at = now + RESOLVE_BACKOFF_SECONDS
                    continue

                # Normal cadence once we have a ticker
                next_resolve_at = now + RESOLVE_EVERY_SECONDS

            # If still no ticker, just wait
            if not active_ticker:
                time.sleep(1)
                continue

            ob = get_orderbook(private_key, active_ticker)
            bid, ask = parse_yes_best(ob)

            if bid and ask and LOG_SPREAD:
                spread = ask["price_cents"] - bid["price_cents"]
                log.info("[SPREAD] %s YES bid=%dc ask=%dc spread=%dc", active_ticker, bid["price_cents"], ask["price_cents"], spread)

            # Keep behavior unchanged for now
            create_order_yes_buy(private_key, active_ticker, BUY_PRICE_CENTS, BASE_SIZE)

        except Exception as e:
            log.exception("[LOOPERR] %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()