import os
import time
import json
import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

# -----------------------------
# Env + Logging
# -----------------------------
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")

ELECTIONS_BASE_URL = os.getenv("ELECTIONS_BASE_URL", "https://api.elections.kalshi.com").rstrip("/")
TRADING_BASE_URL = os.getenv("TRADING_BASE_URL", "https://trading-api.kalshi.com").rstrip("/")  # optional for public
# We'll use ELECTIONS_BASE_URL for ALL signed (portfolio) calls, per migration.

API_KEY_ID = os.getenv("KALSHI_API_KEY", "").strip()  # should be the UUID "API Key ID"
PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15m").strip()
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1.0"))
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "99"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))
POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")
IMPROVE_TICKS = int(os.getenv("IMPROVE_TICKS", "1"))

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() in ("1", "true", "yes", "y")
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() in ("1", "true", "yes", "y")

SUBACCOUNT = os.getenv("SUBACCOUNT", "").strip()  # optional header, if you use subaccounts

# -----------------------------
# Helpers
# -----------------------------
def now_ms() -> str:
    return str(int(time.time() * 1000))

def load_private_key_from_b64(b64: str):
    if not b64:
        raise RuntimeError("Missing KALSHI_PRIVATE_KEY_B64")
    key_bytes = base64.b64decode(b64)
    return serialization.load_pem_private_key(key_bytes, password=None)

def sign_request(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """
    Kalshi signing per docs:
      signature message = timestamp + METHOD + path_without_query
    IMPORTANT: strip query params. Do NOT sign body.  [oai_citation:1‡Kalshi API Documentation](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)
    """
    path_wo_query = path.split("?", 1)[0]
    msg = f"{timestamp_ms}{method.upper()}{path_wo_query}".encode("utf-8")
    sig = private_key.sign(
        msg,
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")

def _headers(private_key, method: str, path: str) -> Dict[str, str]:
    ts = now_ms()
    sig = sign_request(private_key, ts, method, path)
    h = {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }
    if SUBACCOUNT:
        # Some setups support subaccount header; leaving it optional.
        h["KALSHI-SUBACCOUNT"] = SUBACCOUNT
    return h

def _req_json(
    private_key,
    method: str,
    path: str,
    *,
    base_url: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: int = 20,
) -> Dict[str, Any]:
    """
    Sends request. Signature is based on *path only* (without query), so
    we sign `path` and send query separately.  [oai_citation:2‡Kalshi API Documentation](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)
    """
    method = method.upper()

    # Build URL (query is NOT part of signature)
    url = base_url + path
    if params:
        url = url + "?" + urlencode(params, doseq=True)

    h = _headers(private_key, method, path)

    log.info("[REQ] %s %s", method, path + (f"?{url.split('?',1)[1]}" if "?" in url else ""))

    r = requests.request(method, url, headers=h, json=json_body, timeout=timeout)

    # Try parse json, otherwise keep raw
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path}{('?' + urlencode(params) if params else '')}: {data}")
    return data

# -----------------------------
# API wrappers
# -----------------------------
def markets_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # Market data is sometimes available unauthenticated on TRADING_BASE_URL,
    # but to keep behavior consistent with migrations, we call ELECTIONS_BASE_URL
    # without auth only if needed. Here we just use requests directly.
    url = ELECTIONS_BASE_URL + path
    if params:
        url = url + "?" + urlencode(params, doseq=True)
    r = requests.get(url, timeout=20)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path}: {data}")
    return data

def portfolio_get(private_key, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _req_json(private_key, "GET", path, params=params, base_url=ELECTIONS_BASE_URL)

def portfolio_post(private_key, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return _req_json(private_key, "POST", path, json_body=body, base_url=ELECTIONS_BASE_URL)

# -----------------------------
# Market resolution (find active ticker from series)
# -----------------------------
def resolve_active_market(series_prefix: str) -> Optional[str]:
    """
    Your logs show the working query is:
      GET /trade-api/v2/markets?limit=200&series_ticker=KXBTC15M&status=open -> 200
    We'll try uppercasing series prefix first (Kalshi series tickers are often upper).
    """
    series_upper = series_prefix.upper()
    params = {"limit": 200, "series_ticker": series_upper, "status": "open"}
    data = markets_get("/trade-api/v2/markets", params=params)

    markets = data.get("markets") or data.get("data") or []
    if not markets:
        return None

    # pick the first open market (often only 1)
    picked = markets[0]
    ticker = picked.get("ticker") or picked.get("market_ticker")
    return ticker

# -----------------------------
# Orderbook parsing (YES-only)
# -----------------------------
@dataclass
class YesSpread:
    best_bid: Optional[int]  # cents
    best_ask: Optional[int]  # cents

def parse_yes_bid_ask(orderbook: Dict[str, Any]) -> YesSpread:
    """
    The orderbook format you’re seeing:
      {'keys': ['no','no_dollars','yes','yes_dollars'], 'yes': [[1, 340], [2, 341], ...]}
    This often represents *asks* available to BUY that outcome at given prices.

    If we only have asks for YES and asks for NO, we can derive:
      YES best_ask = min(YES asks)
      YES best_bid = 100 - (NO best_ask)
    """
    yes_levels = orderbook.get("yes")
    no_levels = orderbook.get("no")

    def best_ask_from(levels) -> Optional[int]:
        if not isinstance(levels, list) or not levels:
            return None
        # levels like [[price, qty], ...]
        prices = []
        for lvl in levels:
            if isinstance(lvl, (list, tuple)) and len(lvl) >= 1:
                try:
                    prices.append(int(lvl[0]))
                except Exception:
                    pass
        return min(prices) if prices else None

    yes_best_ask = best_ask_from(yes_levels)
    no_best_ask = best_ask_from(no_levels)

    yes_best_bid = None
    if no_best_ask is not None:
        yes_best_bid = 100 - int(no_best_ask)

    return YesSpread(best_bid=yes_best_bid, best_ask=yes_best_ask)

def get_orderbook(market_ticker: str) -> Dict[str, Any]:
    data = markets_get(f"/trade-api/v2/markets/{market_ticker}/orderbook")
    # Kalshi sometimes wraps under "orderbook"
    return data.get("orderbook") or data

# -----------------------------
# Positions / Orders
# -----------------------------
def has_position(private_key, market_ticker: str) -> bool:
    data = portfolio_get(private_key, "/trade-api/v2/portfolio/positions")
    positions = data.get("positions") or data.get("data") or []
    for p in positions:
        t = p.get("market_ticker") or p.get("ticker")
        if t == market_ticker:
            # any nonzero position
            qty = p.get("position") or p.get("quantity") or 0
            try:
                return int(qty) != 0
            except Exception:
                return True
    return False

def list_open_orders(private_key, market_ticker: str) -> List[Dict[str, Any]]:
    data = portfolio_get(private_key, "/trade-api/v2/portfolio/orders", params={"limit": 200, "status": "open"})
    orders = data.get("orders") or data.get("data") or []
    out = []
    for o in orders:
        t = o.get("market_ticker") or o.get("ticker")
        if t == market_ticker:
            out.append(o)
    return out

# -----------------------------
# Trading (YES buy)
# -----------------------------
def compute_yes_buy_price(target_cents: int, spread: YesSpread, post_only: bool, improve_ticks: int) -> Optional[int]:
    """
    Post-only rule: buy price must be STRICTLY < best ask.
    If best ask unknown, skip.
    """
    if spread.best_ask is None:
        return None

    px = int(target_cents)

    if post_only:
        px = min(px, spread.best_ask - improve_ticks)

        # still ensure strictly below
        if px >= spread.best_ask:
            px = spread.best_ask - 1

    # clamp to valid range [1, 99]
    px = max(1, min(99, px))
    return px

def place_yes_buy(private_key, market_ticker: str, price_cents: int, count: int, post_only: bool) -> Dict[str, Any]:
    """
    Create order: YES buy.
    Note: Body fields can vary by Kalshi version. This matches common v2 fields.
    If your account requires different keys (e.g. 'side'/'action'), adjust here only.
    """
    body = {
        "market_ticker": market_ticker,
        "side": "yes",
        "action": "buy",
        "count": int(count),
        "price": int(price_cents),
        "type": "limit",
        "post_only": bool(post_only),
    }
    return portfolio_post(private_key, "/trade-api/v2/portfolio/orders", body)

# -----------------------------
# Main loop
# -----------------------------
def main():
    if not API_KEY_ID:
        raise RuntimeError("Missing KALSHI_API_KEY (must be API Key ID UUID)")

    private_key = load_private_key_from_b64(PRIVATE_KEY_B64)

    log.info(
        "[BOOT] ELECTIONS_BASE_URL=%s SERIES_PREFIX=%s POLL_SECONDS=%.2f BUY_PRICE_CENTS=%s BASE_SIZE=%s POST_ONLY=%s IMPROVE_TICKS=%s ENABLE_TRADING=%s CONFIRM_LIVE_TRADING=%s SUBACCOUNT=%s",
        ELECTIONS_BASE_URL,
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

    active_ticker: Optional[str] = None
    last_resolve = 0.0

    while True:
        try:
            # Re-resolve market periodically (every ~20s)
            if time.time() - last_resolve > 20 or not active_ticker:
                last_resolve = time.time()
                new_ticker = resolve_active_market(SERIES_PREFIX)
                if not new_ticker:
                    log.error("[MARKET] No active market found for series %s", SERIES_PREFIX)
                    time.sleep(max(2.0, POLL_SECONDS))
                    continue
                if new_ticker != active_ticker:
                    active_ticker = new_ticker
                    log.info("[MARKET] Switched active ticker -> %s", active_ticker)

            assert active_ticker is not None

            # Pull orderbook + compute spread
            ob = get_orderbook(active_ticker)
            spread = parse_yes_bid_ask(ob)

            log.info("[SPREAD] %s YES best_bid=%s best_ask=%s", active_ticker, spread.best_bid, spread.best_ask)

            px = compute_yes_buy_price(BUY_PRICE_CENTS, spread, POST_ONLY, IMPROVE_TICKS)
            if px is None:
                log.warning("[PARSE] Could not compute a safe YES buy price; skipping.")
                time.sleep(POLL_SECONDS)
                continue

            # Basic “don’t spam duplicates” guard: if we already have an open YES buy at px, do nothing.
            open_orders = list_open_orders(private_key, active_ticker)
            already = False
            for o in open_orders:
                try:
                    if (o.get("action") == "buy" and o.get("side") == "yes" and int(o.get("price", -1)) == int(px)):
                        already = True
                        break
                except Exception:
                    pass

            if already:
                log.info("[QUOTE] %s already has open YES buy at %sc; skipping.", active_ticker, px)
                time.sleep(POLL_SECONDS)
                continue

            log.info(
                "[QUOTE] %s placing YES buy at %sc (target=%sc post_only=%s)",
                active_ticker,
                px,
                BUY_PRICE_CENTS,
                POST_ONLY,
            )

            if ENABLE_TRADING:
                if not CONFIRM_LIVE_TRADING:
                    log.warning("[SAFE] ENABLE_TRADING true but CONFIRM_LIVE_TRADING is false; not placing orders.")
                else:
                    place_yes_buy(private_key, active_ticker, px, BASE_SIZE, POST_ONLY)

        except Exception as e:
            log.error("[LOOPERR] %s", e, exc_info=True)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()