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
# The key fix: Kalshi "markets/orderbook" reads are now served from api.elections.kalshi.com,
# while portfolio endpoints still work from trading-api.kalshi.com (for many accounts/keys).
ELECTIONS_BASE_URL = os.getenv("KALSHI_ELECTIONS_BASE_URL", "https://api.elections.kalshi.com").strip()
TRADING_BASE_URL = os.getenv("KALSHI_TRADING_BASE_URL", "https://trading-api.kalshi.com").strip()

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "").strip()  # e.g. KXBTC15m

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
RESOLVE_EVERY_SECONDS = int(os.getenv("RESOLVE_EVERY_SECONDS", "20"))
RESOLVE_BACKOFF_SECONDS = int(os.getenv("RESOLVE_BACKOFF_SECONDS", "60"))

BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "99"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))

POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")
IMPROVE_TICKS = int(os.getenv("IMPROVE_TICKS", "1"))

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "true").lower() in ("1", "true", "yes", "y")
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() in ("1", "true", "yes", "y")

LOG_SPREAD = os.getenv("LOG_SPREAD", "true").lower() in ("1", "true", "yes", "y")
LOG_ORDERBOOK_SAMPLE = os.getenv("LOG_ORDERBOOK_SAMPLE", "true").lower() in ("1", "true", "yes", "y")

SUBACCOUNT = os.getenv("SUBACCOUNT", "").strip()  # optional


# -----------------------------
# Helpers
# -----------------------------
def now_utc_ts_ms() -> int:
    return int(time.time() * 1000)


def load_private_key_from_b64(b64: str):
    key_bytes = base64.b64decode(b64)
    return serialization.load_pem_private_key(key_bytes, password=None)


def sign_request(private_key, timestamp_ms: int, method: str, signed_path: str) -> str:
    # Kalshi expects: timestamp + METHOD + PATH(+query)
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


def kalshi_headers(private_key, method: str, signed_path: str) -> Dict[str, str]:
    ts = now_utc_ts_ms()
    h = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign_request(private_key, ts, method, signed_path),
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
    }
    # optional, only if you're actually using it
    if SUBACCOUNT:
        h["KALSHI-ACCESS-SUBACCOUNT"] = SUBACCOUNT
    return h


def _route_base_url(path: str) -> str:
    """
    KEY FIX for your current failure:
    - /trade-api/v2/markets* and /orderbook reads => elections host
    - /trade-api/v2/portfolio/* (positions/orders) => trading host
    """
    p = path or ""
    if p.startswith("/trade-api/v2/markets") or "/orderbook" in p:
        return ELECTIONS_BASE_URL
    if p.startswith("/trade-api/v2/portfolio"):
        return TRADING_BASE_URL
    # default safe choice: elections (where the "moved" message points)
    return ELECTIONS_BASE_URL


def request_json(private_key, method: str, path: str, params=None, body=None) -> Tuple[int, Any, str]:
    signed_path = f"{path}?{urlencode(params)}" if params else path
    base_url = _route_base_url(path)
    url = f"{base_url}{path}"

    resp = requests.request(
        method=method,
        url=url,
        headers=kalshi_headers(private_key, method, signed_path),
        params=params,
        json=body,
        timeout=15,
    )

    code = resp.status_code
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}

    log.info("[REQ] %s %s -> %s", method, signed_path, code)
    return code, data, signed_path


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
# Market resolution
# -----------------------------
def list_markets(private_key, params: Dict[str, Any]) -> Tuple[int, List[Dict[str, Any]], Any]:
    code, data, _ = request_json(private_key, "GET", "/trade-api/v2/markets", params=params)
    if code != 200:
        return code, [], data
    markets = data.get("markets", [])
    if not isinstance(markets, list):
        markets = []
    return code, markets, data


def resolve_active_ticker(private_key, series_prefix: str) -> Tuple[Optional[str], bool]:
    """
    Returns: (ticker_or_none, hit_rate_limit_bool)
    """
    sp = (series_prefix or "").strip()
    if not sp:
        return None, False

    sp_raw = sp
    sp_upper = sp.upper()
    sp_lower = sp.lower()

    probes: List[Tuple[str, Dict[str, Any]]] = [
        ("series_ticker open (upper)", {"limit": 200, "series_ticker": sp_upper, "status": "open"}),
        ("series_ticker active (upper)", {"limit": 200, "series_ticker": sp_upper, "status": "active"}),
        ("series_ticker open (raw)", {"limit": 200, "series_ticker": sp_raw, "status": "open"}),
        ("series_ticker active (raw)", {"limit": 200, "series_ticker": sp_raw, "status": "active"}),
        ("event_ticker open (upper)", {"limit": 200, "event_ticker": sp_upper, "status": "open"}),
        ("event_ticker active (upper)", {"limit": 200, "event_ticker": sp_upper, "status": "active"}),
        ("event_ticker open (raw)", {"limit": 200, "event_ticker": sp_raw, "status": "open"}),
        ("event_ticker active (raw)", {"limit": 200, "event_ticker": sp_raw, "status": "active"}),
        ("series_ticker no-status (upper)", {"limit": 200, "series_ticker": sp_upper}),
        ("event_ticker no-status (upper)", {"limit": 200, "event_ticker": sp_upper}),
        ("series_ticker no-status (raw)", {"limit": 200, "series_ticker": sp_raw}),
        ("event_ticker no-status (raw)", {"limit": 200, "event_ticker": sp_raw}),
        ("series_ticker open (lower)", {"limit": 200, "series_ticker": sp_lower, "status": "open"}),
        ("series_ticker active (lower)", {"limit": 200, "series_ticker": sp_lower, "status": "active"}),
    ]

    for label, params in probes:
        code, markets, err = list_markets(private_key, params)

        if code == 429:
            log.warning("[RL] Resolver probe rate-limited on %s (429).", label)
            return None, True

        if code != 200:
            log.warning("[RESOLVE] probe=%s failed: HTTP %s: %s", label, code, err)
            continue

        if markets:
            picked = pick_soonest_future_market(markets)
            log.info("[RESOLVE] probe=%s markets=%d picked=%s", label, len(markets), picked)
            if picked:
                return picked, False

        log.warning("[RESOLVE] probe=%s returned 200 but markets empty", label)

    return None, False


# -----------------------------
# Orderbook parsing
# -----------------------------
def get_orderbook(private_key, ticker: str) -> Dict[str, Any]:
    code, data, _ = request_json(private_key, "GET", f"/trade-api/v2/markets/{ticker}/orderbook")
    if code != 200:
        raise RuntimeError(f"Orderbook failed {code}: {data}")
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

    # Shape A: dict with bids/asks
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

        nbids = [norm(l) for l in bids]
        nasks = [norm(l) for l in asks]
        nbids = [x for x in nbids if x]
        nasks = [x for x in nasks if x]

        best_bid = max(nbids, key=lambda x: x["price_cents"]) if nbids else None
        best_ask = min(nasks, key=lambda x: x["price_cents"]) if nasks else None
        return best_bid, best_ask

    # Shape B: list of levels
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
                # unknown side => don't guess
                pass

        best_bid = max(bids, key=lambda x: x["price_cents"]) if bids else None
        best_ask = min(asks, key=lambda x: x["price_cents"]) if asks else None
        return best_bid, best_ask

    return None, None


# -----------------------------
# Portfolio / Trading
# -----------------------------
def get_positions(private_key) -> Dict[str, Any]:
    code, data, signed_path = request_json(private_key, "GET", "/trade-api/v2/portfolio/positions")
    if code != 200:
        raise RuntimeError(f"Positions failed {code} {signed_path}: {data}")
    return data if isinstance(data, dict) else {}


def has_position_in_ticker(positions_payload: Dict[str, Any], ticker: str) -> bool:
    """
    Keep conservative: if we can't parse positions shape, return False (do not block trading).
    """
    if not isinstance(positions_payload, dict):
        return False

    # Common shapes:
    # - {"positions":[{"ticker":"...","position":...}, ...]}
    # - {"market_positions":[...]}
    for key in ("positions", "market_positions", "portfolio_positions"):
        arr = positions_payload.get(key)
        if isinstance(arr, list):
            for p in arr:
                if not isinstance(p, dict):
                    continue
                if str(p.get("ticker") or "").strip() == ticker:
                    # any nonzero size?
                    for sk in ("position", "quantity", "count", "shares", "size"):
                        if p.get(sk) is not None:
                            try:
                                return int(p.get(sk)) != 0
                            except Exception:
                                pass
                    return True
    return False


def place_yes_buy(private_key, ticker: str, price_cents: int, count: int) -> None:
    """
    NOTE: Your most recent blocker was resolver 401 "moved". That is fixed by routing.
    This keeps the existing order format you’ve been using.
    """
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

    code, data, signed_path = request_json(private_key, "POST", "/trade-api/v2/portfolio/orders", body=body)
    if code not in (200, 201):
        raise RuntimeError(f"Order failed {code} {signed_path}: {data}")


def safe_maker_buy_price(desired: int, best_bid: Optional[int], best_ask: Optional[int]) -> Optional[int]:
    """
    If POST_ONLY is on, you cannot cross the ask.
    So enforce: price < best_ask (if best_ask exists).
    """
    p = int(desired)

    if best_ask is not None and POST_ONLY:
        if p >= best_ask:
            p = best_ask - max(1, IMPROVE_TICKS)

    # also keep in bounds
    if p < 1:
        return None
    if p > 99:
        p = 99

    # Optional: don't place below bid by a mile — but keep it simple.
    # If you want to be closer to the market, uncomment:
    # if best_bid is not None:
    #     p = max(p, min(99, best_bid + 0))

    return p


# -----------------------------
# Main loop
# -----------------------------
def main():
    if not SERIES_PREFIX:
        log.error("[CONFIG] Missing SERIES_PREFIX (e.g. KXBTC15m)")
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
        "[BOOT] ELECTIONS_BASE_URL=%s TRADING_BASE_URL=%s SERIES_PREFIX=%s POLL_SECONDS=%.2f BUY_PRICE_CENTS=%d BASE_SIZE=%d POST_ONLY=%s IMPROVE_TICKS=%d ENABLE_TRADING=%s CONFIRM_LIVE_TRADING=%s SUBACCOUNT=%s",
        ELECTIONS_BASE_URL,
        TRADING_BASE_URL,
        SERIES_PREFIX,
        POLL_SECONDS,
        BUY_PRICE_CENTS,
        BASE_SIZE,
        POST_ONLY,
        IMPROVE_TICKS,
        ENABLE_TRADING,
        CONFIRM_LIVE_TRADING,
        SUBACCOUNT,
    )
    log.info("[BOOT] Private key loaded OK (b64)")
    log.info("[BOOT] LIVE BTC BOT STARTED")

    if ENABLE_TRADING and not CONFIRM_LIVE_TRADING:
        log.error("[SAFETY] ENABLE_TRADING is true but CONFIRM_LIVE_TRADING is not true. Refusing to trade.")
        while True:
            time.sleep(30)

    active_ticker: Optional[str] = None
    next_resolve_at = 0.0
    ob_sampled = False
    parse_warned = False

    while True:
        try:
            now = time.time()

            # Resolve ticker periodically
            if active_ticker is None or now >= next_resolve_at:
                new_ticker, hit_rl = resolve_active_ticker(private_key, SERIES_PREFIX)

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
                    log.error("[MARKET] No active market found for series %s", SERIES_PREFIX)
                    next_resolve_at = now + RESOLVE_BACKOFF_SECONDS
                    time.sleep(1)
                    continue

                next_resolve_at = now + RESOLVE_EVERY_SECONDS

            # Fetch orderbook
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

            best_bid = bid["price_cents"] if bid else None
            best_ask = ask["price_cents"] if ask else None

            if bid and ask and LOG_SPREAD:
                spread = best_ask - best_bid
                log.info(
                    "[SPREAD] %s YES bid=%dc qty=%d | ask=%dc qty=%d | spread=%dc",
                    active_ticker,
                    bid["price_cents"],
                    bid["count"],
                    ask["price_cents"],
                    ask["count"],
                    spread,
                )

            # Optional: if you want "only one open contract at a time" (you mentioned it)
            # We'll skip placing new orders if you already have a position in this ticker.
            pos_payload = get_positions(private_key)
            if has_position_in_ticker(pos_payload, active_ticker):
                log.info("[POS] Already have position in %s; skipping new order.", active_ticker)
                time.sleep(POLL_SECONDS)
                continue

            # Maker-safe price (prevents "post only cross" if you use aggressive BUY_PRICE_CENTS)
            price_to_post = safe_maker_buy_price(BUY_PRICE_CENTS, best_bid, best_ask)
            if price_to_post is None:
                log.info("[ORDER] No safe maker price (desired=%d bid=%s ask=%s). Skipping.", BUY_PRICE_CENTS, best_bid, best_ask)
                time.sleep(POLL_SECONDS)
                continue

            if ENABLE_TRADING:
                place_yes_buy(private_key, active_ticker, price_to_post, BASE_SIZE)
                log.info("[ORDER] Placed YES BUY %s price=%dc size=%d post_only=%s", active_ticker, price_to_post, BASE_SIZE, POST_ONLY)
            else:
                log.info("[DRYRUN] Would place YES BUY %s price=%dc size=%d", active_ticker, price_to_post, BASE_SIZE)

        except Exception as e:
            log.exception("[LOOPERR] %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()