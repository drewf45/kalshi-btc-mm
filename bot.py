# bot.py
# Kalshi YES-only rolling market maker (post-only)
#
# ✅ FIX (your current issue): the "Get Markets" endpoint expects query param `series_ticker`,
#    NOT `series`. Using `series` gets ignored and you end up rolling into a random market
#    (like the sports multivariate one you saw in logs).
#
# ✅ FIX (your other issue): orderbook endpoint is bids-only. We first use /markets/{ticker}
#    (which includes yes_bid/yes_ask/no_bid/no_ask). If missing, we infer YES ask from NO bids
#    (YES_ASK = 100 - NO_BID) and YES bid from NO asks (YES_BID = 100 - NO_ASK).

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

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return default if v is None or str(v).strip() == "" else int(v)
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    try:
        return default if v is None or str(v).strip() == "" else float(v)
    except Exception:
        return default


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# Kalshi base/prefix
API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").rstrip("/")
API_PREFIX = os.getenv("KALSHI_API_PREFIX", "/trade-api/v2").rstrip("/")

# Market selection
SERIES = os.getenv("SERIES", "KXBTC15M").strip()
EVENT_TICKER = os.getenv("EVENT_TICKER", "").strip()  # optional
MARKET_OVERRIDE = os.getenv("MARKET_OVERRIDE", "").strip()

# Looping
POLL_SECONDS = env_float("POLL", 0.20)

# Trading toggles
DRY_RUN = env_bool("DRY_RUN", False)
ENABLE_TRADING = env_bool("ENABLE_TRADING", True)
POST_ONLY = env_bool("POST_ONLY", True)

# Inventory / skew
MAX_NET_YES_CONTRACTS = env_int("MAX_NET_YES_CONTRACTS", 2)
INVENTORY_SKEW_CENTS = env_int("INVENTORY_SKEW_CENTS", 1)

# Microstructure
NO_IMPROVE_MAX_SPREAD_CENTS = env_int("NO_IMPROVE_MAX_SPREAD_CENTS", 4)

# Order state sanity
ORDER_STATUS_POLL_SECONDS = env_float("ORDER_STATUS_POLL_SECONDS", 1.0)
PAUSE_ON_UNKNOWN_SECONDS = env_float("PAUSE_ON_UNKNOWN_SECONDS", 0.75)

# Spot guard / closeout
ENABLE_SPOT_GUARD = env_bool("ENABLE_SPOT_GUARD", True)
SPOT_POLL_SECONDS = env_float("SPOT_POLL_SECONDS", 12.0)
SPOT_RESOLVED_BUFFER_USD = env_float("SPOT_RESOLVED_BUFFER_USD", 75.0)
CLOSEOUT_SECONDS = env_float("CLOSEOUT_SECONDS", 20.0)
META_REFRESH = env_float("META_REFRESH", 30.0)

# Auth env
KALSHI_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()
KALSHI_PRIV_B64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64", "").strip()


def detect_kalshi_env_keys() -> List[str]:
    keys = [k for k in os.environ.keys() if k.startswith("KALSHI_")]
    return sorted(keys)


# -----------------------------
# Kalshi HTTP + Signing
# -----------------------------
@dataclass
class KalshiAuth:
    key_id: str
    private_key_pem_b64: str
    _private_key: Any = None

    def _load_key(self):
        if self._private_key is not None:
            return
        if not self.private_key_pem_b64:
            raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_BASE64")
        pem = base64.b64decode(self.private_key_pem_b64.encode("utf-8"))
        self._private_key = serialization.load_pem_private_key(pem, password=None)

    def sign(self, message: bytes) -> str:
        self._load_key()
        sig = self._private_key.sign(
            message,
            asy_padding.PSS(
                mgf=asy_padding.MGF1(hashes.SHA256()),
                salt_length=asy_padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")


AUTH = KalshiAuth(KALSHI_KEY_ID, KALSHI_PRIV_B64)


def build_url(path: str) -> str:
    return f"{API_BASE}{API_PREFIX}{path}"


def canonical_path_with_query(path: str, params: Optional[Dict[str, Any]]) -> str:
    # Signature generally includes path + querystring (common Kalshi patterns).
    if not params:
        return f"{API_PREFIX}{path}"
    qs = urlencode([(k, str(v)) for k, v in params.items() if v is not None])
    return f"{API_PREFIX}{path}?{qs}"


def kalshi_request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    method_u = method.upper()
    ts = str(now_ms())

    body_str = "" if json_body is None else json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
    sign_target = f"{ts}{method_u}{canonical_path_with_query(path, params)}{body_str}".encode("utf-8")
    signature = AUTH.sign(sign_target)

    headers = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": AUTH.key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }

    url = build_url(path)
    resp = requests.request(
        method_u,
        url,
        params=params,
        data=(None if json_body is None else body_str),
        headers=headers,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} {API_PREFIX}{path}: {resp.text}")
    if resp.text.strip() == "":
        return {}
    return resp.json()


# -----------------------------
# Spot price (Coinbase)
# -----------------------------
def fetch_btc_spot_usd() -> Optional[float]:
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=8)
        r.raise_for_status()
        data = r.json()
        return float(data["data"]["amount"])
    except Exception as e:
        log.warning(f"[SPOT] Failed to fetch BTC spot: {e}")
        return None


# -----------------------------
# Market selection / rolling
# -----------------------------
def get_markets_for_series(series_ticker: str, limit: int = 100) -> List[Dict[str, Any]]:
    # ✅ Correct param per docs: series_ticker (NOT series)
    params = {"series_ticker": series_ticker, "limit": limit}
    data = kalshi_request("GET", "/markets", params=params)
    return data.get("markets", []) or []


def choose_active_market(markets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not markets:
        return None

    now = datetime.now(timezone.utc)

    def parse_time(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        try:
            # examples: 2025-12-20T01:30:00Z
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    # Prefer open markets that haven't closed yet; pick the one closing soonest.
    candidates: List[Tuple[datetime, Dict[str, Any]]] = []
    for m in markets:
        close_t = parse_time(m.get("close_time"))
        if close_t and close_t > now:
            candidates.append((close_t, m))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    # Otherwise fall back to most recent by close_time (best effort)
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for m in markets:
        close_t = parse_time(m.get("close_time"))
        if close_t:
            scored.append((int(close_t.timestamp()), m))
    if not scored:
        return markets[0]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def resolve_market_from_env() -> Tuple[str, Optional[str], Optional[str]]:
    if MARKET_OVERRIDE:
        return MARKET_OVERRIDE, None, None

    markets = get_markets_for_series(SERIES, limit=100)
    active = choose_active_market(markets)
    if not active:
        return "", None, None

    market_ticker = active.get("ticker") or ""
    event_ticker = active.get("event_ticker")
    series_ticker = active.get("series_ticker")
    return market_ticker, event_ticker, series_ticker


# -----------------------------
# Quotes (YES bid/ask)
# -----------------------------
def get_market_snapshot(market_ticker: str) -> Dict[str, Any]:
    data = kalshi_request("GET", f"/markets/{market_ticker}")
    return data.get("market", data)  # tolerate either shape


def get_yes_bid_ask_from_market_snapshot(m: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    # Many markets include these directly
    yb = m.get("yes_bid")
    ya = m.get("yes_ask")
    nb = m.get("no_bid")
    na = m.get("no_ask")

    # Normalize to int cents if present
    def to_int(x) -> Optional[int]:
        if x is None:
            return None
        try:
            return int(x)
        except Exception:
            return None

    yb_i = to_int(yb)
    ya_i = to_int(ya)
    nb_i = to_int(nb)
    na_i = to_int(na)

    # If yes bid/ask missing, infer from NO side (binary complement).
    # YES_ASK = 100 - NO_BID
    # YES_BID = 100 - NO_ASK
    if ya_i is None and nb_i is not None:
        ya_i = 100 - nb_i
    if yb_i is None and na_i is not None:
        yb_i = 100 - na_i

    return yb_i, ya_i


def clamp_price(p: int) -> int:
    return max(1, min(99, p))


def compute_target_quotes(
    yes_best_bid: int,
    yes_best_ask: int,
    net_yes: int,
) -> Tuple[int, int, str]:
    spread = yes_best_ask - yes_best_bid
    if spread < 1:
        # Degenerate; just back off slightly
        bid = clamp_price(yes_best_bid - 1)
        ask = clamp_price(yes_best_ask + 1)
        if bid >= ask:
            ask = clamp_price(bid + 1)
        return bid, ask, f"degenerate(spread={spread})"

    join_only = spread <= NO_IMPROVE_MAX_SPREAD_CENTS

    if join_only:
        bid = yes_best_bid
        ask = yes_best_ask
        reason = f"join(spread={spread})"
    else:
        bid = yes_best_bid + 1
        ask = yes_best_ask - 1
        if bid >= ask:
            # If spread was 2, improving both collapses; improve only one side
            bid = yes_best_bid + 1
            ask = yes_best_ask
        reason = f"improve(spread={spread})"

    # Inventory skew (push away from adding to existing inventory)
    net_yes_clamped = max(-MAX_NET_YES_CONTRACTS, min(MAX_NET_YES_CONTRACTS, net_yes))
    skew = INVENTORY_SKEW_CENTS * abs(net_yes_clamped)

    if net_yes_clamped > 0:
        # long YES: lower bid, raise ask
        bid -= skew
        ask += skew
        reason += f" skew_long_yes(-{skew})"
    elif net_yes_clamped < 0:
        # short YES: raise bid, lower ask
        bid += skew
        ask -= skew
        reason += f" skew_short_yes(+{skew})"

    bid = clamp_price(bid)
    ask = clamp_price(ask)
    if bid >= ask:
        ask = clamp_price(bid + 1)

    return bid, ask, reason


# -----------------------------
# Orders
# -----------------------------
def get_open_orders(limit: int = 200) -> List[Dict[str, Any]]:
    data = kalshi_request("GET", "/portfolio/orders", params={"limit": limit, "status": "open"})
    return data.get("orders", []) or []


def cancel_order(order_id: str) -> None:
    if DRY_RUN or not ENABLE_TRADING:
        log.info(f"[OM] DRY cancel {order_id}")
        return
    kalshi_request("DELETE", f"/orders/{order_id}")


def create_order_yes(
    market_ticker: str,
    action: str,  # "buy" or "sell"
    price: int,
    count: int,
    client_order_id: str,
) -> Dict[str, Any]:
    body = {
        "ticker": market_ticker,
        "action": action,
        "side": "yes",
        "type": "limit",
        "count": count,
        "yes_price": price,
        "client_order_id": client_order_id,
    }
    if POST_ONLY:
        body["post_only"] = True

    if DRY_RUN or not ENABLE_TRADING:
        log.info(f"[OM] DRY {action.upper()} PLACE @{price} qty={count} cid={client_order_id}")
        return {"order": {"order_id": "DRY", "client_order_id": client_order_id}}

    data = kalshi_request("POST", "/orders", json_body=body)
    return data


def find_bot_orders(open_orders: List[Dict[str, Any]], prefix: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for o in open_orders:
        cid = o.get("client_order_id", "") or ""
        if cid.startswith(prefix):
            out[cid] = o
    return out


def get_positions() -> Dict[str, Any]:
    data = kalshi_request("GET", "/portfolio/positions", params={"limit": 200})
    # shape: {"positions":[...]}
    return data


def net_yes_for_market(positions: Dict[str, Any], market_ticker: str) -> int:
    pos = positions.get("positions", []) or []
    for p in pos:
        if p.get("ticker") == market_ticker or p.get("market_ticker") == market_ticker:
            # depending on API shape, try fields
            for key in ("position", "position_size", "net_position"):
                if key in p:
                    try:
                        return int(float(p[key]))
                    except Exception:
                        pass
            # fallback: yes_count - no_count if present
            yc = p.get("yes_count")
            nc = p.get("no_count")
            try:
                if yc is not None and nc is not None:
                    return int(yc) - int(nc)
            except Exception:
                pass
    return 0


# -----------------------------
# Guards: closeout + spot-resolved
# -----------------------------
def market_close_time_seconds(m: Dict[str, Any]) -> Optional[float]:
    ct = m.get("close_time")
    if not ct:
        return None
    try:
        close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        return (close_dt - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return None


def spot_resolved_guard(m: Dict[str, Any], spot: float) -> bool:
    # Returns True if market likely resolved (skip quoting) based on floor/cap strikes
    floor_strike = m.get("floor_strike")
    cap_strike = m.get("cap_strike")
    try:
        if floor_strike is None or cap_strike is None:
            return False
        floor_s = float(floor_strike)
        cap_s = float(cap_strike)
        if spot < (floor_s - SPOT_RESOLVED_BUFFER_USD):
            return True
        if spot > (cap_s + SPOT_RESOLVED_BUFFER_USD):
            return True
    except Exception:
        return False
    return False


# -----------------------------
# Main loop
# -----------------------------
def main():
    log.info(f"[ENV] Detected KALSHI_* keys: {detect_kalshi_env_keys()}")
    log.info(
        f"API_BASE={API_BASE} API_PREFIX={API_PREFIX} SERIES={SERIES} "
        f"EVENT_TICKER={(EVENT_TICKER or '<auto>')} MARKET_OVERRIDE={(MARKET_OVERRIDE or '<none>')} "
        f"POLL={POLL_SECONDS:.2f}s DRY_RUN={DRY_RUN} ENABLE_TRADING={ENABLE_TRADING} POST_ONLY={POST_ONLY}"
    )
    log.info(f"[INV] MAX_NET_YES_CONTRACTS={MAX_NET_YES_CONTRACTS} INVENTORY_SKEW_CENTS={INVENTORY_SKEW_CENTS} "
             f"ORDER_STATUS_POLL_SECONDS={ORDER_STATUS_POLL_SECONDS:.2f} PAUSE_ON_UNKNOWN_SECONDS={PAUSE_ON_UNKNOWN_SECONDS:.2f}")
    log.info(f"[MICRO] NO_IMPROVE_MAX_SPREAD_CENTS={NO_IMPROVE_MAX_SPREAD_CENTS} (<= this spread: join, do not improve)")
    log.info(f"[SPOT] ENABLE_SPOT_GUARD={ENABLE_SPOT_GUARD} SPOT_POLL_SECONDS={SPOT_POLL_SECONDS:.1f} "
             f"CLOSEOUT_SECONDS={CLOSEOUT_SECONDS:.1f} SPOT_RESOLVED_BUFFER_USD={SPOT_RESOLVED_BUFFER_USD:.1f} META_REFRESH={META_REFRESH:.1f}")

    last_order_poll = 0.0
    last_spot_poll = 0.0
    last_meta_refresh = 0.0

    open_orders_cache: List[Dict[str, Any]] = []
    positions_cache: Dict[str, Any] = {"positions": []}
    spot_cache: Optional[float] = None

    market_ticker = ""
    market_meta: Dict[str, Any] = {}

    bot_prefix = "MMYES"  # stable prefix for client_order_id

    while True:
        t0 = time.time()

        # Resolve/roll market periodically (or if empty)
        if not market_ticker or (time.time() - last_meta_refresh) > META_REFRESH:
            try:
                mt, et, st = resolve_market_from_env()
                if mt and mt != market_ticker:
                    market_ticker = mt
                    log.info(f"[ROLL] Series={SERIES} → Active event={et} market={market_ticker} (via /markets series_ticker)")
                    # hard refresh everything on roll
                    open_orders_cache = []
                    positions_cache = {"positions": []}
                if market_ticker:
                    market_meta = get_market_snapshot(market_ticker)
                last_meta_refresh = time.time()
            except Exception as e:
                log.error(f"[ROLLERR] {e}")
                time.sleep(PAUSE_ON_UNKNOWN_SECONDS)
                continue

        if not market_ticker:
            log.info("[ROLL] No market found yet; sleeping...")
            time.sleep(1.0)
            continue

        # Spot poll
        if ENABLE_SPOT_GUARD and (time.time() - last_spot_poll) > SPOT_POLL_SECONDS:
            spot_cache = fetch_btc_spot_usd()
            last_spot_poll = time.time()

        # Order/position poll (throttled)
        if (time.time() - last_order_poll) > ORDER_STATUS_POLL_SECONDS:
            try:
                open_orders_cache = get_open_orders()
                positions_cache = get_positions()
                last_order_poll = time.time()
            except Exception as e:
                log.warning(f"[INV] PAUSE quoting due to unknown order state: {PAUSE_ON_UNKNOWN_SECONDS:.2f}s remaining ({e})")
                time.sleep(PAUSE_ON_UNKNOWN_SECONDS)
                continue

        # Refresh market snapshot (cheap)
        try:
            market_meta = get_market_snapshot(market_ticker)
        except Exception as e:
            log.warning(f"[META] Failed market snapshot: {e}")
            time.sleep(PAUSE_ON_UNKNOWN_SECONDS)
            continue

        # Closeout guard
        ttc = market_close_time_seconds(market_meta)
        if ttc is not None and ttc <= CLOSEOUT_SECONDS:
            log.info(f"[GUARD] Closeout: {ttc:.2f}s to close ≤ {CLOSEOUT_SECONDS:.2f}s. Cancelling bot orders.")
            # cancel our orders
            bot_orders = find_bot_orders(open_orders_cache, bot_prefix)
            for cid, o in bot_orders.items():
                oid = o.get("order_id")
                if oid:
                    try:
                        cancel_order(oid)
                        log.info(f"[OM] CLOSEOUT cancel {cid} order_id={oid}")
                    except Exception as e:
                        log.warning(f"[OM] CLOSEOUT cancel failed {oid}: {e}")
            time.sleep(0.5)
            continue

        # Spot resolved guard
        if ENABLE_SPOT_GUARD and spot_cache is not None and spot_resolved_guard(market_meta, spot_cache):
            log.info(f"[GUARD] Spot {spot_cache:.2f} outside [{market_meta.get('floor_strike')},{market_meta.get('cap_strike')}] "
                     f"+/-{SPOT_RESOLVED_BUFFER_USD:.0f}. Cancelling bot orders & skipping.")
            bot_orders = find_bot_orders(open_orders_cache, bot_prefix)
            for cid, o in bot_orders.items():
                oid = o.get("order_id")
                if oid:
                    try:
                        cancel_order(oid)
                        log.info(f"[OM] RESOLVED cancel {cid} order_id={oid}")
                    except Exception as e:
                        log.warning(f"[OM] RESOLVED cancel failed {oid}: {e}")
            time.sleep(0.8)
            continue

        # Best bid/ask for YES
        yes_bid, yes_ask = get_yes_bid_ask_from_market_snapshot(market_meta)
        if yes_bid is None or yes_ask is None:
            log.info(f"[TARGET] {market_ticker} → SKIP (no_yes_bid_or_ask)")
            time.sleep(POLL_SECONDS)
            continue

        # Inventory
        net_yes = net_yes_for_market(positions_cache, market_ticker)
        if net_yes > MAX_NET_YES_CONTRACTS or net_yes < -MAX_NET_YES_CONTRACTS:
            log.info(f"[INV] {market_ticker} → SKIP (inventory_limit net_yes={net_yes})")
            time.sleep(POLL_SECONDS)
            continue

        # Compute target quotes
        target_bid, target_ask, reason = compute_target_quotes(yes_bid, yes_ask, net_yes)

        # Identify our current bot orders (2: buy + sell)
        bot_orders = find_bot_orders(open_orders_cache, bot_prefix)

        buy_cid = f"{bot_prefix}-BUY"
        sell_cid = f"{bot_prefix}-SELL"

        def order_price(o: Dict[str, Any]) -> Optional[int]:
            # try yes_price first
            for k in ("yes_price", "price"):
                if k in o and o[k] is not None:
                    try:
                        return int(o[k])
                    except Exception:
                        pass
            return None

        def maybe_reprice(action: str, cid: str, target_price: int):
            existing = bot_orders.get(cid)
            if existing:
                oid = existing.get("order_id")
                cur_price = order_price(existing)
                if oid and cur_price is not None and cur_price != target_price:
                    off_by = abs(cur_price - target_price)
                    try:
                        cancel_order(oid)
                        log.info(f"[OM] {action.upper()} CANCEL @{cur_price} (reprice(off_by={off_by}))")
                    except Exception as e:
                        log.warning(f"[OM] cancel failed {oid}: {e}")
                    # place new
                    try:
                        resp = create_order_yes(market_ticker, action, target_price, 1, cid)
                        new_id = (resp.get("order", {}) or {}).get("order_id")
                        log.info(f"[OM] {action.upper()} PLACE @{target_price} qty=1")
                        if new_id and new_id != "DRY":
                            log.info(f"[OM] {action.upper()} POSTED order_id={new_id} @ {target_price} qty=1")
                    except Exception as e:
                        log.warning(f"[OM] place failed: {e}")
                return

            # no existing: place
            try:
                resp = create_order_yes(market_ticker, action, target_price, 1, cid)
                new_id = (resp.get("order", {}) or {}).get("order_id")
                log.info(f"[OM] {action.upper()} PLACE @{target_price} qty=1")
                if new_id and new_id != "DRY":
                    log.info(f"[OM] {action.upper()} POSTED order_id={new_id} @ {target_price} qty=1")
            except Exception as e:
                log.warning(f"[OM] place failed: {e}")

        # Log quote + target
        log.info(f"[QUOTE] {market_ticker} YES bid={yes_bid} ask={yes_ask}")
        log.info(f"[TARGET] {market_ticker} → would_quote: bid@{target_bid} ask@{target_ask} ({reason}) DRY_RUN={DRY_RUN}")

        # Execute
        if ENABLE_TRADING:
            maybe_reprice("buy", buy_cid, target_bid)
            maybe_reprice("sell", sell_cid, target_ask)

        # Loop sleep
        elapsed = time.time() - t0
        sleep_for = max(0.0, POLL_SECONDS - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()