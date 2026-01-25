# =========================
# bot.py  (PART 1/3)
# =========================
# Kalshi rolling 15m BTC market-maker (YES-side quoting with synthetic asks)
#
# -----------------------------
# NOTES / WHAT CHANGED (THREE FIXES)
# -----------------------------
# FIX #1 (CRITICAL): Reduce-only repricing is CANCEL-FIRST then PLACE, WITH "404 cancel" protection.
#   - When inventory != 0, we are in reduce-only mode (exit inventory).
#   - Previously, place-first-then-cancel could momentarily leave TWO exit orders live
#     (old exit + new exit). If both fill quickly, you can overshoot through zero and flip.
#   - Now: in reduce-only ONLY, we cancel the old exit order first, then place the new exit.
#     If cancel fails, we do NOT place a second exit. Safe > quoted.
#   - If cancel returns 404/not_found (order already gone), we assume it may have FILLED
#     and we ABORT the rest of this iteration until positions refresh.
#
# FIX #2 (CRITICAL): Startup reconciliation / state reset on deploy/restart.
#   - On startup, we reconcile against live exchange state (open orders + positions)
#     BEFORE we quote, so we don't act on stale local state after a redeploy.
#   - Optional: BOOTSTRAP_CANCEL_OPEN_ORDERS=true will cancel all resting YES orders
#     for the active market, then pause and refresh.
#
# FIX #3 (CRITICAL): Open-orders fetch uses correct status filter ("resting").
#   - Kalshi open-order listing is filtered by status=resting (not "open"), so
#     reconciliation sees the true live resting orders and doesn't thrash state.
#
# -----------------------------
# DISCLAIMER
# -----------------------------
# This is for educational purposes. Trading involves risk.

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
# Env / Config
# -----------------------------
load_dotenv()

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return int(str(v).strip())

def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return float(str(v).strip())

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

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")

# -----------------------------
# Core settings
# -----------------------------
DRY_RUN = env_bool("DRY_RUN", False)

KALSHI_API_BASE = getenv_first(["KALSHI_API_BASE"], "https://trading-api.kalshi.com")
# Common key env names seen in your project history
KALSHI_API_KEY_ID = getenv_first(["KALSHI_API_KEY_ID", "KALSHI_KEY_ID", "KALSHI_API_KEY"], "")
KALSHI_PRIVATE_KEY_B64 = getenv_first(["KALSHI_PRIVATE_KEY_B64", "KALSHI_PRIVATE_KEY"], "")

SERIES = os.getenv("SERIES", "KXBTC15M")  # rolling 15m BTC series
QUOTE_SIDE = os.getenv("QUOTE_SIDE", "YES").upper()  # this bot quotes YES-side
ORDER_QTY = env_int("ORDER_QTY", 1)

LOOP_SLEEP_SECONDS = env_float("LOOP_SLEEP_SECONDS", 0.25)
REQUEST_TIMEOUT = env_float("REQUEST_TIMEOUT", 10.0)

# Spread / targeting
MIN_SPREAD_TICKS = env_int("MIN_SPREAD_TICKS", 2)
MAX_PRICE = env_int("MAX_PRICE", 99)
MIN_PRICE = env_int("MIN_PRICE", 1)

# How far away from mid to quote (basic)
EDGE_TICKS = env_int("EDGE_TICKS", 1)

# Repricing thresholds
REPRICE_TICKS = env_int("REPRICE_TICKS", 2)

# Reduce-only repricing threshold (when inventory != 0)
REDUCE_ONLY_REPRICE_TICKS = env_int("REDUCE_ONLY_REPRICE_TICKS", 1)

# --- FIX #2: Startup reconciliation options
BOOTSTRAP_RECONCILE_ON_START = env_bool("BOOTSTRAP_RECONCILE_ON_START", True)
BOOTSTRAP_CANCEL_OPEN_ORDERS = env_bool("BOOTSTRAP_CANCEL_OPEN_ORDERS", False)

# Reconcile controls
RECENTLY_POSTED_GRACE_SECONDS = env_float("RECENTLY_POSTED_GRACE_SECONDS", 3.0)
CLEAN_STRAY_ORDERS = env_bool("CLEAN_STRAY_ORDERS", True)

# Safety / throttling
MAX_REQUESTS_PER_SECOND = env_float("MAX_REQUESTS_PER_SECOND", 8.0)
RATE_LIMIT_PAUSE_SECONDS = env_float("RATE_LIMIT_PAUSE_SECONDS", 1.0)

# Optional: interpret PnL cents logs (kept as-is)
PNL_VALUES_ARE_CENTS = env_bool("PNL_VALUES_ARE_CENTS", True)

# -----------------------------
# Utilities
# -----------------------------
def now_ts() -> float:
    return time.time()

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def clamp_price(px: int) -> int:
    return max(MIN_PRICE, min(MAX_PRICE, int(px)))

def tick_round(px: float) -> int:
    return int(round(px))

def is_rate_limited(exc: Exception) -> bool:
    s = str(exc).lower()
    return "429" in s or "rate limit" in s or "too many requests" in s

def is_not_found_cancel(exc: Exception) -> bool:
    s = str(exc).lower()
    return "404" in s or "not_found" in s or "not found" in s

# -----------------------------
# Kalshi client (RSA signing)
# -----------------------------
def load_private_key_from_b64(b64: str):
    raw = base64.b64decode(b64)
    return serialization.load_pem_private_key(raw, password=None)

@dataclass
class KalshiClient:
    base_url: str
    key_id: str
    private_key: Any

    def _sign(self, timestamp_ms: str, method: str, path: str, body: str) -> str:
        msg = f"{timestamp_ms}{method.upper()}{path}{body}".encode("utf-8")
        sig = self.private_key.sign(
            msg,
            asy_padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if params:
            path_full = f"{path}?{urlencode(params)}"
        else:
            path_full = path

        url = f"{self.base_url}{path_full}"
        body = "" if json_body is None else json.dumps(json_body, separators=(",", ":"))
        ts_ms = str(int(time.time() * 1000))
        sig = self._sign(ts_ms, method, path_full, body)

        headers = {
            "Content-Type": "application/json",
            "Kalshi-Access-Key": self.key_id,
            "Kalshi-Access-Signature": sig,
            "Kalshi-Access-Timestamp": ts_ms,
        }

        resp = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            data=None if json_body is None else body,
            timeout=REQUEST_TIMEOUT,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path_full}: {resp.text}")

        if resp.text.strip() == "":
            return {}
        return resp.json()

# -----------------------------
# API helpers
# -----------------------------
def get_active_market_for_series(client: KalshiClient, series: str) -> Optional[Dict[str, Any]]:
    # Uses /markets?series=XYZ
    data = client.request("GET", "/trade-api/v2/markets", params={"series_ticker": series, "limit": 200})
    markets = data.get("markets") or []
    # pick the one that is trading and not settled; newest end_time
    active = []
    for m in markets:
        if str(m.get("status", "")).lower() in ("settled", "finalized", "resolved"):
            continue
        if str(m.get("close_time", "")).strip() == "":
            # still ok; keep
            pass
        active.append(m)
    if not active:
        return None
    # Sort by close_time/end_time descending if present
    def _key(m):
        ct = m.get("close_time") or m.get("end_time") or ""
        return ct
    active.sort(key=_key, reverse=True)
    return active[0]

def get_orderbook(client: KalshiClient, market_ticker: str) -> Dict[str, Any]:
    return client.request("GET", f"/trade-api/v2/markets/{market_ticker}/orderbook")

def place_order(client: KalshiClient, market_ticker: str, side: str, price: int, qty: int) -> str:
    body = {
        "action": "buy" if side.upper() == "BUY" else "sell",
        "market_ticker": market_ticker,
        "side": "yes",
        "type": "limit",
        "price": int(price),
        "count": int(qty),
        "time_in_force": "GTC",
    }
    if DRY_RUN:
        return f"dry_{int(time.time()*1000)}"
    data = client.request("POST", "/trade-api/v2/portfolio/orders", json_body=body)
    oid = data.get("order", {}).get("order_id") or data.get("order_id") or data.get("id")
    if not oid:
        raise RuntimeError(f"place_order: missing order_id in response: {data}")
    return str(oid)

def cancel_order(client: KalshiClient, order_id: str) -> None:
    if DRY_RUN:
        return
    client.request("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}")

def get_positions(client: KalshiClient) -> Dict[str, Any]:
    return client.request("GET", "/trade-api/v2/portfolio/positions")

def get_open_orders(client: KalshiClient) -> List[Dict[str, Any]]:
    # --- FIX #3: status filter must be "resting"
    data = client.request("GET", "/trade-api/v2/portfolio/orders", params={"limit": 200, "status": "resting"})
    return data.get("orders") or []

# -----------------------------
# State
# -----------------------------
@dataclass
class QuoteState:
    market_ticker: str = ""
    bid_price: Optional[int] = None
    ask_price: Optional[int] = None
    bid_order_id: Optional[str] = None
    ask_order_id: Optional[str] = None

quote = QuoteState()

order_posted_ts: Dict[str, float] = {}   # order_id -> ts
pause_until_ts: float = 0.0

# Request throttle
_last_req_ts = 0.0
def note_successful_request():
    global _last_req_ts
    _last_req_ts = time.time()

def maybe_sleep_rate_limit():
    if MAX_REQUESTS_PER_SECOND <= 0:
        return
    global _last_req_ts
    # very simple pacing
    min_dt = 1.0 / MAX_REQUESTS_PER_SECOND
    dt = time.time() - _last_req_ts
    if dt < min_dt:
        time.sleep(min_dt - dt)

def arm_rate_limit_pause(tag: str):
    global pause_until_ts
    pause_until_ts = max(pause_until_ts, time.time() + RATE_LIMIT_PAUSE_SECONDS)
    log.warning(f"[RATE] pause due to rate limit ({tag}) for {RATE_LIMIT_PAUSE_SECONDS:.2f}s")

def in_pause() -> bool:
    return time.time() < pause_until_ts

# -----------------------------
# Helpers: identify YES orders for market
# -----------------------------
def is_yes_order_obj_for_market(o: Dict[str, Any], market_ticker: str) -> bool:
    return str(o.get("market_ticker")) == market_ticker and str(o.get("side", "")).lower() == "yes"

def order_action_for(o: Dict[str, Any]) -> str:
    return str(o.get("action", o.get("side_action", ""))).lower()

def order_is_buy(o: Dict[str, Any]) -> bool:
    return order_action_for(o) == "buy"

def order_is_sell(o: Dict[str, Any]) -> bool:
    return order_action_for(o) == "sell"

def get_order_id(o: Dict[str, Any]) -> Optional[str]:
    oid = o.get("order_id") or o.get("id")
    return str(oid) if oid else None

def get_remaining(o: Dict[str, Any]) -> int:
    # Kalshi fields vary; try a few
    for k in ("remaining_count", "remaining", "unfilled_count", "count_remaining"):
        if k in o and o[k] is not None:
            try:
                return int(o[k])
            except Exception:
                pass
    # fallback: count - filled_count
    c = o.get("count")
    f = o.get("filled_count")
    try:
        if c is not None and f is not None:
            return max(0, int(c) - int(f))
    except Exception:
        pass
    return 0

def get_price(o: Dict[str, Any]) -> Optional[int]:
    p = o.get("price")
    if p is None:
        return None
    try:
        return int(p)
    except Exception:
        return None

def get_market_position_yes(positions: Dict[str, Any], market_ticker: str) -> int:
    # Return net YES contracts (positive long YES, negative short YES)
    pos = positions.get("positions") or positions.get("portfolio_positions") or []
    for p in pos:
        if str(p.get("market_ticker")) == market_ticker:
            # fields can vary
            for k in ("position", "net_position", "quantity", "count"):
                if k in p and p[k] is not None:
                    try:
                        return int(p[k])
                    except Exception:
                        pass
    return 0
    # =========================
# bot.py  (PART 2/3)
# =========================

def reconcile_quote_state_with_open_orders(client: KalshiClient, tag: str) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
    """
    Reconcile local quote.bid_order_id/ask_order_id with live open orders list.
    Returns: (open_orders_cache or None, did_abort_iteration)
    """
    global pause_until_ts

    try:
        maybe_sleep_rate_limit()
        open_orders_cache = get_open_orders(client)
        note_successful_request()
    except Exception as e:
        if is_rate_limited(e):
            arm_rate_limit_pause("open_orders")
        log.warning(f"[OM] reconcile({tag}): failed to fetch open orders: {e}")
        return None, False

    active_market = quote.market_ticker
    if not active_market:
        return open_orders_cache, False

    open_yes_ids: set[str] = set()
    for o in open_orders_cache:
        if is_yes_order_obj_for_market(o, active_market):
            oid = get_order_id(o)
            if oid:
                open_yes_ids.add(oid)

    now = time.time()
    did_abort = False

    def _recent(oid: Optional[str]) -> bool:
        if not oid:
            return False
        ts = order_posted_ts.get(oid)
        if ts is None:
            return False
        return (now - ts) <= RECENTLY_POSTED_GRACE_SECONDS

    # If tracked order vanished from open list beyond grace, clear local + pause
    # (we don't know if it filled/canceled; safest is refresh positions)
    if quote.bid_order_id and quote.bid_order_id not in open_yes_ids and not _recent(quote.bid_order_id):
        log.warning(f"[OM] reconcile({tag}): bid_missing_openlist order_id={quote.bid_order_id} open_yes_ids={len(open_yes_ids)} -> CLEAR+PAUSE")
        quote.bid_order_id = None
        quote.bid_price = None
        pause_until_ts = max(pause_until_ts, time.time() + 0.75)
        did_abort = True

    if quote.ask_order_id and quote.ask_order_id not in open_yes_ids and not _recent(quote.ask_order_id):
        log.warning(f"[OM] reconcile({tag}): ask_missing_openlist order_id={quote.ask_order_id} open_yes_ids={len(open_yes_ids)} -> CLEAR+PAUSE")
        quote.ask_order_id = None
        quote.ask_price = None
        pause_until_ts = max(pause_until_ts, time.time() + 0.75)
        did_abort = True

    # Cancel stray YES orders (not tracked), if enabled
    if CLEAN_STRAY_ORDERS and active_market:
        tracked = set([oid for oid in [quote.bid_order_id, quote.ask_order_id] if oid])
        for o in open_orders_cache:
            if not is_yes_order_obj_for_market(o, active_market):
                continue
            oid = get_order_id(o)
            if not oid:
                continue
            if oid in tracked:
                continue
            try:
                if not DRY_RUN:
                    maybe_sleep_rate_limit()
                    cancel_order(client, oid)
                    note_successful_request()
                order_posted_ts.pop(oid, None)
                log.warning(f"[OM] reconcile({tag}): CANCEL stray YES order order_id={oid}")
            except Exception as ce:
                if is_rate_limited(ce):
                    arm_rate_limit_pause("reconcile_cancel_stray")
                log.warning(f"[OM] reconcile({tag}): failed to cancel stray order {oid}: {ce}")

    return open_orders_cache, did_abort


def cancel_bid_only(client: KalshiClient) -> bool:
    """
    Cancel bid only. Returns True if a cancel was executed or treated as handled.
    """
    if not quote.bid_order_id:
        return False
    oid = quote.bid_order_id
    try:
        if not DRY_RUN:
            maybe_sleep_rate_limit()
            cancel_order(client, oid)
            note_successful_request()
        log.info(f"[OM] BUY CANCEL order_id={oid}")
    except Exception as e:
        # --- FIX #1: 404/not_found means it is already gone; treat as handled
        if is_not_found_cancel(e):
            log.warning(f"[OM] BUY CANCEL order_id={oid} -> 404/not_found (already gone), treating as filled/gone")
        else:
            if is_rate_limited(e):
                arm_rate_limit_pause("cancel_bid")
            raise
    finally:
        order_posted_ts.pop(oid, None)
        quote.bid_order_id = None
        quote.bid_price = None
    return True


def cancel_ask_only(client: KalshiClient) -> bool:
    """
    Cancel ask only. Returns True if a cancel was executed or treated as handled.
    """
    if not quote.ask_order_id:
        return False
    oid = quote.ask_order_id
    try:
        if not DRY_RUN:
            maybe_sleep_rate_limit()
            cancel_order(client, oid)
            note_successful_request()
        log.info(f"[OM] SELL CANCEL order_id={oid}")
    except Exception as e:
        # --- FIX #1: 404/not_found means it is already gone; treat as handled
        if is_not_found_cancel(e):
            log.warning(f"[OM] SELL CANCEL order_id={oid} -> 404/not_found (already gone), treating as filled/gone")
        else:
            if is_rate_limited(e):
                arm_rate_limit_pause("cancel_ask")
            raise
    finally:
        order_posted_ts.pop(oid, None)
        quote.ask_order_id = None
        quote.ask_price = None
    return True


def cancel_old_then_place_new_reduce_only(client: KalshiClient, side: str, new_px: int, qty: int) -> bool:
    """
    Reduce-only repricing is cancel-first then place-new.
    Returns True if a new order was placed, False otherwise.
    If cancel returns 404/not_found, ABORT by returning False (caller should refresh positions before acting).
    """
    side = side.upper()
    if side == "BUY":
        old_oid = quote.bid_order_id
        if old_oid:
            try:
                if not DRY_RUN:
                    maybe_sleep_rate_limit()
                    cancel_order(client, old_oid)
                    note_successful_request()
                log.info(f"[OM] reduce-only BUY CANCEL-FIRST order_id={old_oid}")
            except Exception as e:
                if is_not_found_cancel(e):
                    log.warning(f"[OM] reduce-only BUY cancel-first got 404/not_found; assume filled/gone; ABORT iteration")
                    # clear local and abort (caller should refresh positions)
                    order_posted_ts.pop(old_oid, None)
                    quote.bid_order_id = None
                    quote.bid_price = None
                    return False
                if is_rate_limited(e):
                    arm_rate_limit_pause("reduce_only_cancel_buy")
                log.warning(f"[OM] reduce-only BUY cancel-first failed; NOT placing second exit: {e}")
                return False
            order_posted_ts.pop(old_oid, None)
            quote.bid_order_id = None
            quote.bid_price = None

        # place new
        oid = place_order(client, quote.market_ticker, "BUY", new_px, qty)
        order_posted_ts[oid] = time.time()
        quote.bid_order_id = oid
        quote.bid_price = new_px
        log.info(f"[OM] reduce-only BUY PLACE order_id={oid} @ {new_px} qty={qty} DRY_RUN={DRY_RUN}")
        return True

    else:  # SELL
        old_oid = quote.ask_order_id
        if old_oid:
            try:
                if not DRY_RUN:
                    maybe_sleep_rate_limit()
                    cancel_order(client, old_oid)
                    note_successful_request()
                log.info(f"[OM] reduce-only SELL CANCEL-FIRST order_id={old_oid}")
            except Exception as e:
                if is_not_found_cancel(e):
                    log.warning(f"[OM] reduce-only SELL cancel-first got 404/not_found; assume filled/gone; ABORT iteration")
                    order_posted_ts.pop(old_oid, None)
                    quote.ask_order_id = None
                    quote.ask_price = None
                    return False
                if is_rate_limited(e):
                    arm_rate_limit_pause("reduce_only_cancel_sell")
                log.warning(f"[OM] reduce-only SELL cancel-first failed; NOT placing second exit: {e}")
                return False
            order_posted_ts.pop(old_oid, None)
            quote.ask_order_id = None
            quote.ask_price = None

        oid = place_order(client, quote.market_ticker, "SELL", new_px, qty)
        order_posted_ts[oid] = time.time()
        quote.ask_order_id = oid
        quote.ask_price = new_px
        log.info(f"[OM] reduce-only SELL PLACE order_id={oid} @ {new_px} qty={qty} DRY_RUN={DRY_RUN}")
        return True


def compute_quotes_from_orderbook(ob: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], int]:
    """
    Returns (best_bid_yes, best_ask_yes, spread_ticks)
    """
    yes = ob.get("yes") or {}
    bids = yes.get("bids") or []
    asks = yes.get("asks") or []
    best_bid = int(bids[0][0]) if bids else None
    best_ask = int(asks[0][0]) if asks else None
    spread = 0
    if best_bid is not None and best_ask is not None:
        spread = int(best_ask) - int(best_bid)
    return best_bid, best_ask, spread
    # =========================
# bot.py  (PART 3/3)
# =========================

def bootstrap_reconcile_on_start(client: KalshiClient) -> None:
    """
    --- FIX #2: Startup reconciliation/state reset
    """
    if not BOOTSTRAP_RECONCILE_ON_START:
        return

    if not quote.market_ticker:
        return

    log.info(f"[BOOT] startup reconcile begin market={quote.market_ticker} cancel_open={BOOTSTRAP_CANCEL_OPEN_ORDERS}")

    # Fetch open orders
    open_orders = []
    try:
        maybe_sleep_rate_limit()
        open_orders = get_open_orders(client)
        note_successful_request()
    except Exception as e:
        if is_rate_limited(e):
            arm_rate_limit_pause("bootstrap_open_orders")
        log.warning(f"[BOOT] failed to fetch open orders: {e}")
        return

    # Optionally cancel all YES orders for this market
    if BOOTSTRAP_CANCEL_OPEN_ORDERS:
        for o in open_orders:
            if not is_yes_order_obj_for_market(o, quote.market_ticker):
                continue
            oid = get_order_id(o)
            if not oid:
                continue
            try:
                if not DRY_RUN:
                    maybe_sleep_rate_limit()
                    cancel_order(client, oid)
                    note_successful_request()
                log.warning(f"[BOOT] canceled resting YES order order_id={oid}")
            except Exception as ce:
                if is_not_found_cancel(ce):
                    log.warning(f"[BOOT] cancel order_id={oid} -> 404/not_found (already gone)")
                elif is_rate_limited(ce):
                    arm_rate_limit_pause("bootstrap_cancel")
                else:
                    log.warning(f"[BOOT] failed to cancel order {oid}: {ce}")

        # clear local quote state and pause a moment
        quote.bid_order_id = None
        quote.ask_order_id = None
        quote.bid_price = None
        quote.ask_price = None
        time.sleep(0.75)
        return

    # Otherwise, adopt live orders if present (best-effort)
    live_buys: List[Tuple[int, str]] = []
    live_sells: List[Tuple[int, str]] = []

    for o in open_orders:
        if not is_yes_order_obj_for_market(o, quote.market_ticker):
            continue
        oid = get_order_id(o)
        px = get_price(o)
        if not oid or px is None:
            continue
        if order_is_buy(o):
            live_buys.append((px, oid))
        elif order_is_sell(o):
            live_sells.append((px, oid))

    # pick the "most relevant" as the one closest to top-of-book later; for now:
    # - highest buy (best bid)
    # - lowest sell (best ask)
    if live_buys:
        live_buys.sort(key=lambda x: x[0], reverse=True)
        quote.bid_price, quote.bid_order_id = live_buys[0]
        order_posted_ts.setdefault(quote.bid_order_id, time.time())
        log.info(f"[BOOT] adopted live BUY order_id={quote.bid_order_id} @ {quote.bid_price}")
    else:
        quote.bid_price, quote.bid_order_id = None, None

    if live_sells:
        live_sells.sort(key=lambda x: x[0])
        quote.ask_price, quote.ask_order_id = live_sells[0]
        order_posted_ts.setdefault(quote.ask_order_id, time.time())
        log.info(f"[BOOT] adopted live SELL order_id={quote.ask_order_id} @ {quote.ask_price}")
    else:
        quote.ask_price, quote.ask_order_id = None, None

    # pause briefly to avoid immediate thrash and ensure position refresh next loop
    time.sleep(0.5)
    log.info("[BOOT] startup reconcile done")


def main():
    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY_B64:
        raise RuntimeError("Missing KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_B64")

    priv = load_private_key_from_b64(KALSHI_PRIVATE_KEY_B64)
    client = KalshiClient(base_url=KALSHI_API_BASE, key_id=KALSHI_API_KEY_ID, private_key=priv)

    # Find active market
    m = get_active_market_for_series(client, SERIES)
    if not m:
        raise RuntimeError(f"No active market found for series={SERIES}")

    quote.market_ticker = str(m.get("ticker") or m.get("market_ticker") or "")
    if not quote.market_ticker:
        raise RuntimeError(f"Active market missing ticker: {m}")

    log.info(f"[BOOT] Active market: {quote.market_ticker}")

    # --- FIX #2: reconcile/state reset on start
    bootstrap_reconcile_on_start(client)

    # Loop
    while True:
        try:
            if in_pause():
                time.sleep(LOOP_SLEEP_SECONDS)
                continue

            # reconcile local vs live open orders
            _, did_abort = reconcile_quote_state_with_open_orders(client, tag="loop")
            # --- FIX #1: abort the rest of this iteration if we detected a vanished tracked order
            if did_abort:
                time.sleep(LOOP_SLEEP_SECONDS)
                continue

            # Fetch positions
            maybe_sleep_rate_limit()
            pos = get_positions(client)
            note_successful_request()

            est_net_yes = get_market_position_yes(pos, quote.market_ticker)

            # Fetch orderbook
            maybe_sleep_rate_limit()
            ob = get_orderbook(client, quote.market_ticker)
            note_successful_request()

            best_bid, best_ask, spread = compute_quotes_from_orderbook(ob)

            if best_bid is None or best_ask is None:
                log.info("[TARGET] SKIP (empty_book)")
                time.sleep(LOOP_SLEEP_SECONDS)
                continue

            if spread < MIN_SPREAD_TICKS:
                log.info(f"[TARGET] {quote.market_ticker} -> SKIP (spread_too_tight({spread}))")
                time.sleep(LOOP_SLEEP_SECONDS)
                continue

            # Basic target pricing
            # Quote around the book with an edge
            bid_px = clamp_price(best_bid - EDGE_TICKS)
            ask_px = clamp_price(best_ask + EDGE_TICKS)

            reduce_only = (est_net_yes != 0)

            # In reduce-only, we only quote the exit side
            allow_bid = True
            allow_ask = True
            if reduce_only:
                if est_net_yes > 0:
                    # long YES -> need SELL to exit
                    allow_bid = False
                elif est_net_yes < 0:
                    # short YES -> need BUY to cover
                    allow_ask = False

            # Decide if we need to update bid/ask
            want_bid_update = False
            want_ask_update = False

            if allow_bid:
                if quote.bid_price is None:
                    want_bid_update = True
                else:
                    off_by = abs(bid_px - quote.bid_price)
                    threshold = REDUCE_ONLY_REPRICE_TICKS if reduce_only else REPRICE_TICKS
                    if off_by >= threshold:
                        want_bid_update = True

            if allow_ask:
                if quote.ask_price is None:
                    want_ask_update = True
                else:
                    off_by = abs(ask_px - quote.ask_price)
                    threshold = REDUCE_ONLY_REPRICE_TICKS if reduce_only else REPRICE_TICKS
                    if off_by >= threshold:
                        want_ask_update = True

            # If reduce-only, cancel/replace MUST be cancel-first then place
            if reduce_only:
                # If we are not allowed to quote that side, ensure we cancel any existing order
                if not allow_bid and quote.bid_order_id:
                    cancel_bid_only(client)
                if not allow_ask and quote.ask_order_id:
                    cancel_ask_only(client)

                # Place/replace only the allowed exit side
                if allow_bid and want_bid_update:
                    placed = cancel_old_then_place_new_reduce_only(client, "BUY", bid_px, ORDER_QTY)
                    # If cancel-first got 404/not_found, placed=False and we should refresh next loop
                    if not placed:
                        time.sleep(LOOP_SLEEP_SECONDS)
                        continue

                if allow_ask and want_ask_update:
                    placed = cancel_old_then_place_new_reduce_only(client, "SELL", ask_px, ORDER_QTY)
                    if not placed:
                        time.sleep(LOOP_SLEEP_SECONDS)
                        continue

            else:
                # Normal mode (non-reduce-only): do cancel-then-place per side if updating
                if want_bid_update:
                    if quote.bid_order_id:
                        cancel_bid_only(client)
                    oid = place_order(client, quote.market_ticker, "BUY", bid_px, ORDER_QTY)
                    order_posted_ts[oid] = time.time()
                    quote.bid_order_id = oid
                    quote.bid_price = bid_px
                    log.info(f"[OM] BUY PLACE @ {bid_px} qty={ORDER_QTY} DRY_RUN={DRY_RUN}")

                if want_ask_update:
                    if quote.ask_order_id:
                        cancel_ask_only(client)
                    oid = place_order(client, quote.market_ticker, "SELL", ask_px, ORDER_QTY)
                    order_posted_ts[oid] = time.time()
                    quote.ask_order_id = oid
                    quote.ask_price = ask_px
                    log.info(f"[OM] SELL PLACE @ {ask_px} qty={ORDER_QTY} DRY_RUN={DRY_RUN}")

                # If a side is disallowed for any reason, cancel it (safety)
                if not allow_bid and quote.bid_order_id:
                    cancel_bid_only(client)
                if not allow_ask and quote.ask_order_id:
                    cancel_ask_only(client)

            # Log quote summary
            log.info(f"[QUOTE] {quote.market_ticker} YES bid={quote.bid_price} ask={quote.ask_price} inv={est_net_yes} spread={spread}")

        except Exception as e:
            if is_rate_limited(e):
                arm_rate_limit_pause("loop_err")
            log.error(f"[LOOPERR] {repr(e)}")

        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()