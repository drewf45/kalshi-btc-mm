import os
import json
import time
import base64
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, List

import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

# -----------------------------
# Env / Config
# -----------------------------
load_dotenv()


def getenv_first(keys: List[str], default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def getenv_by_prefix(prefixes: List[str]) -> str:
    for name, value in os.environ.items():
        for p in prefixes:
            if name.startswith(p) and str(value).strip() != "":
                return str(value).strip()
    return ""


def env_keys_with_prefix(prefix: str) -> List[str]:
    return sorted([k for k in os.environ.keys() if k.startswith(prefix)])


def parse_bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


API_BASE = getenv_first(["KALSHI_API_BASE", "KALSHI_BASE_URL"], "https://trading-api.kalshi.com").rstrip("/")
API_PREFIX = getenv_first(["KALSHI_API_PREFIX"], "/trade-api/v2")

SERIES = getenv_first(["SERIES", "SERIES_TICKER"], "KXBTC15M").strip()

EVENT_TICKER = getenv_first(["EVENT_TICKER", "EVENT"], "").strip()
MARKET_TICKER_OVERRIDE = getenv_first(["MARKET_TICKER", "MARKET"], "").strip()

POLL = float(getenv_first(["POLL", "POLL_SECONDS"], "2.0"))
ROLL_CHECK_MIN_SECONDS = float(getenv_first(["ROLL_CHECK_MIN_SECONDS"], "30.0"))

DRY_RUN = parse_bool(getenv_first(["DRY_RUN"], "true"), default=True)
ENABLE_TRADING = parse_bool(getenv_first(["ENABLE_TRADING"], "true"), default=True)

BACKOFF_START = float(getenv_first(["BACKOFF_START"], "1.0"))
BACKOFF_MAX = float(getenv_first(["BACKOFF_MAX"], "16.0"))

TICK_CENTS = int(getenv_first(["TICK_CENTS"], "1"))
EDGE_CENTS = int(getenv_first(["EDGE_CENTS"], "1"))
MIN_SPREAD_CENTS = int(getenv_first(["MIN_SPREAD_CENTS"], "2"))  # ✅ NEW: require at least this spread to quote

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = getenv_first(["LOG_LEVEL"], "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")

# -----------------------------
# Credentials + diagnostics
# -----------------------------
KALSHI_KEY_ID = getenv_first(
    ["KALSHI_KEY_ID", "KALSHI_API_KEY_ID", "KALSHI_ACCESS_KEY", "KALSHI_API_KEY"],
    ""
).strip()

KALSHI_PRIVATE_KEY_RAW = getenv_first(
    ["KALSHI_PRIVATE_KEY_B64", "KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY"],
    ""
).strip()

if not KALSHI_PRIVATE_KEY_RAW:
    # Render env key in your screenshots is: KALSHI_PRIVATE_KEY_PEM_BASE64
    KALSHI_PRIVATE_KEY_RAW = getenv_by_prefix(["KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY_B64"]).strip()

_detected_kalshi_keys = env_keys_with_prefix("KALSHI_")
log.info("[ENV] Detected KALSHI_* keys: %s", _detected_kalshi_keys if _detected_kalshi_keys else "<none>")

if not KALSHI_KEY_ID or not KALSHI_PRIVATE_KEY_RAW:
    raise RuntimeError(
        "Missing Kalshi credentials at runtime.\n"
        f"Detected KALSHI_* keys: {_detected_kalshi_keys}\n"
        "Expected one of:\n"
        "  - KALSHI_KEY_ID or KALSHI_API_KEY_ID\n"
        "  - KALSHI_PRIVATE_KEY_B64 or KALSHI_PRIVATE_KEY_PEM (or any env starting with KALSHI_PRIVATE_KEY_PEM)\n"
    )

# -----------------------------
# Signing helpers
# -----------------------------
def now_ms() -> int:
    return int(time.time() * 1000)


def load_private_key_from_env(raw: str) -> Any:
    s = raw.strip()
    if "BEGIN" in s and "PRIVATE KEY" in s:
        return serialization.load_pem_private_key(s.encode("utf-8"), password=None)

    try:
        key_bytes = base64.b64decode(s)
        return serialization.load_pem_private_key(key_bytes, password=None)
    except Exception as e:
        raise RuntimeError(
            "Could not parse private key. Provide PEM text in KALSHI_PRIVATE_KEY_PEM "
            "or base64 PEM in KALSHI_PRIVATE_KEY_B64."
        ) from e


PRIVATE_KEY = load_private_key_from_env(KALSHI_PRIVATE_KEY_RAW)


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
def request_json(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = API_BASE + API_PREFIX + path
    params = params or {}
    body_str = "" if json_body is None else json.dumps(json_body, separators=(",", ":"), sort_keys=True)

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
# Rolling via /markets ONLY
# -----------------------------
_last_roll_ts = 0.0
_active_event_ticker: Optional[str] = None
_active_market_ticker: Optional[str] = None


def _parse_dt_to_ts(s: Any) -> float:
    if not isinstance(s, str):
        return 0.0
    try:
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2).timestamp()
    except Exception:
        return 0.0


def pick_open_market(markets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    now = time.time()
    open_markets = []
    for m in markets:
        if not isinstance(m, dict):
            continue
        status = str(m.get("status", "")).lower()
        if status in ("open", "active", "trading"):
            open_markets.append(m)

    candidates = open_markets if open_markets else [m for m in markets if isinstance(m, dict)]
    if not candidates:
        return None

    def score(m: Dict[str, Any]) -> Tuple[int, float]:
        status = str(m.get("status", "")).lower()
        is_open = status in ("open", "active", "trading")
        close_ts = 0.0
        for k in ("close_time", "end_time", "expiration_time", "settlement_time"):
            if k in m:
                close_ts = max(close_ts, _parse_dt_to_ts(m.get(k)))
        if close_ts > 0:
            dtc = close_ts - now
            if dtc >= 0:
                return (2 if is_open else 1, -dtc)
        return (1 if is_open else 0, -1e18)

    return sorted(candidates, key=score, reverse=True)[0]


def resolve_event_and_market_via_markets(series_ticker: str) -> Tuple[Optional[str], Optional[str]]:
    param_sets = [
        {"series_ticker": series_ticker, "limit": 200},
        {"series": series_ticker, "limit": 200},
        {"series_ticker": series_ticker, "status": "open", "limit": 200},
    ]

    last_err = None
    for params in param_sets:
        try:
            data = request_json("GET", "/markets", params=params)
            markets = data.get("markets") or data.get("data") or data.get("results") or []
            if not isinstance(markets, list) or not markets:
                continue

            best = pick_open_market(markets)
            if not best:
                continue

            mkt_ticker = best.get("ticker") or best.get("market_ticker")
            evt_ticker = best.get("event_ticker") or best.get("event") or best.get("eventTicker")

            if mkt_ticker and evt_ticker:
                return (evt_ticker, mkt_ticker)
        except Exception as e:
            last_err = e
            continue

    log.warning("[MARKETS] Could not resolve via /markets: %s", last_err)
    return (None, None)


def roll_active_market() -> str:
    global _last_roll_ts, _active_event_ticker, _active_market_ticker

    if MARKET_TICKER_OVERRIDE:
        if _active_market_ticker != MARKET_TICKER_OVERRIDE:
            log.info("[ROLL] Using MARKET_TICKER override → %s", MARKET_TICKER_OVERRIDE)
        _active_market_ticker = MARKET_TICKER_OVERRIDE
        return _active_market_ticker

    now = time.time()
    if _active_market_ticker and (now - _last_roll_ts) < ROLL_CHECK_MIN_SECONDS:
        return _active_market_ticker

    # If user pins event, still resolve market via /markets.
    if EVENT_TICKER:
        evt, mkt = resolve_event_and_market_via_markets(SERIES)
        if not mkt:
            raise RuntimeError(f"Could not resolve market via /markets for series={SERIES} (EVENT_TICKER pinned={EVENT_TICKER}).")
        _active_event_ticker = EVENT_TICKER
        _active_market_ticker = mkt
        _last_roll_ts = time.time()
        log.info("[ROLL] Event pinned=%s → Active market → %s (via /markets)", _active_event_ticker, _active_market_ticker)
        return _active_market_ticker

    evt, mkt = resolve_event_and_market_via_markets(SERIES)
    if not evt or not mkt:
        raise RuntimeError(f"Could not auto-resolve active event/market for series={SERIES} via /markets.")

    changed = (evt != _active_event_ticker) or (mkt != _active_market_ticker)
    _active_event_ticker = evt
    _active_market_ticker = mkt
    _last_roll_ts = time.time()

    if changed:
        log.info("[ROLL] Series=%s → Active event=%s market=%s (via /markets)", SERIES, _active_event_ticker, _active_market_ticker)

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

    # Derive YES ask from NO best bid: if someone bids NO at X, they offer YES at (100 - X)
    yes_ask = None
    if no_bid is not None:
        yes_ask = 100 - no_bid

    return (yes_bid, yes_ask)


def fetch_orderbook(market_ticker: str) -> Dict[str, Any]:
    return request_json("GET", f"/markets/{market_ticker}/orderbook")

# -----------------------------
# ✅ NEW: quote target calculator (YES-only)
# -----------------------------
def clamp_price(p: int) -> int:
    return max(1, min(99, p))


def compute_target_yes_quotes(yes_bid: Optional[int], yes_ask: Optional[int]) -> Tuple[Optional[int], Optional[int], str]:
    """
    Returns (target_bid, target_ask, reason).
    YES-only: we quote inside the current spread when it's wide enough.
    """
    if yes_bid is None or yes_ask is None:
        return (None, None, "missing_bid_or_ask")

    if yes_ask <= yes_bid:
        return (None, None, "crossed_or_locked")

    spread = yes_ask - yes_bid
    if spread < MIN_SPREAD_CENTS:
        return (None, None, f"spread_too_tight({spread})")

    # Place 1 tick inside the best prices, while maintaining EDGE_CENTS away from the opposite side
    # (simple and safe for now)
    bid = clamp_price(yes_bid + TICK_CENTS)
    ask = clamp_price(yes_ask - TICK_CENTS)

    # Enforce edge: ensure bid <= (yes_ask - EDGE) and ask >= (yes_bid + EDGE)
    max_bid = clamp_price(yes_ask - EDGE_CENTS)
    min_ask = clamp_price(yes_bid + EDGE_CENTS)

    bid = min(bid, max_bid)
    ask = max(ask, min_ask)

    if bid >= ask:
        return (None, None, "no_room_after_edge")

    return (bid, ask, f"ok(spread={spread})")

# -----------------------------
# Main loop
# -----------------------------
def main():
    log.info(
        "API_BASE=%s API_PREFIX=%s SERIES=%s EVENT_TICKER=%s MARKET_OVERRIDE=%s POLL=%.1fs DRY_RUN=%s ENABLE_TRADING=%s",
        API_BASE, API_PREFIX, SERIES,
        EVENT_TICKER or "<auto>", MARKET_TICKER_OVERRIDE or "<none>",
        POLL, DRY_RUN, ENABLE_TRADING
    )

    log.info("QUOTE_PARAMS: TICK_CENTS=%d EDGE_CENTS=%d MIN_SPREAD_CENTS=%d", TICK_CENTS, EDGE_CENTS, MIN_SPREAD_CENTS)

    while True:
        try:
            mkt = roll_active_market()
            ob = fetch_orderbook(mkt)
            yes_bid, yes_ask = get_yes_bid_ask(ob)

            if yes_bid is None and yes_ask is None:
                log.info("[QUOTE] %s YES bid=None ask=None → SKIP (empty)", mkt)
            else:
                log.info("[QUOTE] %s YES bid=%s ask=%s", mkt, yes_bid, yes_ask)

            tb, ta, why = compute_target_yes_quotes(yes_bid, yes_ask)
            if tb is None or ta is None:
                log.info("[TARGET] %s → SKIP (%s)", mkt, why)
            else:
                log.info("[TARGET] %s YES-only would_quote: bid@%d ask@%d (%s) DRY_RUN=%s", mkt, tb, ta, why, DRY_RUN)

        except Exception as e:
            log.error("[LOOPERR] %s", e)

        time.sleep(POLL)


if __name__ == "__main__":
    main()