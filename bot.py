# bot.py
# Kalshi YES-only rolling 15m market maker
# Uses /markets?series=XYZ to discover current market and rolls automatically.

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
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding


# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Env helpers
# -----------------------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else int(v)


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else float(v)


def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else v.strip()


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


# -----------------------------
# Monotonic ms timestamp (FIX)
# -----------------------------
_last_ms = 0


def now_ms_monotonic() -> int:
    """
    Returns a strictly-increasing millisecond timestamp.
    This prevents signature failures if system time jitters backward or repeats.
    """
    global _last_ms
    ms = int(time.time() * 1000)
    if ms <= _last_ms:
        ms = _last_ms + 1
    _last_ms = ms
    return ms


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------
# Config
# -----------------------------
API_BASE = env_str("KALSHI_API_BASE", "https://api.elections.kalshi.com")
API_PREFIX = env_str("KALSHI_API_PREFIX", "/trade-api/v2")

KALSHI_API_KEY_ID = env_str("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PEM_BASE64 = env_str("KALSHI_PRIVATE_KEY_PEM_BASE64", "")

SERIES = env_str("KALSHI_SERIES", "KXBTC15M")
EVENT_TICKER = env_str("KALSHI_EVENT_TICKER", "")  # blank = auto
MARKET_OVERRIDE = env_str("KALSHI_MARKET_TICKER", "")  # blank = auto

POLL_SECONDS = env_float("POLL_SECONDS", 1.5)

DRY_RUN = env_bool("DRY_RUN", True)
ENABLE_TRADING = env_bool("ENABLE_TRADING", False)
POST_ONLY = env_bool("POST_ONLY", True)

ORDER_QTY = env_int("ORDER_QTY", 1)

MIN_SPREAD_CENTS = env_int("MIN_SPREAD_CENTS", 3)
EDGE_CENTS = env_int("EDGE_CENTS", 1)
MAX_TAKE_CENTS = env_int("MAX_TAKE_CENTS", 98)
MIN_TAKE_CENTS = env_int("MIN_TAKE_CENTS", 2)

# Safety
MAX_ORDERS_PER_LOOP = env_int("MAX_ORDERS_PER_LOOP", 2)


# -----------------------------
# Kalshi signing + client
# -----------------------------
def load_private_key_from_base64(b64_pem: str):
    if not b64_pem:
        raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_BASE64")
    pem_bytes = base64.b64decode(b64_pem.encode("utf-8"))
    return serialization.load_pem_private_key(pem_bytes, password=None)


@dataclass
class KalshiClient:
    base: str
    prefix: str
    key_id: str
    private_key: Any
    session: requests.Session

    def _sign(self, timestamp_ms: str, method: str, path_and_query: str) -> str:
        """
        Kalshi signature: RSA-PSS(SHA256) over: "{ts}{METHOD}{path_with_query}"
        """
        payload = f"{timestamp_ms}{method.upper()}{path_and_query}".encode("utf-8")
        sig = self.private_key.sign(
            payload,
            asy_padding.PSS(
                mgf=asy_padding.MGF1(hashes.SHA256()),
                salt_length=asy_padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Any = None) -> Any:
        if params:
            qs = urlencode(params)
            path_q = f"{path}?{qs}"
        else:
            path_q = path

        url = f"{self.base}{self.prefix}{path_q}"

        # ---- ONLY CHANGE: monotonic timestamp used for signing ----
        ts_ms = str(now_ms_monotonic())

        headers = {
            "Content-Type": "application/json",
            "Kalshi-Access-Key": self.key_id,
            "Kalshi-Access-Timestamp": ts_ms,
            "Kalshi-Access-Signature": self._sign(ts_ms, method, f"{self.prefix}{path_q}"),
        }

        log.debug(f"[REQ] {method.upper()} {self.prefix}{path_q}")
        r = self.session.request(method=method.upper(), url=url, headers=headers, json=json_body, timeout=20)

        if r.status_code >= 400:
            text_head = r.text[:300]
            try:
                j = r.json()
                raise RuntimeError(f"HTTP {r.status_code} {path_q}: {j}")
            except Exception:
                raise RuntimeError(f"HTTP {r.status_code} {path_q}: {{'_non_json': True, '_text_head': {text_head!r}}}")

        if r.text.strip() == "":
            return None
        return r.json()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, json_body: Any) -> Any:
        return self.request("POST", path, json_body=json_body)

    def delete(self, path: str, json_body: Any = None) -> Any:
        return self.request("DELETE", path, json_body=json_body)


# -----------------------------
# Market selection / quoting
# -----------------------------
def pick_active_market_from_markets(markets: List[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """
    Returns (event_ticker, market_ticker) for the first open-ish market we can trade.
    """
    if not markets:
        return None

    # Try to prefer open markets; fallback to first entry
    openish = []
    for m in markets:
        status = (m.get("status") or "").lower()
        if status in ("open", "active"):
            openish.append(m)

    candidates = openish if openish else markets

    # Prefer highest volume / liquidity if present
    def score(m: Dict[str, Any]) -> float:
        return float(m.get("volume") or 0) + float(m.get("open_interest") or 0)

    candidates = sorted(candidates, key=score, reverse=True)

    m0 = candidates[0]
    return (m0.get("event_ticker") or "", m0.get("ticker") or "")


def parse_yes_bid_ask_from_market_obj(m: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Attempts to extract YES best bid/ask from a market object (from /markets).
    Different API shapes exist; we try common fields.
    """
    # Common shapes seen in Kalshi responses
    for bid_key in ("yes_bid", "best_yes_bid", "yesBestBid"):
        if bid_key in m and m[bid_key] is not None:
            try:
                yes_bid = int(m[bid_key])
                break
            except Exception:
                yes_bid = None
                break
    else:
        yes_bid = None

    for ask_key in ("yes_ask", "best_yes_ask", "yesBestAsk"):
        if ask_key in m and m[ask_key] is not None:
            try:
                yes_ask = int(m[ask_key])
                break
            except Exception:
                yes_ask = None
                break
    else:
        yes_ask = None

    return yes_bid, yes_ask


def parse_yes_bid_ask_from_orderbook(ob: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Parses best YES bid and best YES ask from /markets/{ticker}/orderbook.
    Expected: ob["orderbook"]["yes"]["bids"] / ["asks"] in price-level format.
    We handle a few shapes defensively.
    """
    yes_bid = None
    yes_ask = None

    book = ob.get("orderbook") or ob.get("book") or ob
    yes = book.get("yes") if isinstance(book, dict) else None
    if not isinstance(yes, dict):
        return None, None

    bids = yes.get("bids") or []
    asks = yes.get("asks") or []

    def first_price(levels):
        if not levels:
            return None
        lvl0 = levels[0]
        # Could be {"price": 52, "quantity": 10} or [52, 10]
        if isinstance(lvl0, dict):
            p = lvl0.get("price")
            return None if p is None else int(p)
        if isinstance(lvl0, (list, tuple)) and len(lvl0) >= 1:
            return int(lvl0[0])
        return None

    yes_bid = first_price(bids)
    yes_ask = first_price(asks)
    return yes_bid, yes_ask


# -----------------------------
# Orders
# -----------------------------
def place_order(client: KalshiClient, ticker: str, side: str, price: int, qty: int) -> Any:
    side = side.upper()
    price = clamp(int(price), 1, 99)
    qty = max(1, int(qty))

    if DRY_RUN or not ENABLE_TRADING:
        log.info(f"[OM] {ticker} {side} PLACE @{price} qty={qty} DRY_RUN={DRY_RUN}")
        return None

    body = {
        "ticker": ticker,
        "side": side,  # BUY/SELL
        "type": "limit",
        "price": price,
        "count": qty,
        "post_only": bool(POST_ONLY),
    }
    return client.post("/portfolio/orders", body)


def cancel_all_orders(client: KalshiClient, ticker: Optional[str] = None) -> None:
    if DRY_RUN or not ENABLE_TRADING:
        log.info(f"[OM] {ticker or '*'} ROLL detected → cleared working orders (DRY_RUN={DRY_RUN})")
        return
    # Best-effort cancel open orders; optionally filter by ticker
    resp = client.get("/portfolio/orders", params={"limit": 200, "status": "open"})
    orders = (resp or {}).get("orders") or []
    for o in orders:
        if ticker and o.get("ticker") != ticker:
            continue
        oid = o.get("order_id") or o.get("id")
        if not oid:
            continue
        try:
            client.delete(f"/portfolio/orders/{oid}")
        except Exception as e:
            log.warning(f"[OM] cancel failed order_id={oid}: {e}")


# -----------------------------
# Auth self-test
# -----------------------------
def self_test_auth(client: KalshiClient) -> bool:
    try:
        client.get("/portfolio/balance")
        return True
    except Exception as e:
        log.error(f"[AUTH] Self-test failed → trading disabled: {e}")
        return False


# -----------------------------
# Main loop
# -----------------------------
def main() -> None:
    # env visibility
    kalshi_keys = sorted([k for k in os.environ.keys() if k.startswith("KALSHI_")])
    log.info(f"[ENV] Detected KALSHI_* keys: {kalshi_keys}")

    log.info(
        f"API_BASE={API_BASE} API_PREFIX={API_PREFIX} "
        f"SERIES={SERIES} EVENT_TICKER={'<auto>' if not EVENT_TICKER else EVENT_TICKER} "
        f"MARKET_OVERRIDE={'<none>' if not MARKET_OVERRIDE else MARKET_OVERRIDE} "
        f"POLL={POLL_SECONDS}s DRY_RUN={DRY_RUN} ENABLE_TRADING={ENABLE_TRADING} POST_ONLY={POST_ONLY}"
    )

    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY_PEM_BASE64:
        raise RuntimeError("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PEM_BASE64")

    private_key = load_private_key_from_base64(KALSHI_PRIVATE_KEY_PEM_BASE64)
    sess = requests.Session()
    client = KalshiClient(base=API_BASE, prefix=API_PREFIX, key_id=KALSHI_API_KEY_ID, private_key=private_key, session=sess)

    # auth gate (don’t trade if auth fails)
    authed = self_test_auth(client)
    trading_enabled = ENABLE_TRADING and authed

    active_event = ""
    active_market = ""

    while True:
        try:
            # Determine market
            if MARKET_OVERRIDE:
                new_event = EVENT_TICKER or "<manual>"
                new_market = MARKET_OVERRIDE
            else:
                params = {"series": SERIES}
                if EVENT_TICKER:
                    params["event_ticker"] = EVENT_TICKER
                resp = client.get("/markets", params=params)
                markets = (resp or {}).get("markets") or (resp or {}).get("data") or []
                picked = pick_active_market_from_markets(markets)
                if not picked:
                    log.warning(f"[ROLL] Series={SERIES} could not pick active market (no markets)")
                    time.sleep(POLL_SECONDS)
                    continue
                new_event, new_market = picked

            # Roll detection
            if new_market != active_market:
                active_event, active_market = new_event, new_market
                log.info(f"[ROLL] Series={SERIES} → Active event={active_event} market={active_market} (via /markets)")
                cancel_all_orders(client, ticker=active_market)

            # Quote extraction
            yes_bid = None
            yes_ask = None

            # First: try from /markets list entry (fast)
            if not MARKET_OVERRIDE:
                # We already have the markets list in scope only inside that branch; re-fetch minimal for safety
                resp = client.get("/markets", params={"ticker": active_market})
                ms = (resp or {}).get("markets") or (resp or {}).get("data") or []
                if ms and isinstance(ms, list):
                    yes_bid, yes_ask = parse_yes_bid_ask_from_market_obj(ms[0])

            # Fallback: orderbook
            if yes_bid is None or yes_ask is None:
                try:
                    ob = client.get(f"/markets/{active_market}/orderbook")
                    b2, a2 = parse_yes_bid_ask_from_orderbook(ob or {})
                    if yes_bid is None:
                        yes_bid = b2
                    if yes_ask is None:
                        yes_ask = a2
                except Exception:
                    pass

            if yes_ask is None:
                log.info(f"[FALLBACK] {active_market} /markets returned no usable yes_ask fields")

            log.info(f"[QUOTE] {active_market} YES bid={yes_bid} ask={yes_ask}")

            # Need both sides
            if yes_bid is None or yes_ask is None:
                log.info(f"[TARGET] {active_market} → SKIP (missing_bid_or_ask)")
                time.sleep(POLL_SECONDS)
                continue

            spread = int(yes_ask) - int(yes_bid)
            if spread < MIN_SPREAD_CENTS:
                log.info(f"[TARGET] {active_market} → SKIP (spread_too_tight({spread}))")
                time.sleep(POLL_SECONDS)
                continue

            # Maker prices inside the spread
            buy_px = clamp(int(yes_bid) + EDGE_CENTS, MIN_TAKE_CENTS, MAX_TAKE_CENTS)
            sell_px = clamp(int(yes_ask) - EDGE_CENTS, MIN_TAKE_CENTS, MAX_TAKE_CENTS)

            # Ensure still a spread after edge
            if sell_px <= buy_px:
                log.info(f"[TARGET] {active_market} → SKIP (spread_ok_not_stable({buy_px}) {sell_px})")
                time.sleep(POLL_SECONDS)
                continue

            log.info(
                f"[TARGET] {active_market} YES-only would_quote: bid@{buy_px} ask@{sell_px} "
                f"(ok_spread={sell_px - buy_px}) DRY_RUN={DRY_RUN}"
            )

            # Place orders (best-effort; cap per loop)
            n = 0
            if trading_enabled and n < MAX_ORDERS_PER_LOOP:
                place_order(client, active_market, "BUY", buy_px, ORDER_QTY)
                n += 1
            elif not trading_enabled:
                # still log what we would do
                place_order(client, active_market, "BUY", buy_px, ORDER_QTY)

            if trading_enabled and n < MAX_ORDERS_PER_LOOP:
                place_order(client, active_market, "SELL", sell_px, ORDER_QTY)
                n += 1
            elif not trading_enabled:
                place_order(client, active_market, "SELL", sell_px, ORDER_QTY)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log.error(f"[LOOPERR] {repr(e)}")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()