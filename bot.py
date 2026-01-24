# bot.py
# Kalshi rolling 15m BTC market-maker (YES-side quoting with synthetic asks)
#
# FIXES in this version:
# 1) MAX_SPREAD_CENTS: if spread is too wide (default 30c), skip quoting entirely.
#    This prevents nonsense targets like bid@2 ask@98 when the book is basically empty.
# 2) Throttle [TARGET] logs so you don't spam every 0.20s (default 10s, env override).
# 3) IMPORTANT SAFETY: whenever we SKIP quoting (no bid/ask, spot-guard, spread too wide/tight, etc.),
#    we CANCEL any live quotes we previously posted for that market so we don't leave stale orders resting.
# 4) Keep an open-orders cache between polls so inventory/allow logic doesn't flip-flop on empty list.
#
# ADDITIONAL SAFETY PATCHES (this request):
# A) "insufficient_balance" circuit breaker: if SELL or BUY fails due to insufficient balance,
#    cancel the other side immediately and cool down for N seconds.
# B) Optional "atomic two-sided quoting": if enabled, do not leave one-sided exposure;
#    place ask then bid, and if either fails, cancel the other side.
#
# Notes:
# - Uses GET /markets?series_ticker=... to auto-roll
# - Uses GET /markets/{ticker}/orderbook
# - Kalshi orderbook is treated as "bids" for YES/NO; YES ask is synthesized from NO bid:
#     yes_ask ~= 100 - best_no_bid
# - POST /portfolio/orders, DELETE /portfolio/orders/{id}
# - RSA-PSS signing:
#     signature_message = f"{timestamp_ms}{method}{path_without_query}"
#
# IMPORTANT DISCLAIMER:
# This bot still does NOT compute true inventory from fills/positions; net_yes is static unless you add that endpoint.

import os
import time
import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

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
# Env helpers
# -----------------------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return int(v)


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return float(v)


def getenv_first(keys: List[str], default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def env_keys_with_prefix(prefix: str) -> List[str]:
    return sorted([k for k in os.environ.keys() if k.startswith(prefix)])


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------
# Config
# -----------------------------
API_BASE = getenv_first(["KALSHI_API_BASE"], "https://api.elections.kalshi.com").rstrip("/")
API_PREFIX = getenv_first(["KALSHI_API_PREFIX"], "/trade-api/v2").rstrip("/")
API_KEY_ID = getenv_first(["KALSHI_API_KEY_ID"], "")
PRIVATE_KEY_PEM_B64 = getenv_first(["KALSHI_PRIVATE_KEY_PEM_BASE64"], "")

SERIES_TICKER = getenv_first(["SERIES", "KALSHI_SERIES", "KALSHI_SERIES_TICKER"], "KXBTC15M")
EVENT_TICKER = getenv_first(["EVENT_TICKER", "KALSHI_EVENT_TICKER"], "<auto>")
MARKET_OVERRIDE = getenv_first(["MARKET_OVERRIDE", "KALSHI_MARKET_OVERRIDE"], "<none>")

POLL_SECONDS = env_float("POLL_SECONDS", 0.20)
META_REFRESH_SECONDS = env_float("META_REFRESH", 30.0)

DRY_RUN = env_bool("DRY_RUN", False)
ENABLE_TRADING = env_bool("ENABLE_TRADING", True)
POST_ONLY = env_bool("POST_ONLY", True)

# Quoting / microstructure
ORDER_QTY = env_int("ORDER_QTY", 1)
IMPROVE_CENTS = env_int("IMPROVE_CENTS", 1)
NO_IMPROVE_MAX_SPREAD_CENTS = env_int("NO_IMPROVE_MAX_SPREAD_CENTS", 4)  # <= spread: join, don't improve
MIN_SPREAD_CENTS = env_int("MIN_SPREAD_CENTS", 2)  # if spread < this, skip
MAX_SPREAD_CENTS = env_int("MAX_SPREAD_CENTS", 30)  # if spread > this, skip (prevents 2/98 nonsense)

# Log throttles
TARGET_LOG_THROTTLE_SECONDS = env_float("TARGET_LOG_THROTTLE_SECONDS", 10.0)
SPOT_SKIP_LOG_THROTTLE_SECONDS = env_float("SPOT_SKIP_LOG_THROTTLE_SECONDS", 2.0)

# Inventory control (simple)
MAX_NET_YES_CONTRACTS = env_int("MAX_NET_YES_CONTRACTS", 2)
INVENTORY_SKEW_CENTS = env_int("INVENTORY_SKEW_CENTS", 1)
INITIAL_NET_YES_CONTRACTS = env_int("INITIAL_NET_YES_CONTRACTS", 0)

# Order-state safety
ORDER_STATUS_POLL_SECONDS = env_float("ORDER_STATUS_POLL_SECONDS", 1.0)
PAUSE_ON_UNKNOWN_SECONDS = env_float("PAUSE_ON_UNKNOWN_SECONDS", 0.75)

# Balance / collateral safety (ADDED)
BALANCE_FAIL_COOLDOWN_SECONDS = env_float("BALANCE_FAIL_COOLDOWN_SECONDS", 10.0)
BALANCE_FAIL_MAX_BURST = env_int("BALANCE_FAIL_MAX_BURST", 3)
REQUIRE_TWO_SIDED_QUOTES = env_bool("REQUIRE_TWO_SIDED_QUOTES", True)

# Spot guard
ENABLE_SPOT_GUARD = env_bool("ENABLE_SPOT_GUARD", True)
SPOT_POLL_SECONDS = env_float("SPOT_POLL_SECONDS", 12.0)
SPOT_RESOLVED_BUFFER_USD = env_float("SPOT_RESOLVED_BUFFER_USD", 75.0)
CLOSEOUT_SECONDS = env_float("CLOSEOUT_SECONDS", 20.0)
SPOT_GUARD_NEAR_CLOSE_SECONDS = env_float("SPOT_GUARD_NEAR_CLOSE_SECONDS", 90.0)

# Coinbase spot endpoint
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"


# -----------------------------
# Kalshi API client (RSA-PSS signing)
# -----------------------------
class KalshiClient:
    def __init__(self, api_base: str, api_prefix: str, key_id: str, private_key_pem_b64: str):
        self.api_base = api_base.rstrip("/")
        self.api_prefix = api_prefix if api_prefix.startswith("/") else f"/{api_prefix}"
        self.key_id = key_id

        if not private_key_pem_b64:
            raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_BASE64")

        pem_bytes = base64.b64decode(private_key_pem_b64)
        self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)

        self.session = requests.Session()

    def _sign_headers(self, method: str, full_url: str) -> Dict[str, str]:
        """
        signature_message = f"{timestamp_ms}{method}{path_without_query}"
        IMPORTANT: path must EXCLUDE query string.
        """
        ts = str(now_ms())
        parsed = urlparse(full_url)
        path = parsed.path  # excludes query
        msg = f"{ts}{method.upper()}{path}".encode("utf-8")

        sig = self.private_key.sign(
            msg,
            asy_padding.PSS(
                mgf=asy_padding.MGF1(hashes.SHA256()),
                salt_length=asy_padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(sig).decode("utf-8")

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Any:
        if not path.startswith("/"):
            path = "/" + path

        url = f"{self.api_base}{self.api_prefix}{path}"

        if params:
            url_with_q = url + "?" + urlencode(params)
        else:
            url_with_q = url

        headers = self._sign_headers(method, url)
        headers["Accept"] = "application/json"
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        resp = self.session.request(
            method=method.upper(),
            url=url_with_q,
            headers=headers,
            json=json_body,
            timeout=timeout,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path}: {resp.text}")

        if resp.content:
            return resp.json()
        return None


# -----------------------------
# Market selection / parsing
# -----------------------------
def pick_active_market(markets: List[Dict[str, Any]]) -> Tuple[str, str, Dict[str, Any]]:
    now_ts = int(time.time())

    def get_ts(obj: Dict[str, Any], key: str) -> Optional[int]:
        v = obj.get(key)
        if v is None:
            return None
        try:
            return int(v)
        except Exception:
            return None

    candidates = []
    for m in markets:
        status = str(m.get("status", "")).lower()
        if status and status != "open":
            continue
        ot = get_ts(m, "open_time") or get_ts(m, "open_ts") or get_ts(m, "open_timestamp")
        ct = get_ts(m, "close_time") or get_ts(m, "close_ts") or get_ts(m, "close_timestamp")
        candidates.append((ot, ct, m))

    active = []
    future = []
    for ot, ct, m in candidates:
        if ot is not None and ct is not None and ot <= now_ts < ct:
            active.append((ct, m))
        elif ct is not None and ct > now_ts:
            future.append((ct, m))

    if active:
        active.sort(key=lambda x: x[0])
        chosen = active[0][1]
    elif future:
        future.sort(key=lambda x: x[0])
        chosen = future[0][1]
    else:
        chosen = markets[0] if markets else {}
        if not chosen:
            raise RuntimeError("No markets available to pick from.")

    market_ticker = chosen.get("ticker") or chosen.get("market_ticker")
    event_ticker = chosen.get("event_ticker") or chosen.get("event", {}).get("ticker") or chosen.get("event_ticker")

    if not market_ticker or not event_ticker:
        raise RuntimeError(f"Could not determine event/market ticker from market object: {chosen}")

    return str(event_ticker), str(market_ticker), chosen


def parse_orderbook_yes_bid_ask(ob: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Treats 'yes' and 'no' arrays as bid books.
    YES ask is synthesized from NO bid: yes_ask = 100 - best_no_bid.
    IMPORTANT: do NOT assume levels[0] is best; scan for the max bid.
    """
    orderbook = ob.get("orderbook") or ob
    yes_levels = orderbook.get("yes") or []
    no_levels = orderbook.get("no") or []

    def max_bid(levels: Any) -> Optional[int]:
        if not isinstance(levels, list) or not levels:
            return None

        best: Optional[int] = None
        for lvl in levels:
            price = None

            # Common format: [price, qty]
            if isinstance(lvl, list) and len(lvl) >= 1:
                price = lvl[0]
            # Sometimes: {"price": x, "quantity": y}
            elif isinstance(lvl, dict):
                price = lvl.get("price") or lvl.get("yes_price") or lvl.get("no_price")
            # Rare: just a number
            elif isinstance(lvl, (int, float)):
                price = lvl

            try:
                p = int(price)
            except Exception:
                continue

            if best is None or p > best:
                best = p

        return best

    yes_bid = max_bid(yes_levels)
    no_bid = max_bid(no_levels)
    yes_ask = (100 - no_bid) if no_bid is not None else None

    # sanity clamp
    if yes_bid is not None:
        yes_bid = max(1, min(99, yes_bid))
    if yes_ask is not None:
        yes_ask = max(1, min(99, yes_ask))

    return yes_bid, yes_ask


# -----------------------------
# Spot guard helpers
# -----------------------------
def fetch_btc_spot_usd(session: requests.Session, timeout: float = 5.0) -> Optional[float]:
    try:
        r = session.get(COINBASE_SPOT_URL, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        amt = data.get("data", {}).get("amount")
        if amt is None:
            return None
        return float(amt)
    except Exception:
        return None


def market_bounds_usd(market_obj: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    lo = None
    hi = None

    for lo_key in ("floor_strike", "lower_strike", "strike_lower", "floor"):
        if lo_key in market_obj:
            try:
                lo = float(market_obj[lo_key])
                break
            except Exception:
                lo = None

    for hi_key in ("cap_strike", "upper_strike", "strike_upper", "cap"):
        if hi_key in market_obj:
            try:
                hi = float(market_obj[hi_key])
                break
            except Exception:
                hi = None

    return lo, hi


def extract_close_ts(market_obj: Dict[str, Any]) -> Optional[int]:
    for k in ("close_time", "close_ts", "close_timestamp"):
        if k in market_obj:
            try:
                return int(market_obj[k])
            except Exception:
                pass
    return None


# -----------------------------
# Order management
# -----------------------------
@dataclass
class QuoteState:
    bid_price: Optional[int] = None
    ask_price: Optional[int] = None
    bid_order_id: Optional[str] = None
    ask_order_id: Optional[str] = None
    last_market_ticker: Optional[str] = None


def build_yes_order_payload(
    market_ticker: str,
    action: str,   # "buy" or "sell"
    price_cents: int,
    count: int,
    post_only: bool,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "ticker": market_ticker,
        "action": action,
        "side": "yes",
        "type": "limit",
        "count": int(count),
        "yes_price": int(price_cents),
    }
    if post_only:
        body["post_only"] = True
    return body


def safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


def get_open_orders(client: KalshiClient) -> List[Dict[str, Any]]:
    resp = client.request("GET", "/portfolio/orders", params={"status": "open", "limit": 200})
    return resp.get("orders", resp if isinstance(resp, list) else [])


def cancel_order(client: KalshiClient, order_id: str) -> None:
    # 404 is normal if the order already filled/canceled/expired between our polls.
    # Treat 404 as success to avoid noisy warnings.
    try:
        client.request("DELETE", f"/portfolio/orders/{order_id}")
    except RuntimeError as e:
        msg = str(e)
        if ("HTTP 404" in msg) or ('"code":"not_found"' in msg) or ('"code": "not_found"' in msg):
            return
        raise


def place_order(client: KalshiClient, payload: Dict[str, Any]) -> str:
    resp = client.request("POST", "/portfolio/orders", json_body=payload)
    if isinstance(resp, dict):
        if "order" in resp and isinstance(resp["order"], dict) and resp["order"].get("order_id"):
            return str(resp["order"]["order_id"])
        if resp.get("order_id"):
            return str(resp["order_id"])
    raise RuntimeError(f"Unexpected create order response: {resp}")


# -----------------------------
# Quoting logic
# -----------------------------
def compute_quotes(
    yes_bid: int,
    yes_ask: int,
    net_yes: int,
) -> Tuple[Optional[int], Optional[int], str]:
    spread = yes_ask - yes_bid

    if spread < MIN_SPREAD_CENTS:
        return None, None, f"spread_too_tight({spread})"

    if spread > MAX_SPREAD_CENTS:
        return None, None, f"spread_too_wide({spread}>{MAX_SPREAD_CENTS})"

    if yes_bid >= yes_ask:
        return None, None, "crossed_or_invalid"

    if spread <= NO_IMPROVE_MAX_SPREAD_CENTS:
        bid = yes_bid
        ask = yes_ask
        why = "join_tight_spread"
    else:
        bid = min(yes_bid + IMPROVE_CENTS, yes_ask - 1)
        ask = max(yes_ask - IMPROVE_CENTS, yes_bid + 1)
        why = "improve"

    if net_yes != 0:
        skew = INVENTORY_SKEW_CENTS * abs(net_yes)
        if net_yes > 0:
            bid -= skew
            ask -= skew
            why += f" skew_long_yes({-abs(net_yes)})"
        else:
            bid += skew
            ask += skew
            why += f" skew_short_yes({abs(net_yes)})"

    bid = max(1, min(99, bid))
    ask = max(1, min(99, ask))
    if bid >= ask:
        return None, None, "bid_ge_ask_after_skew"
    return bid, ask, why


def count_open_yes_orders(open_orders: List[Dict[str, Any]], market_ticker: str) -> Tuple[int, int]:
    buys = 0
    sells = 0
    for o in open_orders:
        if str(o.get("ticker")) != market_ticker:
            continue
        if str(o.get("side", "")).lower() != "yes":
            continue
        action = str(o.get("action", "")).lower()
        if action == "buy":
            buys += safe_int(o.get("remaining_count") or o.get("count") or 0) or 0
        elif action == "sell":
            sells += safe_int(o.get("remaining_count") or o.get("count") or 0) or 0
    return buys, sells


# -----------------------------
# Main loop
# -----------------------------
def main() -> None:
    log.info(f"[ENV] Detected KALSHI_* keys: {env_keys_with_prefix('KALSHI_')}")
    log.info(
        "API_BASE=%s API_PREFIX=%s SERIES=%s EVENT_TICKER=%s MARKET_OVERRIDE=%s POLL=%.2fs DRY_RUN=%s ENABLE_TRADING=%s POST_ONLY=%s",
        API_BASE,
        API_PREFIX,
        SERIES_TICKER,
        EVENT_TICKER,
        MARKET_OVERRIDE if MARKET_OVERRIDE != "" else "<none>",
        POLL_SECONDS,
        DRY_RUN,
        ENABLE_TRADING,
        POST_ONLY,
    )
    log.info("[INV] MAX_NET_YES_CONTRACTS=%d INVENTORY_SKEW_CENTS=%d ORDER_STATUS_POLL_SECONDS=%.2f PAUSE_ON_UNKNOWN_SECONDS=%.2f",
             MAX_NET_YES_CONTRACTS, INVENTORY_SKEW_CENTS, ORDER_STATUS_POLL_SECONDS, PAUSE_ON_UNKNOWN_SECONDS)
    log.info("[MICRO] NO_IMPROVE_MAX_SPREAD_CENTS=%d MIN_SPREAD_CENTS=%d MAX_SPREAD_CENTS=%d IMPROVE_CENTS=%d",
             NO_IMPROVE_MAX_SPREAD_CENTS, MIN_SPREAD_CENTS, MAX_SPREAD_CENTS, IMPROVE_CENTS)
    log.info("[LOG] TARGET_LOG_THROTTLE_SECONDS=%.1f SPOT_SKIP_LOG_THROTTLE_SECONDS=%.1f",
             TARGET_LOG_THROTTLE_SECONDS, SPOT_SKIP_LOG_THROTTLE_SECONDS)
    log.info("[SPOT] ENABLE_SPOT_GUARD=%s SPOT_POLL_SECONDS=%.1f CLOSEOUT_SECONDS=%.1f SPOT_RESOLVED_BUFFER_USD=%.1f META_REFRESH=%.1f SPOT_GUARD_NEAR_CLOSE_SECONDS=%.1f",
             ENABLE_SPOT_GUARD, SPOT_POLL_SECONDS, CLOSEOUT_SECONDS, SPOT_RESOLVED_BUFFER_USD, META_REFRESH_SECONDS, SPOT_GUARD_NEAR_CLOSE_SECONDS)
    log.info("[BAL] REQUIRE_TWO_SIDED_QUOTES=%s BALANCE_FAIL_COOLDOWN_SECONDS=%.1f BALANCE_FAIL_MAX_BURST=%d",
             REQUIRE_TWO_SIDED_QUOTES, BALANCE_FAIL_COOLDOWN_SECONDS, BALANCE_FAIL_MAX_BURST)

    if not API_KEY_ID or not PRIVATE_KEY_PEM_B64:
        raise RuntimeError("Missing KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_PEM_BASE64")

    client = KalshiClient(API_BASE, API_PREFIX, API_KEY_ID, PRIVATE_KEY_PEM_B64)
    http = requests.Session()

    quote = QuoteState()
    net_yes = INITIAL_NET_YES_CONTRACTS

    last_meta_refresh = 0.0
    last_spot_poll = 0.0
    spot_usd: Optional[float] = None

    active_event = None
    active_market = None
    active_market_obj: Dict[str, Any] = {}

    last_order_poll = 0.0
    pause_until = 0.0

    # caches
    open_orders_cache: List[Dict[str, Any]] = []

    # Throttles
    last_spot_skip_log_at = 0.0
    last_spot_skip_msg = ""

    last_target_log_at = 0.0
    last_target_sig: Tuple[Any, ...] = tuple()

    # Balance fail circuit breaker (ADDED)
    balance_fail_burst = 0
    balance_fail_until = 0.0

    def cancel_live_quotes(reason: str) -> None:
        """Cancel any currently tracked live bid/ask orders (if any) and clear local quote state."""
        nonlocal quote
        if not active_market:
            return

        cancelled_any = False

        if quote.bid_order_id:
            try:
                if not DRY_RUN:
                    cancel_order(client, quote.bid_order_id)
                log.info(f"[OM] {active_market} BUY CANCEL ({reason}) order_id={quote.bid_order_id}")
            except Exception as e:
                log.warning(f"[OM] cancel bid failed ({reason}): {e}")
            quote.bid_order_id = None
            quote.bid_price = None
            cancelled_any = True

        if quote.ask_order_id:
            try:
                if not DRY_RUN:
                    cancel_order(client, quote.ask_order_id)
                log.info(f"[OM] {active_market} SELL CANCEL ({reason}) order_id={quote.ask_order_id}")
            except Exception as e:
                log.warning(f"[OM] cancel ask failed ({reason}): {e}")
            quote.ask_order_id = None
            quote.ask_price = None
            cancelled_any = True

        if cancelled_any:
            # keep last_market_ticker so we still know what market we were on
            pass

    # Helpers (ADDED)
    def is_insufficient_balance(e: Exception) -> bool:
        s = str(e)
        return ("insufficient_balance" in s) or ('"code":"insufficient_balance"' in s) or ('"code": "insufficient_balance"' in s)

    def cancel_bid_only(reason: str) -> None:
        nonlocal quote
        if quote.bid_order_id:
            try:
                if not DRY_RUN:
                    cancel_order(client, quote.bid_order_id)
                log.info(f"[OM] {active_market} BUY CANCEL ({reason}) order_id={quote.bid_order_id}")
            except Exception as e:
                log.warning(f"[OM] cancel bid failed ({reason}): {e}")
            quote.bid_order_id = None
            quote.bid_price = None

    def cancel_ask_only(reason: str) -> None:
        nonlocal quote
        if quote.ask_order_id:
            try:
                if not DRY_RUN:
                    cancel_order(client, quote.ask_order_id)
                log.info(f"[OM] {active_market} SELL CANCEL ({reason}) order_id={quote.ask_order_id}")
            except Exception as e:
                log.warning(f"[OM] cancel ask failed ({reason}): {e}")
            quote.ask_order_id = None
            quote.ask_price = None

    def refresh_active_market() -> None:
        nonlocal active_event, active_market, active_market_obj

        if MARKET_OVERRIDE and MARKET_OVERRIDE not in ("<none>", "none", "None", ""):
            active_market = MARKET_OVERRIDE
            active_event = EVENT_TICKER if EVENT_TICKER != "<auto>" else "<manual>"
            active_market_obj = {}
            log.info(f"[ROLL] Series={SERIES_TICKER} → Market override={active_market}")
            return

        params = {
            "series_ticker": SERIES_TICKER,
            "status": "open",
            "limit": 200,
            "mve_filter": "exclude",
        }

        resp = client.request("GET", "/markets", params=params)
        markets = resp.get("markets", [])
        if not markets:
            raise RuntimeError(f"No open markets returned for series_ticker={SERIES_TICKER}")

        event_t, market_t, mobj = pick_active_market(markets)
        active_event = event_t
        active_market = market_t
        active_market_obj = mobj

        log.info(f"[ROLL] Series={SERIES_TICKER} → Active event={active_event} market={active_market} (via /markets series_ticker)")

    refresh_active_market()
    last_meta_refresh = time.time()

    while True:
        t0 = time.time()

        if (t0 - last_meta_refresh) >= META_REFRESH_SECONDS:
            try:
                refresh_active_market()
            except Exception as e:
                log.warning(f"[ROLL] refresh failed: {e}")
            last_meta_refresh = t0

        if not active_market:
            time.sleep(POLL_SECONDS)
            continue

        if ENABLE_SPOT_GUARD and (t0 - last_spot_poll) >= SPOT_POLL_SECONDS:
            spot_usd = fetch_btc_spot_usd(http)
            last_spot_poll = t0

        close_ts = extract_close_ts(active_market_obj)
        secs_to_close: Optional[int] = None
        if close_ts is not None:
            secs_to_close = close_ts - int(time.time())

        # Closeout hard-stop: cancel everything right before close
        if secs_to_close is not None and secs_to_close <= CLOSEOUT_SECONDS:
            cancel_live_quotes("closeout")
            time.sleep(POLL_SECONDS)
            continue

        # Spot guard near close: also cancel (don’t leave quotes up)
        if ENABLE_SPOT_GUARD and spot_usd is not None and secs_to_close is not None and secs_to_close <= SPOT_GUARD_NEAR_CLOSE_SECONDS:
            lo, hi = market_bounds_usd(active_market_obj)

            if lo is not None and abs(spot_usd - lo) <= SPOT_RESOLVED_BUFFER_USD:
                cancel_live_quotes("spot_guard_floor")
                msg = f"[SPOT] skip near close: spot {spot_usd:.2f} within {SPOT_RESOLVED_BUFFER_USD:.1f} of floor {lo:.2f} (t_close={secs_to_close}s)"
                if (t0 - last_spot_skip_log_at) >= SPOT_SKIP_LOG_THROTTLE_SECONDS or msg != last_spot_skip_msg:
                    log.info(msg)
                    last_spot_skip_log_at = t0
                    last_spot_skip_msg = msg
                time.sleep(POLL_SECONDS)
                continue

            if hi is not None and abs(spot_usd - hi) <= SPOT_RESOLVED_BUFFER_USD:
                cancel_live_quotes("spot_guard_cap")
                msg = f"[SPOT] skip near close: spot {spot_usd:.2f} within {SPOT_RESOLVED_BUFFER_USD:.1f} of cap {hi:.2f} (t_close={secs_to_close}s)"
                if (t0 - last_spot_skip_log_at) >= SPOT_SKIP_LOG_THROTTLE_SECONDS or msg != last_spot_skip_msg:
                    log.info(msg)
                    last_spot_skip_log_at = t0
                    last_spot_skip_msg = msg
                time.sleep(POLL_SECONDS)
                continue

        # Poll open orders (cache persists between polls)
        if (t0 - last_order_poll) >= ORDER_STATUS_POLL_SECONDS:
            try:
                open_orders_cache = get_open_orders(client)
                last_order_poll = t0
            except Exception as e:
                pause_until = time.time() + PAUSE_ON_UNKNOWN_SECONDS
                cancel_live_quotes("unknown_order_state_pause")
                log.warning(f"[INV] PAUSE quoting due to unknown order state: {PAUSE_ON_UNKNOWN_SECONDS:.2f}s remaining ({e})")
                time.sleep(POLL_SECONDS)
                continue

        if time.time() < pause_until:
            time.sleep(POLL_SECONDS)
            continue

        # Balance cooldown gate (ADDED)
        if time.time() < balance_fail_until:
            time.sleep(POLL_SECONDS)
            continue

        # Orderbook
        try:
            ob = client.request("GET", f"/markets/{active_market}/orderbook")
        except Exception as e:
            log.warning(f"[OB] {active_market} orderbook fetch failed: {e}")
            time.sleep(POLL_SECONDS)
            continue

        yes_bid, yes_ask = parse_orderbook_yes_bid_ask(ob)
        if yes_bid is None or yes_ask is None:
            cancel_live_quotes("no_yes_bid_or_ask")
            sig = (active_market, "SKIP", "no_yes_bid_or_ask")
            if sig != last_target_sig or (t0 - last_target_log_at) >= TARGET_LOG_THROTTLE_SECONDS:
                log.info(f"[TARGET] {active_market} → SKIP (no_yes_bid_or_ask)")
                last_target_sig = sig
                last_target_log_at = t0
            time.sleep(POLL_SECONDS)
            continue

        open_buys, open_sells = count_open_yes_orders(open_orders_cache, active_market)
        est_net_yes = net_yes

        bid_px, ask_px, why = compute_quotes(yes_bid, yes_ask, est_net_yes)
        if bid_px is None or ask_px is None:
            cancel_live_quotes(f"skip:{why}")
            sig = (active_market, "SKIP", why, yes_bid, yes_ask)
            if sig != last_target_sig or (t0 - last_target_log_at) >= TARGET_LOG_THROTTLE_SECONDS:
                log.info(f"[TARGET] {active_market} → SKIP ({why}) best_yes_bid={yes_bid} best_yes_ask={yes_ask}")
                last_target_sig = sig
                last_target_log_at = t0
            time.sleep(POLL_SECONDS)
            continue

        allow_bid = (est_net_yes + open_buys) < MAX_NET_YES_CONTRACTS
        allow_ask = (est_net_yes - open_sells) > -MAX_NET_YES_CONTRACTS

        # If inventory says "no", pull that side’s quote off the book
        if not allow_bid and quote.bid_order_id:
            cancel_live_quotes("inventory_block_bid")  # cancels both, intentionally conservative
        if not allow_ask and quote.ask_order_id:
            cancel_live_quotes("inventory_block_ask")  # cancels both, intentionally conservative

        # Market roll: cancel anything from prior market
        if quote.last_market_ticker and quote.last_market_ticker != active_market:
            cancel_live_quotes("market_roll")
            quote = QuoteState()

        quote.last_market_ticker = active_market

        # -----------------------------
        # ATOMIC TWO-SIDED QUOTING (ADDED / REPLACED BID+ASK BLOCK)
        # -----------------------------

        # If we require two-sided quotes, only quote when BOTH sides are allowed.
        if REQUIRE_TWO_SIDED_QUOTES and (not allow_bid or not allow_ask):
            # If either side is blocked, cancel both so we don't drift into one-sided exposure.
            if quote.bid_order_id or quote.ask_order_id:
                cancel_live_quotes("two_sided_required")
            time.sleep(POLL_SECONDS)
            continue

        want_bid_update = allow_bid and (quote.bid_price != bid_px)
        want_ask_update = allow_ask and (quote.ask_price != ask_px)

        if want_bid_update or want_ask_update:
            # Cancel only the side(s) we intend to replace
            if want_bid_update and quote.bid_order_id:
                try:
                    if not DRY_RUN:
                        cancel_order(client, quote.bid_order_id)
                    log.info(f"[OM] {active_market} BUY CANCEL @{quote.bid_price} (reprice) order_id={quote.bid_order_id}")
                except Exception as e:
                    log.warning(f"[OM] cancel bid failed: {e}")
                quote.bid_order_id = None
                quote.bid_price = None

            if want_ask_update and quote.ask_order_id:
                try:
                    if not DRY_RUN:
                        cancel_order(client, quote.ask_order_id)
                    log.info(f"[OM] {active_market} SELL CANCEL @{quote.ask_price} (reprice) order_id={quote.ask_order_id}")
                except Exception as e:
                    log.warning(f"[OM] cancel ask failed: {e}")
                quote.ask_order_id = None
                quote.ask_price = None

            placed_ask = False
            placed_bid = False

            # ASK FIRST
            if allow_ask and quote.ask_order_id is None and want_ask_update:
                if ENABLE_TRADING and not DRY_RUN:
                    try:
                        payload = build_yes_order_payload(active_market, "sell", ask_px, ORDER_QTY, POST_ONLY)
                        oid = place_order(client, payload)
                        quote.ask_order_id = oid
                        quote.ask_price = ask_px
                        placed_ask = True
                        log.info(f"[OM] {active_market} SELL POSTED order_id={oid} @ {ask_px} qty={ORDER_QTY}")
                    except Exception as e:
                        # If we can't place the ASK, do NOT place the BID (avoid one-sided)
                        if is_insufficient_balance(e):
                            balance_fail_burst += 1
                            balance_fail_until = time.time() + BALANCE_FAIL_COOLDOWN_SECONDS
                            cancel_bid_only("ask_insufficient_balance")
                            cancel_ask_only("ask_insufficient_balance")
                            log.warning(f"[OM] {active_market} SELL place failed: {e} (cooldown {BALANCE_FAIL_COOLDOWN_SECONDS}s)")
                            time.sleep(POLL_SECONDS)
                            continue
                        else:
                            log.warning(f"[OM] {active_market} SELL place failed: {e}")
                else:
                    quote.ask_price = ask_px
                    placed_ask = True
                    log.info(f"[OM] {active_market} SELL PLACE @ {ask_px} qty={ORDER_QTY} DRY_RUN={DRY_RUN}")

            # BID SECOND
            if allow_bid and quote.bid_order_id is None and want_bid_update:
                if ENABLE_TRADING and not DRY_RUN:
                    try:
                        payload = build_yes_order_payload(active_market, "buy", bid_px, ORDER_QTY, POST_ONLY)
                        oid = place_order(client, payload)
                        quote.bid_order_id = oid
                        quote.bid_price = bid_px
                        placed_bid = True
                        log.info(f"[OM] {active_market} BUY POSTED order_id={oid} @ {bid_px} qty={ORDER_QTY}")
                    except Exception as e:
                        # If bid fails and we just placed ask, cancel ask so we don't become one-sided
                        if placed_ask:
                            cancel_ask_only("bid_failed_atomic")
                        if is_insufficient_balance(e):
                            balance_fail_burst += 1
                            balance_fail_until = time.time() + BALANCE_FAIL_COOLDOWN_SECONDS
                            log.warning(f"[OM] {active_market} BUY place failed: {e} (cooldown {BALANCE_FAIL_COOLDOWN_SECONDS}s)")
                            time.sleep(POLL_SECONDS)
                            continue
                        else:
                            log.warning(f"[OM] {active_market} BUY place failed: {e}")
                else:
                    quote.bid_price = bid_px
                    placed_bid = True
                    log.info(f"[OM] {active_market} BUY PLACE @ {bid_px} qty={ORDER_QTY} DRY_RUN={DRY_RUN}")

            # If we require two-sided and only one is live, cancel it.
            if REQUIRE_TWO_SIDED_QUOTES:
                has_bid = quote.bid_order_id is not None
                has_ask = quote.ask_order_id is not None
                if has_bid != has_ask:
                    cancel_live_quotes("atomic_two_sided_enforce")

        # THROTTLED TARGET LOG
        sig = (active_market, bid_px, ask_px, why)
        if sig != last_target_sig or (t0 - last_target_log_at) >= TARGET_LOG_THROTTLE_SECONDS:
            log.info(f"[TARGET] {active_market} → would_quote: bid@{bid_px} ask@{ask_px} ({why})")
            last_target_sig = sig
            last_target_log_at = t0

        dt = time.time() - t0
        time.sleep(max(0.0, POLL_SECONDS - dt))


if __name__ == "__main__":
    main()