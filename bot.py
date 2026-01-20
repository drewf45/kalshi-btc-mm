import os
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
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Config
# -----------------------------
# READ endpoints (markets/orderbook) still work on trading-api for many accounts.
READ_BASE = os.getenv("KALSHI_READ_BASE", "https://trading-api.kalshi.com").rstrip("/")
# Fallback (and some migrated endpoints) on elections base
FALLBACK_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").rstrip("/")

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
RESOLVE_EVERY_SECONDS = int(os.getenv("RESOLVE_EVERY_SECONDS", "20"))
RESOLVE_BACKOFF_SECONDS = int(os.getenv("RESOLVE_BACKOFF_SECONDS", "60"))

BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", os.getenv("MAX_BUY_PRICE_CENTS", "1")))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))
POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")

LOG_SPREAD = os.getenv("LOG_SPREAD", "true").lower() in ("1", "true", "yes", "y")
LOG_ORDERBOOK_SAMPLE = os.getenv("LOG_ORDERBOOK_SAMPLE", "true").lower() in ("1", "true", "yes", "y")


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


def _req_json(
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
    resp = requests.request(
        method=method,
        url=url,
        headers=kalshi_headers(private_key, method, signed_path),
        params=params,
        json=body,
        timeout=timeout,
    )
    code = resp.status_code
    try:
        data = resp.json()
    except Exception:
        data = resp.text
    log.info("[REQ] %s %s -> %s", method, signed_path, code)
    return code, data, signed_path


# -----------------------------
# Market resolution
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


def list_markets(private_key, params: Dict[str, Any]) -> Tuple[int, List[Dict[str, Any]], Any]:
    # Try READ_BASE first, fallback to FALLBACK_BASE
    code, data, _ = _req_json(READ_BASE, private_key, "GET", "/trade-api/v2/markets", params=params)
    if code == 200:
        mk = data.get("markets", [])
        return 200, mk if isinstance(mk, list) else [], data

    code2, data2, _ = _req_json(FALLBACK_BASE, private_key, "GET", "/trade-api/v2/markets", params=params)
    if code2 == 200:
        mk = data2.get("markets", [])
        return 200, mk if isinstance(mk, list) else [], data2

    return code2, [], data2


def resolve_active_ticker_probe(private_key, series_prefix: str) -> Tuple[Optional[str], bool]:
    sp = (series_prefix or "").strip().upper()
    if not sp:
        return None, False

    candidates: List[Tuple[str, Dict[str, Any]]] = [
        ("series_ticker+open", {"limit": 200, "series_ticker": sp, "status": "open"}),
        ("series_ticker+active", {"limit": 200, "series_ticker": sp, "status": "active"}),
        ("event_ticker+open", {"limit": 200, "event_ticker": sp, "status": "open"}),
        ("event_ticker+active", {"limit": 200, "event_ticker": sp, "status": "active"}),
        ("series_ticker(no status)", {"limit": 200, "series_ticker": sp}),
        ("event_ticker(no status)", {"limit": 200, "event_ticker": sp}),
    ]

    for label, params in candidates:
        code, markets, err = list_markets(private_key, params)

        if code == 429:
            log.warning("[RL] Resolver rate-limited on %s (429).", label)
            return None, True

        if code != 200:
            log.warning("[RESOLVE] probe=%s failed code=%s err=%s", label, code, err)
            continue

        if markets:
            picked = pick_soonest_future_market(markets)
            log.info("[RESOLVE] probe=%s markets=%d picked=%s", label, len(markets), picked)
            if picked:
                return picked, False
        else:
            log.warning("[RESOLVE] probe=%s returned 200 but markets empty", label)

    return None, False


# -----------------------------
# Orderbook + parsing
# -----------------------------
def get_orderbook(private_key, ticker: str) -> Dict[str, Any]:
    path = f"/trade-api/v2/markets/{ticker}/orderbook"
    code, data, _ = _req_json(READ_BASE, private_key, "GET", path)
    if code != 200:
        code2, data2, _ = _req_json(FALLBACK_BASE, private_key, "GET", path)
        if code2 != 200:
            raise RuntimeError(f"Orderbook failed: {data2}")
        data = data2
    ob = data.get("orderbook", {})
    return ob if isinstance(ob, dict) else {}


def _extract_price_cents(level: Dict[str, Any]) -> Optional[int]:
    for k in ("price_cents", "price", "yes_price", "no_price"):
        if k in level and level[k] is not None:
            try:
                return int(level[k])
            except Exception:
                pass
    return None


def _extract_qty(level: Dict[str, Any]) -> int:
    for k in ("count", "qty", "quantity", "shares", "size"):
        if k in level and level[k] is not None:
            try:
                return int(level[k])
            except Exception:
                pass
    return 0


def parse_yes_best(orderbook: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if not isinstance(orderbook, dict):
        return None, None

    y = orderbook.get("yes")

    # Shape A
    if isinstance(y, dict):
        bids = y.get("bids", []) if isinstance(y.get("bids", []), list) else []
        asks = y.get("asks", []) if isinstance(y.get("asks", []), list) else []

        def norm(level: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if not isinstance(level, dict):
                return None
            p = _extract_price_cents(level)
            if p is None:
                return None
            return {"price_cents": p, "count": _extract_qty(level), "raw": level}

        nbids = [x for x in (norm(l) for l in bids) if x]
        nasks = [x for x in (norm(l) for l in asks) if x]

        best_bid = max(nbids, key=lambda x: x["price_cents"]) if nbids else None
        best_ask = min(nasks, key=lambda x: x["price_cents"]) if nasks else None
        return best_bid, best_ask

    # Shape B
    if isinstance(y, list):
        bids: List[Dict[str, Any]] = []
        asks: List[Dict[str, Any]] = []

        for item in y:
            if not isinstance(item, dict):
                continue
            p = _extract_price_cents(item)
            if p is None:
                continue
            side = str(item.get("side") or item.get("type") or item.get("action") or "").lower()
            lvl = {"price_cents": p, "count": _extract_qty(item), "raw": item}

            if side in ("bid", "buy", "bids"):
                bids.append(lvl)
            elif side in ("ask", "sell", "asks", "offer"):
                asks.append(lvl)
            else:
                bids.append(lvl)

        best_bid = max(bids, key=lambda x: x["price_cents"]) if bids else None
        best_ask = min(asks, key=lambda x: x["price_cents"]) if asks else None
        return best_bid, best_ask

    return None, None


# -----------------------------
# Portfolio endpoints (MICRO FIX HERE)
# -----------------------------
def portfolio_get(private_key, paths: List[str]) -> Any:
    """
    MICRO FIX:
    Try multiple possible portfolio paths because positions is 404 on /v2 for your account.
    We attempt each in order; 404 -> try next.
    """
    last_err = None
    for path in paths:
        code, data, signed_path = _req_json(FALLBACK_BASE, private_key, "GET", path)
        if code == 200:
            return data
        if code == 404:
            last_err = (code, data, signed_path)
            continue
        # other errors are real failures
        raise RuntimeError(f"Portfolio GET failed {code} {signed_path}: {data}")
    raise RuntimeError(f"Portfolio GET 404 on all candidates. Last={last_err}")


def has_position(private_key, ticker: str) -> bool:
    # MICRO FIX: try /v2 first then fall back to /trade-api/v2
    data = portfolio_get(
        private_key,
        paths=[
            "/v2/portfolio/positions",
            "/trade-api/v2/portfolio/positions",
        ],
    )

    # tolerate different shapes
    positions = data.get("positions") if isinstance(data, dict) else None
    if not isinstance(positions, list):
        return False

    for p in positions:
        if not isinstance(p, dict):
            continue
        if str(p.get("ticker") or "").strip().upper() == ticker.strip().upper():
            # any non-zero position counts as "has position"
            qty = p.get("position") or p.get("count") or p.get("quantity") or 0
            try:
                return int(qty) != 0
            except Exception:
                return True
    return False


def place_yes_buy(private_key, ticker: str, price: int, count: int):
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

    # Try new orders path first; if 404, try old.
    for path in ("/v2/portfolio/orders", "/trade-api/v2/portfolio/orders"):
        code, data, signed_path = _req_json(FALLBACK_BASE, private_key, "POST", path, body=body)
        if code in (200, 201):
            return
        if code == 404:
            continue
        raise RuntimeError(f"Order failed {code} {signed_path}: {data}")

    raise RuntimeError("Order failed: both order endpoints returned 404")


# -----------------------------
# Main loop
# -----------------------------
def main():
    series_prefix = os.getenv("SERIES_PREFIX", "").strip()
    if not series_prefix:
        log.error("[CONFIG] Missing SERIES_PREFIX")
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
    log.info("[BOOT] Private key loaded OK (b64)")
    log.info("[BOOT] LIVE BTC BOT STARTED")
    log.info("[BOOT] SERIES_PREFIX=%s", series_prefix)

    active_ticker: Optional[str] = None
    next_resolve_at = 0.0

    ob_sampled = False
    parse_warned = False

    while True:
        try:
            now = time.time()

            if active_ticker is None and now < next_resolve_at:
                time.sleep(1)
                continue

            if active_ticker is None or now >= next_resolve_at:
                new_ticker, hit_rl = resolve_active_ticker_probe(private_key, series_prefix)

                if hit_rl:
                    next_resolve_at = now + RESOLVE_BACKOFF_SECONDS
                    active_ticker = None
                    continue

                if new_ticker and new_ticker != active_ticker:
                    log.info("[MARKET] Switched active ticker -> %s", new_ticker)
                    active_ticker = new_ticker
                    ob_sampled = False
                    parse_warned = False

                if not active_ticker:
                    log.error("[MARKET] No active market found for series %s", series_prefix)
                    next_resolve_at = now + RESOLVE_BACKOFF_SECONDS
                    time.sleep(1)
                    continue

                next_resolve_at = now + RESOLVE_EVERY_SECONDS

            if not active_ticker:
                time.sleep(1)
                continue

            # Only 1 open contract at a time: skip placing if we already have a position
            if has_position(private_key, active_ticker):
                log.info("[POS] Already have position in %s -> skipping", active_ticker)
                time.sleep(POLL_SECONDS)
                continue

            ob = get_orderbook(private_key, active_ticker)

            if LOG_ORDERBOOK_SAMPLE and not ob_sampled:
                y = ob.get("yes") if isinstance(ob, dict) else None
                preview = None
                if isinstance(y, list):
                    preview = y[:3]
                elif isinstance(y, dict):
                    preview = {
                        "keys": list(y.keys())[:10],
                        "bids_preview": (y.get("bids") or [])[:2] if isinstance(y.get("bids"), list) else None,
                        "asks_preview": (y.get("asks") or [])[:2] if isinstance(y.get("asks"), list) else None,
                    }
                else:
                    preview = {"type": str(type(y)), "value_preview": str(y)[:200]}
                log.info("[OB] %s yes_type=%s yes_preview=%s", active_ticker, type(y).__name__, preview)
                ob_sampled = True

            bid, ask = parse_yes_best(ob)

            if not bid and not ask:
                if not parse_warned:
                    log.warning("[PARSE] Could not parse YES bid/ask for %s; skipping.", active_ticker)
                    parse_warned = True
                time.sleep(POLL_SECONDS)
                continue

            if bid and ask and LOG_SPREAD:
                spread = ask["price_cents"] - bid["price_cents"]
                log.info(
                    "[SPREAD] %s YES bid=%dc qty=%d | ask=%dc qty=%d | spread=%dc",
                    active_ticker,
                    bid["price_cents"],
                    bid["count"],
                    ask["price_cents"],
                    ask["count"],
                    spread,
                )

            # YES-only buy
            place_yes_buy(private_key, active_ticker, BUY_PRICE_CENTS, BASE_SIZE)

        except Exception as e:
            log.exception("[LOOPERR] %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()