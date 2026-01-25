# bot.py
# Kalshi rolling 15m BTC market-maker (YES contract side, two-sided quoting when flat; reduce-only exits when not flat)
#
# -----------------------------
# NOTES / WHAT CHANGED (MICRO)
# -----------------------------
# CHANGE 1 (NEW): If a cancel returns 404/not_found ANYWHERE (including cancel_bid_only/cancel_ask_only
#                 and cancel-first repricing), treat it as "order likely filled / already gone":
#     • Immediately clear local order_id/price for that side
#     • Force-refresh positions right away
#     • Pause + skip quoting briefly until inventory is confirmed stable
#
# CHANGE 2 (CRITICAL, NEW): In cancel-first repricing, if we hit not_found on one side,
#                           ABORT the rest of order actions for that loop iteration.
#     • Do NOT place/reprice the other side in the same iteration
#     • Wait for positions refresh + stability before doing anything else
#
# CHANGE 3 (CRITICAL, NEW): Startup reconciliation/state reset:
#     • On boot/redeploy, always reconcile from the live exchange state (open orders + positions)
#     • If BOOTSTRAP_CANCEL_OPEN_ORDERS=True: keep existing behavior (cancel leftovers), then reconcile/pause
#     • If BOOTSTRAP_CANCEL_OPEN_ORDERS=False: adopt best live YES bid/ask into local QuoteState to avoid double-posting
#     • Pause briefly after reconciliation to ensure inventory is stable before quoting
#
# CHANGE 4 (CRITICAL, FIX): Open-orders polling used an invalid status filter ("open") for /portfolio/orders.
#     • Kalshi expects status in {resting, canceled, executed} for that endpoint.
#     • Bot now uses status="resting" so open order reconciliation matches reality.
#
# CHANGE 5 (NEW, SAFETY): Market-snapshot fallback sanity guard near terminal markets:
#     • If fallback yields an invalid/terminal quote (bid>=ask OR 99/99), treat as "no book"
#     • This prevents end-of-window "99/99" snapshot values from being interpreted as tradable quotes
#     • Throttles fallback warnings to reduce log spam
#
# LOGGING DIAGNOSTICS (NEW, ONLY): Adds high-signal logs to debug:
#   (A) OMSNP reconcile snapshot lines when open-list != detail
#   (B) LIFE order lifecycle tracking (posted -> seen in open-list -> seen in detail)
#   (C) BOOTSTATE banner on startup to show inherited open orders + position
#   (D) Open-list seen markers (LIFE open-list first-seen)
#
# Everything else is kept as-is.

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
MAX_SPREAD_CENTS = env_int("MAX_SPREAD_CENTS", 30)  # if spread > this, skip

# Proper hysteresis
HYSTERESIS_ENTER_SPREAD_CENTS = env_int("HYSTERESIS_ENTER_SPREAD_CENTS", MIN_SPREAD_CENTS)
HYSTERESIS_EXIT_SPREAD_CENTS = env_int("HYSTERESIS_EXIT_SPREAD_CENTS", max(1, MIN_SPREAD_CENTS - 1))
SPREAD_ENTER_STREAK = env_int("SPREAD_ENTER_STREAK", 2)
SPREAD_EXIT_STREAK = env_int("SPREAD_EXIT_STREAK", 2)

# Log throttles
TARGET_LOG_THROTTLE_SECONDS = env_float("TARGET_LOG_THROTTLE_SECONDS", 10.0)
SPOT_SKIP_LOG_THROTTLE_SECONDS = env_float("SPOT_SKIP_LOG_THROTTLE_SECONDS", 2.0)

# Inventory control (simple)
MAX_NET_YES_CONTRACTS = env_int("MAX_NET_YES_CONTRACTS", 2)
INVENTORY_SKEW_CENTS = env_int("INVENTORY_SKEW_CENTS", 1)
INITIAL_NET_YES_CONTRACTS = env_int("INITIAL_NET_YES_CONTRACTS", 0)

# Absolute cap + reduce-only & two-sided-flat-only
MAX_ABS_YES_CONTRACTS = env_int("MAX_ABS_YES_CONTRACTS", 1)
QUOTE_BOTH_WHEN_FLAT_ONLY = env_bool("QUOTE_BOTH_WHEN_FLAT_ONLY", True)

# Order-state safety
ORDER_STATUS_POLL_SECONDS = env_float("ORDER_STATUS_POLL_SECONDS", 1.0)
PAUSE_ON_UNKNOWN_SECONDS = env_float("PAUSE_ON_UNKNOWN_SECONDS", 0.75)

# Balance / collateral safety
BALANCE_FAIL_COOLDOWN_SECONDS = env_float("BALANCE_FAIL_COOLDOWN_SECONDS", 10.0)
BALANCE_FAIL_MAX_BURST = env_int("BALANCE_FAIL_MAX_BURST", 3)
REQUIRE_TWO_SIDED_QUOTES = env_bool("REQUIRE_TWO_SIDED_QUOTES", True)

# Rate-limit + churn control
MIN_REPRICE_SECONDS = env_float("MIN_REPRICE_SECONDS", 1.25)
MIN_ORDER_ACTION_GAP_SECONDS = env_float("MIN_ORDER_ACTION_GAP_SECONDS", 0.35)

RATE_LIMIT_BACKOFF_START_SECONDS = env_float("RATE_LIMIT_BACKOFF_START_SECONDS", 0.75)
RATE_LIMIT_BACKOFF_MAX_SECONDS = env_float("RATE_LIMIT_BACKOFF_MAX_SECONDS", 8.0)

POST_ONLY_CROSS_COOLDOWN_SECONDS = env_float("POST_ONLY_CROSS_COOLDOWN_SECONDS", 2.0)
SPREAD_SKIP_COOLDOWN_SECONDS = env_float("SPREAD_SKIP_COOLDOWN_SECONDS", 1.5)

# Spot guard
ENABLE_SPOT_GUARD = env_bool("ENABLE_SPOT_GUARD", True)
SPOT_POLL_SECONDS = env_float("SPOT_POLL_SECONDS", 12.0)
SPOT_RESOLVED_BUFFER_USD = env_float("SPOT_RESOLVED_BUFFER_USD", 75.0)
CLOSEOUT_SECONDS = env_float("CLOSEOUT_SECONDS", 20.0)
SPOT_GUARD_NEAR_CLOSE_SECONDS = env_float("SPOT_GUARD_NEAR_CLOSE_SECONDS", 90.0)

# Inventory / P&L polling
POSITIONS_POLL_SECONDS = env_float("POSITIONS_POLL_SECONDS", 3.0)
FILLS_POLL_SECONDS = env_float("FILLS_POLL_SECONDS", 3.0)
PNL_LOG_THROTTLE_SECONDS = env_float("PNL_LOG_THROTTLE_SECONDS", 10.0)

# inventory staleness guard
DEFAULT_MAX_INV_STALENESS_SECONDS = max(2.0, POSITIONS_POLL_SECONDS * 2.0)
MAX_INV_STALENESS_SECONDS = env_float("MAX_INV_STALENESS_SECONDS", DEFAULT_MAX_INV_STALENESS_SECONDS)

# Coinbase spot endpoint
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

# Bootstrap cleanup toggle
BOOTSTRAP_CANCEL_OPEN_ORDERS = env_bool("BOOTSTRAP_CANCEL_OPEN_ORDERS", True)

# PnL unit toggle (cents -> USD)
PNL_VALUES_ARE_CENTS = env_bool("PNL_VALUES_ARE_CENTS", True)

# reduce-only exit repricing controls
REDUCE_ONLY_REPRICE_TICKS = env_int("REDUCE_ONLY_REPRICE_TICKS", 2)
REDUCE_ONLY_MIN_REPRICE_SECONDS = env_float(
    "REDUCE_ONLY_MIN_REPRICE_SECONDS", max(3.0, MIN_REPRICE_SECONDS * 2.0)
)

# cancel any stray open YES orders for the active market
CLEAN_STRAY_ORDERS = env_bool("CLEAN_STRAY_ORDERS", True)

# reconcile grace/confirm delay
RECONCILE_MISSING_GRACE_SECONDS = env_float("RECONCILE_MISSING_GRACE_SECONDS", 0.75)

# open-orders visibility grace
ORDERS_VISIBILITY_GRACE_SECONDS = env_float("ORDERS_VISIBILITY_GRACE_SECONDS", 3.0)

# verify missing orders via order-detail endpoint
USE_ORDER_DETAIL_FOR_RECONCILE = env_bool("USE_ORDER_DETAIL_FOR_RECONCILE", True)

# emergency reduce-only behavior when starting with a big position
EMERGENCY_EXIT_QTY = env_int("EMERGENCY_EXIT_QTY", 1)  # set >1 only if you *want* faster unwind
EMERGENCY_IGNORE_HYSTERESIS = env_bool("EMERGENCY_IGNORE_HYSTERESIS", True)

# orderbook best-bid/ask fallback
ORDERBOOK_FALLBACK_TO_MARKET_SNAPSHOT = env_bool("ORDERBOOK_FALLBACK_TO_MARKET_SNAPSHOT", True)
MARKET_SNAPSHOT_TTL_SECONDS = env_float("MARKET_SNAPSHOT_TTL_SECONDS", 1.0)

# Extra debug
STATE_LOG_SECONDS = env_float("STATE_LOG_SECONDS", 1.5)
RECONCILE_DEBUG_THROTTLE_SECONDS = env_float("RECONCILE_DEBUG_THROTTLE_SECONDS", 5.0)

# -----------------------------
# NEW: Logging / diagnostics toggles (ONLY ADDITIONS)
# -----------------------------
LOG_RECONCILE_SNAPSHOT = env_bool("LOG_RECONCILE_SNAPSHOT", True)
LOG_ORDER_LIFECYCLE = env_bool("LOG_ORDER_LIFECYCLE", True)
LOG_STARTUP_INHERITED_STATE = env_bool("LOG_STARTUP_INHERITED_STATE", True)

# NEW: Order lifecycle tracking (posted -> seen open-list/detail -> terminal)
order_life: Dict[str, Dict[str, Any]] = {}


def _life(oid: str) -> Dict[str, Any]:
    if oid not in order_life:
        order_life[oid] = {
            "posted_ts": None,
            "posted_px": None,
            "posted_action": None,
            "first_seen_open_ts": None,
            "first_seen_detail_ts": None,
            "last_seen_open_ts": None,
            "last_detail_status": None,
            "last_detail_remaining": None,
            "last_detail_px": None,
            "terminal": None,
        }
    return order_life[oid]


def _mark_open_seen(oid: str) -> None:
    if not LOG_ORDER_LIFECYCLE:
        return
    m = _life(oid)
    now = time.time()
    m["last_seen_open_ts"] = now
    if m["first_seen_open_ts"] is None:
        m["first_seen_open_ts"] = now
        log.info(f"[LIFE] open-list first-seen order_id={oid}")


def _mark_detail_seen(oid: str, detail: Dict[str, Any]) -> None:
    if not LOG_ORDER_LIFECYCLE:
        return
    m = _life(oid)
    now = time.time()
    if m["first_seen_detail_ts"] is None:
        m["first_seen_detail_ts"] = now
        log.info(f"[LIFE] detail first-seen order_id={oid}")

    base = detail.get("order") if isinstance(detail.get("order"), dict) else detail
    st = base.get("status")
    rem = base.get("remaining_count")
    px = base.get("yes_price") or base.get("price")

    m["last_detail_status"] = st
    m["last_detail_remaining"] = rem
    m["last_detail_px"] = px


def _life_banner(oid: str) -> str:
    m = order_life.get(oid) or {}
    return (
        f"life(posted_ts={m.get('posted_ts')}, posted_px={m.get('posted_px')}, posted_action={m.get('posted_action')}, "
        f"open_first={m.get('first_seen_open_ts')}, detail_first={m.get('first_seen_detail_ts')}, "
        f"last_open={m.get('last_seen_open_ts')}, detail_status={m.get('last_detail_status')}, "
        f"rem={m.get('last_detail_remaining')}, detail_px={m.get('last_detail_px')}, terminal={m.get('terminal')})"
    )


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
    orderbook = ob.get("orderbook") or ob
    yes_levels = orderbook.get("yes") or []
    no_levels = orderbook.get("no") or []

    def max_bid(levels: Any) -> Optional[int]:
        if not isinstance(levels, list) or not levels:
            return None
        best: Optional[int] = None
        for lvl in levels:
            price = None
            if isinstance(lvl, list) and len(lvl) >= 1:
                price = lvl[0]
            elif isinstance(lvl, dict):
                price = lvl.get("price") or lvl.get("yes_price") or lvl.get("no_price")
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
    action: str,  # "buy" or "sell"
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


def safe_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None


def get_positions(client: KalshiClient) -> List[Dict[str, Any]]:
    resp = client.request("GET", "/portfolio/positions", params={"limit": 200})
    if isinstance(resp, dict):
        for k in ("positions", "market_positions", "portfolio_positions"):
            if k in resp and isinstance(resp[k], list):
                return resp[k]
    if isinstance(resp, list):
        return resp
    return []


def get_fills(client: KalshiClient, min_ts_ms: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"limit": int(limit)}
    if min_ts_ms is not None and min_ts_ms > 0:
        params["min_ts"] = int(min_ts_ms)
    resp = client.request("GET", "/portfolio/fills", params=params)
    if isinstance(resp, dict):
        for k in ("fills", "executions"):
            if k in resp and isinstance(resp[k], list):
                return resp[k]
    if isinstance(resp, list):
        return resp
    return []


def extract_fill_ts_ms(fill: Dict[str, Any]) -> Optional[int]:
    for k in ("created_time", "created_ts", "timestamp", "ts", "time"):
        if k in fill:
            try:
                v = fill[k]
                iv = int(float(v))
                if iv < 10_000_000_000:  # seconds -> ms
                    return iv * 1000
                return iv
            except Exception:
                continue
    return None


def extract_yes_price_cents(obj: Dict[str, Any]) -> Optional[int]:
    for k in ("yes_price", "price", "fill_price", "execution_price"):
        if k in obj:
            try:
                return int(obj[k])
            except Exception:
                continue
    return None


def parse_position_for_market(
    positions: List[Dict[str, Any]],
    market_ticker: str,
) -> Tuple[int, Optional[float], Optional[float]]:
    mt = str(market_ticker)
    for p in positions:
        t = p.get("ticker") or p.get("market_ticker") or p.get("contract_ticker")
        if not t or str(t) != mt:
            continue

        pos_yes: Optional[int] = None
        for k in ("position", "net_position", "yes_position", "net_yes_position", "qty", "count"):
            if k in p:
                try:
                    pos_yes = int(p[k])
                    break
                except Exception:
                    continue
        if pos_yes is None:
            pos_yes = 0

        realized = None
        fees = None
        for k in ("realized_pnl", "realized_pnl_usd", "pnl_realized", "realized"):
            if k in p:
                realized = safe_float(p[k])
                if realized is not None:
                    break
        for k in ("fees_paid", "fees_paid_usd", "fees", "fee_paid"):
            if k in p:
                fees = safe_float(p[k])
                if fees is not None:
                    break

        # Treat realized/fees as cents by default, convert to USD for display
        if PNL_VALUES_ARE_CENTS:
            if realized is not None:
                realized = realized / 100.0
            if fees is not None:
                fees = fees / 100.0

        return pos_yes, realized, fees

    return 0, None, None


def compute_unrealized_usd(pos_yes: int, entry_cents: Optional[int], mark_cents: Optional[int]) -> Optional[float]:
    if pos_yes == 0 or entry_cents is None or mark_cents is None:
        return None
    diff_cents = (mark_cents - entry_cents) * pos_yes
    return diff_cents / 100.0


def update_entry_from_fills_for_market(
    fills: List[Dict[str, Any]],
    market_ticker: str,
    pos_yes: int,
) -> Optional[int]:
    mt = str(market_ticker)
    if pos_yes == 0:
        return None

    want_action = "buy" if pos_yes > 0 else "sell"

    best_ts = -1
    best_px: Optional[int] = None

    for f in fills:
        t = f.get("ticker") or f.get("market_ticker") or f.get("contract_ticker")
        if not t or str(t) != mt:
            continue

        side = str(f.get("side", "")).lower()
        if side and side != "yes":
            continue

        action = str(f.get("action", "")).lower()
        if action != want_action:
            continue

        px = extract_yes_price_cents(f)
        if px is None:
            continue

        ts = extract_fill_ts_ms(f) or 0
        if ts > best_ts:
            best_ts = ts
            best_px = px

    return best_px


def get_open_orders(client: KalshiClient) -> List[Dict[str, Any]]:
    # CHANGE 4: status must be "resting" (not "open") for /portfolio/orders
    resp = client.request("GET", "/portfolio/orders", params={"status": "resting", "limit": 200})
    return resp.get("orders", resp if isinstance(resp, list) else [])


def get_order_by_id(client: KalshiClient, order_id: str) -> Optional[Dict[str, Any]]:
    try:
        return client.request("GET", f"/portfolio/orders/{order_id}")
    except RuntimeError as e:
        msg = str(e)
        if ("HTTP 404" in msg) or ("not_found" in msg):
            return None
        raise


def cancel_order_status(client: KalshiClient, order_id: str) -> str:
    try:
        client.request("DELETE", f"/portfolio/orders/{order_id}")
        return "canceled"
    except RuntimeError as e:
        msg = str(e)
        if ("HTTP 404" in msg) or ("not_found" in msg):
            return "not_found"
        raise


def place_order(client: KalshiClient, payload: Dict[str, Any]) -> str:
    resp = client.request("POST", "/portfolio/orders", json_body=payload)
    if isinstance(resp, dict):
        if "order" in resp and isinstance(resp["order"], dict) and resp["order"].get("order_id"):
            return str(resp["order"]["order_id"])
        if resp.get("order_id"):
            return str(resp["order_id"])
    raise RuntimeError(f"Unexpected create order response: {resp}")


def compute_quotes(
    yes_bid: int,
    yes_ask: int,
    net_yes: int,
    min_spread_cents: int = MIN_SPREAD_CENTS,
) -> Tuple[Optional[int], Optional[int], str]:
    spread = yes_ask - yes_bid

    if spread < min_spread_cents:
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


def open_order_ids_for_market_yes(open_orders: List[Dict[str, Any]], market_ticker: str) -> List[str]:
    ids: List[str] = []
    for o in open_orders:
        if str(o.get("ticker")) != str(market_ticker):
            continue
        if str(o.get("side", "")).lower() != "yes":
            continue
        oid = o.get("order_id") or o.get("id")
        if oid:
            ids.append(str(oid))
    return ids


def is_yes_order_obj_for_market(o: Dict[str, Any], market_ticker: str) -> bool:
    return str(o.get("ticker")) == str(market_ticker) and str(o.get("side", "")).lower() == "yes"


# -----------------------------
# Market snapshot fallback
# -----------------------------
def _extract_best_from_market_snapshot(m: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    if "market" in m and isinstance(m["market"], dict):
        m = m["market"]

    candidates_bid = [
        "yes_bid",
        "best_yes_bid",
        "best_bid",
        "bid",
        "bid_price",
        "yes_bid_price",
        "best_yes_bid_price",
        "best_yes_bid_cents",
    ]
    candidates_ask = [
        "yes_ask",
        "best_yes_ask",
        "best_ask",
        "ask",
        "ask_price",
        "yes_ask_price",
        "best_yes_ask_price",
        "best_yes_ask_cents",
    ]

    bid = None
    ask = None

    for k in candidates_bid:
        if k in m:
            try:
                bid = int(m[k])
                break
            except Exception:
                pass

    for k in candidates_ask:
        if k in m:
            try:
                ask = int(m[k])
                break
            except Exception:
                pass

    if ask is None:
        for k in ("no_bid", "best_no_bid", "best_no_bid_price", "no_bid_price"):
            if k in m:
                try:
                    no_bid = int(m[k])
                    ask = 100 - no_bid
                    break
                except Exception:
                    pass

    if bid is not None:
        bid = max(1, min(99, bid))
    if ask is not None:
        ask = max(1, min(99, ask))

    return bid, ask


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

    if not API_KEY_ID or not PRIVATE_KEY_PEM_B64:
        raise RuntimeError("Missing KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_PEM_BASE64")

    client = KalshiClient(API_BASE, API_PREFIX, API_KEY_ID, PRIVATE_KEY_PEM_B64)
    http = requests.Session()

    quote = QuoteState()
    net_yes = INITIAL_NET_YES_CONTRACTS

    last_positions_poll = 0.0
    last_fills_poll = 0.0
    last_fill_ts_ms: Optional[int] = None

    pos_yes_live = INITIAL_NET_YES_CONTRACTS
    realized_pnl_usd: Optional[float] = None
    fees_paid_usd: Optional[float] = None
    entry_yes_cents: Optional[int] = None

    last_pnl_log_at = 0.0
    last_pnl_sig: Tuple[Any, ...] = tuple()

    last_meta_refresh = 0.0
    last_spot_poll = 0.0
    spot_usd: Optional[float] = None

    active_event = None
    active_market = None
    active_market_obj: Dict[str, Any] = {}

    last_order_poll = 0.0
    pause_until = 0.0

    open_orders_cache: List[Dict[str, Any]] = []

    # Track when we POSTED each order_id (open-orders endpoint can lag).
    order_posted_ts: Dict[str, float] = {}  # order_id -> epoch seconds

    last_spot_skip_log_at = 0.0
    last_spot_skip_msg = ""

    last_target_log_at = 0.0
    last_target_sig: Tuple[Any, ...] = tuple()

    balance_fail_burst = 0
    balance_fail_until = 0.0

    rl_until = 0.0
    rl_backoff = RATE_LIMIT_BACKOFF_START_SECONDS
    skip_quote_until = 0.0
    last_order_action_at = 0.0
    last_reprice_at = 0.0
    last_quote_sig: Tuple[Any, ...] = tuple()

    is_quoting = False
    enter_ok_streak = 0
    exit_bad_streak = 0

    last_cancel_not_found_oid: Optional[str] = None
    last_emergency_sig: Tuple[Any, ...] = tuple()

    # market snapshot cache
    last_market_snapshot_at = 0.0
    last_market_snapshot: Dict[str, Any] = {}

    # debug throttles
    last_state_log_at = 0.0
    last_reconcile_dbg_at = 0.0

    # CHANGE 5: throttle snapshot-fallback warnings to reduce spam
    last_ob_fb_log_at = 0.0
    last_ob_fb_sig: Tuple[Any, ...] = tuple()
    OB_FALLBACK_LOG_THROTTLE_SECONDS = 2.0

    def is_insufficient_balance(e: Exception) -> bool:
        s = str(e)
        return ("insufficient_balance" in s) or ("code" in s and "insufficient_balance" in s)

    def is_rate_limited(e: Exception) -> bool:
        s = str(e)
        return ("HTTP 429" in s) or ("too_many_requests" in s)

    def is_post_only_cross(e: Exception) -> bool:
        s = str(e).lower()
        return ("post only" in s and "cross" in s) or ("post_only" in s and "cross" in s)

    def arm_rate_limit_pause(tag: str) -> None:
        nonlocal rl_until, rl_backoff, skip_quote_until
        now = time.time()
        rl_until = now + rl_backoff
        skip_quote_until = max(skip_quote_until, rl_until)
        log.warning(f"[RL] {tag}: backing off {rl_backoff:.2f}s")
        rl_backoff = min(RATE_LIMIT_BACKOFF_MAX_SECONDS, rl_backoff * 2.0)

    def note_successful_request() -> None:
        nonlocal rl_backoff
        rl_backoff = max(RATE_LIMIT_BACKOFF_START_SECONDS, rl_backoff * 0.9)

    def can_do_order_action() -> bool:
        return (time.time() - last_order_action_at) >= MIN_ORDER_ACTION_GAP_SECONDS

    def mark_order_action() -> None:
        nonlocal last_order_action_at
        last_order_action_at = time.time()

    def refresh_positions_now(tag: str) -> bool:
        nonlocal pos_yes_live, realized_pnl_usd, fees_paid_usd, net_yes, last_positions_poll
        try:
            positions = get_positions(client)
            note_successful_request()
            pos_yes_live, realized_pnl_usd, fees_paid_usd = parse_position_for_market(positions, active_market)
            net_yes = pos_yes_live
            last_positions_poll = time.time()
            log.warning(f"[INV] force-refresh positions ({tag}): market={active_market} pos_yes={pos_yes_live}")
            return True
        except Exception as e:
            if is_rate_limited(e):
                arm_rate_limit_pause(f"positions_force_{tag}")
            log.warning(f"[INV] force-refresh positions failed ({tag}): {e}")
            return False

    def get_market_snapshot() -> Dict[str, Any]:
        nonlocal last_market_snapshot_at, last_market_snapshot
        now = time.time()
        if (now - last_market_snapshot_at) <= MARKET_SNAPSHOT_TTL_SECONDS and last_market_snapshot:
            return last_market_snapshot
        try:
            snap = client.request("GET", f"/markets/{active_market}")
            note_successful_request()
            last_market_snapshot = snap if isinstance(snap, dict) else {}
            last_market_snapshot_at = now
            return last_market_snapshot
        except Exception as e:
            if is_rate_limited(e):
                arm_rate_limit_pause("market_snapshot")
            return last_market_snapshot or {}

    def reconcile_quote_state_with_open_orders(tag: str) -> None:
        nonlocal quote, open_orders_cache, pause_until, skip_quote_until, order_posted_ts, last_reconcile_dbg_at

        if not active_market:
            return

        open_ids = set(open_order_ids_for_market_yes(open_orders_cache, active_market))

        # NEW: lifecycle open-list seen markers
        if LOG_ORDER_LIFECYCLE:
            for oid in open_ids:
                _mark_open_seen(oid)

        def recently_posted(oid: str) -> bool:
            ts = order_posted_ts.get(oid)
            if ts is None:
                return False
            return (time.time() - ts) < ORDERS_VISIBILITY_GRACE_SECONDS

        # NEW (tri-state preserved): (exists?, detail)
        def verify_detail(oid: str) -> Tuple[Optional[bool], Optional[Dict[str, Any]]]:
            if not USE_ORDER_DETAIL_FOR_RECONCILE:
                return None, None
            try:
                detail = get_order_by_id(client, oid)  # None means 404/not_found
                note_successful_request()
                if detail is not None:
                    _mark_detail_seen(oid, detail)
                    return True, detail
                return False, None
            except Exception as e:
                if is_rate_limited(e):
                    arm_rate_limit_pause("reconcile_order_detail")
                log.warning(f"[OM] reconcile({tag}): order-detail lookup failed for {oid}: {e}")
                return None, None

        def clear_local_and_pause(which: str, oid: str) -> None:
            nonlocal pause_until, skip_quote_until
            order_posted_ts.pop(oid, None)
            if which == "bid":
                quote.bid_order_id = None
                quote.bid_price = None
            else:
                quote.ask_order_id = None
                quote.ask_price = None

            _ = refresh_positions_now(f"reconcile_missing_{which}")
            pause_until = max(pause_until, time.time() + PAUSE_ON_UNKNOWN_SECONDS)
            skip_quote_until = max(skip_quote_until, time.time() + max(1.0, POSITIONS_POLL_SECONDS))

        now = time.time()
        if (now - last_reconcile_dbg_at) >= RECONCILE_DEBUG_THROTTLE_SECONDS:
            log.info(
                f"[OMDBG] reconcile({tag}) mkt={active_market} open_yes_ids={len(open_ids)} tracked=(bid={quote.bid_order_id}, ask={quote.ask_order_id}) "
                f"vis_grace={ORDERS_VISIBILITY_GRACE_SECONDS:.2f}s detail_verify={USE_ORDER_DETAIL_FOR_RECONCILE}"
            )
            last_reconcile_dbg_at = now

        if quote.bid_order_id:
            oid = quote.bid_order_id
            if oid not in open_ids:
                if recently_posted(oid):
                    age = time.time() - order_posted_ts.get(oid, time.time())
                    log.warning(
                        f"[OM] reconcile({tag}): bid not visible yet; waiting visibility_grace "
                        f"{ORDERS_VISIBILITY_GRACE_SECONDS:.2f}s (age={age:.2f}s order_id={oid})"
                    )
                else:
                    exists, _detail = verify_detail(oid)
                    if exists is True:
                        if LOG_RECONCILE_SNAPSHOT:
                            age = time.time() - order_posted_ts.get(oid, time.time())
                            log.info(
                                f"[OMSNP] {active_market} {tag} bid_missing_openlist keep_local "
                                f"order_id={oid} age_posted={age:.2f}s open_yes_ids={len(open_ids)} {_life_banner(oid)}"
                            )
                        log.warning(
                            f"[OM] reconcile({tag}): bid missing in open-list but EXISTS via order-detail; keeping local state (order_id={oid})"
                        )
                    elif exists is False:
                        if LOG_RECONCILE_SNAPSHOT:
                            age = time.time() - order_posted_ts.get(oid, time.time())
                            log.info(
                                f"[OMSNP] {active_market} {tag} bid_missing_openlist detail_404 -> clear_pause "
                                f"order_id={oid} age_posted={age:.2f}s open_yes_ids={len(open_ids)} {_life_banner(oid)}"
                            )
                        log.warning(
                            f"[OM] reconcile({tag}): bid missing (404 via order-detail) -> clearing local state (order_id={oid})"
                        )
                        clear_local_and_pause("bid", oid)
                    else:
                        pause_until = max(pause_until, time.time() + min(0.5, PAUSE_ON_UNKNOWN_SECONDS))
                        log.warning(
                            f"[OM] reconcile({tag}): bid missing in open-list; could not verify via detail -> pausing briefly (order_id={oid})"
                        )

        if quote.ask_order_id:
            oid = quote.ask_order_id
            if oid not in open_ids:
                if recently_posted(oid):
                    age = time.time() - order_posted_ts.get(oid, time.time())
                    log.warning(
                        f"[OM] reconcile({tag}): ask not visible yet; waiting visibility_grace "
                        f"{ORDERS_VISIBILITY_GRACE_SECONDS:.2f}s (age={age:.2f}s order_id={oid})"
                    )
                else:
                    exists, _detail = verify_detail(oid)
                    if exists is True:
                        if LOG_RECONCILE_SNAPSHOT:
                            age = time.time() - order_posted_ts.get(oid, time.time())
                            log.info(
                                f"[OMSNP] {active_market} {tag} ask_missing_openlist keep_local "
                                f"order_id={oid} age_posted={age:.2f}s open_yes_ids={len(open_ids)} {_life_banner(oid)}"
                            )
                        log.warning(
                            f"[OM] reconcile({tag}): ask missing in open-list but EXISTS via order-detail; keeping local state (order_id={oid})"
                        )
                    elif exists is False:
                        if LOG_RECONCILE_SNAPSHOT:
                            age = time.time() - order_posted_ts.get(oid, time.time())
                            log.info(
                                f"[OMSNP] {active_market} {tag} ask_missing_openlist detail_404 -> clear_pause "
                                f"order_id={oid} age_posted={age:.2f}s open_yes_ids={len(open_ids)} {_life_banner(oid)}"
                            )
                        log.warning(
                            f"[OM] reconcile({tag}): ask missing (404 via order-detail) -> clearing local state (order_id={oid})"
                        )
                        clear_local_and_pause("ask", oid)
                    else:
                        pause_until = max(pause_until, time.time() + min(0.5, PAUSE_ON_UNKNOWN_SECONDS))
                        log.warning(
                            f"[OM] reconcile({tag}): ask missing in open-list; could not verify via detail -> pausing briefly (order_id={oid})"
                        )

        if CLEAN_STRAY_ORDERS and active_market:
            tracked = set([oid for oid in [quote.bid_order_id, quote.ask_order_id] if oid])
            for o in open_orders_cache:
                if not is_yes_order_obj_for_market(o, active_market):
                    continue
                oid = o.get("order_id") or o.get("id")
                if not oid:
                    continue
                oid_s = str(oid)
                if oid_s in tracked:
                    continue
                try:
                    if not DRY_RUN:
                        st = cancel_order_status(client, oid_s)
                        mark_order_action()
                        note_successful_request()
                    else:
                        st = "canceled"
                    order_posted_ts.pop(oid_s, None)
                    if LOG_ORDER_LIFECYCLE:
                        m = _life(oid_s)
                        m["terminal"] = f"stray_cancel:{st}"
                    log.warning(f"[OM] reconcile({tag}): CANCEL stray YES order order_id={oid_s} status={st}")
                except Exception as ce:
                    if is_rate_limited(ce):
                        arm_rate_limit_pause("reconcile_cancel_stray")
                    log.warning(f"[OM] reconcile({tag}): failed to cancel stray order {oid_s}: {ce}")

    def cancel_all_open_yes_orders_for_active_market(reason: str) -> None:
        nonlocal quote, open_orders_cache, is_quoting, enter_ok_streak, exit_bad_streak, skip_quote_until, pause_until, order_posted_ts

        if not active_market:
            return

        try:
            open_orders_cache = get_open_orders(client)
            note_successful_request()
        except Exception as e:
            log.warning(f"[BOOT] cannot fetch open orders for cleanup: {e}")
            return

        killed = 0
        for o in open_orders_cache:
            if str(o.get("ticker")) != str(active_market):
                continue
            if str(o.get("side", "")).lower() != "yes":
                continue
            oid = o.get("order_id") or o.get("id")
            if not oid:
                continue
            try:
                if not DRY_RUN:
                    _ = cancel_order_status(client, str(oid))
                    mark_order_action()
                    note_successful_request()
                killed += 1
                order_posted_ts.pop(str(oid), None)
                if LOG_ORDER_LIFECYCLE:
                    m = _life(str(oid))
                    m["terminal"] = f"bootstrap_cancel:{reason}"
                log.info(f"[BOOT] {active_market} CANCEL leftover YES order ({reason}) order_id={oid}")
            except Exception as ce:
                if is_rate_limited(ce):
                    arm_rate_limit_pause("bootstrap_cancel")
                log.warning(f"[BOOT] cancel leftover failed: {ce}")

        quote = QuoteState()
        quote.last_market_ticker = active_market
        is_quoting = False
        enter_ok_streak = 0
        exit_bad_streak = 0

        skip_quote_until = max(skip_quote_until, time.time() + 1.0)
        pause_until = max(pause_until, time.time() + 0.5)

        if killed > 0:
            log.warning(f"[BOOT] cleaned {killed} leftover open orders for {active_market} ({reason})")

    # -----------------------------
    # CHANGE 1 applied here too:
    # cancel_*_only treats not_found as likely fill -> refresh + pause
    # -----------------------------
    def cancel_bid_only(reason: str) -> None:
        nonlocal quote, order_posted_ts, pause_until, skip_quote_until
        if not active_market:
            return
        if quote.bid_order_id:
            oid = quote.bid_order_id
            old_px = quote.bid_price
            try:
                if not DRY_RUN:
                    st = cancel_order_status(client, oid)
                    mark_order_action()
                    note_successful_request()
                else:
                    st = "canceled"
                log.info(f"[OM] {active_market} BUY CANCEL ({reason}) order_id={oid} status={st}")

                if st == "not_found":
                    if LOG_ORDER_LIFECYCLE:
                        m = _life(oid)
                        m["terminal"] = "cancel_not_found"
                    # CHANGE 1
                    order_posted_ts.pop(oid, None)
                    quote.bid_order_id = None
                    quote.bid_price = None

                    _ = refresh_positions_now("cancel_bid_only_404_not_found")
                    pause_until = max(pause_until, time.time() + PAUSE_ON_UNKNOWN_SECONDS)
                    skip_quote_until = max(skip_quote_until, time.time() + max(1.5, POSITIONS_POLL_SECONDS * 2.0))
                    log.warning(f"[OM] {active_market} cancel_bid_only got 404/not_found; pausing until inventory stabilizes.")
                    return

            except Exception as e:
                if is_rate_limited(e):
                    arm_rate_limit_pause("cancel_bid_only")
                log.warning(f"[OM] cancel bid failed ({reason}) order_id={oid} @ {old_px}: {e}")
            finally:
                order_posted_ts.pop(oid, None)
                quote.bid_order_id = None
                quote.bid_price = None

    def cancel_ask_only(reason: str) -> None:
        nonlocal quote, order_posted_ts, pause_until, skip_quote_until
        if not active_market:
            return
        if quote.ask_order_id:
            oid = quote.ask_order_id
            old_px = quote.ask_price
            try:
                if not DRY_RUN:
                    st = cancel_order_status(client, oid)
                    mark_order_action()
                    note_successful_request()
                else:
                    st = "canceled"
                log.info(f"[OM] {active_market} SELL CANCEL ({reason}) order_id={oid} status={st}")

                if st == "not_found":
                    if LOG_ORDER_LIFECYCLE:
                        m = _life(oid)
                        m["terminal"] = "cancel_not_found"
                    # CHANGE 1
                    order_posted_ts.pop(oid, None)
                    quote.ask_order_id = None
                    quote.ask_price = None

                    _ = refresh_positions_now("cancel_ask_only_404_not_found")
                    pause_until = max(pause_until, time.time() + PAUSE_ON_UNKNOWN_SECONDS)
                    skip_quote_until = max(skip_quote_until, time.time() + max(1.5, POSITIONS_POLL_SECONDS * 2.0))
                    log.warning(f"[OM] {active_market} cancel_ask_only got 404/not_found; pausing until inventory stabilizes.")
                    return

            except Exception as e:
                if is_rate_limited(e):
                    arm_rate_limit_pause("cancel_ask_only")
                log.warning(f"[OM] cancel ask failed ({reason}) order_id={oid} @ {old_px}: {e}")
            finally:
                order_posted_ts.pop(oid, None)
                quote.ask_order_id = None
                quote.ask_price = None

    def cancel_live_quotes(reason: str) -> None:
        cancel_bid_only(reason)
        cancel_ask_only(reason)

    def trip_balance_circuit(reason: str) -> None:
        nonlocal balance_fail_burst, balance_fail_until
        cancel_live_quotes(reason)
        balance_fail_burst += 1
        cooldown = BALANCE_FAIL_COOLDOWN_SECONDS
        if balance_fail_burst >= BALANCE_FAIL_MAX_BURST:
            cooldown = max(cooldown, BALANCE_FAIL_COOLDOWN_SECONDS * 5)
            log.warning(
                f"[BAL] balance_fail_burst={balance_fail_burst} reached max={BALANCE_FAIL_MAX_BURST}; extending cooldown to {cooldown:.1f}s"
            )
        balance_fail_until = time.time() + cooldown
        log.warning(f"[BAL] cooldown active for {cooldown:.1f}s ({reason})")

    def refresh_active_market() -> None:
        nonlocal active_event, active_market, active_market_obj

        prev_market = active_market

        if MARKET_OVERRIDE and MARKET_OVERRIDE not in ("<none>", "none", "None", ""):
            active_market = MARKET_OVERRIDE
            active_event = EVENT_TICKER if EVENT_TICKER != "<auto>" else "<manual>"
            active_market_obj = {}
            log.info(f"[ROLL] Series={SERIES_TICKER} → Market override={active_market}")
        else:
            params = {
                "series_ticker": SERIES_TICKER,
                "status": "open",
                "limit": 200,
                "mve_filter": "exclude",
            }
            resp = client.request("GET", "/markets", params=params)
            note_successful_request()
            markets = resp.get("markets", [])
            if not markets:
                raise RuntimeError(f"No open markets returned for series_ticker={SERIES_TICKER}")

            event_t, market_t, mobj = pick_active_market(markets)
            active_event = event_t
            active_market = market_t
            active_market_obj = mobj
            log.info(f"[ROLL] Series={SERIES_TICKER} → Active event={active_event} market={active_market} (via /markets series_ticker)")

        if BOOTSTRAP_CANCEL_OPEN_ORDERS and active_market and prev_market != active_market:
            cancel_all_open_yes_orders_for_active_market("market_roll_bootstrap")

    refresh_active_market()
    last_meta_refresh = time.time()

    if BOOTSTRAP_CANCEL_OPEN_ORDERS and active_market:
        cancel_all_open_yes_orders_for_active_market("startup_bootstrap")

    try:
        positions = get_positions(client)
        note_successful_request()
        pos_yes_live, realized_pnl_usd, fees_paid_usd = parse_position_for_market(positions, active_market)
        net_yes = pos_yes_live
        last_positions_poll = time.time()
        log.info(f"[INV] initial positions: market={active_market} pos_yes={pos_yes_live}")
    except Exception as e:
        log.warning(f"[INV] initial positions fetch failed: {e}")

    # NEW: Startup inherited state banner (ONLY LOGGING)
    if LOG_STARTUP_INHERITED_STATE and active_market:
        try:
            oo = get_open_orders(client)
            note_successful_request()
            ids = open_order_ids_for_market_yes(oo, active_market)
            log.warning(
                f"[BOOTSTATE] inherited market={active_market} pos_yes={pos_yes_live} open_yes_orders={len(ids)} "
                f"ids={ids[:10]}{'...' if len(ids) > 10 else ''}"
            )
        except Exception as e:
            log.warning(f"[BOOTSTATE] failed to fetch inherited open orders: {e}")

    # -----------------------------
    # CHANGE 3 (CRITICAL): Startup reconciliation from live exchange state
    # -----------------------------
    if active_market:
        try:
            open_orders_cache = get_open_orders(client)
            note_successful_request()

            # If we are NOT canceling on boot, adopt the best live YES bid/ask into QuoteState to avoid double-posting.
            if not BOOTSTRAP_CANCEL_OPEN_ORDERS:
                best_bid_oid = None
                best_bid_px = None
                best_ask_oid = None
                best_ask_px = None

                for o in open_orders_cache:
                    if not is_yes_order_obj_for_market(o, active_market):
                        continue
                    action = str(o.get("action", "")).lower()
                    px = safe_int(o.get("yes_price") or o.get("price"))
                    oid = o.get("order_id") or o.get("id")
                    if oid is None or px is None:
                        continue

                    if action == "buy":
                        if best_bid_px is None or px > best_bid_px:
                            best_bid_px = px
                            best_bid_oid = str(oid)
                    elif action == "sell":
                        if best_ask_px is None or px < best_ask_px:
                            best_ask_px = px
                            best_ask_oid = str(oid)

                if best_bid_oid is not None:
                    quote.bid_order_id = best_bid_oid
                    quote.bid_price = best_bid_px
                if best_ask_oid is not None:
                    quote.ask_order_id = best_ask_oid
                    quote.ask_price = best_ask_px

                quote.last_market_ticker = active_market

                log.warning(
                    f"[BOOTRECON] adopted live orders: market={active_market} "
                    f"bid={quote.bid_order_id}@{quote.bid_price} ask={quote.ask_order_id}@{quote.ask_price}"
                )
            else:
                # If we DID cancel on boot, ensure local quote state is clean.
                quote.last_market_ticker = active_market

            # Reconcile local view against open-list (and optionally order-detail), then pause briefly for stability.
            reconcile_quote_state_with_open_orders("startup_reconcile")
            _ = refresh_positions_now("startup_reconcile")
            pause_until = max(pause_until, time.time() + PAUSE_ON_UNKNOWN_SECONDS)
            skip_quote_until = max(skip_quote_until, time.time() + max(1.0, POSITIONS_POLL_SECONDS))
            log.warning(
                f"[BOOTRECON] complete: market={active_market} pos_yes={pos_yes_live} "
                f"pause={PAUSE_ON_UNKNOWN_SECONDS:.2f}s skip={max(1.0, POSITIONS_POLL_SECONDS):.2f}s"
            )
        except Exception as e:
            log.warning(f"[BOOTRECON] failed: {e}")

    while True:
        t0 = time.time()

        if (t0 - last_meta_refresh) >= META_REFRESH_SECONDS:
            try:
                refresh_active_market()
            except Exception as e:
                if is_rate_limited(e):
                    arm_rate_limit_pause("refresh_active_market")
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

        if secs_to_close is not None and secs_to_close <= CLOSEOUT_SECONDS:
            cancel_live_quotes("closeout")
            time.sleep(POLL_SECONDS)
            continue

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

        if (t0 - last_order_poll) >= ORDER_STATUS_POLL_SECONDS:
            try:
                open_orders_cache = get_open_orders(client)
                note_successful_request()
                last_order_poll = t0
                reconcile_quote_state_with_open_orders("orders_poll")
            except Exception as e:
                if is_rate_limited(e):
                    arm_rate_limit_pause("open_orders")
                pause_until = time.time() + PAUSE_ON_UNKNOWN_SECONDS
                cancel_live_quotes("unknown_order_state_pause")
                log.warning(f"[INV] PAUSE quoting due to unknown order state: {PAUSE_ON_UNKNOWN_SECONDS:.2f}s remaining ({e})")
                time.sleep(POLL_SECONDS)
                continue

        if (t0 - last_positions_poll) >= POSITIONS_POLL_SECONDS:
            try:
                positions = get_positions(client)
                note_successful_request()
                pos_yes_live, realized_pnl_usd, fees_paid_usd = parse_position_for_market(positions, active_market)
                net_yes = pos_yes_live
                last_positions_poll = t0
            except Exception as e:
                if is_rate_limited(e):
                    arm_rate_limit_pause("positions")
                log.warning(f"[INV] positions fetch failed: {e}")

        if (t0 - last_fills_poll) >= FILLS_POLL_SECONDS:
            try:
                fills = get_fills(client, min_ts_ms=last_fill_ts_ms, limit=200)
                note_successful_request()

                max_ts = last_fill_ts_ms or 0
                for f in fills:
                    ts = extract_fill_ts_ms(f)
                    if ts is not None and ts > max_ts:
                        max_ts = ts
                last_fill_ts_ms = max_ts if max_ts > 0 else last_fill_ts_ms

                new_entry = update_entry_from_fills_for_market(fills, active_market, pos_yes_live)
                if pos_yes_live == 0:
                    entry_yes_cents = None
                elif new_entry is not None:
                    entry_yes_cents = new_entry

                last_fills_poll = t0
            except Exception as e:
                if is_rate_limited(e):
                    arm_rate_limit_pause("fills")
                log.warning(f"[INV] fills fetch failed: {e}")

        if time.time() < pause_until or time.time() < balance_fail_until or time.time() < rl_until or time.time() < skip_quote_until:
            time.sleep(POLL_SECONDS)
            continue

        try:
            ob = client.request("GET", f"/markets/{active_market}/orderbook")
            note_successful_request()
        except Exception as e:
            if is_rate_limited(e):
                arm_rate_limit_pause("orderbook")
            log.warning(f"[OB] {active_market} orderbook fetch failed: {e}")
            time.sleep(POLL_SECONDS)
            continue

        yes_bid, yes_ask = parse_orderbook_yes_bid_ask(ob)

        used_snapshot_fallback = False
        if ORDERBOOK_FALLBACK_TO_MARKET_SNAPSHOT and (yes_bid is None or yes_ask is None):
            snap = get_market_snapshot()
            fb_bid, fb_ask = _extract_best_from_market_snapshot(snap)
            if yes_bid is None and fb_bid is not None:
                yes_bid = fb_bid
                used_snapshot_fallback = True
            if yes_ask is None and fb_ask is not None:
                yes_ask = fb_ask
                used_snapshot_fallback = True

            # CHANGE 5: sanity guard for terminal/invalid snapshot quotes + throttle warnings
            if used_snapshot_fallback and yes_bid is not None and yes_ask is not None:
                fb_sig = (active_market, "fallback", yes_bid, yes_ask)
                if (yes_bid >= yes_ask) or (yes_bid == 99 and yes_ask == 99):
                    msg = (
                        f"[OB] {active_market} snapshot fallback produced invalid/terminal quote: "
                        f"yes_bid={yes_bid} yes_ask={yes_ask} -> treating as no_book"
                    )
                    if fb_sig != last_ob_fb_sig or (t0 - last_ob_fb_log_at) >= OB_FALLBACK_LOG_THROTTLE_SECONDS:
                        log.warning(msg)
                        last_ob_fb_log_at = t0
                        last_ob_fb_sig = fb_sig
                    yes_bid, yes_ask = None, None
                else:
                    msg = f"[OB] {active_market} used market-snapshot fallback: yes_bid={yes_bid} yes_ask={yes_ask}"
                    if fb_sig != last_ob_fb_sig or (t0 - last_ob_fb_log_at) >= OB_FALLBACK_LOG_THROTTLE_SECONDS:
                        log.warning(msg)
                        last_ob_fb_log_at = t0
                        last_ob_fb_sig = fb_sig

        if yes_bid is None or yes_ask is None:
            cancel_live_quotes("no_yes_bid_or_ask")
            skip_quote_until = time.time() + SPREAD_SKIP_COOLDOWN_SECONDS
            sig = (active_market, "SKIP", "no_yes_bid_or_ask")
            if sig != last_target_sig or (t0 - last_target_log_at) >= TARGET_LOG_THROTTLE_SECONDS:
                log.info(f"[TARGET] {active_market} → SKIP (no_yes_bid_or_ask)")
                last_target_sig = sig
                last_target_log_at = t0
            time.sleep(POLL_SECONDS)
            continue

        mark_cents = None
        if pos_yes_live > 0:
            mark_cents = yes_bid
        elif pos_yes_live < 0:
            mark_cents = yes_ask

        unreal_usd = compute_unrealized_usd(pos_yes_live, entry_yes_cents, mark_cents)

        pnl_sig = (active_market, pos_yes_live, entry_yes_cents, mark_cents, realized_pnl_usd, fees_paid_usd, unreal_usd)
        if pnl_sig != last_pnl_sig or (t0 - last_pnl_log_at) >= PNL_LOG_THROTTLE_SECONDS:
            log.info(
                "[PNL] %s pos_yes=%d entry=%s mark=%s unreal=%s realized=%s fees=%s",
                active_market,
                pos_yes_live,
                (f"{entry_yes_cents}c" if entry_yes_cents is not None else "n/a"),
                (f"{mark_cents}c" if mark_cents is not None else "n/a"),
                (f"${unreal_usd:.4f}" if unreal_usd is not None else "n/a"),
                (f"${realized_pnl_usd:.4f}" if realized_pnl_usd is not None else "n/a"),
                (f"${fees_paid_usd:.4f}" if fees_paid_usd is not None else "n/a"),
            )
            last_pnl_sig = pnl_sig
            last_pnl_log_at = t0

        spread_now = yes_ask - yes_bid

        emergency_reduce_only = abs(pos_yes_live) > MAX_ABS_YES_CONTRACTS
        if emergency_reduce_only:
            sig = (active_market, pos_yes_live, MAX_ABS_YES_CONTRACTS)
            if sig != last_emergency_sig:
                log.error(
                    f"[FUSE] pos_yes={pos_yes_live} exceeds MAX_ABS_YES_CONTRACTS={MAX_ABS_YES_CONTRACTS}. Entering EMERGENCY reduce-only exit mode."
                )
                last_emergency_sig = sig

        if not (emergency_reduce_only and EMERGENCY_IGNORE_HYSTERESIS):
            if spread_now >= HYSTERESIS_ENTER_SPREAD_CENTS:
                enter_ok_streak += 1
            else:
                enter_ok_streak = 0

            if spread_now < HYSTERESIS_EXIT_SPREAD_CENTS:
                exit_bad_streak += 1
            else:
                exit_bad_streak = 0

            if not is_quoting:
                if enter_ok_streak >= SPREAD_ENTER_STREAK:
                    is_quoting = True
                    exit_bad_streak = 0
                else:
                    if quote.bid_order_id or quote.ask_order_id:
                        cancel_live_quotes("hysteresis_not_quoting")
                    sig = (active_market, "SKIP", "hysteresis_wait_enter", spread_now, yes_bid, yes_ask)
                    if sig != last_target_sig or (t0 - last_target_log_at) >= TARGET_LOG_THROTTLE_SECONDS:
                        log.info(
                            f"[TARGET] {active_market} → SKIP (hysteresis_wait_enter) spread={spread_now} best_yes_bid={yes_bid} best_yes_ask={yes_ask}"
                        )
                        last_target_sig = sig
                        last_target_log_at = t0
                    time.sleep(POLL_SECONDS)
                    continue
            else:
                if spread_now < HYSTERESIS_EXIT_SPREAD_CENTS and exit_bad_streak < SPREAD_EXIT_STREAK:
                    sig = (active_market, "HOLD", "hysteresis_hold_below_exit", spread_now, yes_bid, yes_ask)
                    if sig != last_target_sig or (t0 - last_target_log_at) >= TARGET_LOG_THROTTLE_SECONDS:
                        log.info(f"[TARGET] {active_market} → HOLD (hysteresis) spread={spread_now} best_yes_bid={yes_bid} best_yes_ask={yes_ask}")
                        last_target_sig = sig
                        last_target_log_at = t0
                    time.sleep(POLL_SECONDS)
                    continue

                if exit_bad_streak >= SPREAD_EXIT_STREAK:
                    is_quoting = False
                    cancel_live_quotes("hysteresis_exit_spread_too_tight")
                    skip_quote_until = time.time() + SPREAD_SKIP_COOLDOWN_SECONDS
                    sig = (active_market, "SKIP", "hysteresis_exit", spread_now, yes_bid, yes_ask)
                    if sig != last_target_sig or (t0 - last_target_log_at) >= TARGET_LOG_THROTTLE_SECONDS:
                        log.info(f"[TARGET] {active_market} → SKIP (hysteresis_exit) spread={spread_now} best_yes_bid={yes_bid} best_yes_ask={yes_ask}")
                        last_target_sig = sig
                        last_target_log_at = t0
                    time.sleep(POLL_SECONDS)
                    continue
        else:
            is_quoting = True

        open_buys, open_sells = count_open_yes_orders(open_orders_cache, active_market)
        est_net_yes = net_yes

        inv_age = (t0 - last_positions_poll) if last_positions_poll > 0 else 9999.0
        if inv_age > MAX_INV_STALENESS_SECONDS:
            cancel_live_quotes("inv_stale")
            skip_quote_until = max(skip_quote_until, time.time() + SPREAD_SKIP_COOLDOWN_SECONDS)
            sig = (active_market, "SKIP", "inv_stale")
            if sig != last_target_sig or (t0 - last_target_log_at) >= TARGET_LOG_THROTTLE_SECONDS:
                log.warning(f"[INV] stale positions: age={inv_age:.2f}s > {MAX_INV_STALENESS_SECONDS:.2f}s; canceling quotes")
                log.info(f"[TARGET] {active_market} → SKIP (inv_stale)")
                last_target_sig = sig
                last_target_log_at = t0
            time.sleep(POLL_SECONDS)
            continue

        reduce_only = (est_net_yes != 0)

        min_spread_for_quotes = HYSTERESIS_EXIT_SPREAD_CENTS if is_quoting else HYSTERESIS_ENTER_SPREAD_CENTS
        skew_net_for_pricing = 0 if reduce_only else est_net_yes

        if emergency_reduce_only:
            min_spread_for_quotes = 1

        bid_px, ask_px, why = compute_quotes(yes_bid, yes_ask, skew_net_for_pricing, min_spread_cents=min_spread_for_quotes)

        quote_sig = (active_market, bid_px, ask_px, why, est_net_yes)
        is_new_quote = (quote_sig != last_quote_sig)
        if is_new_quote:
            last_quote_sig = quote_sig

        if is_new_quote:
            min_reprice = REDUCE_ONLY_MIN_REPRICE_SECONDS if reduce_only else MIN_REPRICE_SECONDS
            if (time.time() - last_reprice_at) < min_reprice and not emergency_reduce_only:
                time.sleep(POLL_SECONDS)
                continue

        if bid_px is None or ask_px is None:
            cancel_live_quotes(f"skip:{why}")
            if not emergency_reduce_only:
                is_quoting = False
                enter_ok_streak = 0
                exit_bad_streak = 0
            skip_quote_until = time.time() + SPREAD_SKIP_COOLDOWN_SECONDS
            sig = (active_market, "SKIP", why, yes_bid, yes_ask)
            if sig != last_target_sig or (t0 - last_target_log_at) >= TARGET_LOG_THROTTLE_SECONDS:
                log.info(f"[TARGET] {active_market} → SKIP ({why}) best_yes_bid={yes_bid} best_yes_ask={yes_ask}")
                last_target_sig = sig
                last_target_log_at = t0
            time.sleep(POLL_SECONDS)
            continue

        if reduce_only:
            if est_net_yes > 0:
                allow_bid = False
                allow_ask = True
                why += " reduce_only(long_exit)"
            else:
                allow_bid = True
                allow_ask = False
                why += " reduce_only(short_exit)"
        else:
            allow_bid = (0 + open_buys + ORDER_QTY) <= MAX_ABS_YES_CONTRACTS
            allow_ask = (0 - open_sells - ORDER_QTY) >= -MAX_ABS_YES_CONTRACTS
            allow_bid = allow_bid and ((est_net_yes + open_buys) < MAX_NET_YES_CONTRACTS)
            allow_ask = allow_ask and ((est_net_yes - open_sells) > -MAX_NET_YES_CONTRACTS)

        if emergency_reduce_only and est_net_yes != 0:
            if est_net_yes > 0:
                if quote.bid_order_id:
                    cancel_bid_only("emergency_long_cancel_bids")
            else:
                if quote.ask_order_id:
                    cancel_ask_only("emergency_short_cancel_asks")

        if not allow_bid and quote.bid_order_id:
            cancel_bid_only("inventory_block_bid")
        if not allow_ask and quote.ask_order_id:
            cancel_ask_only("inventory_block_ask")

        if quote.last_market_ticker and quote.last_market_ticker != active_market:
            cancel_live_quotes("market_roll")
            quote = QuoteState()
            is_quoting = False
            enter_ok_streak = 0
            exit_bad_streak = 0

        quote.last_market_ticker = active_market

        enforce_two_sided_now = REQUIRE_TWO_SIDED_QUOTES and (not QUOTE_BOTH_WHEN_FLAT_ONLY or est_net_yes == 0)
        if enforce_two_sided_now and (not allow_bid or not allow_ask):
            if quote.bid_order_id or quote.ask_order_id:
                cancel_live_quotes("two_sided_required")
            time.sleep(POLL_SECONDS)
            continue

        reduce_qty = ORDER_QTY
        if reduce_only:
            desired = EMERGENCY_EXIT_QTY if emergency_reduce_only else ORDER_QTY
            desired = max(1, int(desired))
            reduce_qty = min(desired, abs(est_net_yes))
            reduce_qty = max(1, int(reduce_qty))

        want_bid_update = allow_bid and (quote.bid_price != bid_px)
        want_ask_update = allow_ask and (quote.ask_price != ask_px)

        if reduce_only:
            if est_net_yes > 0:
                if quote.ask_price is not None and ask_px is not None:
                    if abs(ask_px - quote.ask_price) < REDUCE_ONLY_REPRICE_TICKS and not emergency_reduce_only:
                        want_ask_update = False
            else:
                if quote.bid_price is not None and bid_px is not None:
                    if abs(bid_px - quote.bid_price) < REDUCE_ONLY_REPRICE_TICKS and not emergency_reduce_only:
                        want_bid_update = False

        def cancel_old_then_place_new(
            side: str,
            action: str,
            new_price: int,
            new_qty: int,
            old_order_id: Optional[str],
            old_price: Optional[int],
        ) -> Tuple[Optional[str], Optional[int], bool]:
            nonlocal skip_quote_until, pause_until, last_cancel_not_found_oid, quote, order_posted_ts

            last_cancel_not_found_oid = None

            if not ENABLE_TRADING or DRY_RUN:
                return None, new_price, True

            if old_order_id:
                try:
                    st = cancel_order_status(client, old_order_id)
                    mark_order_action()
                    note_successful_request()
                    tag2 = "BUY" if action == "buy" else "SELL"
                    log.info(f"[OM] {active_market} {tag2} CANCEL @{old_price} (reprice cancel-first) order_id={old_order_id} status={st}")
                    order_posted_ts.pop(old_order_id, None)
                    if LOG_ORDER_LIFECYCLE:
                        m = _life(old_order_id)
                        m["terminal"] = f"reprice_cancel:{st}"

                    if st == "not_found":
                        last_cancel_not_found_oid = old_order_id

                        if side == "bid":
                            quote.bid_order_id = None
                            quote.bid_price = None
                        else:
                            quote.ask_order_id = None
                            quote.ask_price = None

                        _ = refresh_positions_now("cancel_404_not_found")
                        pause_until = max(pause_until, time.time() + PAUSE_ON_UNKNOWN_SECONDS)
                        skip_quote_until = max(skip_quote_until, time.time() + max(1.5, POSITIONS_POLL_SECONDS * 2.0))
                        log.warning(
                            f"[OM] {active_market} cancel got 404/not_found; cleared local state, forced positions refresh, pausing until inventory stabilizes."
                        )
                        return None, None, False

                except Exception as ce:
                    if is_rate_limited(ce):
                        arm_rate_limit_pause(f"cancel_old_{side}")
                        return None, None, False
                    log.warning(f"[OM] cancel old {side} failed: {ce}")
                    return None, None, False

            try:
                payload = build_yes_order_payload(active_market, action, new_price, new_qty, POST_ONLY)
                new_oid = place_order(client, payload)
                mark_order_action()
                note_successful_request()
                order_posted_ts[new_oid] = time.time()

                # NEW: lifecycle on POSTED (ONLY LOGGING)
                if LOG_ORDER_LIFECYCLE:
                    m = _life(new_oid)
                    m["posted_ts"] = order_posted_ts[new_oid]
                    m["posted_px"] = new_price
                    m["posted_action"] = action
                    log.info(f"[LIFE] posted order_id={new_oid} action={action} px={new_price} qty={new_qty}")

                log.info(f"[OM] {active_market} {action.upper()} POSTED order_id={new_oid} @ {new_price} qty={new_qty}")
                return new_oid, new_price, True
            except Exception as e:
                if is_rate_limited(e):
                    arm_rate_limit_pause(f"place_{side}")
                    return None, None, False
                if is_post_only_cross(e):
                    skip_quote_until = time.time() + POST_ONLY_CROSS_COOLDOWN_SECONDS
                    log.warning(
                        f"[OM] {active_market} {action.upper()} place failed (post-only-cross). Cooldown {POST_ONLY_CROSS_COOLDOWN_SECONDS:.1f}s: {e}"
                    )
                    return None, None, False
                if is_insufficient_balance(e):
                    trip_balance_circuit(f"{side}_insufficient_balance")
                    log.warning(f"[OM] {active_market} {action.upper()} place failed: {e}")
                    return None, None, False
                log.warning(f"[OM] {active_market} {action.upper()} place failed: {e}")
                return None, None, False

        if want_bid_update or want_ask_update:
            if not can_do_order_action():
                time.sleep(POLL_SECONDS)
                continue

            last_reprice_at = time.time()

            abort_iteration = False

            if want_ask_update and allow_ask:
                new_oid, new_px, ok = cancel_old_then_place_new(
                    side="ask",
                    action="sell",
                    new_price=ask_px,
                    new_qty=(reduce_qty if reduce_only else ORDER_QTY),
                    old_order_id=quote.ask_order_id,
                    old_price=quote.ask_price,
                )

                # CHANGE 2: if cancel returned not_found, abort the rest of the loop's order actions
                if last_cancel_not_found_oid is not None:
                    abort_iteration = True
                elif ok:
                    quote.ask_order_id = new_oid
                    quote.ask_price = new_px

            if abort_iteration:
                time.sleep(POLL_SECONDS)
                continue

            if want_bid_update and allow_bid:
                new_oid, new_px, ok = cancel_old_then_place_new(
                    side="bid",
                    action="buy",
                    new_price=bid_px,
                    new_qty=(reduce_qty if reduce_only else ORDER_QTY),
                    old_order_id=quote.bid_order_id,
                    old_price=quote.bid_price,
                )

                if last_cancel_not_found_oid is not None:
                    abort_iteration = True
                elif ok:
                    quote.bid_order_id = new_oid
                    quote.bid_price = new_px

            if abort_iteration:
                time.sleep(POLL_SECONDS)
                continue

            if enforce_two_sided_now:
                has_bid = quote.bid_order_id is not None
                has_ask = quote.ask_order_id is not None
                if has_bid != has_ask:
                    cancel_live_quotes("atomic_two_sided_enforce")
                else:
                    if has_bid and has_ask:
                        balance_fail_burst = 0

        sig = (active_market, bid_px, ask_px, why)
        if sig != last_target_sig or (t0 - last_target_log_at) >= TARGET_LOG_THROTTLE_SECONDS:
            log.info(f"[TARGET] {active_market} → would_quote: bid@{bid_px} ask@{ask_px} ({why})")
            last_target_sig = sig
            last_target_log_at = t0

        if (t0 - last_state_log_at) >= STATE_LOG_SECONDS:
            b_vis = quote.bid_order_id is not None and quote.bid_order_id in set(open_order_ids_for_market_yes(open_orders_cache, active_market))
            a_vis = quote.ask_order_id is not None and quote.ask_order_id in set(open_order_ids_for_market_yes(open_orders_cache, active_market))
            log.info(
                f"[STATE] mkt={active_market} pos={pos_yes_live} spread={spread_now} best=({yes_bid},{yes_ask}) tgt=({bid_px},{ask_px}) "
                f"reduce_only={reduce_only} emergency={emergency_reduce_only} allow=(b:{allow_bid},a:{allow_ask}) open=(b:{open_buys},s:{open_sells}) "
                f"inv_age={max(0.0, t0 - last_positions_poll):.2f}s quote=(b:{quote.bid_order_id}@{quote.bid_price} vis={b_vis}, a:{quote.ask_order_id}@{quote.ask_price} vis={a_vis}) "
                f"cooldowns(pause={max(0.0, pause_until-time.time()):.2f} bal={max(0.0, balance_fail_until-time.time()):.2f} rl={max(0.0, rl_until-time.time()):.2f} skip={max(0.0, skip_quote_until-time.time()):.2f})"
            )
            last_state_log_at = t0

        dt = time.time() - t0
        time.sleep(max(0.0, POLL_SECONDS - dt))


if __name__ == "__main__":
    main()