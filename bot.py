import os
import time
import json
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

load_dotenv()

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

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15m")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1.0"))

BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "99"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))
POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")

IMPROVE_TICKS = int(os.getenv("IMPROVE_TICKS", "1"))

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() in ("1", "true", "yes", "y")
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() in ("1", "true", "yes", "y")

KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "").strip()  # API Key ID (UUID)
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

SUBACCOUNT = os.getenv("SUBACCOUNT", "").strip()

# -----------------------------
# Helpers
# -----------------------------
def now_ms() -> int:
    return int(time.time() * 1000)

def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def _load_private_key_from_b64(b64_str: str):
    raw = base64.b64decode(b64_str.encode("utf-8"))
    return serialization.load_pem_private_key(raw, password=None)

def _sign(private_key, message: str) -> str:
    sig = private_key.sign(
        message.encode("utf-8"),
        asy_padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")

def _headers(private_key, method: str, path_with_query: str) -> Dict[str, str]:
    ts = str(now_ms())
    msg = f"{ts}{method.upper()}{path_with_query}"
    signature = _sign(private_key, msg)
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Kalshi-Api-Key": KALSHI_API_KEY,
        "Kalshi-Timestamp": ts,
        "Kalshi-Signature": signature,
    }
    if SUBACCOUNT:
        h["Kalshi-Subaccount"] = SUBACCOUNT
    return h

def _req_json(
    private_key,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    base_url: str = ELECTIONS_BASE_URL,
) -> Any:
    if not path.startswith("/"):
        path = "/" + path

    path_with_query = path
    if params:
        path_with_query = f"{path}?{urlencode(params)}"

    url = base_url + path_with_query
    h = _headers(private_key, method, path_with_query)

    if method.upper() == "GET":
        r = requests.get(url, headers=h, timeout=15)
    elif method.upper() == "POST":
        r = requests.post(url, headers=h, data=json.dumps(json_body or {}), timeout=15)
    else:
        raise ValueError(f"Unsupported method: {method}")

    # log request outcome
    log.info("[REQ] %s %s -> %s", method.upper(), path_with_query, r.status_code)

    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path_with_query}: {data}")

    return data

def markets_get(private_key, params: Dict[str, Any]) -> Any:
    return _req_json(private_key, "GET", "/trade-api/v2/markets", params=params, base_url=ELECTIONS_BASE_URL)

def market_orderbook_get(private_key, market_ticker: str) -> Any:
    return _req_json(private_key, "GET", f"/trade-api/v2/markets/{market_ticker}/orderbook", base_url=ELECTIONS_BASE_URL)

def portfolio_get(private_key, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    return _req_json(private_key, "GET", path, params=params, base_url=ELECTIONS_BASE_URL)

def portfolio_post(private_key, path: str, body: Dict[str, Any]) -> Any:
    return _req_json(private_key, "POST", path, json_body=body, base_url=ELECTIONS_BASE_URL)

# -----------------------------
# Market resolver (series -> active market ticker)
# -----------------------------
def resolve_active_market(private_key, series_prefix: str) -> Optional[str]:
    probes = [
        {"series_ticker": series_prefix.upper(), "status": "open", "limit": 200},
        {"series_ticker": series_prefix.upper(), "status": "active", "limit": 200},
        {"series_ticker": series_prefix, "status": "open", "limit": 200},
        {"series_ticker": series_prefix, "status": "active", "limit": 200},
        {"event_ticker": series_prefix.upper(), "status": "open", "limit": 200},
        {"event_ticker": series_prefix.upper(), "status": "active", "limit": 200},
        {"event_ticker": series_prefix, "status": "open", "limit": 200},
        {"event_ticker": series_prefix, "status": "active", "limit": 200},
        {"series_ticker": series_prefix.upper(), "limit": 200},
        {"event_ticker": series_prefix.upper(), "limit": 200},
        {"series_ticker": series_prefix, "limit": 200},
        {"event_ticker": series_prefix, "limit": 200},
        {"series_ticker": series_prefix.lower(), "status": "open", "limit": 200},
        {"series_ticker": series_prefix.lower(), "status": "active", "limit": 200},
    ]

    for p in probes:
        try:
            data = markets_get(private_key, p)
            markets = data.get("markets") or []
            if markets:
                picked = markets[0].get("ticker")
                log.info("[RESOLVE] probe=%s markets=%s picked=%s", list(p.keys())[0], len(markets), picked)
                return picked
        except Exception as e:
            log.warning("[RESOLVE] probe failed params=%s err=%s", p, e)

    return None

# -----------------------------
# Orderbook parsing (YES side)
# -----------------------------
def parse_yes_best_bid_ask(orderbook: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Kalshi orderbook for these BTC markets often looks like:
      {'yes': [[1, 340], [2, 341], ...], 'no': [...], 'yes_dollars': ..., 'no_dollars': ...}

    In some responses, 'yes' may represent one side only depending on endpoint format.
    We'll interpret:
      - bids are highest price someone is bidding (max price)
      - asks are lowest price someone is asking (min price)
    If only one ladder is present, best_bid might be None (unknown).
    """
    yes = orderbook.get("yes")

    if not isinstance(yes, list) or not yes:
        return None, None

    prices: List[int] = []
    for level in yes:
        if isinstance(level, list) and len(level) >= 1 and isinstance(level[0], (int, float)):
            prices.append(int(level[0]))

    if not prices:
        return None, None

    # If we only have one ladder, we can't truly know bid vs ask.
    # Heuristic: treat it as asks when it's very low-to-high and includes 1/2/etc.
    # We'll return best_ask = min(prices), best_bid = max(prices) ONLY if ladder spans a range.
    mn, mx = min(prices), max(prices)
    if mn == mx:
        return None, mn

    return mx, mn

# -----------------------------
# Trading
# -----------------------------
def has_any_open_orders(private_key) -> bool:
    data = portfolio_get(private_key, "/trade-api/v2/portfolio/orders", params={"limit": 200, "status": "open"})
    orders = data.get("orders") or []
    return len(orders) > 0

def place_yes_buy(private_key, market_ticker: str, price_cents: int, size: int, post_only: bool) -> Any:
    # ---- FIX HERE: ensure ticker field is present and correct ----
    if not market_ticker or not isinstance(market_ticker, str):
        raise RuntimeError(f"place_yes_buy called with invalid market_ticker={market_ticker!r}")

    body = {
        "ticker": market_ticker,        # ✅ REQUIRED (this was missing)
        "side": "buy",
        "type": "limit",
        "price": int(price_cents),
        "count": int(size),
        "post_only": bool(post_only),
        "yes": True,                    # keep existing style signal (harmless if ignored)
    }

    # Some Kalshi variants use 'action'/'quantity'. If your account requires it,
    # keep this minimal: only add compatibility fields if needed (not doing that here).

    return portfolio_post(private_key, "/trade-api/v2/portfolio/orders", body)

# -----------------------------
# Main loop
# -----------------------------
def main():
    log.info(
        "[BOOT] ELECTIONS_BASE_URL=%s TRADING_BASE_URL=%s SERIES_PREFIX=%s POLL_SECONDS=%.2f BUY_PRICE_CENTS=%s BASE_SIZE=%s POST_ONLY=%s IMPROVE_TICKS=%s ENABLE_TRADING=%s CONFIRM_LIVE_TRADING=%s SUBACCOUNT=%s",
        ELECTIONS_BASE_URL, TRADING_BASE_URL, SERIES_PREFIX, POLL_SECONDS, BUY_PRICE_CENTS, BASE_SIZE,
        POST_ONLY, IMPROVE_TICKS, ENABLE_TRADING, CONFIRM_LIVE_TRADING, SUBACCOUNT,
    )

    if not KALSHI_API_KEY:
        raise RuntimeError("Missing KALSHI_API_KEY (must be API Key ID UUID)")
    if not KALSHI_PRIVATE_KEY_B64:
        raise RuntimeError("Missing KALSHI_PRIVATE_KEY_B64 (base64-encoded PEM)")

    private_key = _load_private_key_from_b64(KALSHI_PRIVATE_KEY_B64)
    log.info("[BOOT] Private key loaded OK (b64)")
    log.info("[BOOT] LIVE BTC BOT STARTED")

    active_ticker: Optional[str] = None
    last_resolve = 0.0

    while True:
        try:
            # Resolve active market every ~20s or when none
            if not active_ticker or (time.time() - last_resolve) > 20:
                picked = resolve_active_market(private_key, SERIES_PREFIX)
                last_resolve = time.time()
                if picked and picked != active_ticker:
                    active_ticker = picked
                    log.info("[MARKET] Switched active ticker -> %s", active_ticker)
                if not active_ticker:
                    log.error("[MARKET] No active market found for series %s", SERIES_PREFIX)
                    time.sleep(max(POLL_SECONDS, 2.0))
                    continue

            # Get orderbook
            ob = market_orderbook_get(private_key, active_ticker)
            preview = {"keys": list(ob.keys())[:10], "yes": ob.get("yes")}
            log.info("[OB] %s orderbook_preview=%s", active_ticker, preview)

            best_bid, best_ask = parse_yes_best_bid_ask(ob)
            log.info("[SPREAD] %s YES best_bid=%s best_ask=%s", active_ticker, best_bid, best_ask)

            # Decide quote price (very simple)
            target = BUY_PRICE_CENTS
            px = target

            # if post_only, don't cross
            if POST_ONLY and best_ask is not None:
                px = min(px, best_ask - IMPROVE_TICKS)
                px = max(px, 1)

            log.info("[QUOTE] %s placing YES buy at %sc (target=%sc post_only=%s)", active_ticker, px, target, POST_ONLY)

            if ENABLE_TRADING and CONFIRM_LIVE_TRADING:
                # Optional: don't spam if you already have an open order
                if not has_any_open_orders(private_key):
                    place_yes_buy(private_key, active_ticker, px, BASE_SIZE, POST_ONLY)
                else:
                    log.info("[SKIP] open order exists; not placing another")
            else:
                log.info("[DRYRUN] ENABLE_TRADING/CONFIRM_LIVE_TRADING not enabled; not placing order")

        except Exception as e:
            log.error("[LOOPERR] %s", e, exc_info=True)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()