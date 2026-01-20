import os
import time
import base64
import uuid
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
# Config (from Render env)
# -----------------------------
# You currently set this in Render to https://api.elections.kalshi.com
KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").rstrip("/")

# Read base (some read endpoints still work here for some accounts)
KALSHI_READ_BASE = os.getenv("KALSHI_READ_BASE", "https://trading-api.kalshi.com").rstrip("/")

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "").strip()  # ex: KXBTC15m
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))

BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", os.getenv("MAX_BUY_PRICE_CENTS", "1")))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))
POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")


# -----------------------------
# Helpers
# -----------------------------
def now_utc_ts_ms() -> int:
    return int(time.time() * 1000)


def load_private_key_from_b64(b64: str):
    key_bytes = base64.b64decode(b64)
    return serialization.load_pem_private_key(key_bytes, password=None)


def sign_request(private_key, timestamp_ms: int, method: str, path_with_query: str) -> str:
    sign_str = f"{timestamp_ms}{method.upper()}{path_with_query}"
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


def _request_json(
    base_url: str,
    private_key,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = 15,
) -> Tuple[int, Any, str]:
    signed_path = f"{path}?{urlencode(params)}" if params else path
    url = f"{base_url}{path}"
    r = requests.request(
        method=method,
        url=url,
        headers=kalshi_headers(private_key, method, signed_path),
        params=params,
        json=body,
        timeout=timeout,
    )
    try:
        data = r.json()
    except Exception:
        data = r.text

    log.info("[REQ] %s %s -> %s", method, signed_path, r.status_code)
    return r.status_code, data, signed_path


# -----------------------------
# Market resolution (simple)
# -----------------------------
def _parse_close_ms(m: Dict[str, Any]) -> Optional[int]:
    close_iso = m.get("close_time")
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


def pick_soonest_future_market(markets: List[Dict[str, Any]]) -> Optional[str]:
    now_ms = now_utc_ts_ms()
    best: Optional[Tuple[int, str]] = None
    for m in markets:
        if not isinstance(m, dict):
            continue
        t = str(m.get("ticker") or "").strip()
        if not t:
            continue
        close_ms = _parse_close_ms(m)
        if close_ms is None:
            continue
        delta = close_ms - now_ms
        if delta <= 0:
            continue
        if best is None or delta < best[0]:
            best = (delta, t)
    return best[1] if best else None


def resolve_active_market(private_key) -> Optional[str]:
    sp = SERIES_PREFIX.strip().upper()
    params = {"limit": 200, "series_ticker": sp, "status": "open"}

    # Try read base first
    code, data, _ = _request_json(KALSHI_READ_BASE, private_key, "GET", "/trade-api/v2/markets", params=params)
    if code == 200 and isinstance(data, dict):
        mk = data.get("markets", [])
        if isinstance(mk, list) and mk:
            return pick_soonest_future_market(mk)

    # Fallback elections base
    code2, data2, _ = _request_json(KALSHI_API_BASE, private_key, "GET", "/trade-api/v2/markets", params=params)
    if code2 == 200 and isinstance(data2, dict):
        mk = data2.get("markets", [])
        if isinstance(mk, list) and mk:
            return pick_soonest_future_market(mk)

    return None


def get_orderbook(private_key, ticker: str) -> Dict[str, Any]:
    path = f"/trade-api/v2/markets/{ticker}/orderbook"

    code, data, _ = _request_json(KALSHI_READ_BASE, private_key, "GET", path)
    if code == 200 and isinstance(data, dict):
        ob = data.get("orderbook", {})
        return ob if isinstance(ob, dict) else {}

    code2, data2, _ = _request_json(KALSHI_API_BASE, private_key, "GET", path)
    if code2 == 200 and isinstance(data2, dict):
        ob = data2.get("orderbook", {})
        return ob if isinstance(ob, dict) else {}

    raise RuntimeError(f"Orderbook failed: {data2}")


# -----------------------------
# MICRO FIX: portfolio positions endpoint fallback (no crash on 404)
# -----------------------------
def portfolio_get(private_key, path_candidates: List[str]) -> Optional[Dict[str, Any]]:
    """
    Try multiple portfolio endpoints.
    404 means wrong path -> try next (DO NOT crash).
    """
    bases = [KALSHI_API_BASE, KALSHI_READ_BASE]  # try elections then trading
    last = None

    for base in bases:
        for path in path_candidates:
            code, data, signed_path = _request_json(base, private_key, "GET", path)
            last = (code, base, signed_path, data)

            if code == 200 and isinstance(data, dict):
                return data

            if code == 404:
                # Key change: treat as "not found", continue trying other paths
                log.warning("[PORTFOLIO] 404 on %s%s (trying next)", base, signed_path)
                continue

            if code in (401, 403):
                # auth issue on this base/path; try other base/path
                log.warning("[PORTFOLIO] %s on %s%s (trying next)", code, base, signed_path)
                continue

            # other errors: also keep trying
            log.warning("[PORTFOLIO] %s on %s%s (trying next) body=%s", code, base, signed_path, data)

    log.error("[PORTFOLIO] All candidates failed. last=%s", last)
    return None


def has_position(private_key, ticker: str) -> bool:
    # Try old + new path variants
    data = portfolio_get(
        private_key,
        path_candidates=[
            "/trade-api/v2/portfolio/positions",  # OLD (most likely correct for you)
            "/v2/portfolio/positions",            # NEW (your logs show 404 here)
        ],
    )
    if not data:
        return False

    positions = data.get("positions")
    if not isinstance(positions, list):
        return False

    tkr = ticker.strip().upper()
    for p in positions:
        if not isinstance(p, dict):
            continue
        pt = str(p.get("ticker") or "").strip().upper()
        if pt != tkr:
            continue
        qty = p.get("position") or p.get("count") or p.get("quantity") or 0
        try:
            return int(qty) != 0
        except Exception:
            return True
    return False


# -----------------------------
# Orders (kept simple)
# -----------------------------
def place_yes_buy(private_key, ticker: str, price_cents: int, count: int):
    body = {
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": int(count),
        "yes_price": int(price_cents),
        "client_order_id": str(uuid.uuid4()),
        "post_only": bool(POST_ONLY),
    }

    # Orders moved to elections base for you
    code, data, signed_path = _request_json(KALSHI_API_BASE, private_key, "POST", "/trade-api/v2/portfolio/orders", body=body)
    if code in (200, 201):
        return
    raise RuntimeError(f"Order failed {code} {signed_path}: {data}")


# -----------------------------
# Main loop
# -----------------------------
def main():
    if not SERIES_PREFIX:
        raise RuntimeError("Missing SERIES_PREFIX env var")
    if not KALSHI_KEY_ID:
        raise RuntimeError("Missing KALSHI_KEY_ID env var")
    if not KALSHI_PRIVATE_KEY_B64:
        raise RuntimeError("Missing KALSHI_PRIVATE_KEY_B64 env var")

    private_key = load_private_key_from_b64(KALSHI_PRIVATE_KEY_B64)
    log.info("[BOOT] Private key loaded OK (b64)")
    log.info("[BOOT] LIVE BTC BOT STARTED")
    log.info("[BOOT] SERIES_PREFIX=%s", SERIES_PREFIX)

    active_ticker = resolve_active_market(private_key)
    if not active_ticker:
        log.error("[MARKET] No active market found for %s", SERIES_PREFIX)
        # keep running; resolver can be improved later
        time.sleep(10)
        return

    log.info("[MARKET] Active ticker=%s", active_ticker)

    while True:
        try:
            # Only 1 open contract at a time
            if has_position(private_key, active_ticker):
                log.info("[POS] Already have position in %s -> skipping", active_ticker)
                time.sleep(POLL_SECONDS)
                continue

            _ = get_orderbook(private_key, active_ticker)  # keep as a heartbeat read

            # YES-only buy (your current strategy)
            place_yes_buy(private_key, active_ticker, BUY_PRICE_CENTS, BASE_SIZE)
            log.info("[ORDER] placed YES buy %s @ %dc x%d", active_ticker, BUY_PRICE_CENTS, BASE_SIZE)

        except Exception as e:
            log.exception("[LOOPERR] %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()