# bot.py
# Kalshi BTC 15m YES-only market maker
#
# ONLY CHANGE vs your last working version:
#   ✅ Fix SPOT_GUARD "spot_resolved" false positives by pulling the TRUE strike
#     from GET /markets/{ticker} (floor_strike/cap_strike + strike_type)
#     instead of trying to infer strike from the market ticker suffix (e.g. "-15").
#
# Everything else is kept in the same style: series rolling via /markets?series=,
# YES orderbook parsing, post-only quoting, inventory skew, and simple order management.

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


def getenv_first(keys: List[str], default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


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


# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Helpers
# -----------------------------
def now_ms() -> int:
    return int(time.time() * 1000)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))


# -----------------------------
# Kalshi REST signing
# -----------------------------
@dataclass
class KalshiAuth:
    api_base: str
    api_prefix: str
    key_id: str
    private_key_pem_b64: str

    def _load_private_key(self):
        pem = base64.b64decode(self.private_key_pem_b64.encode("utf-8"))
        return serialization.load_pem_private_key(pem, password=None)

    def sign_headers(self, method: str, path: str, body: str) -> Dict[str, str]:
        """
        This matches the standard Kalshi REST pattern many bots use:
          prehash = f"{timestamp}{method}{path}{body}"
          signature = RSA-PSS(SHA256(prehash))
        Your logs show auth is working; keep this unchanged.
        """
        ts = str(int(time.time()))
        prehash = (ts + method.upper() + path + (body or "")).encode("utf-8")
        priv = self._load_private_key()
        sig = priv.sign(
            prehash,
            asy_padding.PSS(mgf=asy_padding.MGF1(hashes.SHA256()), salt_length=asy_padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(sig).decode("utf-8")
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }


class KalshiClient:
    def __init__(self, auth: KalshiAuth, timeout: float = 10.0):
        self.auth = auth
        self.timeout = timeout
        self.s = requests.Session()

    def _url(self, path: str) -> str:
        return self.auth.api_base.rstrip("/") + self.auth.api_prefix + path

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Any = None) -> Any:
        url = self._url(path)
        body_str = "" if json_body is None else json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
        if params:
            url = url + "?" + urlencode(params)

        headers = self.auth.sign_headers(method, self.auth.api_prefix + path + (("?" + urlencode(params)) if params else ""), body_str)

        log.debug(f"[REQ] {method.upper()} {path} params={params} body={body_str[:200]}")
        resp = self.s.request(
            method=method.upper(),
            url=url,
            headers=headers,
            data=(None if json_body is None else body_str),
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path}: {resp.text}")

        return resp.json() if resp.text else {}

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params, json_body=None)

    def post(self, path: str, json_body: Any) -> Any:
        return self.request("POST", path, params=None, json_body=json_body)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path, params=None, json_body=None)


# -----------------------------
# Market / Orderbook parsing
# -----------------------------
def parse_yes_best_bid_ask(ob: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Flexible parsing for Kalshi orderbook shapes.
    We try common patterns:
      - {"orderbook": {"yes": {"bids":[{"price":..}], "asks":[...]}}}
      - {"orderbook": {"yes_bids":[...], "yes_asks":[...]}}
      - {"yes": {"bids":[...], "asks":[...]}}
    Prices expected in cents integers [1..99].
    """
    def best_price(levels: Any, is_bid: bool) -> Optional[int]:
        if not levels:
            return None
        # levels could be list of dicts with "price" or list/tuple like [price, qty]
        if isinstance(levels, list) and len(levels) > 0:
            first = levels[0]
            if isinstance(first, dict) and "price" in first:
                return int(first["price"])
            if isinstance(first, (list, tuple)) and len(first) >= 1:
                return int(first[0])
        return None

    root = ob.get("orderbook", ob)

    # pattern A: orderbook.yes.{bids,asks}
    yes = root.get("yes")
    if isinstance(yes, dict):
        bid = best_price(yes.get("bids"), is_bid=True)
        ask = best_price(yes.get("asks"), is_bid=False)
        return bid, ask

    # pattern B: yes_bids / yes_asks
    bid = best_price(root.get("yes_bids"), is_bid=True)
    ask = best_price(root.get("yes_asks"), is_bid=False)
    if bid is not None or ask is not None:
        return bid, ask

    # pattern C: nested "contracts" etc (fallback)
    contracts = root.get("contracts")
    if isinstance(contracts, dict) and "YES" in contracts:
        y = contracts["YES"]
        if isinstance(y, dict):
            return best_price(y.get("bids"), True), best_price(y.get("asks"), False)

    return None, None


# -----------------------------
# Spot price (Coinbase)
# -----------------------------
def get_btc_spot_usd() -> Optional[float]:
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
        if r.status_code != 200:
            return None
        j = r.json()
        amt = j.get("data", {}).get("amount")
        return float(amt)
    except Exception:
        return None


# -----------------------------
# ✅ Strike helper (THE FIX)
# -----------------------------
def fetch_market_strike_and_type(k: KalshiClient, market_ticker: str) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    """
    Pull real strike metadata from GET /markets/{ticker}.
    Returns: (strike_type, floor_strike, cap_strike)

    This is the fix that prevents cases like:
      |spot - 15| = 87665  (caused by parsing "-15" as the strike)
    """
    m = k.get(f"/markets/{market_ticker}")
    market = m.get("market", m)  # docs often wrap as {"market": {...}}
    strike_type = market.get("strike_type")
    floor_strike = market.get("floor_strike")
    cap_strike = market.get("cap_strike")

    # normalize numeric
    fs = float(floor_strike) if floor_strike is not None else None
    cs = float(cap_strike) if cap_strike is not None else None
    return strike_type, fs, cs


def is_spot_effectively_resolved(spot: float, strike_type: Optional[str], floor_strike: Optional[float], cap_strike: Optional[float], buffer_usd: float) -> bool:
    """
    Conservative "don't quote if outcome is basically decided" guard.

    For BTC 15m markets this is typically strike_type == "greater":
      YES if spot > floor_strike (at expiry).
    """
    if strike_type is None:
        return False

    st = strike_type.lower()

    if st == "greater":
        if floor_strike is None:
            return False
        return abs(spot - floor_strike) >= buffer_usd

    if st == "less":
        if cap_strike is None:
            return False
        return abs(spot - cap_strike) >= buffer_usd

    if st == "between":
        if floor_strike is None or cap_strike is None:
            return False
        # resolved-ish if well outside the band
        if spot <= floor_strike - buffer_usd:
            return True
        if spot >= cap_strike + buffer_usd:
            return True
        return False

    # unknown strike types -> don't block
    return False


# -----------------------------
# Strategy: choose active market from series
# -----------------------------
def pick_active_market_from_series(k: KalshiClient, series: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (event_ticker, market_ticker)
    Picks the OPEN market in the series with the nearest close_ts in the future.
    """
    j = k.get("/markets", params={"series_ticker": series, "status": "open", "limit": 200})
    markets = j.get("markets", j.get("items", []))  # different wrappers exist
    if not isinstance(markets, list) or not markets:
        return None, None

    now_ts = int(time.time())
    best = None
    for m in markets:
        close_ts = m.get("close_ts") or m.get("close_time_ts") or m.get("close_time")
        try:
            close_ts = int(close_ts)
        except Exception:
            continue
        if close_ts < now_ts:
            continue
        if best is None or close_ts < best[0]:
            best = (close_ts, m)

    if best is None:
        return None, None

    m = best[1]
    event_ticker = m.get("event_ticker")
    market_ticker = m.get("ticker") or m.get("market_ticker")
    return event_ticker, market_ticker


# -----------------------------
# Orders / Portfolio
# -----------------------------
def fetch_open_orders(k: KalshiClient, market_ticker: str) -> List[Dict[str, Any]]:
    j = k.get("/portfolio/orders", params={"status": "open", "limit": 200})
    orders = j.get("orders", j.get("items", []))
    if not isinstance(orders, list):
        return []
    return [o for o in orders if o.get("ticker") == market_ticker]


def cancel_order(k: KalshiClient, order_id: str, dry_run: bool):
    if dry_run:
        log.info(f"[OM] CANCEL DRY_RUN order_id={order_id}")
        return
    k.delete(f"/portfolio/orders/{order_id}")


def place_order_yes(k: KalshiClient, market_ticker: str, side: str, price: int, qty: int, post_only: bool, dry_run: bool):
    """
    side: "buy" buys YES, "sell" sells YES.
    """
    payload = {
        "ticker": market_ticker,
        "action": side.lower(),   # Kalshi uses action = buy/sell
        "type": "limit",
        "count": int(qty),
        "price": int(price),
    }
    if post_only:
        payload["client_order_id"] = f"mm-{market_ticker}-{side}-{price}-{now_ms()}"
        payload["post_only"] = True

    if dry_run:
        log.info(f"[OM] {side.upper()} PLACE @{price} qty={qty} DRY_RUN=True")
        return

    r = k.post("/portfolio/orders", payload)
    oid = r.get("order", {}).get("order_id") or r.get("order_id")
    log.info(f"[OM] {side.upper()} POSTED order_id={oid} @{price} qty={qty}")


def ensure_one_order_each_side(
    k: KalshiClient,
    market_ticker: str,
    want_bid: Optional[int],
    want_ask: Optional[int],
    qty: int,
    post_only: bool,
    dry_run: bool,
):
    open_orders = fetch_open_orders(k, market_ticker)

    # find existing by action
    buy_orders = [o for o in open_orders if (o.get("action") or "").lower() == "buy"]
    sell_orders = [o for o in open_orders if (o.get("action") or "").lower() == "sell"]

    def current_price(o: Dict[str, Any]) -> Optional[int]:
        p = o.get("price")
        return int(p) if p is not None else None

    # BUY side
    if want_bid is None:
        for o in buy_orders:
            cancel_order(k, o["order_id"], dry_run)
            log.info(f"[OM] {market_ticker} BUY CANCEL @{current_price(o)} (no_target)")
    else:
        # keep one, cancel extras
        keep = None
        for o in buy_orders:
            if keep is None:
                keep = o
            else:
                cancel_order(k, o["order_id"], dry_run)
                log.info(f"[OM] {market_ticker} BUY CANCEL @{current_price(o)} (extra)")

        if keep is None:
            log.info(f"[OM] {market_ticker} BUY PLACE @{want_bid} qty={qty} DRY_RUN={dry_run}")
            place_order_yes(k, market_ticker, "buy", want_bid, qty, post_only, dry_run)
        else:
            cp = current_price(keep)
            if cp != want_bid:
                cancel_order(k, keep["order_id"], dry_run)
                log.info(f"[OM] {market_ticker} BUY CANCEL @{cp} (reprice)")
                log.info(f"[OM] {market_ticker} BUY PLACE @{want_bid} qty={qty} DRY_RUN={dry_run}")
                place_order_yes(k, market_ticker, "buy", want_bid, qty, post_only, dry_run)

    # SELL side
    if want_ask is None:
        for o in sell_orders:
            cancel_order(k, o["order_id"], dry_run)
            log.info(f"[OM] {market_ticker} SELL CANCEL @{current_price(o)} (no_target)")
    else:
        keep = None
        for o in sell_orders:
            if keep is None:
                keep = o
            else:
                cancel_order(k, o["order_id"], dry_run)
                log.info(f"[OM] {market_ticker} SELL CANCEL @{current_price(o)} (extra)")

        if keep is None:
            log.info(f"[OM] {market_ticker} SELL PLACE @{want_ask} qty={qty} DRY_RUN={dry_run}")
            place_order_yes(k, market_ticker, "sell", want_ask, qty, post_only, dry_run)
        else:
            cp = current_price(keep)
            if cp != want_ask:
                cancel_order(k, keep["order_id"], dry_run)
                log.info(f"[OM] {market_ticker} SELL CANCEL @{cp} (reprice)")
                log.info(f"[OM] {market_ticker} SELL PLACE @{want_ask} qty={qty} DRY_RUN={dry_run}")
                place_order_yes(k, market_ticker, "sell", want_ask, qty, post_only, dry_run)


# -----------------------------
# Inventory (optional/simple)
# -----------------------------
def fetch_net_yes(k: KalshiClient, market_ticker: str) -> int:
    """
    Tries to read positions and return net YES contracts for this market.
    If endpoint shape differs, returns 0 (fails safe).
    """
    try:
        j = k.get("/portfolio/positions", params={"limit": 200})
        items = j.get("positions", j.get("items", []))
        if not isinstance(items, list):
            return 0
        for p in items:
            if p.get("ticker") == market_ticker:
                # common names: net_position / position / count
                for key in ("net_position", "position", "count"):
                    if key in p:
                        return int(p[key])
        return 0
    except Exception:
        return 0


# -----------------------------
# Main
# -----------------------------
def main():
    # detect env keys (for your log line)
    detected = [k for k in sorted(os.environ.keys()) if k.startswith("KALSHI_")]
    log.info(f"[ENV] Detected KALSHI_* keys: {detected}")

    api_base = getenv_first(["KALSHI_API_BASE"], "https://api.elections.kalshi.com")
    api_prefix = getenv_first(["KALSHI_API_PREFIX"], "/trade-api/v2")

    key_id = getenv_first(["KALSHI_API_KEY_ID", "KALSHI_KEY_ID"], "")
    pk_b64 = getenv_first(["KALSHI_PRIVATE_KEY_PEM_BASE64", "KALSHI_PRIVATE_KEY_B64"], "")

    if not key_id or not pk_b64:
        raise RuntimeError("Missing KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_PEM_BASE64")

    SERIES = getenv_first(["SERIES", "KALSHI_SERIES"], "KXBTC15M")
    EVENT_TICKER = getenv_first(["EVENT_TICKER", "KALSHI_EVENT_TICKER"], "")
    MARKET_OVERRIDE = getenv_first(["MARKET_TICKER", "MARKET_OVERRIDE", "KALSHI_MARKET_TICKER"], "")

    POLL_SECONDS = env_float("POLL_SECONDS", 0.20)
    DRY_RUN = env_bool("DRY_RUN", False)
    ENABLE_TRADING = env_bool("ENABLE_TRADING", True)
    POST_ONLY = env_bool("POST_ONLY", True)

    QTY = env_int("ORDER_QTY", 1)

    # inventory / skew
    MAX_NET_YES_CONTRACTS = env_int("MAX_NET_YES_CONTRACTS", 2)
    INVENTORY_SKEW_CENTS = env_int("INVENTORY_SKEW_CENTS", 1)

    # micro behavior
    NO_IMPROVE_MAX_SPREAD_CENTS = env_int("NO_IMPROVE_MAX_SPREAD_CENTS", 4)

    # spot guard
    ENABLE_SPOT_GUARD = env_bool("ENABLE_SPOT_GUARD", True)
    SPOT_POLL_SECONDS = env_float("SPOT_POLL_SECONDS", 12.0)
    SPOT_RESOLVED_BUFFER_USD = env_float("SPOT_RESOLVED_BUFFER_USD", 75.0)
    META_REFRESH = env_float("META_REFRESH", 30.0)

    log.info(
        f"API_BASE={api_base} API_PREFIX={api_prefix} "
        f"SERIES={SERIES} EVENT_TICKER={EVENT_TICKER or '<auto>'} MARKET_OVERRIDE={MARKET_OVERRIDE or '<none>'} "
        f"POLL={POLL_SECONDS:.2f}s DRY_RUN={DRY_RUN} ENABLE_TRADING={ENABLE_TRADING} POST_ONLY={POST_ONLY}"
    )
    log.info(
        f"[INV] MAX_NET_YES_CONTRACTS={MAX_NET_YES_CONTRACTS} INVENTORY_SKEW_CENTS={INVENTORY_SKEW_CENTS}"
    )
    log.info(
        f"[MICRO] NO_IMPROVE_MAX_SPREAD_CENTS={NO_IMPROVE_MAX_SPREAD_CENTS} (<= this spread: join, do not improve)"
    )
    log.info(
        f"[SPOT] ENABLE_SPOT_GUARD={ENABLE_SPOT_GUARD} SPOT_POLL_SECONDS={SPOT_POLL_SECONDS} "
        f"SPOT_RESOLVED_BUFFER_USD={SPOT_RESOLVED_BUFFER_USD} META_REFRESH={META_REFRESH}"
    )

    k = KalshiClient(KalshiAuth(api_base=api_base, api_prefix=api_prefix, key_id=key_id, private_key_pem_b64=pk_b64))

    current_market = None
    current_event = None
    last_meta = 0.0
    last_spot = 0.0
    spot = None

    # cached strike metadata for current market (THE FIX uses this)
    strike_type = None
    floor_strike = None
    cap_strike = None

    while True:
        try:
            now = time.time()

            # refresh active market / rolling
            if now - last_meta >= META_REFRESH or current_market is None:
                last_meta = now

                if MARKET_OVERRIDE:
                    new_event = EVENT_TICKER or "<override>"
                    new_market = MARKET_OVERRIDE
                else:
                    if EVENT_TICKER:
                        # If you ever pin EVENT_TICKER, you can still set MARKET_OVERRIDE for precision.
                        # For now: series rolling is the intended mode, so we keep it series-based.
                        new_event, new_market = pick_active_market_from_series(k, SERIES)
                    else:
                        new_event, new_market = pick_active_market_from_series(k, SERIES)

                if new_market and new_market != current_market:
                    current_market = new_market
                    current_event = new_event
                    log.info(f"[ROLL] Series={SERIES} → Active event={current_event} market={current_market} (via /markets)")
                    # clear working orders on roll
                    try:
                        for o in fetch_open_orders(k, current_market):
                            cancel_order(k, o["order_id"], DRY_RUN)
                        log.info(f"[OM] {current_market} ROLL detected → cleared working orders (DRY_RUN={DRY_RUN})")
                    except Exception as e:
                        log.warning(f"[OM] roll clear failed: {e}")

                    # ✅ refresh strike metadata for new market (THE FIX)
                    try:
                        strike_type, floor_strike, cap_strike = fetch_market_strike_and_type(k, current_market)
                    except Exception as e:
                        strike_type, floor_strike, cap_strike = None, None, None
                        log.warning(f"[SPOT] strike fetch failed: {e}")

            if not current_market:
                time.sleep(1.0)
                continue

            # spot polling
            if ENABLE_SPOT_GUARD and (spot is None or (now - last_spot) >= SPOT_POLL_SECONDS):
                last_spot = now
                spot = get_btc_spot_usd()

                # ✅ ensure strike metadata exists (in case API hiccup earlier)
                if strike_type is None and current_market:
                    try:
                        strike_type, floor_strike, cap_strike = fetch_market_strike_and_type(k, current_market)
                    except Exception:
                        pass

            # orderbook
            ob = k.get(f"/markets/{current_market}/orderbook")
            yes_bid, yes_ask = parse_yes_best_bid_ask(ob)

            if yes_bid is None or yes_ask is None:
                log.info(f"[QUOTE] {current_market} YES bid={yes_bid} ask={yes_ask}")
                log.info(f"[TARGET] {current_market} → SKIP (no_orderbook)")
                time.sleep(POLL_SECONDS)
                continue

            log.info(f"[QUOTE] {current_market} YES bid={yes_bid} ask={yes_ask}")

            spread = yes_ask - yes_bid
            if spread <= 0:
                log.info(f"[TARGET] {current_market} → SKIP (crossed_or_locked spread={spread})")
                time.sleep(POLL_SECONDS)
                continue

            # ✅ spot guard decision (FIXED strike)
            if ENABLE_SPOT_GUARD and spot is not None and strike_type is not None:
                if is_spot_effectively_resolved(spot, strike_type, floor_strike, cap_strike, SPOT_RESOLVED_BUFFER_USD):
                    # for log parity with your output
                    # choose a single "strike" number to display (greater->floor, less->cap)
                    strike_display = floor_strike if (strike_type or "").lower() == "greater" else cap_strike
                    diff = abs(float(spot) - float(strike_display)) if strike_display is not None else 0.0
                    log.info(f"[TARGET] {current_market} → SKIP (spot_resolved(|spot-strike|={int(diff)}>=BUFFER={int(SPOT_RESOLVED_BUFFER_USD)}))")
                    time.sleep(POLL_SECONDS)
                    continue

            # inventory skew
            net_yes = fetch_net_yes(k, current_market)
            skew = 0
            if net_yes > 0:
                skew = -min(10, net_yes) * INVENTORY_SKEW_CENTS
            elif net_yes < 0:
                skew = min(10, -net_yes) * INVENTORY_SKEW_CENTS

            # compute target prices
            if spread <= NO_IMPROVE_MAX_SPREAD_CENTS:
                want_bid = yes_bid
                want_ask = yes_ask
            else:
                want_bid = yes_bid + 1
                want_ask = yes_ask - 1

            want_bid = clamp(want_bid + skew, 1, 99)
            want_ask = clamp(want_ask + skew, 1, 99)

            # keep valid market
            if want_bid >= want_ask:
                # widen minimally
                want_bid = clamp(want_ask - 1, 1, 98)

            # inventory limits (simple)
            if net_yes >= MAX_NET_YES_CONTRACTS:
                want_bid = None
            if net_yes <= -MAX_NET_YES_CONTRACTS:
                want_ask = None

            log.info(
                f"[TARGET] {current_market} → would_quote: bid@{want_bid if want_bid is not None else '-'} "
                f"ask@{want_ask if want_ask is not None else '-'} "
                f"(ok(spread={spread}) skew={skew} net_yes={net_yes}) DRY_RUN={DRY_RUN}"
            )

            if ENABLE_TRADING:
                ensure_one_order_each_side(
                    k=k,
                    market_ticker=current_market,
                    want_bid=want_bid,
                    want_ask=want_ask,
                    qty=QTY,
                    post_only=POST_ONLY,
                    dry_run=DRY_RUN,
                )

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log.exception(f"[LOOPERR] {repr(e)}")
            time.sleep(1.0)


if __name__ == "__main__":
    main()