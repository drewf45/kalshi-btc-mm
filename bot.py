# bot.py
# Kalshi rolling 15m BTC market-maker (YES-side quoting with synthetic asks)
#
# Key features:
# - Auto-rolls to the active market in a series via GET /markets?series_ticker=...
# - Reads orderbook via GET /markets/{ticker}/orderbook (Kalshi provides bids only)
#   and synthesizes YES ask from NO bid: yes_ask = 100 - best_no_bid
# - Places POST-ONLY limit orders via POST /portfolio/orders
# - Cancels/reprices when targets move
# - Inventory skew + max net inventory guard (simple, uses local + open orders only)
# - Spot guard near resolution bounds (optional; uses Coinbase spot)
# - Correct Kalshi request signing (RSA-PSS) per docs:
#   signature_message = f"{timestamp_ms}{method}{path_without_query}"

import os
import time
import base64
import json
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
MIN_SPREAD_CENTS = env_int("MIN_SPREAD_CENTS", 2)  # if spread < this, skip (or will be invalid)

# Inventory control (simple)
MAX_NET_YES_CONTRACTS = env_int("MAX_NET_YES_CONTRACTS", 2)
INVENTORY_SKEW_CENTS = env_int("INVENTORY_SKEW_CENTS", 1)
INITIAL_NET_YES_CONTRACTS = env_int("INITIAL_NET_YES_CONTRACTS", 0)

# Order-state safety
ORDER_STATUS_POLL_SECONDS = env_float("ORDER_STATUS_POLL_SECONDS", 1.0)
PAUSE_ON_UNKNOWN_SECONDS = env_float("PAUSE_ON_UNKNOWN_SECONDS", 0.75)

# Spot guard
ENABLE_SPOT_GUARD = env_bool("ENABLE_SPOT_GUARD", True)
SPOT_POLL_SECONDS = env_float("SPOT_POLL_SECONDS", 12.0)
SPOT_RESOLVED_BUFFER_USD = env_float("SPOT_RESOLVED_BUFFER_USD", 75.0)
CLOSEOUT_SECONDS = env_float("CLOSEOUT_SECONDS", 20.0)

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
        Per Kalshi docs:
          signature_message = f"{timestamp_ms}{method}{path_without_query}"
          headers:
            KALSHI-ACCESS-KEY
            KALSHI-ACCESS-SIGNATURE (base64)
            KALSHI-ACCESS-TIMESTAMP (ms)
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
            # Query is appended to URL, but signature must be based on path WITHOUT query.
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
    """
    Choose the market that is currently active:
    - Prefer open_time <= now < close_time
    - Else pick soonest close_time in the future
    Returns (event_ticker, market_ticker, market_obj)
    """
    now_ts = int(time.time())

    def get_ts(obj: Dict[str, Any], key: str) -> Optional[int]:
        v = obj.get(key)
        if v is None:
            return None
        # docs typically use unix seconds
        try:
            return int(v)
        except Exception:
            return None

    candidates = []
    for m in markets:
        status = m.get("status", "").lower()
        if status and status != "open":
            continue
        ot = get_ts(m, "open_time") or get_ts(m, "open_ts") or get_ts(m, "open_timestamp")
        ct = get_ts(m, "close_time") or get_ts(m, "close_ts") or get_ts(m, "close_timestamp")
        # if timestamps missing, still allow, but sort last
        candidates.append((ot, ct, m))

    # Active first
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
        # fallback: first
        chosen = markets[0] if markets else {}
        if not chosen:
            raise RuntimeError("No markets available to pick from.")

    market_ticker = chosen.get("ticker") or chosen.get("market_ticker")
    event_ticker = chosen.get("event_ticker") or chosen.get("event", {}).get("ticker") or chosen.get("event_ticker")

    if not market_ticker or not event_ticker:
        raise RuntimeError(f"Could not determine event/market ticker from market object: {chosen}")

    return event_ticker, market_ticker, chosen


def parse_orderbook_yes_bid_ask(ob: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Kalshi orderbook endpoint returns BIDS ONLY for YES and NO:
      orderbook: { yes: [[price, qty?], ...], no: [[price, qty?], ...] }

    Synthetic asks:
      YES ask = 100 - best NO bid
      NO ask  = 100 - best YES bid

    We quote YES side, so return:
      yes_best_bid, yes_best_ask
    """
    orderbook = ob.get("orderbook") or ob
    yes_bids = orderbook.get("yes") or []
    no_bids = orderbook.get("no") or []

    def best_price(levels: Any) -> Optional[int]:
        if not levels or not isinstance(levels, list):
            return None
        top = levels[0]
        if isinstance(top, list) and len(top) >= 1:
            try:
                return int(top[0])
            except Exception:
                return None
        if isinstance(top, (int, float)):
            return int(top)
        return None

    yes_bid = best_price(yes_bids)
    no_bid = best_price(no_bids)
    yes_ask = (100 - no_bid) if no_bid is not None else None

    return yes_bid, yes_ask


# -----------------------------
# Spot guard
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
    """
    Try common keys Kalshi uses for range markets.
    We only use this if present; otherwise spot guard becomes a no-op.
    """
    for lo_key in ("floor_strike", "lower_strike", "strike_lower", "floor"):
        if lo_key in market_obj:
            try:
                lo = float(market_obj[lo_key])
                break
            except Exception:
                lo = None
    else:
        lo = None

    for hi_key in ("cap_strike", "upper_strike", "strike_upper", "cap"):
        if hi_key in market_obj:
            try:
                hi = float(market_obj[hi_key])
                break
            except Exception:
                hi = None
    else:
        hi = None

    return lo, hi


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
    """
    Create order body per Kalshi v2:
      ticker, action, side, type, count, yes_price/no_price, (post_only)
    We trade YES side only -> side="yes" and yes_price=<cents>
    """
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
    # docs usually return {"orders":[...], ...}
    return resp.get("orders", resp if isinstance(resp, list) else [])


def cancel_order(client: KalshiClient, order_id: str) -> None:
    client.request("DELETE", f"/portfolio/orders/{order_id}")


def place_order(client: KalshiClient, payload: Dict[str, Any]) -> str:
    resp = client.request("POST", "/portfolio/orders", json_body=payload)
    # docs commonly return {"order": {"order_id": "..."}}
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
    """
    Returns (bid_px, ask_px, reason)
    - Improve by 1c only if spread > NO_IMPROVE_MAX_SPREAD_CENTS, else join.
    - Apply inventory skew: long YES -> shift both down; short -> shift both up.
    """
    spread = yes_ask - yes_bid
    if spread < MIN_SPREAD_CENTS:
        return None, None, f"spread_too_tight({spread})"
    if yes_bid >= yes_ask:
        return None, None, "crossed_or_invalid"

    # base quote
    if spread <= NO_IMPROVE_MAX_SPREAD_CENTS:
        bid = yes_bid
        ask = yes_ask
        why = "join_tight_spread"
    else:
        bid = min(yes_bid + IMPROVE_CENTS, yes_ask - 1)
        ask = max(yes_ask - IMPROVE_CENTS, yes_bid + 1)
        why = "improve"

    # inventory skew
    if net_yes != 0:
        skew = INVENTORY_SKEW_CENTS * abs(net_yes)
        if net_yes > 0:
            # long YES: make buying less aggressive, selling more aggressive
            bid -= skew
            ask -= skew
            why += f" skew_long_yes({-abs(net_yes)})"
        else:
            # short YES: make buying more aggressive, selling less aggressive
            bid += skew
            ask += skew
            why += f" skew_short_yes({abs(net_yes)})"

    # clamp bounds
    bid = max(1, min(99, bid))
    ask = max(1, min(99, ask))
    if bid >= ask:
        return None, None, "bid_ge_ask_after_skew"
    return bid, ask, why


def count_open_yes_orders(open_orders: List[Dict[str, Any]], market_ticker: str) -> Tuple[int, int]:
    """
    Count open YES buys/sells in this market (to help avoid stacking).
    Returns (open_buys, open_sells)
    """
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
    log.info("[MICRO] NO_IMPROVE_MAX_SPREAD_CENTS=%d (<= this spread: join, do not improve)", NO_IMPROVE_MAX_SPREAD_CENTS)
    log.info("[SPOT] ENABLE_SPOT_GUARD=%s SPOT_POLL_SECONDS=%.1f CLOSEOUT_SECONDS=%.1f SPOT_RESOLVED_BUFFER_USD=%.1f META_REFRESH=%.1f",
             ENABLE_SPOT_GUARD, SPOT_POLL_SECONDS, CLOSEOUT_SECONDS, SPOT_RESOLVED_BUFFER_USD, META_REFRESH_SECONDS)

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
            "mve_filter": "exclude",  # avoid multivariate markets hijacking series searches
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

    # Initial roll
    refresh_active_market()
    last_meta_refresh = time.time()

    while True:
        t0 = time.time()

        # Refresh market selection periodically
        if (t0 - last_meta_refresh) >= META_REFRESH_SECONDS:
            try:
                refresh_active_market()
            except Exception as e:
                log.warning(f"[ROLL] refresh failed: {e}")
            last_meta_refresh = t0

        if not active_market:
            time.sleep(POLL_SECONDS)
            continue

        # Spot poll (optional)
        if ENABLE_SPOT_GUARD and (t0 - last_spot_poll) >= SPOT_POLL_SECONDS:
            spot_usd = fetch_btc_spot_usd(http)
            last_spot_poll = t0

        # Closeout guard near close time (if we have timestamps)
        close_ts = None
        for k in ("close_time", "close_ts", "close_timestamp"):
            if k in active_market_obj:
                try:
                    close_ts = int(active_market_obj[k])
                    break
                except Exception:
                    pass
        if close_ts is not None:
            secs_to_close = close_ts - int(time.time())
            if secs_to_close <= CLOSEOUT_SECONDS:
                # Cancel our working orders and wait for roll
                if quote.bid_order_id:
                    try:
                        if not DRY_RUN:
                            cancel_order(client, quote.bid_order_id)
                        log.info(f"[OM] {active_market} BUY CANCEL (closeout) order_id={quote.bid_order_id}")
                    except Exception as e:
                        log.warning(f"[OM] cancel bid failed: {e}")
                    quote.bid_order_id = None
                if quote.ask_order_id:
                    try:
                        if not DRY_RUN:
                            cancel_order(client, quote.ask_order_id)
                        log.info(f"[OM] {active_market} SELL CANCEL (closeout) order_id={quote.ask_order_id}")
                    except Exception as e:
                        log.warning(f"[OM] cancel ask failed: {e}")
                    quote.ask_order_id = None

                time.sleep(POLL_SECONDS)
                continue

        # Spot resolved-buffer guard (only if we can read bounds)
        if ENABLE_SPOT_GUARD and spot_usd is not None:
            lo, hi = market_bounds_usd(active_market_obj)
            if lo is not None and abs(spot_usd - lo) <= SPOT_RESOLVED_BUFFER_USD:
                log.info(f"[SPOT] skip: spot {spot_usd:.2f} within {SPOT_RESOLVED_BUFFER_USD:.1f} of floor {lo:.2f}")
                time.sleep(POLL_SECONDS)
                continue
            if hi is not None and abs(spot_usd - hi) <= SPOT_RESOLVED_BUFFER_USD:
                log.info(f"[SPOT] skip: spot {spot_usd:.2f} within {SPOT_RESOLVED_BUFFER_USD:.1f} of cap {hi:.2f}")
                time.sleep(POLL_SECONDS)
                continue

        # Poll open orders for safety / inventory-ish context
        open_orders: List[Dict[str, Any]] = []
        if (t0 - last_order_poll) >= ORDER_STATUS_POLL_SECONDS:
            try:
                open_orders = get_open_orders(client)
                last_order_poll = t0
            except Exception as e:
                # If we can't confirm state, pause quoting
                pause_until = time.time() + PAUSE_ON_UNKNOWN_SECONDS
                log.warning(f"[INV] PAUSE quoting due to unknown order state: {PAUSE_ON_UNKNOWN_SECONDS:.2f}s remaining ({e})")
                time.sleep(POLL_SECONDS)
                continue

        if time.time() < pause_until:
            time.sleep(POLL_SECONDS)
            continue

        # Read orderbook
        try:
            ob = client.request("GET", f"/markets/{active_market}/orderbook")
        except Exception as e:
            log.warning(f"[OB] {active_market} orderbook fetch failed: {e}")
            time.sleep(POLL_SECONDS)
            continue

        yes_bid, yes_ask = parse_orderbook_yes_bid_ask(ob)

        if yes_bid is None or yes_ask is None:
            log.info(f"[TARGET] {active_market} → SKIP (no_yes_bid_or_ask)")
            time.sleep(POLL_SECONDS)
            continue

        # Simple inventory cap: if we're already max long, stop bidding; if max short, stop asking.
        open_buys, open_sells = count_open_yes_orders(open_orders, active_market)
        est_net_yes = net_yes  # local estimate; you can wire in real positions later
        # also consider open orders as "pending" exposure
        est_pending_long = open_buys
        est_pending_short = open_sells

        bid_px, ask_px, why = compute_quotes(yes_bid, yes_ask, est_net_yes)
        if bid_px is None or ask_px is None:
            log.info(f"[TARGET] {active_market} → SKIP ({why})")
            time.sleep(POLL_SECONDS)
            continue

        # Inventory hard stops
        allow_bid = (est_net_yes + est_pending_long) < MAX_NET_YES_CONTRACTS
        allow_ask = (est_net_yes - est_pending_short) > -MAX_NET_YES_CONTRACTS

        log.debug(f"[QUOTE] {active_market} YES bid={yes_bid} ask={yes_ask} → target bid@{bid_px} ask@{ask_px} ({why})")

        # If market changed, clear state (and try to cancel lingering orders)
        if quote.last_market_ticker and quote.last_market_ticker != active_market:
            # best-effort cancels
            if quote.bid_order_id:
                try:
                    if not DRY_RUN:
                        cancel_order(client, quote.bid_order_id)
                    log.info(f"[OM] {quote.last_market_ticker} BUY CANCEL (market_roll) order_id={quote.bid_order_id}")
                except Exception:
                    pass
            if quote.ask_order_id:
                try:
                    if not DRY_RUN:
                        cancel_order(client, quote.ask_order_id)
                    log.info(f"[OM] {quote.last_market_ticker} SELL CANCEL (market_roll) order_id={quote.ask_order_id}")
                except Exception:
                    pass
            quote = QuoteState()

        quote.last_market_ticker = active_market

        # Reprice / (re)place BID
        if allow_bid:
            if quote.bid_price != bid_px:
                # cancel old
                if quote.bid_order_id:
                    try:
                        if not DRY_RUN:
                            cancel_order(client, quote.bid_order_id)
                        log.info(f"[OM] {active_market} BUY CANCEL @{quote.bid_price} (reprice) order_id={quote.bid_order_id}")
                    except Exception as e:
                        log.warning(f"[OM] cancel bid failed: {e}")
                    quote.bid_order_id = None

                # place new
                if ENABLE_TRADING and not DRY_RUN:
                    try:
                        payload = build_yes_order_payload(active_market, "buy", bid_px, ORDER_QTY, POST_ONLY)
                        oid = place_order(client, payload)
                        quote.bid_order_id = oid
                        quote.bid_price = bid_px
                        log.info(f"[OM] {active_market} BUY POSTED order_id={oid} @ {bid_px} qty={ORDER_QTY}")
                    except Exception as e:
                        log.warning(f"[OM] {active_market} BUY place failed: {e}")
                else:
                    quote.bid_price = bid_px
                    log.info(f"[OM] {active_market} BUY PLACE @ {bid_px} qty={ORDER_QTY} DRY_RUN={DRY_RUN}")
        else:
            # not allowed to bid: ensure no bid order working
            if quote.bid_order_id:
                try:
                    if not DRY_RUN:
                        cancel_order(client, quote.bid_order_id)
                    log.info(f"[OM] {active_market} BUY CANCEL (inv_cap) order_id={quote.bid_order_id}")
                except Exception as e:
                    log.warning(f"[OM] cancel bid failed: {e}")
                quote.bid_order_id = None
                quote.bid_price = None

        # Reprice / (re)place ASK
        if allow_ask:
            if quote.ask_price != ask_px:
                # cancel old
                if quote.ask_order_id:
                    try:
                        if not DRY_RUN:
                            cancel_order(client, quote.ask_order_id)
                        log.info(f"[OM] {active_market} SELL CANCEL @{quote.ask_price} (reprice) order_id={quote.ask_order_id}")
                    except Exception as e:
                        log.warning(f"[OM] cancel ask failed: {e}")
                    quote.ask_order_id = None

                # place new
                if ENABLE_TRADING and not DRY_RUN:
                    try:
                        payload = build_yes_order_payload(active_market, "sell", ask_px, ORDER_QTY, POST_ONLY)
                        oid = place_order(client, payload)
                        quote.ask_order_id = oid
                        quote.ask_price = ask_px
                        log.info(f"[OM] {active_market} SELL POSTED order_id={oid} @ {ask_px} qty={ORDER_QTY}")
                    except Exception as e:
                        log.warning(f"[OM] {active_market} SELL place failed: {e}")
                else:
                    quote.ask_price = ask_px
                    log.info(f"[OM] {active_market} SELL PLACE @ {ask_px} qty={ORDER_QTY} DRY_RUN={DRY_RUN}")
        else:
            # not allowed to ask: ensure no ask order working
            if quote.ask_order_id:
                try:
                    if not DRY_RUN:
                        cancel_order(client, quote.ask_order_id)
                    log.info(f"[OM] {active_market} SELL CANCEL (inv_cap) order_id={quote.ask_order_id}")
                except Exception as e:
                    log.warning(f"[OM] cancel ask failed: {e}")
                quote.ask_order_id = None
                quote.ask_price = None

        # Target log (matches your style)
        log.info(f"[TARGET] {active_market} → would_quote: bid@{bid_px} ask@{ask_px} ({why})")

        # sleep
        dt = time.time() - t0
        time.sleep(max(0.0, POLL_SECONDS - dt))


if __name__ == "__main__":
    main()