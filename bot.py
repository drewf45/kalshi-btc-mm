import os
import json
import time
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List

import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

# -----------------------------
# Env / Config
# -----------------------------
load_dotenv()

API_BASE = os.getenv("KALSHI_API_BASE", "https://trading-api.kalshi.com").rstrip("/")
API_PREFIX = os.getenv("KALSHI_API_PREFIX", "/trade-api/v2")

SERIES = os.getenv("SERIES", "KXBTC15M").strip()
EVENT_TICKER = os.getenv("EVENT_TICKER", "").strip()  # <-- IMPORTANT: event, not market
MARKET_TICKER_OVERRIDE = os.getenv("MARKET_TICKER", "").strip()  # optional hard pin

POLL = float(os.getenv("POLL", "2.0"))
ROLL_CHECK_MIN_SECONDS = float(os.getenv("ROLL_CHECK_MIN_SECONDS", "30.0"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes", "y")
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "true").lower() in ("1", "true", "yes", "y")

# backoff for 429s
BACKOFF_START = float(os.getenv("BACKOFF_START", "1.0"))
BACKOFF_MAX = float(os.getenv("BACKOFF_MAX", "16.0"))

# quoting knobs (safe defaults; tune later)
TICK_CENTS = int(os.getenv("TICK_CENTS", "1"))
EDGE_CENTS = int(os.getenv("EDGE_CENTS", "1"))

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

if not KALSHI_KEY_ID or not KALSHI_PRIVATE_KEY_B64:
    raise RuntimeError("Missing KALSHI_KEY_ID or KALSHI_PRIVATE_KEY_B64 env vars")

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")

# -----------------------------
# Signing helpers
# -----------------------------
def now_ms() -> int:
    return int(time.time() * 1000)

def load_private_key() -> Any:
    key_bytes = base64.b64decode(KALSHI_PRIVATE_KEY_B64)
    return serialization.load_pem_private_key(key_bytes, password=None)

PRIVATE_KEY = load_private_key()

def sign_message(message: str) -> str:
    sig = PRIVATE_KEY.sign(
        message.encode("utf-8"),
        asy_padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")

def build_signature_headers(method: str, path_with_query: str, body: str) -> Dict[str, str]:
    ts = str(now_ms())
    payload = ts + method.upper() + path_with_query + body
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign_message(payload),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

# -----------------------------
# HTTP helper (with 429 backoff)
# -----------------------------
def request_json(method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = API_BASE + API_PREFIX + path
    params = params or {}
    body_str = "" if json_body is None else json.dumps(json_body, separators=(",", ":"), sort_keys=True)

    # Sign the exact path+query
    if params:
        req = requests.Request(method.upper(), url, params=params, data=body_str)
        prepped = req.prepare()
        signed_path = prepped.path_url
    else:
        signed_path = path

    headers = build_signature_headers(method, signed_path, body_str)

    backoff = BACKOFF_START
    while True:
        resp = requests.request(method.upper(), url, params=params, data=body_str, headers=headers, timeout=20)

        if resp.status_code == 429:
            log.warning("[429] %s backing off %.1fs", path, backoff)
            time.sleep(backoff)
            backoff = min(BACKOFF_MAX, backoff * 2)
            continue

        if resp.status_code >= 400:
            try:
                j = resp.json()
                raise RuntimeError(f"HTTP {resp.status_code} {path}: {j}")
            except Exception:
                text_head = (resp.text or "")[:200]
                raise RuntimeError(f"HTTP {resp.status_code} {path}: {{'_non_json': True, '_text_head': {text_head!r}}}")

        try:
            return resp.json()
        except Exception:
            text_head = (resp.text or "")[:200]
            raise RuntimeError(f"Bad JSON response for {path}: {text_head!r}")

# -----------------------------
# Rolling: EVENT -> MARKET
# -----------------------------
_last_roll_ts = 0.0
_active_market_ticker: Optional[str] = None

def pick_open_market(markets: List[Dict[str, Any]]) -> Optional[str]:
    """
    Choose an 'open' market if possible.
    If multiple, prefer the one closing soonest (best for current window).
    """
    def parse_dt(s: Any) -> float:
        if not isinstance(s, str):
            return 0.0
        try:
            s2 = s.replace("Z", "+00:00")
            return datetime.fromisoformat(s2).timestamp()
        except Exception:
            return 0.0

    open_markets = []
    for m in markets:
        if not isinstance(m, dict):
            continue
        status = str(m.get("status", "")).lower()
        if status in ("open", "active"):
            open_markets.append(m)

    if not open_markets:
        # fallback: anything with a ticker
        for m in markets:
            t = m.get("ticker") or m.get("market_ticker")
            if t:
                return t
        return None

    # prefer closest close_time in the future
    now = time.time()
    def score(m: Dict[str, Any]) -> Tuple[int, float]:
        # higher is better: open first, then minimal positive time-to-close
        t = 0.0
        for k in ("close_time", "end_time", "expiration_time", "settlement_time"):
            if k in m:
                t = max(t, parse_dt(m.get(k)))
        # time_to_close: prefer smallest > now
        if t <= 0:
            return (1, -1e18)
        dtc = t - now
        # if already passed, deprioritize
        if dtc < 0:
            return (1, -1e12 + dtc)
        return (1, -dtc)  # smaller dtc => bigger score

    open_markets_sorted = sorted(open_markets, key=score, reverse=True)
    return open_markets_sorted[0].get("ticker") or open_markets_sorted[0].get("market_ticker")

def roll_active_market() -> str:
    global _last_roll_ts, _active_market_ticker

    if MARKET_TICKER_OVERRIDE:
        if _active_market_ticker != MARKET_TICKER_OVERRIDE:
            log.info("[ROLL] Using MARKET_TICKER override → %s", MARKET_TICKER_OVERRIDE)
        _active_market_ticker = MARKET_TICKER_OVERRIDE
        return _active_market_ticker

    now = time.time()
    if _active_market_ticker and (now - _last_roll_ts) < ROLL_CHECK_MIN_SECONDS:
        return _active_market_ticker

    if not EVENT_TICKER:
        raise RuntimeError("EVENT_TICKER is empty. Set EVENT_TICKER=KXBTC15M-26JAN210730 (from your app link).")

    # This is the key change: get markets for the EVENT, not scanning everything.
    # If this 404s, we’ll adjust the path based on the returned schema.
    data = request_json("GET", f"/events/{EVENT_TICKER}/markets", params={"limit": 200})
    markets = data.get("markets") or data.get("data") or data.get("results") or []
    if not isinstance(markets, list) or not markets:
        raise RuntimeError(f"No markets returned for event {EVENT_TICKER}. Response keys={list(data.keys())}")

    picked = pick_open_market(markets)
    if not picked:
        raise RuntimeError(f"Could not pick a market for event {EVENT_TICKER} (markets={len(markets)})")

    _active_market_ticker = picked
    _last_roll_ts = time.time()
    log.info("[ROLL] Event=%s → Active market → %s", EVENT_TICKER, _active_market_ticker)
    return _active_market_ticker

# -----------------------------
# Orderbook parsing (binary)
# -----------------------------
def best_bid_from_side(side: Any) -> Optional[int]:
    if not isinstance(side, list) or not side:
        return None
    best = None
    for lvl in side:
        if isinstance(lvl, list) and len(lvl) >= 1:
            try:
                p = int(lvl[0])
                best = p if best is None else max(best, p)
            except Exception:
                continue
    return best

def get_yes_bid_ask(orderbook_payload: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    ob = orderbook_payload.get("orderbook") if isinstance(orderbook_payload, dict) else None
    if not isinstance(ob, dict):
        return (None, None)

    yes = ob.get("yes")
    no = ob.get("no")

    yes_bid = best_bid_from_side(yes)
    no_bid = best_bid_from_side(no)

    yes_ask = None
    if no_bid is not None:
        yes_ask = 100 - no_bid

    return (yes_bid, yes_ask)

def fetch_orderbook(market_ticker: str) -> Dict[str, Any]:
    return request_json("GET", f"/markets/{market_ticker}/orderbook")

# -----------------------------
# Main loop
# -----------------------------
def main():
    log.info("SERIES=%s EVENT_TICKER=%s POLL=%.1fs DRY_RUN=%s ENABLE_TRADING=%s",
             SERIES, EVENT_TICKER or "<unset>", POLL, DRY_RUN, ENABLE_TRADING)

    while True:
        try:
            mkt = roll_active_market()
            ob = fetch_orderbook(mkt)
            yes_bid, yes_ask = get_yes_bid_ask(ob)

            if yes_bid is None and yes_ask is None:
                log.info("[QUOTE] %s YES bid=None ask=None → SKIP (empty)", mkt)
            else:
                log.info("[QUOTE] %s YES bid=%s ask=%s", mkt, yes_bid, yes_ask)

                # later: compute our quotes & place/cancel orders
                # For now, DRY_RUN just prints.

        except Exception as e:
            log.error("[LOOPERR] %s", e)

        time.sleep(POLL)

if __name__ == "__main__":
    main()