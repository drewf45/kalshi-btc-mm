import os
import json
import time
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
# Env / Config
# -----------------------------
ELECTIONS_BASE_URL = os.getenv("ELECTIONS_BASE_URL", "https://api.elections.kalshi.com").rstrip("/")
TRADING_BASE_URL = os.getenv("TRADING_BASE_URL", "https://trading-api.kalshi.com").rstrip("/")

# Your BTC 15m recurring series prefix
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15m")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "99"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))

POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")
IMPROVE_TICKS = int(os.getenv("IMPROVE_TICKS", "1"))  # maker improvement from best bid

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() in ("1", "true", "yes", "y")
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() in ("1", "true", "yes", "y")

SUBACCOUNT = os.getenv("SUBACCOUNT", "").strip()

KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

if ENABLE_TRADING and not CONFIRM_LIVE_TRADING:
    raise RuntimeError("Refusing to trade: ENABLE_TRADING=true but CONFIRM_LIVE_TRADING!=true")


# -----------------------------
# Helpers
# -----------------------------
def now_utc_ms() -> str:
    return str(int(time.time() * 1000))


def load_private_key_from_b64(b64_str: str):
    raw = base64.b64decode(b64_str)
    return serialization.load_pem_private_key(raw, password=None)


def sign_request(private_key, timestamp_ms: str, method: str, path_with_query: str, body: str) -> str:
    """
    Kalshi signature: RSA-PSS over string: <timestamp><METHOD><path_with_query><body>
    """
    msg = (timestamp_ms + method.upper() + path_with_query + body).encode("utf-8")
    sig = private_key.sign(
        msg,
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def _is_api_moved_response(data: Any) -> bool:
    # Kalshi sometimes returns 401 with a raw string that says API moved.
    if isinstance(data, dict) and "raw" in data and isinstance(data["raw"], str):
        return "API has been moved" in data["raw"]
    if isinstance(data, str):
        return "API has been moved" in data
    return False


def _req_json(
    private_key,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    if not path.startswith("/"):
        path = "/" + path

    qs = ""
    if params:
        qs = "?" + urlencode(params, doseq=True)

    path_with_query = path + qs
    url_base = (base_url or ELECTIONS_BASE_URL).rstrip("/")
    url = url_base + path_with_query

    body_str = ""
    if json_body is not None:
        body_str = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)

    ts = now_utc_ms()
    sig = sign_request(private_key, ts, method, path_with_query, body_str)

    headers = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }
    if SUBACCOUNT:
        headers["KALSHI-SUBACCOUNT"] = SUBACCOUNT

    log.info("[REQ] %s %s%s", method.upper(), path, (qs if qs else ""))

    r = requests.request(
        method=method.upper(),
        url=url,
        headers=headers,
        data=body_str if body_str else None,
        timeout=15,
    )

    # best-effort parse
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path_with_query}: {data}")

    return data


# -----------------------------
# API wrappers
# -----------------------------
def market_get(private_key, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _req_json(private_key, "GET", path, params=params, base_url=ELECTIONS_BASE_URL)


def portfolio_get(private_key, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _req_json(private_key, "GET", path, params=params, base_url=ELECTIONS_BASE_URL)


def portfolio_post(private_key, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return _req_json(private_key, "POST", path, json_body=body, base_url=ELECTIONS_BASE_URL)


# -----------------------------
# Market resolution
# -----------------------------
def _extract_markets(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Kalshi docs: GET /trade-api/v2/markets returns {"markets":[...], ...}
    if isinstance(resp, dict) and isinstance(resp.get("markets"), list):
        return resp["markets"]
    # fallback
    if isinstance(resp, dict) and isinstance(resp.get("data"), list):
        return resp["data"]
    return []


def resolve_active_market(private_key) -> Optional[str]:
    """
    Find current open/active market ticker for the series.
    We probe multiple variants (upper/lower, series_ticker/event_ticker, status=open/active).
    """
    probes = []

    # prefer official: series_ticker
    probes.append(("series_ticker", SERIES_PREFIX.upper(), "open", "upper"))
    probes.append(("series_ticker", SERIES_PREFIX.upper(), "active", "upper"))
    probes.append(("series_ticker", SERIES_PREFIX, "open", "raw"))
    probes.append(("series_ticker", SERIES_PREFIX, "active", "raw"))

    # sometimes users confuse "event_ticker"; keep as fallback
    probes.append(("event_ticker", SERIES_PREFIX.upper(), "open", "upper"))
    probes.append(("event_ticker", SERIES_PREFIX.upper(), "active", "upper"))
    probes.append(("event_ticker", SERIES_PREFIX, "open", "raw"))
    probes.append(("event_ticker", SERIES_PREFIX, "active", "raw"))

    # no status
    probes.append(("series_ticker", SERIES_PREFIX.upper(), None, "upper"))
    probes.append(("event_ticker", SERIES_PREFIX.upper(), None, "upper"))
    probes.append(("series_ticker", SERIES_PREFIX, None, "raw"))
    probes.append(("event_ticker", SERIES_PREFIX, None, "raw"))
    probes.append(("series_ticker", SERIES_PREFIX.lower(), "open", "lower"))
    probes.append(("series_ticker", SERIES_PREFIX.lower(), "active", "lower"))

    for key, val, status, label in probes:
        params = {"limit": 200, key: val}
        if status:
            params["status"] = status
        try:
            resp = market_get(private_key, "/trade-api/v2/markets", params=params)
            markets = _extract_markets(resp)
            if markets:
                # pick earliest closing / soonest? For BTC 15m, the list is usually 1 anyway.
                picked = markets[0].get("ticker") or markets[0].get("market_ticker")
                log.info(
                    "[RESOLVE] probe=%s %s (%s) markets=%d picked=%s",
                    key,
                    (status if status else "no-status"),
                    label,
                    len(markets),
                    picked,
                )
                if picked:
                    return str(picked)
        except Exception as e:
            log.warning("[RESOLVE] probe=%s %s (%s) failed: %s", key, (status if status else "no-status"), label, e)

    return None


# -----------------------------
# Orderbook parsing (BIDS ONLY)
# -----------------------------
def _best_bid_from_levels(levels: Any) -> Optional[Tuple[int, int]]:
    """
    levels can be like:
      [[price_cents, qty], ...]
      [[price_cents, qty, order_count], ...]
      [[price_cents], ...]  (legacy)
    We return (price_cents, qty_int) for the best (first) level.
    Docs: returned best-to-worst.
    """
    if not isinstance(levels, list) or not levels:
        return None
    first = levels[0]
    if not isinstance(first, list) or not first:
        return None

    # price
    try:
        price = int(first[0])
    except Exception:
        return None

    qty = 0
    if len(first) >= 2:
        try:
            qty = int(first[1])
        except Exception:
            qty = 0
    return (price, qty)


def parse_yes_best_bid_ask(orderbook_obj: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Kalshi orderbook endpoint returns bids only:
      - yes is YES bids
      - no is NO bids
    YES ask can be derived from NO best bid: yes_ask = 100 - no_best_bid
    """
    yes_levels = orderbook_obj.get("yes")
    no_levels = orderbook_obj.get("no")

    yes_best = _best_bid_from_levels(yes_levels)
    no_best = _best_bid_from_levels(no_levels)

    yes_best_bid = yes_best[0] if yes_best else None
    yes_best_ask = None

    if no_best:
        yes_best_ask = 100 - int(no_best[0])

    return yes_best_bid, yes_best_ask


# -----------------------------
# Portfolio checks
# -----------------------------
def has_position(private_key, market_ticker: str) -> bool:
    """
    Keep it simple: if you hold ANY net position in this market ticker, don't open another.
    """
    resp = portfolio_get(private_key, "/trade-api/v2/portfolio/positions")
    positions = resp.get("positions") or resp.get("data") or []
    if not isinstance(positions, list):
        return False

    for p in positions:
        t = p.get("ticker") or p.get("market_ticker")
        if t == market_ticker:
            # net position size might be "position", "quantity", etc.
            for key in ("position", "quantity", "count", "contracts"):
                if key in p:
                    try:
                        return int(p[key]) != 0
                    except Exception:
                        pass
            # if we can't parse, assume present means "has something"
            return True
    return False


def has_open_order(private_key, market_ticker: str) -> bool:
    """
    Optional extra guard: if there is already an open order for this ticker, don't spam.
    """
    try:
        resp = portfolio_get(
            private_key,
            "/trade-api/v2/portfolio/orders",
            params={"limit": 200, "status": "open"},
        )
    except Exception:
        return False

    orders = resp.get("orders") or resp.get("data") or []
    if not isinstance(orders, list):
        return False

    for o in orders:
        t = o.get("ticker") or o.get("market_ticker")
        if t == market_ticker:
            return True
    return False


# -----------------------------
# Trading
# -----------------------------
def place_yes_buy(private_key, market_ticker: str, price_cents: int, qty: int, post_only: bool) -> None:
    body: Dict[str, Any] = {
        "ticker": market_ticker,
        "action": "buy",
        "side": "yes",
        "count": qty,
        "type": "limit",
        "price": price_cents,
    }

    # Kalshi create-order supports "client_order_id" etc; keep minimal.
    # Post-only: docs use "time_in_force" or "post_only" depending on version.
    # Many accounts accept "post_only": true.
    if post_only:
        body["post_only"] = True

    data = portfolio_post(private_key, "/trade-api/v2/portfolio/orders", body)
    log.info("[ORDER] placed YES buy ticker=%s price=%sc qty=%s resp_keys=%s", market_ticker, price_cents, qty, list(data.keys()))


def compute_maker_buy_price(target_cents: int, yes_best_bid: Optional[int], yes_best_ask: Optional[int]) -> int:
    """
    Maker logic:
      - If we know the spread, never cross (avoid "post only cross").
      - Prefer to improve best bid by IMPROVE_TICKS, but cap below ask.
    """
    px = target_cents

    if yes_best_bid is not None:
        px = max(px, yes_best_bid + max(0, IMPROVE_TICKS))

    if yes_best_ask is not None:
        # must be strictly below ask to avoid crossing
        px = min(px, yes_best_ask - 1)

    # clamp 1..99 for bids
    px = max(1, min(99, px))
    return px


# -----------------------------
# Main loop
# -----------------------------
def main() -> None:
    log.info(
        "[BOOT] ELECTIONS_BASE_URL=%s TRADING_BASE_URL=%s SERIES_PREFIX=%s POLL_SECONDS=%.2f BUY_PRICE_CENTS=%s BASE_SIZE=%s POST_ONLY=%s IMPROVE_TICKS=%s ENABLE_TRADING=%s CONFIRM_LIVE_TRADING=%s SUBACCOUNT=%s",
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

    if not KEY_ID:
        raise RuntimeError("Missing KALSHI_KEY_ID env var")
    if not PRIVATE_KEY_B64:
        raise RuntimeError("Missing KALSHI_PRIVATE_KEY_B64 env var")

    private_key = load_private_key_from_b64(PRIVATE_KEY_B64)
    log.info("[BOOT] Private key loaded OK (b64)")
    log.info("[BOOT] LIVE BTC BOT STARTED")

    active_ticker: Optional[str] = None
    last_resolve = 0.0

    while True:
        try:
            # Re-resolve periodically (market rolls every 15m)
            if (time.time() - last_resolve) > 15:
                last_resolve = time.time()
                new_ticker = resolve_active_market(private_key)
                if not new_ticker:
                    log.error("[MARKET] No active market found for series %s", SERIES_PREFIX)
                    time.sleep(max(1.0, POLL_SECONDS))
                    continue
                if new_ticker != active_ticker:
                    active_ticker = new_ticker
                    log.info("[MARKET] Switched active ticker -> %s", active_ticker)

            if not active_ticker:
                time.sleep(max(1.0, POLL_SECONDS))
                continue

            # one open contract / one open order at a time
            if has_position(private_key, active_ticker):
                log.info("[GUARD] Already have position in %s; skipping.", active_ticker)
                time.sleep(POLL_SECONDS)
                continue

            if has_open_order(private_key, active_ticker):
                log.info("[GUARD] Already have an open order in %s; skipping.", active_ticker)
                time.sleep(POLL_SECONDS)
                continue

            ob = market_get(private_key, f"/trade-api/v2/markets/{active_ticker}/orderbook")
            orderbook_obj = ob.get("orderbook") or {}
            log.info(
                "[OB] %s orderbook_preview=%s",
                active_ticker,
                {"keys": list(orderbook_obj.keys()), "yes": orderbook_obj.get("yes")},
            )

            yes_bid, yes_ask = parse_yes_best_bid_ask(orderbook_obj)

            if yes_bid is None and yes_ask is None:
                log.warning("[PARSE] Could not parse YES bid/ask for %s; skipping.", active_ticker)
                time.sleep(POLL_SECONDS)
                continue

            log.info("[SPREAD] %s YES best_bid=%s best_ask=%s", active_ticker, yes_bid, yes_ask)

            px = compute_maker_buy_price(BUY_PRICE_CENTS, yes_bid, yes_ask)
            log.info("[QUOTE] %s placing YES buy at %sc (target=%sc post_only=%s)", active_ticker, px, BUY_PRICE_CENTS, POST_ONLY)

            if ENABLE_TRADING:
                place_yes_buy(private_key, active_ticker, px, BASE_SIZE, POST_ONLY)
            else:
                log.info("[DRYRUN] ENABLE_TRADING=false; not placing order.")

        except Exception as e:
            log.error("[LOOPERR] %s", e, exc_info=True)

        # ✅ FIX: ensure parentheses are closed (this is the SyntaxError you hit)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()