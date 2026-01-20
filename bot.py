import os
import time
import json
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")

# -----------------------------
# Env
# -----------------------------
BASE_URL = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").rstrip("/")
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

SERIES_PREFIX = os.getenv("SERIES_PREFIX", os.getenv("SERIES_TICKER", "KXBTC15m")).strip()

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
RESOLVE_EVERY_SECONDS = int(os.getenv("RESOLVE_EVERY_SECONDS", "20"))
RESOLVE_BACKOFF_SECONDS = int(os.getenv("RESOLVE_BACKOFF_SECONDS", "60"))

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "FALSE").upper() == "TRUE"
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "FALSE").upper() == "TRUE"

POST_ONLY = os.getenv("POST_ONLY", "TRUE").upper() == "TRUE"
IMPROVE_TICKS = int(os.getenv("IMPROVE_TICKS", "1"))

# Your env currently shows BUY_PRICE_CENTS=99; leaving as-is by design
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", os.getenv("MAX_BUY_PRICE_CENTS", "1")))
BASE_SIZE = int(float(os.getenv("BASE_SIZE", "1")))

KALSHI_SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT", "").strip()  # optional

# -----------------------------
# Helpers
# -----------------------------
def now_utc_ts() -> int:
    return int(time.time() * 1000)

def load_private_key_from_b64(b64_str: str):
    if not b64_str:
        raise RuntimeError("Missing KALSHI_PRIVATE_KEY_B64")
    raw = base64.b64decode(b64_str)
    try:
        return serialization.load_pem_private_key(raw, password=None)
    except Exception:
        return serialization.load_der_private_key(raw, password=None)

def sign_request(private_key, method: str, path_with_query: str, ts_ms: int) -> str:
    msg = f"{ts_ms}{method.upper()}{path_with_query}".encode("utf-8")
    sig = private_key.sign(msg, asy_padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("utf-8")

def auth_headers(private_key, method: str, signed_path: str) -> Dict[str, str]:
    ts = now_utc_ts()
    sig = sign_request(private_key, method, signed_path, ts)
    h = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
    }
    if KALSHI_SUBACCOUNT:
        h["KALSHI-ACCESS-SUBACCOUNT"] = KALSHI_SUBACCOUNT
    return h

def _req_json(private_key, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Any = None):
    if params:
        qs = urlencode(params, doseq=True)
        signed_path = f"{path}?{qs}"
        url = f"{BASE_URL}{path}?{qs}"
    else:
        signed_path = path
        url = f"{BASE_URL}{path}"

    headers = auth_headers(private_key, method, signed_path)
    r = requests.request(
        method,
        url,
        headers=headers,
        data=(json.dumps(body) if body is not None else None),
        timeout=10,
    )

    try:
        data = r.json() if r.content else {}
    except Exception:
        data = {"raw": r.text}

    log.info("[REQ] %s %s -> %s", method.upper(), signed_path, r.status_code)

    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {signed_path}: {data}")

    return data

def market_get(private_key, path: str, params: Optional[Dict[str, Any]] = None):
    return _req_json(private_key, "GET", path, params=params)

def portfolio_get(private_key, path: str, params: Optional[Dict[str, Any]] = None):
    return _req_json(private_key, "GET", path, params=params)

def portfolio_post(private_key, path: str, body: Any):
    return _req_json(private_key, "POST", path, body=body)

# -----------------------------
# Market resolving (FIXED)
# -----------------------------
def _parse_close_ms(m: Dict[str, Any]) -> Optional[int]:
    close_iso = m.get("close_time") or m.get("end_time") or m.get("settlement_time")
    if close_iso:
        try:
            dt = datetime.fromisoformat(str(close_iso).replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
    for k in ("close_time_ms", "close_ts_ms", "end_time_ms", "settlement_time_ms"):
        if m.get(k) is not None:
            try:
                return int(m[k])
            except Exception:
                pass
    return None

def _pick_best_market_ticker(markets: List[Dict[str, Any]]) -> Optional[str]:
    """
    Pick the soonest *future* market (for 15-min cadence there should be one).
    If close times missing, fallback to first ticker.
    """
    now_ms = now_utc_ts()
    best: Optional[Tuple[int, str]] = None  # (delta_ms, ticker)

    for m in markets:
        if not isinstance(m, dict):
            continue
        t = str(m.get("ticker") or "").strip()
        if not t:
            continue
        close_ms = _parse_close_ms(m)
        if close_ms is None:
            # keep as weak fallback
            if best is None:
                best = (10**18, t)
            continue
        delta = close_ms - now_ms
        if delta <= 0:
            continue
        if best is None or delta < best[0]:
            best = (delta, t)

    return best[1] if best else None

def resolve_active_market(private_key) -> Optional[str]:
    """
    FIX: probe multiple combinations because some series return empty on:
      series_ticker + status=open
    """
    sp_raw = SERIES_PREFIX.strip()
    sp_upper = sp_raw.upper()
    sp_lower = sp_raw.lower()

    probes: List[Tuple[str, Dict[str, Any]]] = [
        ("series_ticker open (upper)", {"series_ticker": sp_upper, "status": "open", "limit": 200}),
        ("series_ticker active (upper)", {"series_ticker": sp_upper, "status": "active", "limit": 200}),
        ("series_ticker open (raw)", {"series_ticker": sp_raw, "status": "open", "limit": 200}),
        ("series_ticker active (raw)", {"series_ticker": sp_raw, "status": "active", "limit": 200}),
        ("event_ticker open (upper)", {"event_ticker": sp_upper, "status": "open", "limit": 200}),
        ("event_ticker active (upper)", {"event_ticker": sp_upper, "status": "active", "limit": 200}),
        ("event_ticker open (raw)", {"event_ticker": sp_raw, "status": "open", "limit": 200}),
        ("event_ticker active (raw)", {"event_ticker": sp_raw, "status": "active", "limit": 200}),
        ("series_ticker no-status (upper)", {"series_ticker": sp_upper, "limit": 200}),
        ("event_ticker no-status (upper)", {"event_ticker": sp_upper, "limit": 200}),
        ("series_ticker no-status (raw)", {"series_ticker": sp_raw, "limit": 200}),
        ("event_ticker no-status (raw)", {"event_ticker": sp_raw, "limit": 200}),
        ("series_ticker open (lower)", {"series_ticker": sp_lower, "status": "open", "limit": 200}),
        ("series_ticker active (lower)", {"series_ticker": sp_lower, "status": "active", "limit": 200}),
    ]

    for label, params in probes:
        try:
            resp = market_get(private_key, "/trade-api/v2/markets", params=params)
        except Exception as e:
            log.warning("[RESOLVE] probe=%s failed: %s", label, e)
            continue

        markets = resp.get("markets") or resp.get("data", {}).get("markets") or []
        if not isinstance(markets, list) or not markets:
            log.info("[RESOLVE] probe=%s -> 0 markets", label)
            continue

        picked = _pick_best_market_ticker(markets)
        log.info("[RESOLVE] probe=%s -> markets=%d picked=%s", label, len(markets), picked)
        if picked:
            return picked

    return None

def get_orderbook(private_key, market_ticker: str) -> Dict[str, Any]:
    return market_get(private_key, f"/trade-api/v2/markets/{market_ticker}/orderbook")

# -----------------------------
# Post-only cross protection (unchanged)
# -----------------------------
def _best_price(levels, want: str) -> Optional[int]:
    if not isinstance(levels, list) or not levels:
        return None
    prices = []
    for lv in levels:
        if isinstance(lv, dict):
            p = lv.get("price")
        elif isinstance(lv, (list, tuple)) and lv:
            p = lv[0]
        else:
            continue
        if isinstance(p, (int, float)):
            prices.append(int(p))
    if not prices:
        return None
    return max(prices) if want == "bid" else min(prices)

def best_yes_bid_ask(orderbook: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    yb = orderbook.get("yes_bids")
    ya = orderbook.get("yes_asks")
    if isinstance(yb, list) or isinstance(ya, list):
        return (_best_price(yb, "bid"), _best_price(ya, "ask"))

    bids = orderbook.get("bids")
    asks = orderbook.get("asks")
    if isinstance(bids, list) or isinstance(asks, list):
        return (_best_price(bids, "bid"), _best_price(asks, "ask"))

    return (None, None)

def compute_safe_post_only_yes_buy(
    desired_price: int,
    best_bid: Optional[int],
    best_ask: Optional[int],
    improve_ticks: int,
) -> Optional[int]:
    if best_ask is None:
        return desired_price

    max_maker_price = best_ask - 1
    if max_maker_price < 1:
        return None

    if best_bid is None:
        return min(desired_price, max_maker_price)

    improved = best_bid + max(1, improve_ticks)
    price = min(desired_price, improved, max_maker_price)
    if price < 1:
        return None
    return price

# -----------------------------
# Trading
# -----------------------------
def has_position(private_key, market_ticker: str) -> bool:
    resp = portfolio_get(private_key, "/trade-api/v2/portfolio/positions")
    positions = resp.get("positions") or resp.get("data", {}).get("positions") or []
    for p in positions:
        if p.get("market_ticker") == market_ticker:
            net = p.get("position") or p.get("net_position") or 0
            try:
                if float(net) != 0:
                    return True
            except Exception:
                return True
    return False

def place_yes_buy(private_key, market_ticker: str, price_cents: int, count: int):
    ob = get_orderbook(private_key, market_ticker)
    best_bid, best_ask = best_yes_bid_ask(ob)

    final_price = price_cents
    if POST_ONLY:
        safe = compute_safe_post_only_yes_buy(price_cents, best_bid, best_ask, IMPROVE_TICKS)
        if safe is None:
            log.info(
                "[SKIP] No maker-safe YES buy price (best_bid=%s best_ask=%s desired=%s post_only=True)",
                best_bid, best_ask, price_cents
            )
            return
        final_price = safe
        if best_ask is not None and final_price >= best_ask:
            log.info("[SKIP] Would cross (best_ask=%s final_price=%s).", best_ask, final_price)
            return

    log.info(
        "[ORDER] YES BUY market=%s desired=%sc final=%sc best_bid=%s best_ask=%s post_only=%s",
        market_ticker, price_cents, final_price, best_bid, best_ask, POST_ONLY
    )

    body = {
        "market_ticker": market_ticker,
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": count,
        "price": final_price,
        "client_order_id": f"mm-{market_ticker}-{now_utc_ts()}",
    }
    if POST_ONLY:
        body["post_only"] = True

    portfolio_post(private_key, "/trade-api/v2/portfolio/orders", body)

# -----------------------------
# Main loop
# -----------------------------
def main():
    private_key = load_private_key_from_b64(KALSHI_PRIVATE_KEY_B64)
    log.info(
        "[BOOT] BASE_URL=%s SERIES_PREFIX=%s POLL_SECONDS=%.2f BUY_PRICE_CENTS=%s BASE_SIZE=%s POST_ONLY=%s IMPROVE_TICKS=%s ENABLE_TRADING=%s CONFIRM_LIVE_TRADING=%s SUBACCOUNT=%s",
        BASE_URL, SERIES_PREFIX, POLL_SECONDS, BUY_PRICE_CENTS, BASE_SIZE, POST_ONLY, IMPROVE_TICKS,
        ENABLE_TRADING, CONFIRM_LIVE_TRADING, (KALSHI_SUBACCOUNT or "")
    )
    log.info("[BOOT] Private key loaded OK (b64)")
    log.info("[BOOT] LIVE BTC BOT STARTED")

    active_ticker: Optional[str] = None
    last_resolve = 0.0
    last_resolve_fail = 0.0

    while True:
        try:
            now = time.time()
            if (active_ticker is None) or (now - last_resolve >= RESOLVE_EVERY_SECONDS):
                if active_ticker is None and (now - last_resolve_fail) < RESOLVE_BACKOFF_SECONDS:
                    pass
                else:
                    last_resolve = now
                    t = resolve_active_market(private_key)
                    if not t:
                        last_resolve_fail = now
                        log.error("[MARKET] No active market found for series %s", SERIES_PREFIX)
                    else:
                        if t != active_ticker:
                            log.info("[MARKET] Active market -> %s", t)
                        active_ticker = t

            if not active_ticker:
                time.sleep(POLL_SECONDS)
                continue

            if not (ENABLE_TRADING and CONFIRM_LIVE_TRADING):
                time.sleep(POLL_SECONDS)
                continue

            if has_position(private_key, active_ticker):
                time.sleep(POLL_SECONDS)
                continue

            place_yes_buy(private_key, active_ticker, BUY_PRICE_CENTS, BASE_SIZE)

        except Exception as e:
            log.error("[LOOPERR] %s", e, exc_info=True)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()