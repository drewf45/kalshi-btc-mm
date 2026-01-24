import os
import json
import time
import base64
import logging
from dataclasses import dataclass
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


API_BASE = getenv_first(
    ["KALSHI_API_BASE", "KALSHI_BASE_URL"],
    "https://trading-api.kalshi.com",
).rstrip("/")
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

# ✅ B-player default: participate at 2¢ via join-only; still skips 1¢ unless MIN_SPREAD_CENTS=1
MIN_SPREAD_CENTS = int(getenv_first(["MIN_SPREAD_CENTS"], "2"))

# --- Safety / Patience gates ---
ENTER_OK_SECONDS = float(getenv_first(["ENTER_OK_SECONDS"], "3.0"))
EXIT_BAD_SECONDS = float(getenv_first(["EXIT_BAD_SECONDS"], "6.0"))
NOT_STABLE_EXIT_SECONDS = float(getenv_first(["NOT_STABLE_EXIT_SECONDS"], "1.5"))

# Order params
ORDER_QTY = int(getenv_first(["ORDER_QTY"], "1"))

# Minimum time between reprices (prevents churn)
MIN_REQUOTE_SECONDS = float(getenv_first(["MIN_REQUOTE_SECONDS"], "3.0"))

# Only reprice if we are "meaningfully" off target
REPRICE_IF_OFF_BY_CENTS = int(getenv_first(["REPRICE_IF_OFF_BY_CENTS"], "2"))

# Anti-churn / volatility controls
MAX_CHASE_CENTS = int(getenv_first(["MAX_CHASE_CENTS"], "4"))
UNSAFE_GRACE_SECONDS = float(getenv_first(["UNSAFE_GRACE_SECONDS"], "0.6"))

# When BOTH targets are None, hold existing orders instead of canceling immediately
HOLD_ON_SKIP = parse_bool(getenv_first(["HOLD_ON_SKIP"], "true"), default=True)

# Only cancel after BOTH targets have been None continuously for this long (generic no-target)
CANCEL_IF_NO_TARGET_SECONDS = float(getenv_first(["CANCEL_IF_NO_TARGET_SECONDS"], "10.0"))

# Micro #1: ask cache TTL to ride out NO-side blips
ASK_CACHE_TTL_SECONDS = float(getenv_first(["ASK_CACHE_TTL_SECONDS"], "5.0"))

# Micro #2: fallback to /markets/{ticker} when ask missing (throttled)
MARKET_FALLBACK_MIN_SECONDS = float(getenv_first(["MARKET_FALLBACK_MIN_SECONDS"], "5.0"))

# Micro #3 toggles
ENABLE_ONE_SIDED_TIGHT = parse_bool(getenv_first(["ENABLE_ONE_SIDED_TIGHT"], "true"), default=True)
ENABLE_JOIN_TIGHT_SPREAD = parse_bool(getenv_first(["ENABLE_JOIN_TIGHT_SPREAD"], "true"), default=True)

# ✅ Join-only threshold for tight spreads (B-player)
JOIN_ONLY_MAX_SPREAD_CENTS = int(getenv_first(["JOIN_ONLY_MAX_SPREAD_CENTS"], "2"))

# ✅ NEW MICRO: never "improve" when spread is still small; just join.
# This reduces pickoff/adverse selection in 3–4¢ regimes.
NO_IMPROVE_MAX_SPREAD_CENTS = int(getenv_first(["NO_IMPROVE_MAX_SPREAD_CENTS"], "4"))

# Micro #4: per-side hysteresis
SIDE_HOLD_SECONDS = float(getenv_first(["SIDE_HOLD_SECONDS"], "5.0"))

# Live flags (optional)
POST_ONLY = parse_bool(getenv_first(["POST_ONLY"], "true"), default=True)

# ✅ NEW: post-only anti-cross safety buffer (in cents)
POST_ONLY_BUFFER_CENTS = int(getenv_first(["POST_ONLY_BUFFER_CENTS"], "1"))

# -----------------------------
# Inventory / Reconciliation (NEW)
# -----------------------------
MAX_NET_YES_CONTRACTS = int(getenv_first(["MAX_NET_YES_CONTRACTS"], "2"))  # hard cap
INVENTORY_SKEW_CENTS = int(getenv_first(["INVENTORY_SKEW_CENTS"], "1"))    # 0..3 recommended
ORDER_STATUS_POLL_SECONDS = float(getenv_first(["ORDER_STATUS_POLL_SECONDS"], "1.0"))
PAUSE_ON_UNKNOWN_SECONDS = float(getenv_first(["PAUSE_ON_UNKNOWN_SECONDS"], "10.0"))

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
    KALSHI_PRIVATE_KEY_RAW = getenv_by_prefix(
        ["KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY_B64"]
    ).strip()

_detected_kalshi_keys = env_keys_with_prefix("KALSHI_")
log.info(
    "[ENV] Detected KALSHI_* keys: %s",
    _detected_kalshi_keys if _detected_kalshi_keys else "<none>",
)

if not KALSHI_KEY_ID or not KALSHI_PRIVATE_KEY_RAW:
    raise RuntimeError(
        "Missing Kalshi credentials at runtime.\n"
        f"Detected KALSHI_* keys: {_detected_kalshi_keys}\n"
        "Expected one of:\n"
        "  - KALSHI_KEY_ID or KALSHI_API_KEY_ID\n"
        "  - KALSHI_PRIVATE_KEY_B64 or KALSHI_PRIVATE_KEY_PEM\n"
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
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def build_signature_headers(method: str, path_with_query: str) -> Dict[str, str]:
    ts = str(now_ms())
    path_wo_query = path_with_query.split("?", 1)[0]
    payload = ts + method.upper() + path_wo_query
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

    if json_body is None:
        body_bytes = b""
    else:
        body_str = json.dumps(json_body, separators=(",", ":"), sort_keys=True)
        body_bytes = body_str.encode("utf-8")

    backoff = BACKOFF_START
    session = requests.Session()

    while True:
        req = requests.Request(
            method.upper(),
            url,
            params=params,
            data=body_bytes,
        )
        prepped = req.prepare()

        signed_path = prepped.path_url
        headers = build_signature_headers(method, signed_path)
        prepped.headers.update(headers)

        resp = session.send(prepped, timeout=20)

        if resp.status_code == 429:
            log.warning("[429] %s backing off %.1fs", path, backoff)
            time.sleep(backoff)
            backoff = min(BACKOFF_MAX, backoff * 2)
            continue

        if resp.status_code >= 400:
            text = resp.text or ""
            try:
                j = resp.json()
                raise RuntimeError(f"HTTP {resp.status_code} {path}: {j}")
            except Exception:
                raise RuntimeError(
                    f"HTTP {resp.status_code} {path}: "
                    f"{{'_non_json': True, '_text_head': {text[:200]!r}}}"
                )

        if resp.status_code == 204:
            return {}

        try:
            return resp.json()
        except Exception:
            raise RuntimeError(f"Bad JSON response for {path}: {(resp.text or '')[:200]!r}")

# -----------------------------
# LIVE ORDER ROUTES
# -----------------------------
def _clamp(p: int) -> int:
    return max(1, min(99, int(p)))


def _post_only_safe_price(action: str, price: int, yes_bid: Optional[int], yes_ask: Optional[int]) -> int:
    """
    ✅ pre-guard against post-only cross using our latest observed book.
    - buy must be <= (ask - buffer)
    - sell must be >= (bid + buffer)
    """
    p = _clamp(price)
    b = POST_ONLY_BUFFER_CENTS
    if not POST_ONLY or b <= 0:
        return p

    if action == "buy" and yes_ask is not None:
        p = min(p, _clamp(int(yes_ask) - b))
    if action == "sell" and yes_bid is not None:
        p = max(p, _clamp(int(yes_bid) + b))
    return _clamp(p)


def place_order_live(market_ticker: str, action: str, yes_price_cents: int, count: int) -> str:
    body: Dict[str, Any] = {
        "ticker": market_ticker,
        "action": action,
        "type": "limit",
        "side": "yes",
        "count": int(count),
        "yes_price": int(yes_price_cents),
    }
    if POST_ONLY:
        body["post_only"] = True

    resp = request_json("POST", "/portfolio/orders", json_body=body)

    order = resp.get("order") if isinstance(resp, dict) else None
    if isinstance(order, dict):
        oid = order.get("order_id") or order.get("id")
        if oid:
            return str(oid)

    oid = resp.get("order_id") or resp.get("id")
    if oid:
        return str(oid)

    raise RuntimeError(f"Order placed but could not find order_id in response: {resp}")


def cancel_order_live(order_id: str) -> str:
    """
    Returns:
      - "canceled"  : cancel request accepted
      - "not_found" : 404 not_found (ambiguous) -> caller must reconcile
    """
    try:
        request_json("DELETE", f"/portfolio/orders/{order_id}")
        return "canceled"
    except Exception as e:
        msg = str(e).lower()
        if ("http 404" in msg) and ("not_found" in msg):
            log.warning("[OM] CANCEL got 404 for order_id=%s (state ambiguous; will reconcile)", order_id)
            return "not_found"
        raise


def fetch_order_status(order_id: str) -> Dict[str, Any]:
    # Kalshi: GET /portfolio/orders/{order_id}
    return request_json("GET", f"/portfolio/orders/{order_id}")


def fetch_open_orders(limit: int = 200) -> List[Dict[str, Any]]:
    """
    Used only for reconciliation when order state is ambiguous.
    """
    data = request_json("GET", "/portfolio/orders", params={"limit": int(limit), "status": "open"})
    orders = data.get("orders") or data.get("data") or data.get("results") or []
    return orders if isinstance(orders, list) else []


def is_order_open(order_id: str) -> bool:
    oid = str(order_id)
    try:
        for o in fetch_open_orders(limit=200):
            if not isinstance(o, dict):
                continue
            oo = o.get("order") if isinstance(o.get("order"), dict) else o
            got = oo.get("order_id") or oo.get("id")
            if got is not None and str(got) == oid:
                return True
    except Exception as e:
        # Conservative: if probe fails, assume it *might* be open.
        log.warning("[INV] is_order_open probe failed for %s: %s", oid, e)
        return True
    return False

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
                return (str(evt_ticker), str(mkt_ticker))
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

    evt, mkt = resolve_event_and_market_via_markets(SERIES)
    if not evt or not mkt:
        raise RuntimeError(f"Could not auto-resolve active event/market for series={SERIES} via /markets.")

    changed = (evt != _active_event_ticker) or (mkt != _active_market_ticker)
    _active_event_ticker = evt
    _active_market_ticker = mkt
    _last_roll_ts = time.time()

    if changed:
        log.info(
            "[ROLL] Series=%s → Active event=%s market=%s (via /markets)",
            SERIES,
            _active_event_ticker,
            _active_market_ticker,
        )

    return _active_market_ticker

# -----------------------------
# Orderbook parsing (robust)
# -----------------------------
def _price_from_level(lvl: Any) -> Optional[int]:
    # ✅ PATCH: accept cents OR dollars (float/str) and normalize to cents
    def _to_cents_local(v: Any) -> Optional[int]:
        try:
            if v is None:
                return None
            fx = float(str(v).strip())
            cents = int(round(fx * 100)) if fx <= 1.0 else int(round(fx))
            if 1 <= cents <= 99:
                return cents
            return None
        except Exception:
            return None

    try:
        if isinstance(lvl, list) and len(lvl) >= 1:
            return _to_cents_local(lvl[0])
        if isinstance(lvl, dict):
            for k in ("price", "yes_price", "p", "price_dollars", "yes_price_dollars"):
                if k in lvl and lvl.get(k) is not None:
                    return _to_cents_local(lvl.get(k))
    except Exception:
        return None
    return None


def _best_bid(levels: Any) -> Optional[int]:
    if not isinstance(levels, list) or not levels:
        return None
    best: Optional[int] = None
    for lvl in levels:
        p = _price_from_level(lvl)
        if p is None:
            continue
        best = p if best is None else max(best, p)
    return best


def _best_ask(levels: Any) -> Optional[int]:
    if not isinstance(levels, list) or not levels:
        return None
    best: Optional[int] = None
    for lvl in levels:
        p = _price_from_level(lvl)
        if p is None:
            continue
        best = p if best is None else min(best, p)
    return best


def _extract_side_books(side_obj: Any) -> Tuple[Any, Any]:
    if isinstance(side_obj, dict):
        bids = side_obj.get("bids") or side_obj.get("bid") or side_obj.get("buy")
        asks = side_obj.get("asks") or side_obj.get("ask") or side_obj.get("sell")
        return bids, asks
    if isinstance(side_obj, list):
        return side_obj, None
    return None, None


_YES_ASK_CACHE: Dict[str, Any] = {"ask": None, "ts": 0.0}


def _cache_yes_ask(ask: int) -> None:
    _YES_ASK_CACHE["ask"] = int(ask)
    _YES_ASK_CACHE["ts"] = time.time()


def _get_cached_yes_ask() -> Optional[int]:
    ask = _YES_ASK_CACHE.get("ask")
    ts = float(_YES_ASK_CACHE.get("ts") or 0.0)
    if ask is None:
        return None
    if ASK_CACHE_TTL_SECONDS <= 0:
        return None
    if (time.time() - ts) <= ASK_CACHE_TTL_SECONDS:
        return int(ask)
    return None


_LAST_MARKET_FALLBACK_TS: float = 0.0
_LAST_FALLBACK_LOG_TS: Dict[str, float] = {}


def fetch_market(market_ticker: str) -> Dict[str, Any]:
    return request_json("GET", f"/markets/{market_ticker}")


def _to_cents(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        if isinstance(x, str):
            s = x.strip()
            if s == "":
                return None
            fx = float(s)
        elif isinstance(x, (int, float)):
            fx = float(x)
        else:
            return None

        if fx <= 1.0:
            cents = int(round(fx * 100))
        else:
            cents = int(round(fx))

        if 1 <= cents <= 99:
            return cents
        return None
    except Exception:
        return None


def _market_top_of_book_yes(market_payload: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    if not isinstance(market_payload, dict):
        return (None, None)

    m = market_payload.get("market")
    d = m if isinstance(m, dict) else market_payload

    yes_bid = _to_cents(
        d.get("yes_bid_dollars")
        or d.get("yes_bid")
        or d.get("best_yes_bid")
        or d.get("yes_bid_price")
        or d.get("yes_bid_price_cents")
        or d.get("yes_bid_cents")
        or d.get("best_bid_yes")
        or d.get("yesBid")
    )
    yes_ask = _to_cents(
        d.get("yes_ask_dollars")
        or d.get("yes_ask")
        or d.get("best_yes_ask")
        or d.get("yes_ask_price")
        or d.get("yes_ask_price_cents")
        or d.get("yes_ask_cents")
        or d.get("best_ask_yes")
        or d.get("yesAsk")
    )
    return (yes_bid, yes_ask)


def get_yes_bid_ask(orderbook_payload: Dict[str, Any], market_ticker: str) -> Tuple[Optional[int], Optional[int]]:
    ob = orderbook_payload.get("orderbook") if isinstance(orderbook_payload, dict) else None
    if not isinstance(ob, dict):
        return (None, None)

    yes_obj = ob.get("yes")
    no_obj = ob.get("no")

    yes_bids, yes_asks = _extract_side_books(yes_obj)
    no_bids, no_asks = _extract_side_books(no_obj)

    # ✅ PATCH: also support *_dollars arrays that Kalshi returns on this endpoint
    yes_bids_dollars = ob.get("yes_dollars")
    no_bids_dollars = ob.get("no_dollars")

    yes_bid = _best_bid(yes_bids)
    yes_ask = _best_ask(yes_asks)

    if yes_bid is None:
        yes_bid = _best_bid(yes_bids_dollars)

    if yes_ask is None:
        no_best_bid = _best_bid(no_bids)
        if no_best_bid is None:
            no_best_bid = _best_bid(no_bids_dollars)
        if no_best_bid is not None:
            yes_ask = 100 - int(no_best_bid)

    if yes_bid is None:
        no_best_ask = _best_ask(no_asks)
        if no_best_ask is not None:
            yes_bid = 100 - int(no_best_ask)

    if yes_ask is not None:
        _cache_yes_ask(int(yes_ask))
        return (yes_bid, yes_ask)

    cached = _get_cached_yes_ask()
    if cached is not None:
        return (yes_bid, cached)

    global _LAST_MARKET_FALLBACK_TS
    now = time.time()
    if MARKET_FALLBACK_MIN_SECONDS > 0 and (now - _LAST_MARKET_FALLBACK_TS) < MARKET_FALLBACK_MIN_SECONDS:
        return (yes_bid, None)

    _LAST_MARKET_FALLBACK_TS = now
    try:
        mp = fetch_market(market_ticker)
        fb_bid, fb_ask = _market_top_of_book_yes(mp)
        if fb_ask is not None:
            _cache_yes_ask(int(fb_ask))
            if yes_bid is None and fb_bid is not None:
                yes_bid = int(fb_bid)
            log.info(
                "[FALLBACK] %s /markets top-of-book: yes_bid=%s yes_ask=%s",
                market_ticker,
                fb_bid,
                fb_ask,
            )
            return (yes_bid, int(fb_ask))

        last = float(_LAST_FALLBACK_LOG_TS.get(market_ticker, 0.0))
        if (now - last) >= 5.0:
            log.info("[FALLBACK] %s /markets returned no usable yes_ask fields", market_ticker)
            _LAST_FALLBACK_LOG_TS[market_ticker] = now

    except Exception as e:
        last = float(_LAST_FALLBACK_LOG_TS.get(market_ticker, 0.0))
        if (now - last) >= 5.0:
            log.info("[FALLBACK] %s /markets error: %s", market_ticker, e)
            _LAST_FALLBACK_LOG_TS[market_ticker] = now

    return (yes_bid, None)


def fetch_orderbook(market_ticker: str) -> Dict[str, Any]:
    return request_json("GET", f"/markets/{market_ticker}/orderbook")

# -----------------------------
# Quote target calculator (YES-only)
# -----------------------------
def clamp_price(p: int) -> int:
    return max(1, min(99, p))


# -----------------------------
# Inventory tracking (NEW)
# NET_YES: + = long YES, - = short YES (often shows as NO exposure in UI)
# -----------------------------
NET_YES: int = 0
_ORDER_LAST_FILLED: Dict[str, int] = {}
_LAST_ORDER_STATUS_POLL_TS: Dict[str, float] = {}
_UNKNOWN_UNTIL_TS: float = 0.0


def _extract_order_obj(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    o = payload.get("order")
    return o if isinstance(o, dict) else payload


def _extract_filled_count(order_obj: Dict[str, Any]) -> int:
    # Try a bunch of likely keys; default 0
    for k in ("filled_count", "filled", "fill_count", "filledContracts", "filled_contracts"):
        v = order_obj.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except Exception:
            continue
    # Some APIs nest fills; keep it simple (0) if not present
    return 0


def _extract_status(order_obj: Dict[str, Any]) -> str:
    s = order_obj.get("status") or order_obj.get("state") or order_obj.get("order_status")
    return str(s or "").lower()


def _should_poll_order(order_id: str, now: float) -> bool:
    if ORDER_STATUS_POLL_SECONDS <= 0:
        return True
    last = float(_LAST_ORDER_STATUS_POLL_TS.get(order_id, 0.0))
    return (now - last) >= ORDER_STATUS_POLL_SECONDS


def _mark_polled(order_id: str, now: float) -> None:
    _LAST_ORDER_STATUS_POLL_TS[order_id] = now


def _apply_fill_delta(side_key: str, order_id: str, new_filled: int) -> None:
    global NET_YES
    prev = int(_ORDER_LAST_FILLED.get(order_id, 0))
    delta = int(new_filled) - prev
    if delta <= 0:
        _ORDER_LAST_FILLED[order_id] = int(new_filled)
        return

    # buy fills increase NET_YES, sell fills decrease NET_YES
    if side_key == "buy":
        NET_YES += delta
    elif side_key == "sell":
        NET_YES -= delta

    _ORDER_LAST_FILLED[order_id] = int(new_filled)
    log.info("[INV] fill_delta side=%s order_id=%s delta=%d NET_YES=%d", side_key, order_id, delta, NET_YES)


def reconcile_order_status_for_working(side_key: str, cur: "WorkingOrder", now: float) -> None:
    """
    Poll the order status (throttled) and:
      - apply incremental filled deltas to NET_YES
      - clear WORKING slot if order is no longer open
      - on ambiguous 404, pause quoting temporarily (PAUSE_ON_UNKNOWN_SECONDS)
    """
    global _UNKNOWN_UNTIL_TS
    global WORKING, _LAST_KEEP_LOGGED  # ✅ MICRO CHANGE: allow clearing from here

    if DRY_RUN or (not ENABLE_TRADING) or (not cur.order_id):
        return

    oid = str(cur.order_id)

    if not _should_poll_order(oid, now):
        return

    _mark_polled(oid, now)

    try:
        payload = fetch_order_status(oid)
        order_obj = _extract_order_obj(payload)
        status = _extract_status(order_obj)
        filled = _extract_filled_count(order_obj)

        _apply_fill_delta(side_key, oid, filled)

        # If order is no longer working, clear it so we can replace
        # (status strings vary, so treat anything not "open" / "resting" / "active" as done)
        if status and status not in ("open", "resting", "active", "placed", "pending"):
            log.info("[INV] order_done side=%s order_id=%s status=%s filled=%d → clear WORKING", side_key, oid, status, filled)
            WORKING[side_key] = None
            _LAST_KEEP_LOGGED[side_key] = None
            return

    except Exception as e:
        s = str(e).lower()
        if "http 404" in s and "not_found" in s:
            # ✅ MICRO CHANGE:
            # If the order is NOT in open-orders, treat it as gone immediately (no pause).
            if not is_order_open(oid):
                log.info(
                    "[INV] order_status_404 side=%s order_id=%s but order not open → clear WORKING (no pause)",
                    side_key,
                    oid,
                )
                WORKING[side_key] = None
                _LAST_KEEP_LOGGED[side_key] = None
                return

            # Otherwise truly ambiguous: pause briefly and let it settle.
            _UNKNOWN_UNTIL_TS = max(_UNKNOWN_UNTIL_TS, now + max(0.0, PAUSE_ON_UNKNOWN_SECONDS))
            log.warning(
                "[INV] order_status_404 side=%s order_id=%s → pause %.1fs (UNKNOWN_UNTIL=%.3f)",
                side_key,
                oid,
                PAUSE_ON_UNKNOWN_SECONDS,
                _UNKNOWN_UNTIL_TS,
            )
            return
        raise


def apply_inventory_gates_and_skew(
    bid: Optional[int],
    ask: Optional[int],
) -> Tuple[Optional[int], Optional[int], str]:
    """
    Apply:
      - hard caps: MAX_NET_YES_CONTRACTS
      - skew: INVENTORY_SKEW_CENTS
    Returns updated (bid, ask, note).
    """
    note_parts = []

    # Hard cap gates
    if MAX_NET_YES_CONTRACTS > 0:
        if NET_YES >= MAX_NET_YES_CONTRACTS:
            bid = None
            note_parts.append(f"cap_long_yes(net={NET_YES}>=max={MAX_NET_YES_CONTRACTS})")
        if NET_YES <= -MAX_NET_YES_CONTRACTS:
            ask = None
            note_parts.append(f"cap_short_yes(net={NET_YES}<=-max={MAX_NET_YES_CONTRACTS})")

    # Skew (only if both present; if one side gated out, we just keep the other)
    if INVENTORY_SKEW_CENTS and bid is not None and ask is not None:
        k = max(0, min(3, int(INVENTORY_SKEW_CENTS)))

        # magnitude: 1..3 per contract, capped
        mag = min(6, abs(int(NET_YES)) * k)  # cap skew so it doesn't explode
        if mag > 0:
            if NET_YES > 0:
                # long YES → discourage more buys (lower bid), encourage sells (lower ask)
                bid = clamp_price(int(bid) - mag)
                ask = clamp_price(int(ask) - mag)
                note_parts.append(f"skew_long_yes(-{mag})")
            elif NET_YES < 0:
                # short YES → encourage buys (higher bid), discourage sells (higher ask)
                bid = clamp_price(int(bid) + mag)
                ask = clamp_price(int(ask) + mag)
                note_parts.append(f"skew_short_yes(+{mag})")

            if bid is not None and ask is not None and bid >= ask:
                # If skew collapsed spread, back off to safe no-quote
                return (None, None, "inv_skew_locked_or_crossed")

    note = " ".join(note_parts) if note_parts else "inv_ok"
    return (bid, ask, note)


def compute_target_yes_quotes(
    yes_bid: Optional[int],
    yes_ask: Optional[int],
) -> Tuple[Optional[int], Optional[int], str]:
    if yes_bid is None or yes_ask is None:
        return (None, None, "missing_bid_or_ask")

    if yes_ask <= yes_bid:
        return (None, None, "crossed_or_locked")

    spread = yes_ask - yes_bid
    if spread < MIN_SPREAD_CENTS:
        return (None, None, f"spread_too_tight({spread})")

    if ENABLE_JOIN_TIGHT_SPREAD and spread <= JOIN_ONLY_MAX_SPREAD_CENTS:
        bid = clamp_price(int(yes_bid))
        ask = clamp_price(int(yes_ask))
        if bid >= ask:
            return (None, None, "join_locked_or_crossed")
        bid, ask, inv_note = apply_inventory_gates_and_skew(bid, ask)
        return (bid, ask, f"join(spread={spread}) {inv_note}")

    # ✅ MICRO FIX: for small-but-not-join spreads, do NOT improve.
    # Just join the best bid/ask to reduce adverse selection.
    if NO_IMPROVE_MAX_SPREAD_CENTS > 0 and spread <= NO_IMPROVE_MAX_SPREAD_CENTS:
        bid = clamp_price(int(yes_bid))
        ask = clamp_price(int(yes_ask))
        if bid >= ask:
            return (None, None, "no_improve_join_locked_or_crossed")
        bid, ask, inv_note = apply_inventory_gates_and_skew(bid, ask)
        return (bid, ask, f"no_improve_join(spread={spread}) {inv_note}")

    bid = clamp_price(yes_bid + TICK_CENTS)
    ask = clamp_price(yes_ask - TICK_CENTS)

    max_bid = clamp_price(yes_ask - EDGE_CENTS)
    min_ask = clamp_price(yes_bid + EDGE_CENTS)

    bid = min(bid, max_bid)
    ask = max(ask, min_ask)

    if bid >= ask:
        return (None, None, "no_room_after_edge")

    bid, ask, inv_note = apply_inventory_gates_and_skew(bid, ask)
    if bid is None and ask is None:
        return (None, None, f"inv_gated({inv_note})")

    return (bid, ask, f"ok(spread={spread}) {inv_note}")

# -----------------------------
# Order management
# -----------------------------
@dataclass
class WorkingOrder:
    side: str
    price_cents: int
    qty: int
    created_ts: float
    order_id: Optional[str] = None


WORKING: Dict[str, Optional[WorkingOrder]] = {"buy": None, "sell": None}
_LAST_KEEP_LOGGED: Dict[str, Optional[int]] = {"buy": None, "sell": None}

NO_TARGET_SINCE_TS: Optional[float] = None
NO_TARGET_SIDE_SINCE_TS: Dict[str, Optional[float]] = {"buy": None, "sell": None}
_LAST_SIDE_HOLD_LOG_TS: Dict[str, float] = {"buy": 0.0, "sell": 0.0}

TIGHT_SPREAD_SINCE_TS: Optional[float] = None
NOT_STABLE_SINCE_TS: Optional[float] = None

UNSAFE_SINCE_TS: Dict[str, Optional[float]] = {"buy": None, "sell": None}


def reconcile_quotes(
    market_ticker: str,
    target_bid: Optional[int],
    target_ask: Optional[int],
    qty: int,
    dry_run: bool,
    yes_bid: Optional[int],
    yes_ask: Optional[int],
    skip_reason: Optional[str] = None,
) -> None:
    global NO_TARGET_SINCE_TS, NO_TARGET_SIDE_SINCE_TS, _LAST_SIDE_HOLD_LOG_TS
    global TIGHT_SPREAD_SINCE_TS, NOT_STABLE_SINCE_TS, UNSAFE_SINCE_TS
    global _UNKNOWN_UNTIL_TS

    now = time.time()

    # ✅ Inventory reconciliation first (throttled)
    try:
        for sk in ("buy", "sell"):
            cur = WORKING.get(sk)
            if cur is not None:
                reconcile_order_status_for_working(sk, cur, now)
    except Exception as e:
        log.warning("[INV] reconcile_order_status error: %s", e)

    # ✅ Pause if order states are ambiguous (404 window)
    if (not dry_run) and ENABLE_TRADING and _UNKNOWN_UNTIL_TS > 0 and now < _UNKNOWN_UNTIL_TS:
        remaining = _UNKNOWN_UNTIL_TS - now
        log.warning("[INV] PAUSE quoting due to unknown order state: %.2fs remaining", remaining)
        return

    def price_off(cur_price: int, target_price: int) -> int:
        return abs(int(cur_price) - int(target_price))

    def can_requote(cur: WorkingOrder) -> bool:
        if MIN_REQUOTE_SECONDS <= 0:
            return True
        return (now - cur.created_ts) >= MIN_REQUOTE_SECONDS

    def cancel(side_key: str, reason: str) -> None:
        cur = WORKING[side_key]
        if cur is None:
            return

        log.info(
            f"[OM] {market_ticker} {side_key.upper()} CANCEL @{cur.price_cents} ({reason}) DRY_RUN={dry_run}"
        )

        cancel_status = "canceled"
        if (not dry_run) and ENABLE_TRADING and cur.order_id:
            cancel_status = cancel_order_live(cur.order_id)

        if cancel_status == "canceled":
            WORKING[side_key] = None
            _LAST_KEEP_LOGGED[side_key] = None
            return

        # 404 not_found (ambiguous): DO NOT clear local state yet.
        # Pause briefly, then reconcile using open-orders probe.
        global _UNKNOWN_UNTIL_TS
        _UNKNOWN_UNTIL_TS = max(_UNKNOWN_UNTIL_TS, now + max(0.0, PAUSE_ON_UNKNOWN_SECONDS))

        if cur.order_id and (not is_order_open(cur.order_id)):
            log.info(
                "[OM] %s %s CANCEL 404 but order not open → treat as gone; clear WORKING",
                market_ticker,
                side_key.upper(),
            )
            WORKING[side_key] = None
            _LAST_KEEP_LOGGED[side_key] = None
            return

        log.warning(
            "[OM] %s %s CANCEL 404 and order still appears OPEN → KEEP local state; will retry via reconcile",
            market_ticker,
            side_key.upper(),
        )
        return

    def _is_post_only_cross_error(e: Exception) -> bool:
        s = str(e).lower()
        return ("invalid_order" in s) and ("post only cross" in s or "post_only_cross" in s or "post-only cross" in s)

    def place(side_key: str, price: int) -> None:
        # ✅ pre-guard price vs current observed book to reduce POST_ONLY rejects
        p = int(price)
        p = _post_only_safe_price(side_key, p, yes_bid=yes_bid, yes_ask=yes_ask)

        log.info(f"[OM] {market_ticker} {side_key.upper()} PLACE @{p} qty={qty} DRY_RUN={dry_run}")

        order_id = None
        if (not dry_run) and ENABLE_TRADING:
            try:
                order_id = place_order_live(
                    market_ticker=market_ticker,
                    action=side_key,
                    yes_price_cents=int(p),
                    count=int(qty),
                )
                log.info(f"[OM] {market_ticker} {side_key.upper()} POSTED order_id={order_id} @ {p} qty={qty}")

            except Exception as e:
                # ✅ handle post-only cross as a soft failure + retry once with 1 more tick of safety
                if POST_ONLY and _is_post_only_cross_error(e):
                    bump = max(1, POST_ONLY_BUFFER_CENTS)
                    p2 = p - bump if side_key == "buy" else p + bump
                    p2 = _clamp(p2)

                    log.info(
                        "[OM] %s %s POST_ONLY_CROSS @%d → retry @%d",
                        market_ticker,
                        side_key.upper(),
                        p,
                        p2,
                    )
                    try:
                        order_id = place_order_live(
                            market_ticker=market_ticker,
                            action=side_key,
                            yes_price_cents=int(p2),
                            count=int(qty),
                        )
                        log.info(f"[OM] {market_ticker} {side_key.upper()} POSTED order_id={order_id} @ {p2} qty={qty}")
                        p = p2
                    except Exception as e2:
                        if _is_post_only_cross_error(e2):
                            log.info(
                                "[OM] %s %s SKIP (post_only_cross even after retry) target=%d retry=%d",
                                market_ticker,
                                side_key.upper(),
                                p,
                                p2,
                            )
                            return
                        raise
                else:
                    raise

        WORKING[side_key] = WorkingOrder(
            side=side_key,
            price_cents=int(p),
            qty=int(qty),
            created_ts=now,
            order_id=order_id,
        )
        _LAST_KEEP_LOGGED[side_key] = None

        # Initialize last-filled tracking so first poll doesn't double-count
        if order_id:
            _ORDER_LAST_FILLED.setdefault(str(order_id), 0)

    def keep(side_key: str, cur: WorkingOrder, note: str) -> None:
        if _LAST_KEEP_LOGGED.get(side_key) != cur.price_cents:
            log.info(
                f"[OM] {market_ticker} {side_key.upper()} KEEP @{cur.price_cents} qty={cur.qty} ({note}) "
                f"DRY_RUN={dry_run} order_id={cur.order_id}"
            )
            _LAST_KEEP_LOGGED[side_key] = cur.price_cents

    def is_unsafe(side_key: str, price_cents: int) -> bool:
        if yes_bid is None or yes_ask is None:
            return False
        if side_key == "sell":
            return int(price_cents) <= int(yes_bid)
        if side_key == "buy":
            return int(price_cents) >= int(yes_ask)
        return False

    def unsafe_to_hold_with_grace(side_key: str, cur: WorkingOrder) -> bool:
        if not is_unsafe(side_key, cur.price_cents):
            UNSAFE_SINCE_TS[side_key] = None
            return False

        if UNSAFE_SINCE_TS[side_key] is None:
            UNSAFE_SINCE_TS[side_key] = now
            return False

        unsafe_for = now - float(UNSAFE_SINCE_TS[side_key] or now)
        return unsafe_for >= max(0.0, UNSAFE_GRACE_SECONDS)

    both_missing = (target_bid is None) and (target_ask is None)

    if both_missing:
        is_tight_spread_skip = isinstance(skip_reason, str) and skip_reason.startswith("spread_too_tight")
        is_not_stable_skip = isinstance(skip_reason, str) and skip_reason.startswith("spread_ok_not_stable")

        if is_tight_spread_skip:
            NOT_STABLE_SINCE_TS = None
            if TIGHT_SPREAD_SINCE_TS is None:
                TIGHT_SPREAD_SINCE_TS = now
            tight_for = now - TIGHT_SPREAD_SINCE_TS

            if HOLD_ON_SKIP and tight_for < EXIT_BAD_SECONDS:
                if WORKING["buy"] or WORKING["sell"]:
                    log.info(
                        "[OM] %s HOLD (tight_spread) held_for=%.2fs<%.2fs DRY_RUN=%s",
                        market_ticker,
                        tight_for,
                        EXIT_BAD_SECONDS,
                        dry_run,
                    )
                return

            cancel("buy", f"tight_spread>{EXIT_BAD_SECONDS:.2f}s({skip_reason})")
            cancel("sell", f"tight_spread>{EXIT_BAD_SECONDS:.2f}s({skip_reason})")
            NO_TARGET_SINCE_TS = None
            return

        if is_not_stable_skip:
            TIGHT_SPREAD_SINCE_TS = None
            if NOT_STABLE_SINCE_TS is None:
                NOT_STABLE_SINCE_TS = now
            ns_for = now - NOT_STABLE_SINCE_TS

            if HOLD_ON_SKIP and ns_for < NOT_STABLE_EXIT_SECONDS:
                if WORKING["buy"] or WORKING["sell"]:
                    log.info(
                        "[OM] %s HOLD (not_stable) held_for=%.2fs<%.2fs DRY_RUN=%s",
                        market_ticker,
                        ns_for,
                        NOT_STABLE_EXIT_SECONDS,
                        dry_run,
                    )
                return

            cancel("buy", f"not_stable>{NOT_STABLE_EXIT_SECONDS:.2f}s({skip_reason})")
            cancel("sell", f"not_stable>{NOT_STABLE_EXIT_SECONDS:.2f}s({skip_reason})")
            NO_TARGET_SINCE_TS = None
            return

        TIGHT_SPREAD_SINCE_TS = None
        NOT_STABLE_SINCE_TS = None

        if NO_TARGET_SINCE_TS is None:
            NO_TARGET_SINCE_TS = now

        held_for = now - NO_TARGET_SINCE_TS

        if HOLD_ON_SKIP and held_for < CANCEL_IF_NO_TARGET_SECONDS:
            if WORKING["buy"] or WORKING["sell"]:
                log.info(
                    "[OM] %s HOLD (no_target) held_for=%.2fs<%.2fs DRY_RUN=%s",
                    market_ticker,
                    held_for,
                    CANCEL_IF_NO_TARGET_SECONDS,
                    dry_run,
                )
            return

        cancel("buy", f"no_target>{CANCEL_IF_NO_TARGET_SECONDS:.2f}s")
        cancel("sell", f"no_target>{CANCEL_IF_NO_TARGET_SECONDS:.2f}s")
        return

    NO_TARGET_SINCE_TS = None
    TIGHT_SPREAD_SINCE_TS = None
    NOT_STABLE_SINCE_TS = None

    if target_bid is not None:
        NO_TARGET_SIDE_SINCE_TS["buy"] = None
    if target_ask is not None:
        NO_TARGET_SIDE_SINCE_TS["sell"] = None

    def handle_missing_side(side_key: str) -> None:
        cur = WORKING[side_key]
        if cur is None:
            return

        if unsafe_to_hold_with_grace(side_key, cur):
            cancel(side_key, f"unsafe_hold(cross_risk>{UNSAFE_GRACE_SECONDS:.2f}s)")
            NO_TARGET_SIDE_SINCE_TS[side_key] = None
            UNSAFE_SINCE_TS[side_key] = None
            return

        if NO_TARGET_SIDE_SINCE_TS[side_key] is None:
            NO_TARGET_SIDE_SINCE_TS[side_key] = now

        held_for = now - float(NO_TARGET_SIDE_SINCE_TS[side_key] or now)

        if SIDE_HOLD_SECONDS > 0 and held_for < SIDE_HOLD_SECONDS:
            if (now - _LAST_SIDE_HOLD_LOG_TS.get(side_key, 0.0)) >= 5.0:
                log.info(
                    "[OM] %s %s HOLD_SIDE held_for=%.2fs<%.2fs price=@%d DRY_RUN=%s",
                    market_ticker,
                    side_key.upper(),
                    held_for,
                    SIDE_HOLD_SECONDS,
                    cur.price_cents,
                    dry_run,
                )
                _LAST_SIDE_HOLD_LOG_TS[side_key] = now
            return

        cancel(side_key, f"no_target_side>{SIDE_HOLD_SECONDS:.2f}s")
        NO_TARGET_SIDE_SINCE_TS[side_key] = None

    if target_bid is None:
        handle_missing_side("buy")
    if target_ask is None:
        handle_missing_side("sell")

    def maintain(side_key: str, target_price: Optional[int]) -> None:
        if target_price is None:
            return

        cur = WORKING[side_key]
        if cur is None:
            place(side_key, int(target_price))
            return

        off = price_off(cur.price_cents, int(target_price))

        if unsafe_to_hold_with_grace(side_key, cur):
            cancel(side_key, f"unsafe_hold(cross_risk>{UNSAFE_GRACE_SECONDS:.2f}s)")
            UNSAFE_SINCE_TS[side_key] = None
            if off >= MAX_CHASE_CENTS:
                return
            place(side_key, int(target_price))
            return

        if off >= MAX_CHASE_CENTS:
            cancel(side_key, f"too_far_to_chase(off_by={off}>=MAX_CHASE_CENTS={MAX_CHASE_CENTS})")
            return

        if off < REPRICE_IF_OFF_BY_CENTS:
            keep(side_key, cur, f"off_by={off}<thresh({REPRICE_IF_OFF_BY_CENTS})")
            return

        # ✅ B-player anti-churn: during cooldown, KEEP (do not cancel)
        if not can_requote(cur):
            age = now - cur.created_ts
            keep(side_key, cur, f"cooldown(age={age:.2f}s<{MIN_REQUOTE_SECONDS:.2f}s off_by={off})")
            return

        cancel(side_key, f"reprice(off_by={off})")
        place(side_key, int(target_price))

    maintain("buy", target_bid)
    maintain("sell", target_ask)

# -----------------------------
# Main loop
# -----------------------------
def main():
    log.info(
        "API_BASE=%s API_PREFIX=%s SERIES=%s EVENT_TICKER=%s MARKET_OVERRIDE=%s POLL=%.2fs DRY_RUN=%s ENABLE_TRADING=%s POST_ONLY=%s",
        API_BASE,
        API_PREFIX,
        SERIES,
        EVENT_TICKER or "<auto>",
        MARKET_TICKER_OVERRIDE or "<none>",
        POLL,
        DRY_RUN,
        ENABLE_TRADING,
        POST_ONLY,
    )
    log.info(
        "[INV] MAX_NET_YES_CONTRACTS=%d INVENTORY_SKEW_CENTS=%d ORDER_STATUS_POLL_SECONDS=%.2f PAUSE_ON_UNKNOWN_SECONDS=%.2f",
        MAX_NET_YES_CONTRACTS,
        INVENTORY_SKEW_CENTS,
        ORDER_STATUS_POLL_SECONDS,
        PAUSE_ON_UNKNOWN_SECONDS,
    )
    log.info(
        "[MICRO] NO_IMPROVE_MAX_SPREAD_CENTS=%d (<= this spread: join, do not improve)",
        NO_IMPROVE_MAX_SPREAD_CENTS,
    )

    last_market: Optional[str] = None
    last_yes_bid: Optional[int] = None
    last_yes_ask: Optional[int] = None
    last_tb: Optional[int] = None
    last_ta: Optional[int] = None
    last_why: Optional[str] = None

    spread_ok_since: Optional[float] = None
    last_inv_log_ts: float = 0.0

    while True:
        try:
            mkt = roll_active_market()

            market_changed = (mkt != last_market)
            if market_changed:
                last_market = mkt
                last_yes_bid = None
                last_yes_ask = None
                last_tb = None
                last_ta = None
                last_why = None

                WORKING["buy"] = None
                WORKING["sell"] = None
                _LAST_KEEP_LOGGED["buy"] = None
                _LAST_KEEP_LOGGED["sell"] = None

                global NO_TARGET_SINCE_TS, NO_TARGET_SIDE_SINCE_TS, TIGHT_SPREAD_SINCE_TS, NOT_STABLE_SINCE_TS
                global UNSAFE_SINCE_TS
                NO_TARGET_SINCE_TS = None
                NO_TARGET_SIDE_SINCE_TS = {"buy": None, "sell": None}
                TIGHT_SPREAD_SINCE_TS = None
                NOT_STABLE_SINCE_TS = None
                UNSAFE_SINCE_TS = {"buy": None, "sell": None}

                _YES_ASK_CACHE["ask"] = None
                _YES_ASK_CACHE["ts"] = 0.0

                global _LAST_MARKET_FALLBACK_TS
                _LAST_MARKET_FALLBACK_TS = 0.0

                global _LAST_FALLBACK_LOG_TS
                _LAST_FALLBACK_LOG_TS = {}

                global _LAST_ORDER_STATUS_POLL_TS, _UNKNOWN_UNTIL_TS
                _LAST_ORDER_STATUS_POLL_TS = {}
                _UNKNOWN_UNTIL_TS = 0.0

                spread_ok_since = None
                log.info("[OM] %s ROLL detected → cleared working orders (DRY_RUN=%s)", mkt, DRY_RUN)

            ob = fetch_orderbook(mkt)
            yes_bid, yes_ask = get_yes_bid_ask(ob, mkt)

            quote_changed = (yes_bid != last_yes_bid) or (yes_ask != last_yes_ask) or market_changed
            if quote_changed:
                last_yes_bid, last_yes_ask = yes_bid, yes_ask
                log.info("[QUOTE] %s YES bid=%s ask=%s", mkt, yes_bid, yes_ask)

            # Periodic inventory log
            now = time.time()
            if (now - last_inv_log_ts) >= 10.0:
                log.info("[INV] NET_YES=%d (cap=%d)", NET_YES, MAX_NET_YES_CONTRACTS)
                last_inv_log_ts = now

            tb, ta, why = compute_target_yes_quotes(yes_bid, yes_ask)

            if tb is not None and ta is not None and yes_bid is not None and yes_ask is not None:
                spread = int(yes_ask) - int(yes_bid)
                if spread >= MIN_SPREAD_CENTS:
                    if spread_ok_since is None:
                        spread_ok_since = time.time()
                    ok_for = time.time() - spread_ok_since
                    if ok_for < ENTER_OK_SECONDS:
                        tb, ta, why = (None, None, f"spread_ok_not_stable({spread}) {ok_for:.2f}s<{ENTER_OK_SECONDS:.2f}s")
                else:
                    spread_ok_since = None
            else:
                if isinstance(why, str) and why.startswith("spread_too_tight"):
                    spread_ok_since = None

            target_changed = (tb != last_tb) or (ta != last_ta) or (why != last_why) or market_changed
            if target_changed:
                last_tb, last_ta, last_why = tb, ta, why
                if tb is None and ta is None:
                    log.info("[TARGET] %s → SKIP (%s)", mkt, why)
                else:
                    log.info("[TARGET] %s YES-only would_quote: bid@%s ask@%s (%s) DRY_RUN=%s", mkt, tb, ta, why, DRY_RUN)

            reconcile_quotes(
                market_ticker=mkt,
                target_bid=tb,
                target_ask=ta,
                qty=ORDER_QTY,
                dry_run=DRY_RUN,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                skip_reason=why if (tb is None and ta is None) else None,
            )

        except Exception as e:
            log.error("[LOOPERR] %s", e)

        time.sleep(POLL)


if __name__ == "__main__":
    main()