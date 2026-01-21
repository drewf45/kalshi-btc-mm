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
MIN_SPREAD_CENTS = int(getenv_first(["MIN_SPREAD_CENTS"], "3"))

# --- Safety / Patience gates (THIS is the big long-term win) ---
# Spread must be >= MIN_SPREAD_CENTS continuously for this long before we place quotes
ENTER_OK_SECONDS = float(getenv_first(["ENTER_OK_SECONDS"], "3.0"))
# Once we are quoting, spread must be "bad" continuously for this long before we cancel due to tight spread
EXIT_BAD_SECONDS = float(getenv_first(["EXIT_BAD_SECONDS"], "6.0"))

# Order params
ORDER_QTY = int(getenv_first(["ORDER_QTY"], "1"))

# Minimum time between reprices (prevents churn)
MIN_REQUOTE_SECONDS = float(getenv_first(["MIN_REQUOTE_SECONDS"], "3.0"))

# Only reprice if we are "meaningfully" off target
REPRICE_IF_OFF_BY_CENTS = int(getenv_first(["REPRICE_IF_OFF_BY_CENTS"], "2"))

# When BOTH targets are None, hold existing orders instead of canceling immediately
HOLD_ON_SKIP = parse_bool(getenv_first(["HOLD_ON_SKIP"], "true"), default=True)

# Only cancel after BOTH targets have been None continuously for this long (generic no-target)
CANCEL_IF_NO_TARGET_SECONDS = float(getenv_first(["CANCEL_IF_NO_TARGET_SECONDS"], "10.0"))

# Micro #1: ask cache TTL to ride out NO-side blips
ASK_CACHE_TTL_SECONDS = float(getenv_first(["ASK_CACHE_TTL_SECONDS"], "5.0"))

# Micro #2: fallback to /markets/{ticker} when ask missing (throttled)
MARKET_FALLBACK_MIN_SECONDS = float(getenv_first(["MARKET_FALLBACK_MIN_SECONDS"], "5.0"))

# Micro #3 toggles (kept, but we still hard-gate on MIN_SPREAD_CENTS)
ENABLE_ONE_SIDED_TIGHT = parse_bool(getenv_first(["ENABLE_ONE_SIDED_TIGHT"], "true"), default=True)
ENABLE_JOIN_TIGHT_SPREAD = parse_bool(getenv_first(["ENABLE_JOIN_TIGHT_SPREAD"], "true"), default=True)

# Micro #4: per-side hysteresis (don’t instantly cancel the missing side)
SIDE_HOLD_SECONDS = float(getenv_first(["SIDE_HOLD_SECONDS"], "5.0"))

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
    # Render env key in your logs: KALSHI_PRIVATE_KEY_PEM_BASE64
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

    if EVENT_TICKER:
        evt, mkt = resolve_event_and_market_via_markets(SERIES)
        if not mkt:
            raise RuntimeError(
                f"Could not resolve market via /markets for series={SERIES} (EVENT_TICKER pinned={EVENT_TICKER})."
            )
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


# Micro #1: cache inferred YES ask for short TTL
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


# Micro #2: /markets/{ticker} fallback (throttled)
_LAST_MARKET_FALLBACK_TS: float = 0.0


def fetch_market(market_ticker: str) -> Dict[str, Any]:
    return request_json("GET", f"/markets/{market_ticker}")


def _to_cents(x: Any) -> Optional[int]:
    """
    Accepts:
      - dollars like 0.91, "0.91", 0.91 -> 91
      - cents like 91, "91" -> 91
    Returns int cents (1..99) or None
    """
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
        or d.get("best_bid_yes")
    )
    yes_ask = _to_cents(
        d.get("yes_ask_dollars")
        or d.get("yes_ask")
        or d.get("best_yes_ask")
        or d.get("yes_ask_price")
        or d.get("best_ask_yes")
    )
    return (yes_bid, yes_ask)


def get_yes_bid_ask(orderbook_payload: Dict[str, Any], market_ticker: str) -> Tuple[Optional[int], Optional[int]]:
    ob = orderbook_payload.get("orderbook") if isinstance(orderbook_payload, dict) else None
    if not isinstance(ob, dict):
        return (None, None)

    yes = ob.get("yes")
    no = ob.get("no")

    yes_bid = best_bid_from_side(yes)
    no_bid = best_bid_from_side(no)

    # Kalshi binary: YES ask can be inferred from NO bid: yes_ask = 100 - no_best_bid
    if no_bid is not None:
        yes_ask = 100 - int(no_bid)
        _cache_yes_ask(yes_ask)
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
            log.info("[FALLBACK] %s /markets top-of-book: yes_bid=%s yes_ask=%s", market_ticker, fb_bid, fb_ask)
            return (yes_bid, int(fb_ask))
        else:
            log.info("[FALLBACK] %s /markets returned no usable yes_ask fields", market_ticker)
    except Exception as e:
        log.info("[FALLBACK] %s /markets error: %s", market_ticker, e)

    return (yes_bid, None)


def fetch_orderbook(market_ticker: str) -> Dict[str, Any]:
    return request_json("GET", f"/markets/{market_ticker}/orderbook")

# -----------------------------
# Quote target calculator (YES-only)
# -----------------------------
def clamp_price(p: int) -> int:
    return max(1, min(99, p))


def compute_target_yes_quotes(yes_bid: Optional[int], yes_ask: Optional[int]) -> Tuple[Optional[int], Optional[int], str]:
    if yes_bid is None or yes_ask is None:
        return (None, None, "missing_bid_or_ask")

    if yes_ask <= yes_bid:
        return (None, None, "crossed_or_locked")

    spread = yes_ask - yes_bid

    # Hard gate — do NOT quote unless spread >= MIN_SPREAD_CENTS
    if spread < MIN_SPREAD_CENTS:
        return (None, None, f"spread_too_tight({spread})")

    # Normal two-sided quoting: step inside by 1 tick (or configured tick)
    bid = clamp_price(yes_bid + TICK_CENTS)
    ask = clamp_price(yes_ask - TICK_CENTS)

    max_bid = clamp_price(yes_ask - EDGE_CENTS)
    min_ask = clamp_price(yes_bid + EDGE_CENTS)

    bid = min(bid, max_bid)
    ask = max(ask, min_ask)

    if bid >= ask:
        return (None, None, "no_room_after_edge")

    return (bid, ask, f"ok(spread={spread})")

# -----------------------------
# Order management (DRY_RUN now, real later)
# -----------------------------
@dataclass
class WorkingOrder:
    side: str            # "buy" or "sell"
    price_cents: int
    qty: int
    created_ts: float


WORKING: Dict[str, Optional[WorkingOrder]] = {"buy": None, "sell": None}
_LAST_KEEP_LOGGED: Dict[str, Optional[int]] = {"buy": None, "sell": None}

NO_TARGET_SINCE_TS: Optional[float] = None
NO_TARGET_SIDE_SINCE_TS: Dict[str, Optional[float]] = {"buy": None, "sell": None}
_LAST_SIDE_HOLD_LOG_TS: Dict[str, float] = {"buy": 0.0, "sell": 0.0}

# Tight spread timer (used for "EXIT_BAD_SECONDS" patience)
TIGHT_SPREAD_SINCE_TS: Optional[float] = None


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
    global NO_TARGET_SINCE_TS, NO_TARGET_SIDE_SINCE_TS, _LAST_SIDE_HOLD_LOG_TS, TIGHT_SPREAD_SINCE_TS

    now = time.time()

    def price_off(cur_price: int, target_price: int) -> int:
        return abs(int(cur_price) - int(target_price))

    def can_requote(cur: WorkingOrder) -> bool:
        if MIN_REQUOTE_SECONDS <= 0:
            return True
        return (now - cur.created_ts) >= MIN_REQUOTE_SECONDS

    def cancel(side: str, reason: str) -> None:
        cur = WORKING[side]
        if cur is None:
            return
        log.info(f"[OM] {market_ticker} {side.upper()} CANCEL @{cur.price_cents} ({reason}) DRY_RUN={dry_run}")
        WORKING[side] = None
        _LAST_KEEP_LOGGED[side] = None

    def place(side: str, price: int) -> None:
        log.info(f"[OM] {market_ticker} {side.upper()} PLACE @{price} qty={qty} DRY_RUN={dry_run}")
        WORKING[side] = WorkingOrder(side=side, price_cents=price, qty=qty, created_ts=now)
        _LAST_KEEP_LOGGED[side] = None

    def keep(side: str, cur: WorkingOrder, note: str) -> None:
        if _LAST_KEEP_LOGGED.get(side) != cur.price_cents:
            log.info(f"[OM] {market_ticker} {side.upper()} KEEP @{cur.price_cents} qty={cur.qty} ({note}) DRY_RUN={dry_run}")
            _LAST_KEEP_LOGGED[side] = cur.price_cents

    def unsafe_to_hold(side: str, cur: WorkingOrder) -> bool:
        # If we have a book snapshot, avoid being taker / crossed
        if yes_bid is None or yes_ask is None:
            return False
        if side == "sell":
            return int(cur.price_cents) <= int(yes_bid)
        if side == "buy":
            return int(cur.price_cents) >= int(yes_ask)
        return False

    both_missing = (target_bid is None) and (target_ask is None)

    # BOTH missing: global HOLD/CANCEL logic, but with "tight spread patience"
    if both_missing:
        # If we're skipping because spread is tight, do NOT insta-cancel.
        # Hold until EXIT_BAD_SECONDS has elapsed continuously in tight-spread state.
        is_tight_spread_skip = isinstance(skip_reason, str) and skip_reason.startswith("spread_too_tight")

        if is_tight_spread_skip:
            if TIGHT_SPREAD_SINCE_TS is None:
                TIGHT_SPREAD_SINCE_TS = now
            tight_for = now - TIGHT_SPREAD_SINCE_TS

            if HOLD_ON_SKIP and tight_for < EXIT_BAD_SECONDS:
                if WORKING["buy"] or WORKING["sell"]:
                    log.info(
                        "[OM] %s HOLD (tight_spread) held_for=%.2fs<%.2fs DRY_RUN=%s",
                        market_ticker, tight_for, EXIT_BAD_SECONDS, dry_run
                    )
                return

            # After EXIT_BAD_SECONDS tight, we do allow canceling (risk control)
            cancel("buy", f"tight_spread>{EXIT_BAD_SECONDS:.2f}s({skip_reason})")
            cancel("sell", f"tight_spread>{EXIT_BAD_SECONDS:.2f}s({skip_reason})")
            NO_TARGET_SINCE_TS = None
            return

        # Not a tight-spread skip: fall back to generic no-target hold/cancel
        TIGHT_SPREAD_SINCE_TS = None

        if NO_TARGET_SINCE_TS is None:
            NO_TARGET_SINCE_TS = now

        held_for = now - NO_TARGET_SINCE_TS

        if HOLD_ON_SKIP and held_for < CANCEL_IF_NO_TARGET_SECONDS:
            if WORKING["buy"] or WORKING["sell"]:
                log.info(
                    "[OM] %s HOLD (no_target) held_for=%.2fs<%.2fs DRY_RUN=%s",
                    market_ticker, held_for, CANCEL_IF_NO_TARGET_SECONDS, dry_run
                )
            return

        cancel("buy", f"no_target>{CANCEL_IF_NO_TARGET_SECONDS:.2f}s")
        cancel("sell", f"no_target>{CANCEL_IF_NO_TARGET_SECONDS:.2f}s")
        return

    # We have at least one target => reset "no-target" timers
    NO_TARGET_SINCE_TS = None
    TIGHT_SPREAD_SINCE_TS = None

    # per-side hold when a single side is missing
    if target_bid is not None:
        NO_TARGET_SIDE_SINCE_TS["buy"] = None
    if target_ask is not None:
        NO_TARGET_SIDE_SINCE_TS["sell"] = None

    def handle_missing_side(side: str) -> None:
        cur = WORKING[side]
        if cur is None:
            return

        if unsafe_to_hold(side, cur):
            cancel(side, "unsafe_hold(cross_risk)")
            NO_TARGET_SIDE_SINCE_TS[side] = None
            return

        if NO_TARGET_SIDE_SINCE_TS[side] is None:
            NO_TARGET_SIDE_SINCE_TS[side] = now

        held_for = now - float(NO_TARGET_SIDE_SINCE_TS[side] or now)

        if SIDE_HOLD_SECONDS > 0 and held_for < SIDE_HOLD_SECONDS:
            if (now - _LAST_SIDE_HOLD_LOG_TS.get(side, 0.0)) >= 5.0:
                log.info(
                    "[OM] %s %s HOLD_SIDE held_for=%.2fs<%.2fs price=@%d DRY_RUN=%s",
                    market_ticker, side.upper(), held_for, SIDE_HOLD_SECONDS, cur.price_cents, dry_run
                )
                _LAST_SIDE_HOLD_LOG_TS[side] = now
            return

        cancel(side, f"no_target_side>{SIDE_HOLD_SECONDS:.2f}s")
        NO_TARGET_SIDE_SINCE_TS[side] = None

    if target_bid is None:
        handle_missing_side("buy")
    if target_ask is None:
        handle_missing_side("sell")

    # maintain logic for sides that have targets
    def maintain(side: str, target_price: Optional[int]) -> None:
        if target_price is None:
            return

        cur = WORKING[side]
        if cur is None:
            place(side, int(target_price))
            return

        off = price_off(cur.price_cents, int(target_price))

        # Safety guard: if current order is now unsafe vs book snapshot, cancel it.
        if unsafe_to_hold(side, cur):
            cancel(side, "unsafe_hold(cross_risk)")
            place(side, int(target_price))
            return

        if off < REPRICE_IF_OFF_BY_CENTS:
            keep(side, cur, f"off_by={off}<thresh({REPRICE_IF_OFF_BY_CENTS})")
            return

        if not can_requote(cur):
            age = now - cur.created_ts
            keep(side, cur, f"cooldown age={age:.2f}s<{MIN_REQUOTE_SECONDS:.2f}s off_by={off}")
            return

        cancel(side, f"reprice(off_by={off})")
        place(side, int(target_price))

    maintain("buy", target_bid)
    maintain("sell", target_ask)

# -----------------------------
# Main loop
# -----------------------------
def main():
    log.info(
        "API_BASE=%s API_PREFIX=%s SERIES=%s EVENT_TICKER=%s MARKET_OVERRIDE=%s POLL=%.1fs DRY_RUN=%s ENABLE_TRADING=%s",
        API_BASE, API_PREFIX, SERIES,
        EVENT_TICKER or "<auto>", MARKET_TICKER_OVERRIDE or "<none>",
        POLL, DRY_RUN, ENABLE_TRADING,
    )
    log.info("QUOTE_PARAMS: TICK_CENTS=%d EDGE_CENTS=%d MIN_SPREAD_CENTS=%d", TICK_CENTS, EDGE_CENTS, MIN_SPREAD_CENTS)
    log.info("SAFETY_GATES: ENTER_OK_SECONDS=%.2f EXIT_BAD_SECONDS=%.2f", ENTER_OK_SECONDS, EXIT_BAD_SECONDS)
    log.info(
        "ORDER_PARAMS: ORDER_QTY=%d MIN_REQUOTE_SECONDS=%.2f REPRICE_IF_OFF_BY_CENTS=%d HOLD_ON_SKIP=%s CANCEL_IF_NO_TARGET_SECONDS=%.2f",
        ORDER_QTY, MIN_REQUOTE_SECONDS, REPRICE_IF_OFF_BY_CENTS, HOLD_ON_SKIP, CANCEL_IF_NO_TARGET_SECONDS,
    )
    log.info("ASK_CACHE: ASK_CACHE_TTL_SECONDS=%.2f", ASK_CACHE_TTL_SECONDS)
    log.info("MARKET_FALLBACK: MARKET_FALLBACK_MIN_SECONDS=%.2f", MARKET_FALLBACK_MIN_SECONDS)
    log.info("TIGHT_MODE: ENABLE_ONE_SIDED_TIGHT=%s", ENABLE_ONE_SIDED_TIGHT)
    log.info("TIGHT_JOIN: ENABLE_JOIN_TIGHT_SPREAD=%s", ENABLE_JOIN_TIGHT_SPREAD)
    log.info("SIDE_HOLD: SIDE_HOLD_SECONDS=%.2f", SIDE_HOLD_SECONDS)

    last_market: Optional[str] = None
    last_yes_bid: Optional[int] = None
    last_yes_ask: Optional[int] = None
    last_tb: Optional[int] = None
    last_ta: Optional[int] = None
    last_why: Optional[str] = None

    # Spread stability tracking
    spread_ok_since: Optional[float] = None

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
                global NO_TARGET_SINCE_TS, NO_TARGET_SIDE_SINCE_TS, TIGHT_SPREAD_SINCE_TS
                NO_TARGET_SINCE_TS = None
                NO_TARGET_SIDE_SINCE_TS = {"buy": None, "sell": None}
                TIGHT_SPREAD_SINCE_TS = None

                _YES_ASK_CACHE["ask"] = None
                _YES_ASK_CACHE["ts"] = 0.0

                global _LAST_MARKET_FALLBACK_TS
                _LAST_MARKET_FALLBACK_TS = 0.0

                spread_ok_since = None

                log.info("[OM] %s ROLL detected → cleared working orders (DRY_RUN=%s)", mkt, DRY_RUN)

            ob = fetch_orderbook(mkt)
            yes_bid, yes_ask = get_yes_bid_ask(ob, mkt)

            quote_changed = (yes_bid != last_yes_bid) or (yes_ask != last_yes_ask) or market_changed
            if quote_changed:
                last_yes_bid, last_yes_ask = yes_bid, yes_ask
                if yes_bid is None and yes_ask is None:
                    log.info("[QUOTE] %s YES bid=None ask=None → SKIP (empty)", mkt)
                else:
                    log.info("[QUOTE] %s YES bid=%s ask=%s", mkt, yes_bid, yes_ask)

            # Compute raw targets (spread gate lives here)
            tb, ta, why = compute_target_yes_quotes(yes_bid, yes_ask)

            # Add ENTER_OK_SECONDS stability requirement (prevents place->cancel churn)
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
                # If not currently eligible, reset stability timer unless we just lack data
                if isinstance(why, str) and why.startswith("spread_too_tight"):
                    spread_ok_since = None

            target_changed = (tb != last_tb) or (ta != last_ta) or (why != last_why) or market_changed
            if target_changed:
                last_tb, last_ta, last_why = tb, ta, why
                if tb is None and ta is None:
                    log.info("[TARGET] %s → SKIP (%s)", mkt, why)
                else:
                    log.info(
                        "[TARGET] %s YES-only would_quote: bid@%s ask@%s (%s) DRY_RUN=%s",
                        mkt,
                        str(tb) if tb is not None else "None",
                        str(ta) if ta is not None else "None",
                        why,
                        DRY_RUN,
                    )

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