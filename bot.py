import os
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


# -----------------------------
# Market resolution (MICRO-CHANGE #6: probe)
# -----------------------------
def list_markets_with_params(private_key, params: Dict[str, Any]) -> Tuple[int, List[Dict[str, Any]], Any]:
    code, data = request(private_key, "GET", "/trade-api/v2/markets", params=params)
    if code != 200:
        return code, [], data
    markets = data.get("markets", [])
    if not isinstance(markets, list):
        markets = []
    return code, markets, data


def resolve_active_ticker_probe(private_key, series_prefix: str, series_ticker: str) -> Tuple[Optional[str], bool]:
    """
    Returns (ticker, hit_rate_limit)

    We don't guess one endpoint anymore.
    We try a tiny set of likely filters and log which one works.
    """
    # Build candidates (keep it very small to avoid spamming)
    # NOTE: Kalshi sometimes uses uppercase identifiers even if UI path is lowercase.
    sp = (series_prefix or "").strip()
    st = (series_ticker or "").strip()
    st_upper = st.upper()
    sp_upper = sp.upper()

    candidates: List[Tuple[str, Dict[str, Any]]] = [
        ("series_ticker+open", {"limit": 200, "series_ticker": st_upper, "status": "open"}),
        ("series_ticker+active", {"limit": 200, "series_ticker": st_upper, "status": "active"}),
        # Some APIs use "event_ticker" for grouping (try both forms)
        ("event_ticker+open(st)", {"limit": 200, "event_ticker": st_upper, "status": "open"}),
        ("event_ticker+open(sp)", {"limit": 200, "event_ticker": sp_upper, "status": "open"}),
        # Last resort: omit status (still filtered)
        ("series_ticker(no status)", {"limit": 200, "series_ticker": st_upper}),
    ]

    for label, params in candidates:
        code, markets, err = list_markets_with_params(private_key, params)

        if code == 429:
            log.warning("[RL] Resolver probe rate-limited on %s (429).", label)
            return None, True

        if code != 200:
            log.warning("[RESOLVE] probe=%s failed code=%s err=%s", label, code, err)
            continue

        if markets:
            ticker = pick_soonest_future_market(markets)
            log.info("[RESOLVE] probe=%s markets=%d picked=%s", label, len(markets), ticker)
            if ticker:
                return ticker, False

        log.warning("[RESOLVE] probe=%s returned 200 but markets empty", label)

    return None, False


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
    # keep your existing env var aliases
    series_prefix = (
        os.getenv("SERIES_PREFIX", "").strip()
        or os.getenv("Series_PREFIC", "").strip()
        or os.getenv("SERIES_PREFIC", "").strip()
    )

    if not series_prefix:
        log.error("[CONFIG] Missing SERIES prefix. Set SERIES_PREFIX (preferred) or your existing Series_PREFIC.")
        while True:
            time.sleep(30)

    # explicit SERIES_TICKER if you want, else derive
    series_ticker = os.getenv("SERIES_TICKER", "").strip() or series_prefix.upper()

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
        "[BOOT] BASE_URL=%s SERIES_PREFIX=%s SERIES_TICKER=%s POLL_SECONDS=%.2f RESOLVE_EVERY_SECONDS=%d BUY_PRICE_CENTS=%d BASE_SIZE=%d POST_ONLY=%s RESOLVE_BACKOFF_SECONDS=%d",
        BASE_URL, series_prefix, series_ticker, POLL_SECONDS, RESOLVE_EVERY_SECONDS, BUY_PRICE_CENTS, BASE_SIZE, POST_ONLY, RESOLVE_BACKOFF_SECONDS
    )

    active_ticker = None
    next_resolve_at = 0.0

    while True:
        try:
            now = time.time()

            if active_ticker is None and now < next_resolve_at:
                time.sleep(1)
                continue

            if active_ticker is None or now >= next_resolve_at:
                new_ticker, hit_rl = resolve_active_ticker_probe(private_key, series_prefix, series_ticker)

                if hit_rl:
                    next_resolve_at = now + RESOLVE_BACKOFF_SECONDS
                    active_ticker = None
                    continue

                if new_ticker and new_ticker != active_ticker:
                    log.info("[MARKET] Switched active ticker -> %s", new_ticker)
                    active_ticker = new_ticker

                if not active_ticker:
                    log.warning("[MARKET] No active ticker resolved yet for series_ticker=%s (cooldown %ds)", series_ticker, RESOLVE_BACKOFF_SECONDS)
                    next_resolve_at = now + RESOLVE_BACKOFF_SECONDS
                    continue

                next_resolve_at = now + RESOLVE_EVERY_SECONDS

            if not active_ticker:
                time.sleep(1)
                continue

            ob = get_orderbook(private_key, active_ticker)
            bid, ask = parse_yes_best(ob)

            if bid and ask and LOG_SPREAD:
                spread = ask["price_cents"] - bid["price_cents"]
                log.info("[SPREAD] %s YES bid=%dc ask=%dc spread=%dc", active_ticker, bid["price_cents"], ask["price_cents"], spread)

            create_order_yes_buy(private_key, active_ticker, BUY_PRICE_CENTS, BASE_SIZE)

        except Exception as e:
            log.exception("[LOOPERR] %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main() 