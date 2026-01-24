# bot.py
# Kalshi YES-only rolling 15m market maker
# - Rolls to the current active event/market for a SERIES (via /markets?series=...)
# - Quotes both sides (YES bid + YES ask) as post-only maker orders
# - Inventory-aware skew (net YES contracts)
# - “Do not improve” micro-logic when spread is tight
# - Optional spot-guard (skips quoting if market looks “resolved” vs spot)
#
# Env vars (most important):
#   KALSHI_API_BASE=https://api.elections.kalshi.com
#   KALSHI_API_KEY_ID=...
#   KALSHI_PRIVATE_KEY_PEM_BASE64=...   (base64 of your RSA private key PEM)
#
#   SERIES=KXBTC15M
#   EVENT_TICKER=              (optional override; else auto)
#   MARKET_OVERRIDE=           (optional override market ticker)
#
#   POLL_SECONDS=0.20
#   META_REFRESH_SECONDS=30
#   DRY_RUN=false
#   ENABLE_TRADING=true
#   POST_ONLY=true
#
# Quoting / sizing:
#   ORDER_QTY=1
#   REPRICE_TOLERANCE_CENTS=1
#   MAX_NET_YES_CONTRACTS=2
#   INVENTORY_SKEW_CENTS=1
#   NO_IMPROVE_MAX_SPREAD_CENTS=4
#
# Safety:
#   ORDER_STATUS_POLL_SECONDS=1.0
#   PAUSE_ON_UNKNOWN_SECONDS=0.75
#
# Spot guard:
#   ENABLE_SPOT_GUARD=true
#   SPOT_POLL_SECONDS=12
#   SPOT_RESOLVED_BUFFER_USD=75
#   CLOSEOUT_SECONDS=20

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
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Env helpers
# -----------------------------
load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return int(v.strip())


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return float(v.strip())


def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None:
        return default
    s = str(v).strip()
    return s if s != "" else default


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------
# Config
# -----------------------------
API_BASE = env_str("KALSHI_API_BASE", "https://api.elections.kalshi.com")
API_PREFIX = env_str("KALSHI_API_PREFIX", "/trade-api/v2")

# Normalize trailing slash issues (your logs show both styles)
API_BASE = API_BASE.rstrip("/")

SERIES = env_str("SERIES", "KXBTC15M")
EVENT_TICKER_OVERRIDE = env_str("EVENT_TICKER", "")
MARKET_OVERRIDE = env_str("MARKET_OVERRIDE", "")

POLL_SECONDS = env_float("POLL_SECONDS", 0.20)
META_REFRESH_SECONDS = env_float("META_REFRESH_SECONDS", 30.0)

DRY_RUN = env_bool("DRY_RUN", False)
ENABLE_TRADING = env_bool("ENABLE_TRADING", True)
POST_ONLY = env_bool("POST_ONLY", True)

ORDER_QTY = env_int("ORDER_QTY", 1)
REPRICE_TOLERANCE_CENTS = env_int("REPRICE_TOLERANCE_CENTS", 1)

MAX_NET_YES_CONTRACTS = env_int("MAX_NET_YES_CONTRACTS", 2)
INVENTORY_SKEW_CENTS = env_int("INVENTORY_SKEW_CENTS", 1)

NO_IMPROVE_MAX_SPREAD_CENTS = env_int("NO_IMPROVE_MAX_SPREAD_CENTS", 4)

ORDER_STATUS_POLL_SECONDS = env_float("ORDER_STATUS_POLL_SECONDS", 1.00)
PAUSE_ON_UNKNOWN_SECONDS = env_float("PAUSE_ON_UNKNOWN_SECONDS", 0.75)

ENABLE_SPOT_GUARD = env_bool("ENABLE_SPOT_GUARD", True)
SPOT_POLL_SECONDS = env_float("SPOT_POLL_SECONDS", 12.0)
SPOT_RESOLVED_BUFFER_USD = env_float("SPOT_RESOLVED_BUFFER_USD", 75.0)
CLOSEOUT_SECONDS = env_float("CLOSEOUT_SECONDS", 20.0)

# Auth
KALSHI_KEY_ID = env_str("KALSHI_API_KEY_ID", "")
KALSHI_PRIV_PEM_B64 = env_str("KALSHI_PRIVATE_KEY_PEM_BASE64", "")

if not KALSHI_KEY_ID or not KALSHI_PRIV_PEM_B64:
    log.warning("[ENV] Missing KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_PEM_BASE64")

detected_kalshi = sorted([k for k in os.environ.keys() if k.startswith("KALSHI_")])
log.info(f"[ENV] Detected KALSHI_* keys: {detected_kalshi}")

log.info(
    f"API_BASE={API_BASE} API_PREFIX={API_PREFIX} SERIES={SERIES} "
    f"EVENT_TICKER={'<auto>' if not EVENT_TICKER_OVERRIDE else EVENT_TICKER_OVERRIDE} "
    f"MARKET_OVERRIDE={'<none>' if not MARKET_OVERRIDE else MARKET_OVERRIDE} "
    f"POLL={POLL_SECONDS:.2f}s DRY_RUN={DRY_RUN} ENABLE_TRADING={ENABLE_TRADING} POST_ONLY={POST_ONLY}"
)
log.info(
    f"[INV] MAX_NET_YES_CONTRACTS={MAX_NET_YES_CONTRACTS} INVENTORY_SKEW_CENTS={INVENTORY_SKEW_CENTS} "
    f"ORDER_STATUS_POLL_SECONDS={ORDER_STATUS_POLL_SECONDS:.2f} PAUSE_ON_UNKNOWN_SECONDS={PAUSE_ON_UNKNOWN_SECONDS:.2f}"
)
log.info(
    f"[MICRO] NO_IMPROVE_MAX_SPREAD_CENTS={NO_IMPROVE_MAX_SPREAD_CENTS} (<= this spread: join, do not improve)"
)
log.info(
    f"[SPOT] ENABLE_SPOT_GUARD={ENABLE_SPOT_GUARD} SPOT_POLL_SECONDS={SPOT_POLL_SECONDS:.1f} "
    f"CLOSEOUT_SECONDS={CLOSEOUT_SECONDS:.1f} SPOT_RESOLVED_BUFFER_USD={SPOT_RESOLVED_BUFFER_USD:g} META_REFRESH={META_REFRESH_SECONDS:.1f}"
)


# -----------------------------
# Signing (Kalshi RSA-PSS)
# -----------------------------
def load_private_key() -> Any:
    raw = base64.b64decode(KALSHI_PRIV_PEM_B64.encode("utf-8"))
    return serialization.load_pem_private_key(raw, password=None)


_PRIVATE_KEY = None
if KALSHI_PRIV_PEM_B64:
    try:
        _PRIVATE_KEY = load_private_key()
    except Exception as e:
        log.error(f"[AUTH] Failed to load private key: {e}")
        _PRIVATE_KEY = None


def sign_pss_sha256(message: bytes) -> str:
    """
    Produces base64 signature.
    Official docs: KALSHI-ACCESS-SIGNATURE is RSA-PSS signature of the request.
    Kalshi quick-start examples commonly sign: timestamp + method + path (no body).
    """
    if _PRIVATE_KEY is None:
        raise RuntimeError("Private key not loaded")
    sig = _PRIVATE_KEY.sign(
        message,
        asy_padding.PSS(mgf=asy_padding.MGF1(hashes.SHA256()), salt_length=asy_padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def make_headers(method: str, path: str) -> Dict[str, str]:
    ts = str(now_ms())
    # IMPORTANT: Kalshi examples frequently sign timestamp+method+path_without_query
    path_no_query = path.split("?", 1)[0]
    to_sign = (ts + method.upper() + path_no_query).encode("utf-8")
    signature = sign_pss_sha256(to_sign)
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


# -----------------------------
# HTTP
# -----------------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "kalshi-btc-mm/1.0"})


def api_url(path: str) -> str:
    return f"{API_BASE}{API_PREFIX}{path}"


def request_json(method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Any = None) -> Dict[str, Any]:
    url = api_url(path)
    if params:
        url = url + ("?" + urlencode(params))
        path = path + ("?" + urlencode(params))

    headers = make_headers(method, path)

    data = None
    if body is not None:
        data = json.dumps(body)

    log.debug(f"[REQ] {method.upper()} {path}")
    r = SESSION.request(method.upper(), url, headers=headers, data=data, timeout=10)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path}: {r.text}")
    if r.text.strip() == "":
        return {}
    return r.json()


# -----------------------------
# Kalshi API helpers
# -----------------------------
def get_markets_for_series(series: str, limit: int = 200) -> List[Dict[str, Any]]:
    # Uses /markets?series=XYZ (your earlier fix)
    j = request_json("GET", "/markets", params={"series": series, "limit": limit})
    # Response typically has {"markets":[...]} or {"markets":[...], ...}
    mkts = j.get("markets") or []
    return mkts


def get_market(market_ticker: str) -> Dict[str, Any]:
    j = request_json("GET", f"/markets/{market_ticker}")
    return j.get("market") or j


def get_orderbook(market_ticker: str) -> Dict[str, Any]:
    j = request_json("GET", f"/markets/{market_ticker}/orderbook")
    return j


def get_open_orders(limit: int = 200) -> List[Dict[str, Any]]:
    j = request_json("GET", "/portfolio/orders", params={"limit": limit, "status": "open"})
    # Response often {"orders":[...]}
    return j.get("orders") or []


def cancel_order(order_id: str) -> None:
    request_json("DEL", f"/portfolio/orders/{order_id}")


def create_yes_limit_order(market_ticker: str, action: str, yes_price: int, count: int, client_order_id: str) -> Dict[str, Any]:
    body = {
        "ticker": market_ticker,
        "side": "yes",
        "action": action,  # "buy" or "sell"
        "type": "limit",
        "yes_price": int(yes_price),
        "count": int(count),
        "client_order_id": client_order_id,
        "post_only": bool(POST_ONLY),
    }
    j = request_json("POST", "/portfolio/orders", body=body)
    return j.get("order") or j


def get_positions(limit: int = 200) -> List[Dict[str, Any]]:
    j = request_json("GET", "/portfolio/positions", params={"limit": limit})
    return j.get("positions") or []


# -----------------------------
# Spot (BTC-USD) for guard
# -----------------------------
_LAST_SPOT_TS = 0.0
_LAST_SPOT = None


def fetch_spot_btc_usd() -> Optional[float]:
    # Coinbase public endpoint (no auth)
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=6)
        if r.status_code != 200:
            return None
        j = r.json()
        amt = j.get("data", {}).get("amount")
        if amt is None:
            return None
        return float(amt)
    except Exception:
        return None


def get_spot_cached() -> Optional[float]:
    global _LAST_SPOT_TS, _LAST_SPOT
    now = time.time()
    if _LAST_SPOT is None or (now - _LAST_SPOT_TS) >= SPOT_POLL_SECONDS:
        s = fetch_spot_btc_usd()
        if s is not None:
            _LAST_SPOT = s
            _LAST_SPOT_TS = now
    return _LAST_SPOT


# -----------------------------
# Parsing
# -----------------------------
def _best_from_levels(levels: Any, want_best: str) -> Optional[int]:
    """
    levels is expected to be a list like [[price, qty], ...] or [{"price":..,"size":..}, ...]
    want_best: "bid" or "ask"
    Returns best price in cents (int).
    """
    if not levels:
        return None
    # Normalize to list of (price, qty)
    norm: List[Tuple[int, float]] = []
    for lv in levels:
        if isinstance(lv, (list, tuple)) and len(lv) >= 1:
            try:
                p = int(lv[0])
                q = float(lv[1]) if len(lv) > 1 else 0.0
                norm.append((p, q))
            except Exception:
                continue
        elif isinstance(lv, dict):
            if "price" in lv:
                try:
                    p = int(lv["price"])
                    q = float(lv.get("size", lv.get("quantity", 0.0)))
                    norm.append((p, q))
                except Exception:
                    continue
    if not norm:
        return None
    # bids: best = max price; asks: best = min price
    if want_best == "bid":
        return max(p for p, _ in norm)
    return min(p for p, _ in norm)


def parse_yes_best_bid_ask(orderbook_json: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Tries common Kalshi shapes:
      - {"orderbook":{"yes":{"bids":[[p,q]],"asks":[[p,q]]}, "no":...}}
      - {"orderbook":{"bids":[...], "asks":[...]} with 'side' info}
      - {"yes":[...]} etc
    """
    ob = orderbook_json.get("orderbook", orderbook_json)

    # Most common:
    yes = ob.get("yes")
    if isinstance(yes, dict):
        best_bid = _best_from_levels(yes.get("bids"), "bid")
        best_ask = _best_from_levels(yes.get("asks"), "ask")
        return best_bid, best_ask

    # Alternate: top-level bids/asks with side tagged
    if "bids" in ob or "asks" in ob:
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []

        def filt(levels: List[Any]) -> List[Any]:
            out = []
            for lv in levels:
                if isinstance(lv, dict) and lv.get("side") == "yes":
                    out.append([lv.get("price"), lv.get("size", 0)])
                elif isinstance(lv, (list, tuple)) and len(lv) >= 3:
                    # maybe [price, qty, side]
                    if str(lv[2]).lower() == "yes":
                        out.append([lv[0], lv[1]])
            return out

        yes_bids = filt(bids)
        yes_asks = filt(asks)
        best_bid = _best_from_levels(yes_bids, "bid")
        best_ask = _best_from_levels(yes_asks, "ask")
        return best_bid, best_ask

    return None, None


def extract_strike_like_usd(market_meta: Dict[str, Any]) -> Optional[Tuple[float, str]]:
    """
    Best-effort extraction of a “strike-like” value for spot guard.
    If we cannot find anything reliable, we return None and the spot guard will not block trading.
    """
    m = market_meta or {}

    # Common candidates
    candidates = [
        ("strike_price", m.get("strike_price")),
        ("strike", m.get("strike")),
        ("floor_strike", m.get("floor_strike")),
        ("cap_strike", m.get("cap_strike")),
        ("floor_strike_price", m.get("floor_strike_price")),
        ("cap_strike_price", m.get("cap_strike_price")),
    ]

    # If floor/cap exist, prefer the nearest boundary logic
    floor = m.get("floor_strike") or m.get("floor_strike_price")
    cap = m.get("cap_strike") or m.get("cap_strike_price")

    def to_float(x: Any) -> Optional[float]:
        if x is None:
            return None
        try:
            return float(x)
        except Exception:
            return None

    f = to_float(floor)
    c = to_float(cap)
    if f is not None and c is not None and f > 0 and c > 0:
        # Heuristic: if values look like cents (< 1000) but spot is huge, they are not USD strikes.
        # Still return something labeled "range" so we can decide intelligently later.
        mid = (f + c) / 2.0
        return mid, "range_mid"

    for name, val in candidates:
        v = to_float(val)
        if v is None:
            continue
        if v <= 0:
            continue
        return v, name

    return None


# -----------------------------
# Strategy helpers
# -----------------------------
def compute_targets(best_bid: Optional[int], best_ask: Optional[int], net_yes: int) -> Tuple[Optional[int], Optional[int], str]:
    """
    Returns (our_bid, our_ask, reason).
    """
    if best_bid is None or best_ask is None:
        return None, None, "no_orderbook_side"

    spread = best_ask - best_bid
    if spread <= 0:
        return None, None, f"crossed_or_locked(spread={spread})"

    # Micro behavior: if spread is tight, join; else improve by 1c
    if spread <= NO_IMPROVE_MAX_SPREAD_CENTS:
        bid = best_bid
        ask = best_ask
        micro = f"join(spread={spread})"
    else:
        bid = best_bid + 1
        ask = best_ask - 1
        if ask <= bid:
            bid = best_bid
            ask = best_ask
            micro = f"join_due_to_cross(spread={spread})"
        else:
            micro = f"improve(spread={spread})"

    # Inventory skew (YES-only): long YES => discourage buying & encourage selling
    # net_yes > 0 => shift both bid/ask down; net_yes < 0 => shift both up
    skew = 0
    if net_yes > 0:
        skew = -INVENTORY_SKEW_CENTS
    elif net_yes < 0:
        skew = +INVENTORY_SKEW_CENTS

    bid += skew
    ask += skew

    bid = clamp(bid, 1, 99)
    ask = clamp(ask, 1, 99)

    if ask <= bid:
        return None, None, f"invalid_after_skew(bid={bid},ask={ask},{micro},skew={skew})"

    inv_note = f"skew_long_yes({skew})" if skew != 0 else "skew(0)"
    return bid, ask, f"ok({micro} {inv_note})"


def get_net_yes_for_market(market_ticker: str) -> int:
    """
    Best-effort: looks up positions for this market ticker and returns net YES contracts.
    If not found, returns 0.
    """
    try:
        positions = get_positions(limit=200)
        for p in positions:
            if p.get("ticker") == market_ticker:
                # common fields: "position" or "yes_position"/"no_position"
                if "position" in p:
                    return int(p["position"])
                if "yes_position" in p:
                    return int(p["yes_position"])
        return 0
    except Exception:
        return 0


@dataclass
class WorkingOrders:
    buy_order_id: Optional[str] = None
    buy_price: Optional[int] = None
    sell_order_id: Optional[str] = None
    sell_price: Optional[int] = None
    last_refresh_ts: float = 0.0


def refresh_working_orders(market_ticker: str) -> WorkingOrders:
    """
    Rebuild working order state from open orders on the account for this market.
    """
    wo = WorkingOrders()
    try:
        orders = get_open_orders(limit=200)
        for o in orders:
            if o.get("ticker") != market_ticker:
                continue
            if o.get("side") != "yes":
                continue
            action = o.get("action")
            order_id = o.get("order_id")
            yes_price = o.get("yes_price")
            if action == "buy":
                wo.buy_order_id = order_id
                wo.buy_price = int(yes_price) if yes_price is not None else None
            elif action == "sell":
                wo.sell_order_id = order_id
                wo.sell_price = int(yes_price) if yes_price is not None else None
    except Exception:
        pass
    wo.last_refresh_ts = time.time()
    return wo


def cancel_if_needed(order_id: Optional[str], side_label: str, market_ticker: str, price: Optional[int], target: Optional[int]) -> bool:
    """
    Returns True if we canceled.
    """
    if not order_id or price is None or target is None:
        return False
    off_by = abs(price - target)
    if off_by <= REPRICE_TOLERANCE_CENTS:
        return False
    if DRY_RUN or not ENABLE_TRADING:
        log.info(f"[OM] {market_ticker} {side_label.upper()} CANCEL @{price} (reprice(off_by={off_by})) DRY_RUN=True")
        return True
    log.info(f"[OM] {market_ticker} {side_label.upper()} CANCEL @{price} (reprice(off_by={off_by})) DRY_RUN=False")
    cancel_order(order_id)
    return True


def place_if_needed(action: str, market_ticker: str, target_price: Optional[int], existing_order_id: Optional[str]) -> Optional[str]:
    if target_price is None:
        return None
    if existing_order_id:
        return existing_order_id

    client_order_id = f"mm-{action}-{int(time.time()*1000)}"
    if DRY_RUN or not ENABLE_TRADING:
        log.info(f"[OM] {market_ticker} {action.upper()} PLACE @{target_price} qty={ORDER_QTY} DRY_RUN=True")
        return "DRY_RUN_ORDER_ID"
    log.info(f"[OM] {market_ticker} {action.upper()} PLACE @{target_price} qty={ORDER_QTY} DRY_RUN=False")
    o = create_yes_limit_order(market_ticker, action=action, yes_price=target_price, count=ORDER_QTY, client_order_id=client_order_id)
    oid = o.get("order_id")
    log.info(f"[OM] {market_ticker} {action.upper()} POSTED order_id={oid} @ {target_price} qty={ORDER_QTY}")
    return oid


# -----------------------------
# Rolling logic
# -----------------------------
def pick_active_market_for_series(series: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (event_ticker, market_ticker).
    Uses /markets?series=... and selects an open market with the soonest close time (or best guess).
    If EVENT_TICKER override is set, prefers that.
    If MARKET_OVERRIDE is set, returns that.
    """
    if MARKET_OVERRIDE:
        # Derive event ticker as "everything except last -chunk"
        evt = "-".join(MARKET_OVERRIDE.split("-")[:-1]) if "-" in MARKET_OVERRIDE else ""
        return evt, MARKET_OVERRIDE

    mkts = get_markets_for_series(series, limit=200)

    # Filter open-ish markets
    openish = []
    for m in mkts:
        status = str(m.get("status", "")).lower()
        if status in ("open", "active", "trading", ""):
            openish.append(m)

    if not openish:
        openish = mkts

    # If user forces an event, keep only that event
    if EVENT_TICKER_OVERRIDE:
        openish = [m for m in openish if str(m.get("event_ticker", "")) == EVENT_TICKER_OVERRIDE] or openish

    # Pick a market with the nearest close time in the future, else fallback first
    now = datetime.now(timezone.utc)

    def parse_time(s: Any) -> Optional[datetime]:
        if not s:
            return None
        try:
            # examples are often ISO timestamps
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None

    scored = []
    for m in openish:
        ct = parse_time(m.get("close_time") or m.get("close_ts") or m.get("expiration_time"))
        # Some market lists may not include close; score by 0 then.
        if ct is None:
            score = 999999999.0
        else:
            dt = (ct - now).total_seconds()
            # prefer small positive; if negative, shove back
            score = dt if dt >= 0 else (999999999.0 + abs(dt))
        scored.append((score, m))

    scored.sort(key=lambda x: x[0])
    chosen = scored[0][1] if scored else None
    if not chosen:
        return None, None

    market_ticker = chosen.get("ticker") or chosen.get("market_ticker")
    event_ticker = chosen.get("event_ticker")
    if not event_ticker and market_ticker and "-" in market_ticker:
        event_ticker = "-".join(market_ticker.split("-")[:-1])

    return event_ticker, market_ticker


# -----------------------------
# Spot guard decision
# -----------------------------
def should_skip_for_spot_guard(market_ticker: str, market_meta: Dict[str, Any]) -> Tuple[bool, str]:
    if not ENABLE_SPOT_GUARD:
        return False, "spot_guard_disabled"

    spot = get_spot_cached()
    if spot is None:
        return False, "no_spot"

    strike_info = extract_strike_like_usd(market_meta)
    if strike_info is None:
        # If we can't reliably infer a strike/range midpoint, do NOT block trading.
        return False, "no_strike_info"

    strike_usd, label = strike_info

    # If strike value looks obviously not in USD (e.g., < 1000 while BTC spot is huge),
    # do NOT block trading (this is the exact failure mode that causes infinite SKIP).
    if strike_usd < 1000 and spot > 1000:
        return False, f"strike_not_usd({label}={strike_usd:g})"

    dist = abs(spot - strike_usd)
    if dist >= SPOT_RESOLVED_BUFFER_USD:
        return True, f"spot_resolved(|spot-strike|={int(dist)}>=BUFFER={int(SPOT_RESOLVED_BUFFER_USD)})"
    return False, f"spot_ok(|spot-strike|={dist:.1f}<BUFFER={SPOT_RESOLVED_BUFFER_USD:g})"


# -----------------------------
# Main loop
# -----------------------------
def main() -> None:
    current_event = None
    current_market = None
    last_meta_refresh = 0.0
    market_meta: Dict[str, Any] = {}
    working = WorkingOrders()
    pause_until = 0.0

    while True:
        t0 = time.time()

        try:
            # Roll / pick active market
            evt, mkt = pick_active_market_for_series(SERIES)
            if not evt or not mkt:
                log.info(f"[ROLL] Series={SERIES} → no active market found (via /markets)")
                time.sleep(max(POLL_SECONDS, 0.5))
                continue

            if (evt != current_event) or (mkt != current_market):
                log.info(f"[ROLL] Series={SERIES} → Active event={evt} market={mkt} (via /markets)")
                # If market changed, clear working orders state (and cancel any old orders on previous market)
                if current_market and current_market != mkt:
                    # cancel old working orders (best-effort)
                    if working.buy_order_id and working.buy_order_id != "DRY_RUN_ORDER_ID":
                        try:
                            if not DRY_RUN and ENABLE_TRADING:
                                cancel_order(working.buy_order_id)
                        except Exception:
                            pass
                    if working.sell_order_id and working.sell_order_id != "DRY_RUN_ORDER_ID":
                        try:
                            if not DRY_RUN and ENABLE_TRADING:
                                cancel_order(working.sell_order_id)
                        except Exception:
                            pass
                    log.info(f"[OM] {mkt} ROLL detected → cleared working orders (DRY_RUN={DRY_RUN})")

                current_event, current_market = evt, mkt
                working = WorkingOrders()

                # force meta refresh right away
                last_meta_refresh = 0.0

            # Refresh meta occasionally
            if (time.time() - last_meta_refresh) >= META_REFRESH_SECONDS:
                try:
                    market_meta = get_market(current_market)
                except Exception:
                    market_meta = {}
                last_meta_refresh = time.time()

            # Pause if we are in unknown state cooldown
            if time.time() < pause_until:
                time.sleep(POLL_SECONDS)
                continue

            # Spot guard
            skip, reason = should_skip_for_spot_guard(current_market, market_meta)
            if skip:
                log.info(f"[TARGET] {current_market} → SKIP ({reason})")
                time.sleep(POLL_SECONDS)
                continue

            # Orderbook
            ob = get_orderbook(current_market)
            best_bid, best_ask = parse_yes_best_bid_ask(ob)
            if best_bid is None or best_ask is None:
                log.info(f"[TARGET] {current_market} → SKIP (no_yes_bid_or_ask)")
                time.sleep(POLL_SECONDS)
                continue

            log.info(f"[QUOTE] {current_market} YES bid={best_bid} ask={best_ask}")

            # Inventory
            net_yes = get_net_yes_for_market(current_market)

            # Inventory cap: if too long, do not place buy; if too short, do not place sell
            allow_buy = net_yes < MAX_NET_YES_CONTRACTS
            allow_sell = net_yes > -MAX_NET_YES_CONTRACTS  # symmetric

            # Targets
            bid_tgt, ask_tgt, why = compute_targets(best_bid, best_ask, net_yes)

            if bid_tgt is None or ask_tgt is None:
                log.info(f"[TARGET] {current_market} → SKIP ({why})")
                time.sleep(POLL_SECONDS)
                continue

            # Apply inventory cap
            if not allow_buy:
                bid_tgt = None
            if not allow_sell:
                ask_tgt = None

            log.info(
                f"[TARGET] {current_market} → {'YES-only would_quote:'} "
                f"bid@{bid_tgt if bid_tgt is not None else 'OFF'} "
                f"ask@{ask_tgt if ask_tgt is not None else 'OFF'} "
                f"({why}) DRY_RUN={DRY_RUN}"
            )

            # Refresh working orders periodically
            if (time.time() - working.last_refresh_ts) >= ORDER_STATUS_POLL_SECONDS:
                working = refresh_working_orders(current_market)

            # If we can't determine order state (rare), pause briefly (matches your “unknown order state” pattern)
            # (Here, we treat “DRY_RUN_ORDER_ID” as known.)
            unknown_state = False
            if working.buy_order_id == "DRY_RUN_ORDER_ID" or working.sell_order_id == "DRY_RUN_ORDER_ID":
                unknown_state = False

            if unknown_state:
                pause_until = time.time() + PAUSE_ON_UNKNOWN_SECONDS
                log.warning(f"[INV] PAUSE quoting due to unknown order state: {pause_until - time.time():.2f}s remaining")
                time.sleep(POLL_SECONDS)
                continue

            # Reprice / cancel
            buy_canceled = cancel_if_needed(
                working.buy_order_id if working.buy_order_id != "DRY_RUN_ORDER_ID" else None,
                "buy",
                current_market,
                working.buy_price,
                bid_tgt,
            )
            if buy_canceled:
                working.buy_order_id = None
                working.buy_price = None

            sell_canceled = cancel_if_needed(
                working.sell_order_id if working.sell_order_id != "DRY_RUN_ORDER_ID" else None,
                "sell",
                current_market,
                working.sell_price,
                ask_tgt,
            )
            if sell_canceled:
                working.sell_order_id = None
                working.sell_price = None

            # Place if missing
            if bid_tgt is not None:
                working.buy_order_id = place_if_needed("buy", current_market, bid_tgt, working.buy_order_id)
                working.buy_price = bid_tgt
            if ask_tgt is not None:
                working.sell_order_id = place_if_needed("sell", current_market, ask_tgt, working.sell_order_id)
                working.sell_price = ask_tgt

        except Exception as e:
            log.error(f"[LOOPERR] {repr(e)}")
            time.sleep(0.5)

        # pacing
        dt = time.time() - t0
        sleep_for = max(0.0, POLL_SECONDS - dt)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()