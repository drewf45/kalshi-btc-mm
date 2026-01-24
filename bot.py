# bot.py
# Kalshi rolling 15m BTC market-maker (YES-side quoting with synthetic asks)
#
# =============================
# NOTES / WHAT CHANGED (ONLY THESE CHANGES)
# =============================
# FIX #1 (CRITICAL): Reduce-only repricing is now CANCEL-FIRST then PLACE,
#                    WITH "404 cancel" protection to prevent flip-through-zero.
#   - When inventory != 0, we are in reduce-only mode (exit inventory).
#   - Previously, place-first-then-cancel could momentarily leave TWO exit orders live
#     (old exit + new exit). If both fill quickly, you can overshoot through zero and flip.
#   - Now: in reduce-only ONLY, we cancel the old exit order first, then place the new exit.
#     If cancel fails, we do NOT place a second exit. Safe > quoted.
#   - Additional safety: if cancel returns 404/not_found (order already gone),
#     we assume it may have FILLED and PAUSE until positions refresh before placing a new exit.
#
# FIX #2 (CRITICAL): Inventory freshness guard (prevents acting on stale inventory/order state).
#   - We track how recently we refreshed positions.
#   - If positions are too old/unknown, we PAUSE quoting and force-refresh.
#
# FIX #3 (REQUESTED): PnL logging scaling/labeling.
#   - Any realized/unrealized/fees values we track internally are treated as CENTS.
#   - Logs now print both: dollars (scaled) and raw cents for clarity.
#
# FIX #4 (REQUESTED): Startup reconciliation/state reset.
#   - On boot (and after deploy/restart), we reconcile from live exchange state:
#     fetch open orders + position for the active market and align local state.
#   - Optional: cancel stray open orders on startup (default ON) so we never “double quote”
#     due to local state loss across deploys.
#
# -----------------------------
# DISCLAIMER
# -----------------------------
# This is a trading bot. Run at your own risk. Start in DRY_RUN=True.
#
# -----------------------------
# Dependencies
# -----------------------------
# pip install requests python-dotenv cryptography
#
# -----------------------------
# Env vars (existing + minimal additions)
# -----------------------------
# Required:
#   KALSHI_API_KEY                 (or KALSHI_ACCESS_KEY)
#   KALSHI_PRIVATE_KEY_B64         (base64-encoded PEM)  OR  KALSHI_PRIVATE_KEY_PATH
#
# Recommended:
#   KALSHI_API_BASE                default: https://trading-api.kalshi.com
#   SERIES_TICKER                  default: KXBTC15M
#
# New (for FIX #4):
#   STARTUP_CANCEL_OPEN_ORDERS     default: true
#
# Everything else has sane defaults below.

import os
import json
import time
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


def getenv_first(keys: List[str], default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def parse_bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def parse_int(v: str, default: int) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


def parse_float(v: str, default: float) -> float:
    try:
        return float(str(v).strip())
    except Exception:
        return default


# Core auth
API_KEY = getenv_first(["KALSHI_API_KEY", "KALSHI_ACCESS_KEY"], "")
PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
API_BASE = os.getenv("KALSHI_API_BASE", "https://trading-api.kalshi.com").rstrip("/")

# Market selection
SERIES_TICKER = os.getenv("SERIES_TICKER", "KXBTC15M").strip()
# For BTC 15m series, you often want a specific strike market like "...-15" (YES contract).
MARKET_SUFFIX = os.getenv("MARKET_SUFFIX", "-15").strip()  # appended to event ticker

# Quoting behavior
ORDER_QTY = parse_int(os.getenv("ORDER_QTY", "1"), 1)
MAX_QTY_PER_SIDE = parse_int(os.getenv("MAX_QTY_PER_SIDE", "1"), 1)

MIN_SPREAD_CENTS = parse_int(os.getenv("MIN_SPREAD_CENTS", "2"), 2)
# If spread is 1, optionally join tight spread on exit/when allowed (your prior logic used this)
ENABLE_JOIN_TIGHT_SPREAD = parse_bool(os.getenv("ENABLE_JOIN_TIGHT_SPREAD", "true"), True)

# Timing / reconciliation
LOOP_SLEEP_SECONDS = parse_float(os.getenv("LOOP_SLEEP_SECONDS", "0.35"), 0.35)
VISIBILITY_GRACE_SECONDS = parse_float(os.getenv("VISIBILITY_GRACE_SECONDS", "3.0"), 3.0)

# FIX #2: inventory freshness guard
POSITION_REFRESH_MAX_AGE_SECONDS = parse_float(
    os.getenv("POSITION_REFRESH_MAX_AGE_SECONDS", "1.25"),
    1.25,
)

# FIX #4: startup reconcile
STARTUP_CANCEL_OPEN_ORDERS = parse_bool(os.getenv("STARTUP_CANCEL_OPEN_ORDERS", "true"), True)

# Safety
DRY_RUN = parse_bool(os.getenv("DRY_RUN", "true"), True)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Helpers
# -----------------------------
def now_ts() -> float:
    return time.time()


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dollars_from_cents(cents: Optional[int]) -> str:
    if cents is None:
        return "n/a"
    return f"${(cents / 100.0):.2f}"


def clamp_price_cents(px: int) -> int:
    # Kalshi prices typically 1..99 (cents)
    return max(1, min(99, int(px)))


def method_upper(m: str) -> str:
    return m.upper().strip()


# -----------------------------
# Kalshi Auth + HTTP
# -----------------------------
class KalshiClient:
    def __init__(self, api_key: str, private_key_pem: bytes, base_url: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self._priv = serialization.load_pem_private_key(private_key_pem, password=None)

    @staticmethod
    def _ts_ms_str() -> str:
        # Kalshi expects a timestamp string; many examples use milliseconds
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        msg = (ts + method + path + body).encode("utf-8")
        sig = self._priv.sign(msg, asy_padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode("utf-8")

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Any = None) -> Any:
        method = method_upper(method)
        p = path if path.startswith("/") else "/" + path

        url = self.base_url + p
        if params:
            url += "?" + urlencode(params, doseq=True)

        body_str = "" if json_body is None else json.dumps(json_body, separators=(",", ":"))
        ts = self._ts_ms_str()
        sig = self._sign(ts, method, p, body_str)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }

        if DRY_RUN and method in ("POST", "DELETE", "PUT", "PATCH"):
            log.info(f"[DRYRUN] {method} {p} params={params} body={body_str}")
            return {"dry_run": True}

        r = self.session.request(method, url, headers=headers, data=(None if body_str == "" else body_str), timeout=20)
        if r.status_code >= 400:
            try:
                j = r.json()
            except Exception:
                j = {"raw": r.text}
            raise RuntimeError(f"HTTP {r.status_code} {p}: {j}")
        if r.text.strip() == "":
            return {}
        return r.json()


def load_private_key_pem() -> bytes:
    if PRIVATE_KEY_B64.strip():
        return base64.b64decode(PRIVATE_KEY_B64.strip())
    if PRIVATE_KEY_PATH.strip():
        with open(PRIVATE_KEY_PATH.strip(), "rb") as f:
            return f.read()
    raise RuntimeError("Missing private key: set KALSHI_PRIVATE_KEY_B64 or KALSHI_PRIVATE_KEY_PATH")


# -----------------------------
# State
# -----------------------------
@dataclass
class QuoteOrder:
    order_id: str
    side: str  # "buy" or "sell"
    price: int
    qty: int
    posted_ts: float
    visible: bool = False


@dataclass
class LocalState:
    market_ticker: str = ""
    event_ticker: str = ""
    pos_yes: int = 0
    last_pos_refresh_ts: float = 0.0

    bid: Optional[QuoteOrder] = None
    ask: Optional[QuoteOrder] = None

    realized_cents: int = 0     # FIX #3: stored in cents
    unreal_cents: Optional[int] = None
    fees_cents: int = 0         # FIX #3: stored in cents

    pause_until_ts: float = 0.0

    def paused(self) -> bool:
        return now_ts() < self.pause_until_ts


# -----------------------------
# Kalshi API wrappers (v2 trade-api)
# -----------------------------
def api_get_markets_by_series(client: KalshiClient, series_ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
    # The bot’s earlier fix: /markets?series=XYZ rather than non-existent /series endpoint
    out = []
    cursor = None
    while True:
        params = {"series_ticker": series_ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        j = client.request("GET", "/trade-api/v2/markets", params=params)
        items = j.get("markets", []) or j.get("data", []) or []
        out.extend(items)
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def pick_active_event_and_market(markets: List[Dict[str, Any]], market_suffix: str) -> Tuple[str, str]:
    """
    Returns (event_ticker, market_ticker).
    Heuristic: choose the market with the latest close/expiration that is still open.
    Then build the specific market ticker using event_ticker + suffix if needed.
    """
    # Many Kalshi market payloads include:
    #  - ticker: market ticker (e.g. KXBTC15M-26JAN241815-15)
    #  - event_ticker: KXBTC15M-26JAN241815
    #  - close_time / expiration_time / status
    open_markets = []
    for m in markets:
        status = (m.get("status") or "").lower()
        if status and status not in ("open", "active", "trading"):
            continue
        # fallback: if status missing, still consider
        open_markets.append(m)

    if not open_markets:
        # fallback: just take latest by close_time among all
        open_markets = markets[:]

    def parse_time(m: Dict[str, Any]) -> float:
        # robust parsing; if missing, put 0
        t = m.get("close_time") or m.get("expiration_time") or m.get("end_time")
        if not t:
            return 0.0
        try:
            # often ISO8601
            dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0.0

    chosen = max(open_markets, key=parse_time)
    mt = str(chosen.get("ticker") or "").strip()
    et = str(chosen.get("event_ticker") or "").strip()

    # If the chosen market already ends with suffix, use it.
    if mt and mt.endswith(market_suffix):
        return et or mt.rsplit("-", 1)[0], mt

    # Otherwise, derive market ticker from event_ticker + suffix (common pattern in this series)
    if not et:
        # derive from market ticker by stripping the last hyphen chunk
        et = mt.rsplit("-", 1)[0] if "-" in mt else mt

    return et, et + market_suffix


def get_orderbook_yes_best(client: KalshiClient, market_ticker: str) -> Tuple[Optional[int], Optional[int]]:
    j = client.request("GET", f"/trade-api/v2/markets/{market_ticker}/orderbook")
    ob = j.get("orderbook") or j
    # Expect "yes" side book; formats vary. We handle common shapes.
    yes = ob.get("yes") if isinstance(ob, dict) else None
    if yes is None:
        yes = ob.get("YES") if isinstance(ob, dict) else None

    best_bid = None
    best_ask = None

    # Common: yes = {"bids":[[price,qty],...], "asks":[[price,qty],...]}
    if isinstance(yes, dict):
        bids = yes.get("bids") or []
        asks = yes.get("asks") or []
        if bids:
            best_bid = int(bids[0][0])
        if asks:
            best_ask = int(asks[0][0])
    else:
        # Some variants put bids/asks at top-level
        bids = ob.get("bids") if isinstance(ob, dict) else []
        asks = ob.get("asks") if isinstance(ob, dict) else []
        if bids:
            best_bid = int(bids[0][0])
        if asks:
            best_ask = int(asks[0][0])

    return best_bid, best_ask


def list_open_orders(client: KalshiClient, limit: int = 200) -> List[Dict[str, Any]]:
    j = client.request("GET", "/trade-api/v2/portfolio/orders", params={"limit": limit, "status": "open"})
    return j.get("orders", []) or j.get("data", []) or []


def get_order_detail(client: KalshiClient, order_id: str) -> Dict[str, Any]:
    return client.request("GET", f"/trade-api/v2/portfolio/orders/{order_id}")


def cancel_order(client: KalshiClient, order_id: str) -> Dict[str, Any]:
    # Kalshi v2 uses DELETE on /portfolio/orders/{id}
    return client.request("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}")


def place_limit_order_yes(client: KalshiClient, market_ticker: str, side: str, price: int, qty: int) -> Dict[str, Any]:
    payload = {
        "market_ticker": market_ticker,
        "action": side,      # "buy" or "sell"
        "type": "limit",
        "side": "yes",
        "count": int(qty),
        "price": int(price),
    }
    return client.request("POST", "/trade-api/v2/portfolio/orders", json_body=payload)


def get_positions(client: KalshiClient) -> List[Dict[str, Any]]:
    j = client.request("GET", "/trade-api/v2/portfolio/positions", params={"limit": 200})
    return j.get("positions", []) or j.get("data", []) or []


def get_yes_position_for_market(positions: List[Dict[str, Any]], market_ticker: str) -> int:
    # Look for exact ticker match
    for p in positions:
        if str(p.get("market_ticker") or p.get("ticker") or "").strip() == market_ticker:
            # Expect net position under "position" or similar
            v = p.get("position") or p.get("net_position") or p.get("count") or 0
            try:
                return int(v)
            except Exception:
                return 0
    return 0


# -----------------------------
# Quoting logic
# -----------------------------
def compute_target_quotes(
    best_bid: Optional[int],
    best_ask: Optional[int],
    pos_yes: int,
) -> Tuple[Optional[int], Optional[int], str]:
    """
    Returns (target_bid, target_ask, reason).
    YES-only quoting:
      - Normal mode (pos == 0): quote both sides as synthetic spread maker if allowed by spread.
      - Reduce-only (pos != 0): quote ONLY the exit side.
    """
    if best_bid is None or best_ask is None:
        return None, None, "no_book"

    spread = best_ask - best_bid
    if spread < 0:
        return None, None, "bad_book"

    # Reduce-only: exit inventory
    if pos_yes > 0:
        # long YES -> exit via SELL (ask)
        if spread < MIN_SPREAD_CENTS and not ENABLE_JOIN_TIGHT_SPREAD:
            return None, None, f"spread_too_tight({spread})"
        # join or improve by 0..?
        tgt_ask = best_ask if (spread <= 1 and ENABLE_JOIN_TIGHT_SPREAD) else best_ask
        return None, clamp_price_cents(tgt_ask), "reduce_only(long_exit)"
    if pos_yes < 0:
        # short YES -> exit via BUY (bid)
        if spread < MIN_SPREAD_CENTS and not ENABLE_JOIN_TIGHT_SPREAD:
            return None, None, f"spread_too_tight({spread})"
        tgt_bid = best_bid if (spread <= 1 and ENABLE_JOIN_TIGHT_SPREAD) else best_bid
        return clamp_price_cents(tgt_bid), None, "reduce_only(short_exit)"

    # Flat: market make if spread wide enough
    if spread < MIN_SPREAD_CENTS:
        return None, None, f"spread_too_tight({spread})"

    # Basic: join best bid/ask (you can add skew logic elsewhere if you already had it)
    tgt_bid = best_bid
    tgt_ask = best_ask
    return clamp_price_cents(tgt_bid), clamp_price_cents(tgt_ask), f"ok(spread={spread})"


# -----------------------------
# Reconciliation
# -----------------------------
def refresh_positions(state: LocalState, client: KalshiClient) -> None:
    pos = get_yes_position_for_market(get_positions(client), state.market_ticker)
    state.pos_yes = pos
    state.last_pos_refresh_ts = now_ts()


def inventory_fresh_enough(state: LocalState) -> bool:
    # FIX #2
    age = now_ts() - (state.last_pos_refresh_ts or 0.0)
    return age <= POSITION_REFRESH_MAX_AGE_SECONDS


def pause_and_force_refresh(state: LocalState, client: KalshiClient, seconds: float, why: str) -> None:
    state.pause_until_ts = max(state.pause_until_ts, now_ts() + seconds)
    log.warning(f"[INV] PAUSE quoting due to {why}: {seconds:.2f}s remaining")
    try:
        refresh_positions(state, client)
    except Exception as e:
        log.warning(f"[INV] force-refresh positions failed: {e}")


def reconcile_open_orders(state: LocalState, client: KalshiClient) -> Tuple[int, int]:
    """
    Align local tracked bid/ask with exchange.
    Returns (open_buys, open_sells) for this market (YES-side orders).
    """
    open_orders = list_open_orders(client)
    market_orders = [o for o in open_orders if str(o.get("market_ticker") or "").strip() == state.market_ticker]

    # Count open per side
    open_buys = sum(1 for o in market_orders if (o.get("action") or "").lower() == "buy" and (o.get("side") or "").lower() == "yes")
    open_sells = sum(1 for o in market_orders if (o.get("action") or "").lower() == "sell" and (o.get("side") or "").lower() == "yes")

    open_ids = set(str(o.get("order_id") or o.get("id") or "") for o in market_orders)

    # Helper: verify tracked order by detail if it’s not in open list yet (visibility lag)
    def verify_or_clear(q: Optional[QuoteOrder], label: str) -> Optional[QuoteOrder]:
        if not q:
            return None
        if q.order_id in open_ids:
            q.visible = True
            return q

        age = now_ts() - q.posted_ts
        if age < VISIBILITY_GRACE_SECONDS:
            log.warning(f"[OM] reconcile(orders_poll): {label} not visible yet; waiting visibility_grace {VISIBILITY_GRACE_SECONDS:.2f}s (age={age:.2f}s order_id={q.order_id})")
            return q

        # Past grace: verify via order detail
        try:
            d = get_order_detail(client, q.order_id)
            # Many payloads: { "order": {...} }
            od = d.get("order", d)
            status = (od.get("status") or "").lower()
            remaining = od.get("remaining_count") if od.get("remaining_count") is not None else od.get("remaining")  # some variants
            if remaining is None:
                remaining = od.get("count_remaining") if od.get("count_remaining") is not None else 0

            try:
                rem_i = int(remaining)
            except Exception:
                rem_i = 0

            # If executed/filled/canceled, clear local
            if status in ("executed", "filled", "canceled", "cancelled", "rejected") or rem_i == 0:
                return None

            # If detail says still open but list_open_orders didn’t show it, keep local (your log line)
            log.warning(f"[OM] reconcile(orders_poll): {label} missing in open-list but EXISTS via order-detail; keeping local state (order_id={q.order_id})")
            return q
        except Exception as e:
            log.warning(f"[OM] reconcile(orders_poll): {label} verify failed; clearing local state (order_id={q.order_id}) err={e}")
            return None

    state.bid = verify_or_clear(state.bid, "bid")
    state.ask = verify_or_clear(state.ask, "ask")

    return open_buys, open_sells


# -----------------------------
# Order Management
# -----------------------------
def cancel_safely(client: KalshiClient, state: LocalState, order: QuoteOrder, reason: str) -> Tuple[bool, bool]:
    """
    Returns (canceled_ok, got_404_not_found).
    FIX #1: if cancel returns 404/not_found, treat as possibly-filled and force refresh/pause.
    """
    try:
        j = cancel_order(client, order.order_id)
        # some APIs return {error:{code:'not_found'}} with 404 handled upstream; but we keep this too
        log.info(f"[OM] {state.market_ticker} {order.side.upper()} CANCEL @{order.price} ({reason}) order_id={order.order_id}")
        return True, False
    except Exception as e:
        msg = str(e).lower()
        is_404 = ("404" in msg) or ("not_found" in msg) or ("not found" in msg)
        log.info(
            f"[OM] {state.market_ticker} {order.side.upper()} CANCEL @{order.price} ({reason}) order_id={order.order_id} status={'not_found' if is_404 else 'error'}"
        )
        return False, is_404


def place_and_track(client: KalshiClient, state: LocalState, side: str, price: int, qty: int) -> Optional[QuoteOrder]:
    j = place_limit_order_yes(client, state.market_ticker, side, price, qty)
    if j.get("dry_run"):
        # fake id for logs
        oid = f"dry_{side}_{int(now_ts())}"
    else:
        oid = str(j.get("order_id") or j.get("id") or (j.get("order", {}) or {}).get("order_id") or "")
    log.info(f"[OM] {state.market_ticker} {side.upper()} PLACE @{price} qty={qty} DRY_RUN={DRY_RUN}")
    if not oid:
        return None
    return QuoteOrder(order_id=oid, side=side, price=price, qty=qty, posted_ts=now_ts(), visible=False)


def reprice_order_normal(client: KalshiClient, state: LocalState, existing: Optional[QuoteOrder], side: str, target_px: int) -> Optional[QuoteOrder]:
    """
    Normal mode: (place-first-then-cancel) is fine because inventory == 0.
    Keep your previous behavior here.
    """
    if existing and existing.price == target_px:
        return existing

    # place new
    newq = place_and_track(client, state, side, target_px, ORDER_QTY)
    # cancel old
    if existing:
        cancel_safely(client, state, existing, "reprice(place-first)")
    return newq


def reprice_order_reduce_only_cancel_first(client: KalshiClient, state: LocalState, existing: Optional[QuoteOrder], side: str, target_px: int) -> Optional[QuoteOrder]:
    """
    FIX #1: Reduce-only repricing MUST be cancel-first, then place.
    If cancel fails, do not place a second exit.
    If cancel is 404/not_found, assume it may have filled -> force refresh + pause.
    """
    if existing and existing.price == target_px:
        return existing

    if existing:
        ok, got_404 = cancel_safely(client, state, existing, "reprice cancel-first")
        if got_404:
            # Treat as potentially filled; clear local + force refresh + pause
            log.warning(f"[INV] force-refresh positions (cancel_404_not_found): market={state.market_ticker} pos_yes={state.pos_yes}")
            state.bid = None if side == "buy" else state.bid
            state.ask = None if side == "sell" else state.ask
            try:
                refresh_positions(state, client)
            finally:
                state.pause_until_ts = max(state.pause_until_ts, now_ts() + POSITION_REFRESH_MAX_AGE_SECONDS)
            log.warning(f"[OM] {state.market_ticker} cancel got 404/not_found; cleared local state, forced positions refresh, pausing until inventory stabilizes.")
            return None

        if not ok:
            # cancel failed (non-404) -> do not place new exit
            log.warning(f"[OM] {state.market_ticker} reduce-only cancel failed; NOT placing second exit order. Safe > quoted.")
            return existing

    # place after cancel
    return place_and_track(client, state, side, target_px, ORDER_QTY)


# -----------------------------
# Startup Reconcile (FIX #4)
# -----------------------------
def startup_reconcile(state: LocalState, client: KalshiClient) -> None:
    """
    On boot, reconcile from live exchange state so deploy/restart doesn’t create duplicate orders.
    """
    # Always refresh position first (best effort)
    try:
        refresh_positions(state, client)
    except Exception as e:
        log.warning(f"[OM] startup reconcile: positions refresh failed: {e}")

    # Pull open orders for this market and either cancel them (default) or adopt them
    try:
        open_orders = list_open_orders(client)
        market_orders = [o for o in open_orders if str(o.get("market_ticker") or "").strip() == state.market_ticker and (o.get("side") or "").lower() == "yes"]
        if not market_orders:
            state.bid = None
            state.ask = None
            return

        if STARTUP_CANCEL_OPEN_ORDERS:
            for o in market_orders:
                oid = str(o.get("order_id") or o.get("id") or "")
                px = int(o.get("price") or 0)
                act = (o.get("action") or "").lower()
                if not oid:
                    continue
                try:
                    cancel_order(client, oid)
                    log.warning(f"[OM] startup reconcile: canceled stray open order order_id={oid} action={act} px={px}")
                except Exception as e:
                    log.warning(f"[OM] startup reconcile: failed cancel order_id={oid} err={e}")

            state.bid = None
            state.ask = None

            # Refresh again to ensure we see any immediate fills
            try:
                refresh_positions(state, client)
            except Exception:
                pass
            return

        # If not canceling, adopt the most recent per side
        # (We set posted_ts to “now” so visibility grace applies.)
        bid = None
        ask = None
        for o in market_orders:
            oid = str(o.get("order_id") or o.get("id") or "")
            px = int(o.get("price") or 0)
            act = (o.get("action") or "").lower()
            if act == "buy":
                bid = QuoteOrder(order_id=oid, side="buy", price=px, qty=int(o.get("remaining_count") or o.get("count") or ORDER_QTY), posted_ts=now_ts(), visible=True)
            elif act == "sell":
                ask = QuoteOrder(order_id=oid, side="sell", price=px, qty=int(o.get("remaining_count") or o.get("count") or ORDER_QTY), posted_ts=now_ts(), visible=True)
        state.bid = bid
        state.ask = ask
        log.warning(f"[OM] startup reconcile: adopted open orders bid={state.bid.order_id if state.bid else None} ask={state.ask.order_id if state.ask else None}")

    except Exception as e:
        log.warning(f"[OM] startup reconcile: open-orders reconcile failed: {e}")


# -----------------------------
# PnL Logging (FIX #3)
# -----------------------------
def log_pnl(state: LocalState, mark_cents: Optional[int]) -> None:
    # We treat state.realized_cents / fees_cents as CENTS (ints).
    # Unreal is optional (you can compute if you have entry, etc). We still format correctly.
    realized_d = dollars_from_cents(state.realized_cents)
    fees_d = dollars_from_cents(state.fees_cents)
    unreal_d = dollars_from_cents(state.unreal_cents) if state.unreal_cents is not None else "n/a"
    mark_s = f"{mark_cents}c" if mark_cents is not None else "n/a"
    log.info(
        f"[PNL] {state.market_ticker} pos_yes={state.pos_yes} mark={mark_s} "
        f"unreal={unreal_d} (raw_cents={state.unreal_cents}) "
        f"realized={realized_d} (raw_cents={state.realized_cents}) "
        f"fees={fees_d} (raw_cents={state.fees_cents})"
    )


# -----------------------------
# Main Loop
# -----------------------------
def main() -> None:
    if not API_KEY:
        raise RuntimeError("Missing API key: set KALSHI_API_KEY (or KALSHI_ACCESS_KEY)")

    client = KalshiClient(API_KEY, load_private_key_pem(), API_BASE)

    # Discover active market
    markets = api_get_markets_by_series(client, SERIES_TICKER)
    if not markets:
        raise RuntimeError(f"No markets returned for series_ticker={SERIES_TICKER}")

    event_ticker, market_ticker = pick_active_event_and_market(markets, MARKET_SUFFIX)
    state = LocalState(market_ticker=market_ticker, event_ticker=event_ticker)

    log.info(f"[ROLL] Series={SERIES_TICKER} → Active event={event_ticker} market={market_ticker} (via /markets series_ticker)")

    # FIX #4: startup reconcile from live state
    startup_reconcile(state, client)

    # Initial position refresh
    try:
        refresh_positions(state, client)
    except Exception as e:
        log.warning(f"[INV] initial positions refresh failed: {e}")

    while True:
        try:
            # If we rolled (event changed), re-discover occasionally:
            # Keep your prior cadence; here we do it every loop only if you want, but that’s “more changes”.
            # So: only re-check every ~10s to be gentle.
            # (If you already had a different schedule, keep it.)
            # ---------------------------------------------------
            # Minimal “roll” check: once every 10 seconds
            # ---------------------------------------------------
            if int(now_ts()) % 10 == 0:
                try:
                    mkts = api_get_markets_by_series(client, SERIES_TICKER)
                    et2, mt2 = pick_active_event_and_market(mkts, MARKET_SUFFIX)
                    if mt2 and mt2 != state.market_ticker:
                        log.info(f"[ROLL] Series={SERIES_TICKER} → Active event={et2} market={mt2} (via /markets series_ticker)")
                        state.market_ticker = mt2
                        state.event_ticker = et2
                        state.bid = None
                        state.ask = None
                        startup_reconcile(state, client)
                        refresh_positions(state, client)
                except Exception as e:
                    log.warning(f"[ROLL] roll-check failed: {e}")

            # FIX #2: inventory freshness guard
            if not inventory_fresh_enough(state):
                pause_and_force_refresh(state, client, POSITION_REFRESH_MAX_AGE_SECONDS, "stale_inventory")
                time.sleep(LOOP_SLEEP_SECONDS)
                continue

            # Reconcile open orders vs local
            open_buys, open_sells = reconcile_open_orders(state, client)

            if state.paused():
                time.sleep(LOOP_SLEEP_SECONDS)
                continue

            best_bid, best_ask = get_orderbook_yes_best(client, state.market_ticker)
            if best_bid is None or best_ask is None:
                log.info(f"[TARGET] {state.market_ticker} → SKIP (no_orderbook)")
                time.sleep(LOOP_SLEEP_SECONDS)
                continue

            spread = best_ask - best_bid

            # Compute targets
            tgt_bid, tgt_ask, reason = compute_target_quotes(best_bid, best_ask, state.pos_yes)

            reduce_only = state.pos_yes != 0

            # PnL log (FIX #3 formatting)
            # mark: use best_ask as a rough mark for short, best_bid for long; for flat use mid
            if state.pos_yes < 0:
                mark = best_ask
            elif state.pos_yes > 0:
                mark = best_bid
            else:
                mark = (best_bid + best_ask) // 2
            log_pnl(state, mark)

            # State snapshot (similar to your logs)
            log.info(
                f"[STATE] mkt={state.market_ticker} pos={state.pos_yes} spread={spread} best=({best_bid},{best_ask}) "
                f"tgt=({tgt_bid},{tgt_ask}) reduce_only={reduce_only} "
                f"open=(b:{open_buys},s:{open_sells}) "
                f"inv_age={now_ts()-state.last_pos_refresh_ts:.2f}s "
                f"quote=(b:{state.bid.order_id+'@'+str(state.bid.price) if state.bid else None} vis={state.bid.visible if state.bid else False}, "
                f"a:{state.ask.order_id+'@'+str(state.ask.price) if state.ask else None} vis={state.ask.visible if state.ask else False})"
            )

            if tgt_bid is None and tgt_ask is None:
                log.info(f"[TARGET] {state.market_ticker} → SKIP ({reason})")
                time.sleep(LOOP_SLEEP_SECONDS)
                continue

            # Manage orders
            # If reduce-only: only exit side is allowed
            if reduce_only:
                if state.pos_yes < 0:
                    # Need BUY to cover short
                    if tgt_bid is not None:
                        # FIX #1: cancel-first for reduce-only repricing
                        state.bid = reprice_order_reduce_only_cancel_first(client, state, state.bid, "buy", tgt_bid)
                    # Ensure we have no ask in reduce-only short mode
                    if state.ask:
                        cancel_safely(client, state, state.ask, "reduce_only(short) clear_ask")
                        state.ask = None

                elif state.pos_yes > 0:
                    # Need SELL to exit long
                    if tgt_ask is not None:
                        # FIX #1: cancel-first for reduce-only repricing
                        state.ask = reprice_order_reduce_only_cancel_first(client, state, state.ask, "sell", tgt_ask)
                    # Ensure we have no bid in reduce-only long mode
                    if state.bid:
                        cancel_safely(client, state, state.bid, "reduce_only(long) clear_bid")
                        state.bid = None

            else:
                # Flat: can quote both
                if tgt_bid is not None:
                    if open_buys < MAX_QTY_PER_SIDE:
                        state.bid = reprice_order_normal(client, state, state.bid, "buy", tgt_bid)
                else:
                    if state.bid:
                        cancel_safely(client, state, state.bid, "no_target_bid")
                        state.bid = None

                if tgt_ask is not None:
                    if open_sells < MAX_QTY_PER_SIDE:
                        state.ask = reprice_order_normal(client, state, state.ask, "sell", tgt_ask)
                else:
                    if state.ask:
                        cancel_safely(client, state, state.ask, "no_target_ask")
                        state.ask = None

            time.sleep(LOOP_SLEEP_SECONDS)

        except RuntimeError as e:
            # If we hit auth or 401 loops, you probably want a longer backoff
            log.error(f"[LOOPERR] {repr(e)}")
            time.sleep(2.0)
        except Exception as e:
            log.error(f"[LOOPERR] {repr(e)}")
            time.sleep(1.0)


if __name__ == "__main__":
    main()
