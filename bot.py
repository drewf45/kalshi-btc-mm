import os
import json
import time
import base64
import logging
import datetime as dt
from typing import Any, Dict, Optional, Tuple, List

import requests
from dotenv import load_dotenv

# cryptography for Kalshi signing
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("btc15m-bot")


# ----------------------------
# Config (env)
# ----------------------------
load_dotenv()

API_BASE = os.getenv("API_BASE", "https://api.elections.kalshi.com").rstrip("/")
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() == "true"

SUBACCOUNT = os.getenv("SUBACCOUNT", "").strip() or None

# $ per side per cycle (your “$1 per trade”)
USD_PER_SIDE = float(os.getenv("USD_PER_SIDE", "1.00"))
MAX_CONTRACTS_PER_SIDE = int(os.getenv("MAX_CONTRACTS_PER_SIDE", "10"))  # safety cap

# price to bid at (cents) – simple market-maker: place a bid on each side at best_bid or a configured cap
MAX_BID_CENTS = int(os.getenv("MAX_BID_CENTS", "98"))  # never bid 99/100 by accident
DEFAULT_BID_CENTS = int(os.getenv("DEFAULT_BID_CENTS", "45"))  # fallback bid if no book

# How far ahead to look
LOOKAHEAD_INTERVALS = int(os.getenv("LOOKAHEAD_INTERVALS", "4"))  # try next 4 intervals if needed

# Debugging
DEBUG_JSON = os.getenv("DEBUG_JSON", "true").lower() == "true"
DEBUG_MAX_CHARS = int(os.getenv("DEBUG_MAX_CHARS", "4000"))  # keep Render logs readable


# Credentials
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

if not KALSHI_KEY_ID:
    raise RuntimeError("Missing env var: KALSHI_KEY_ID")
if not KALSHI_PRIVATE_KEY_B64:
    raise RuntimeError("Missing env var: KALSHI_PRIVATE_KEY_B64")


# ----------------------------
# Helpers
# ----------------------------
def is_dict(x: Any) -> bool:
    return isinstance(x, dict)

def is_list(x: Any) -> bool:
    return isinstance(x, list)

def jdump(obj: Any) -> str:
    try:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return str(obj)

def debug_dump(label: str, obj: Any) -> None:
    if not DEBUG_JSON:
        return
    s = jdump(obj)
    if len(s) > DEBUG_MAX_CHARS:
        s = s[:DEBUG_MAX_CHARS] + "...<truncated>"
    log.info(f"[DEBUG_JSON] {label}={s}")

def et_now() -> dt.datetime:
    # ET = UTC-5 (ignoring DST). Your earlier code used UTC-5; keeping consistent with your logs.
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-5)))

def ceil_to_next_15m(t: dt.datetime) -> dt.datetime:
    # Round UP to next 15-minute boundary
    minute = t.minute
    add = (15 - (minute % 15)) % 15
    if add == 0:
        add = 15
    t2 = t + dt.timedelta(minutes=add)
    return t2.replace(second=0, microsecond=0)

def format_ticker_time(t: dt.datetime) -> str:
    # matches "26JAN182245"
    return t.strftime("%y%b%d%H%M").upper()

def build_ticker(series_prefix: str, t: dt.datetime) -> str:
    return f"{series_prefix}-{format_ticker_time(t)}"


# ----------------------------
# Kalshi signing + client
# ----------------------------
def load_private_key_from_b64(b64: str):
    raw = base64.b64decode(b64)
    # If PEM
    if raw.startswith(b"-----BEGIN"):
        return serialization.load_pem_private_key(raw, password=None)
    # If raw Ed25519 seed (32 bytes) or expanded (64)
    if len(raw) == 32:
        return Ed25519PrivateKey.from_private_bytes(raw)
    if len(raw) == 64:
        # some systems store 64 bytes (seed+pub); Ed25519PrivateKey expects 32 seed
        return Ed25519PrivateKey.from_private_bytes(raw[:32])
    raise ValueError(f"Unknown private key byte length: {len(raw)}")

def sign_ed25519(priv: Ed25519PrivateKey, msg: bytes) -> str:
    sig = priv.sign(msg)
    return base64.b64encode(sig).decode("ascii")


class KalshiClient:
    def __init__(self, api_base: str, key_id: str, private_key_b64: str, subaccount: Optional[str] = None):
        self.api_base = api_base.rstrip("/")
        self.key_id = key_id
        self.priv = load_private_key_from_b64(private_key_b64)
        self.session = requests.Session()
        self.subaccount = subaccount
        self.api_prefix = None  # discovered later

    def _headers(self, method: str, path: str, body: str) -> Dict[str, str]:
        # Kalshi v2 signature scheme is: timestamp + method + path + body
        ts = str(int(time.time() * 1000))  # ms
        payload = (ts + method.upper() + path + body).encode("utf-8")

        # Only support Ed25519PrivateKey in this simplified bot
        if not isinstance(self.priv, Ed25519PrivateKey):
            # if user has PEM non-ed25519, this will fail; logs will show it clearly
            raise RuntimeError("Private key is not Ed25519. Re-export key as base64 Ed25519 bytes.")

        sig = sign_ed25519(self.priv, payload)

        h = {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
        if self.subaccount:
            h["KALSHI-SUBACCOUNT"] = self.subaccount
        return h

    def request(self, method: str, path: str, json_body: Optional[dict] = None, timeout: int = 20) -> Tuple[int, Any]:
        body = "" if json_body is None else json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
        url = self.api_base + path
        headers = self._headers(method, path, body)

        resp = self.session.request(
            method=method,
            url=url,
            data=body if body else None,
            headers=headers,
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data

    def discover_prefix(self) -> str:
        # Your logs show /trade-api/v2 works, but we auto-probe.
        candidates = ["/trade-api/v2", "/trade-api/v1"]
        for p in candidates:
            code, data = self.request("GET", f"{p}/markets?limit=1")
            if code == 200:
                self.api_prefix = p
                log.info(f"Discovered API prefix: {p} (probe {p}/markets?limit=1 -> 200)")
                if DEBUG_JSON:
                    debug_dump("probe_markets", data)
                return p
        raise RuntimeError("Could not discover working API prefix. Check API_BASE and credentials.")

    # ---- API helpers (robust parsing) ----
    def get_markets(self, series_prefix: str, limit: int = 200) -> Any:
        p = self.api_prefix or self.discover_prefix()
        # Some APIs support ?series_ticker=...; we also fallback to search if needed
        code, data = self.request("GET", f"{p}/markets?limit={limit}")
        if code != 200:
            raise RuntimeError(f"GET markets failed HTTP {code}: {data}")
        return data

    def get_orderbook(self, ticker: str, depth: int = 1) -> Any:
        p = self.api_prefix or self.discover_prefix()
        code, data = self.request("GET", f"{p}/markets/{ticker}/orderbook?depth={depth}")
        if code != 200:
            raise RuntimeError(f"GET orderbook failed HTTP {code}: {data}")
        return data

    def get_balance(self) -> Any:
        p = self.api_prefix or self.discover_prefix()
        # Try a handful of common balance endpoints
        endpoints = [
            f"{p}/portfolio/balance",
            f"{p}/portfolio/balances",
            f"{p}/portfolio",
            f"{p}/portfolio/summary",
        ]
        last = None
        for ep in endpoints:
            code, data = self.request("GET", ep)
            if code == 200:
                return data
            last = (code, data, ep)
        code, data, ep = last
        raise RuntimeError(f"Balance endpoints failed; last {ep} HTTP {code}: {data}")

    def place_order(self, ticker: str, side: str, price_cents: int, count: int) -> Any:
        """
        side: 'yes' or 'no'
        price_cents: 1..99
        count: contracts
        """
        p = self.api_prefix or self.discover_prefix()
        payload = {
            "ticker": ticker,
            "side": side.lower(),
            "type": "limit",
            "price": int(price_cents),
            "count": int(count),
        }
        code, data = self.request("POST", f"{p}/orders", json_body=payload)
        if code not in (200, 201):
            raise RuntimeError(f"POST orders failed HTTP {code}: {data}")
        return data


# ----------------------------
# Parsing routines (type-safe)
# ----------------------------
def extract_markets_list(markets_resp: Any) -> List[dict]:
    """
    Handle market list shapes:
    - dict with key 'markets' or 'data'
    - list of market dicts
    """
    if is_list(markets_resp):
        return [m for m in markets_resp if is_dict(m)]
    if is_dict(markets_resp):
        for k in ("markets", "data", "results"):
            v = markets_resp.get(k)
            if is_list(v):
                return [m for m in v if is_dict(m)]
    return []

def market_matches_series(m: dict, series_prefix: str) -> bool:
    t = str(m.get("ticker", ""))
    return t.startswith(series_prefix + "-")

def is_market_open(m: dict) -> bool:
    # different APIs use different fields; be permissive
    status = str(m.get("status", "")).lower()
    if status in ("open", "active", "trading"):
        return True
    # some have "can_trade" bool
    if m.get("can_trade") is True:
        return True
    return False

def parse_best_bid_from_orderbook(orderbook_resp: Any, side: str) -> Optional[int]:
    """
    We want the best bid price for 'yes' or 'no'.

    Orderbook shapes vary wildly. This function logs what it sees, and then tries:
    - dict['orderbook'][side]['bids']
    - dict[side]['bids']
    - dict with 'yes_asks' etc (fallback)
    Bids levels might be:
    - list of dicts [{'price': 45, 'count': 12}, ...]
    - list of lists [[45, 12], ...]
    """
    # Debug the raw shape
    if DEBUG_JSON:
        debug_dump(f"orderbook_raw_{side}", orderbook_resp)

    ob = orderbook_resp
    # unwrap common wrappers
    if is_dict(ob) and "orderbook" in ob:
        ob = ob["orderbook"]

    # side node
    node = None
    if is_dict(ob):
        if side in ob:
            node = ob.get(side)
        elif side.upper() in ob:
            node = ob.get(side.upper())
    if node is None:
        # sometimes orderbook is list like [{'side':'yes','bids':...}, ...]
        if is_list(ob):
            for item in ob:
                if is_dict(item) and str(item.get("side", "")).lower() == side:
                    node = item
                    break

    if node is None:
        log.warning(f"No orderbook node found for side={side}.")
        return None

    # bids list
    bids = None
    if is_dict(node):
        bids = node.get("bids") or node.get("bid") or node.get("levels")
    elif is_list(node):
        # sometimes node itself is the bids array
        bids = node

    if not is_list(bids) or len(bids) == 0:
        log.warning(f"No bids list found for side={side}. node_type={type(node).__name__}")
        return None

    best = bids[0]

    # dict form
    if is_dict(best):
        px = best.get("price") or best.get("p")
        if isinstance(px, (int, float)):
            return int(px)

    # list/tuple form: [price, count] or [price, ...]
    if is_list(best) and len(best) >= 1 and isinstance(best[0], (int, float)):
        return int(best[0])

    log.warning(f"Unrecognized bids[0] shape for side={side}: {type(best).__name__}")
    return None

def parse_cash_usd(balance_resp: Any) -> Optional[float]:
    """
    Your logs show:
      keys=['balance','portfolio_value','updated_ts']
      and balance child shape: int

    We'll interpret:
      balance=int -> cents
      balance=dict -> try balance['available_cash'] etc
    """
    if DEBUG_JSON:
        debug_dump("balance_raw", balance_resp)

    if is_dict(balance_resp):
        bal = balance_resp.get("balance")
        if isinstance(bal, int):
            return bal / 100.0
        if isinstance(bal, float):
            return float(bal)
        if is_dict(bal):
            for k in ("cash", "available_cash", "available", "usd", "dollars"):
                v = bal.get(k)
                if isinstance(v, (int, float)):
                    # if int, assume cents
                    return (v / 100.0) if isinstance(v, int) else float(v)

    # sometimes response is list of balances
    if is_list(balance_resp):
        for item in balance_resp:
            if is_dict(item):
                # try same logic on item
                c = parse_cash_usd(item)
                if c is not None:
                    return c

    return None


# ----------------------------
# Trading logic
# ----------------------------
def choose_target_tickers(series_prefix: str) -> List[str]:
    now_et = et_now()
    base = ceil_to_next_15m(now_et)
    tickers = []
    for i in range(LOOKAHEAD_INTERVALS):
        t = base + dt.timedelta(minutes=15 * i)
        tickers.append(build_ticker(series_prefix, t))
    return tickers

def calc_contracts_for_usd(price_cents: int, usd_budget: float) -> int:
    # Contract costs price_cents/100 dollars each
    if price_cents <= 0:
        return 0
    per = price_cents / 100.0
    n = int(usd_budget / per)
    return max(0, n)

def clamp_price(px: Optional[int]) -> int:
    if px is None:
        return DEFAULT_BID_CENTS
    px = int(px)
    px = max(1, min(px, MAX_BID_CENTS))
    return px

def should_trade() -> bool:
    if not ENABLE_TRADING:
        return False
    if ENABLE_TRADING and not CONFIRM_LIVE_TRADING:
        # extra safety: require explicit CONFIRM_LIVE_TRADING to be true
        return False
    return True


# ----------------------------
# Main loop
# ----------------------------
def main():
    log.info("=== BOT STARTED ===")
    log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    log.info(f"CONFIRM_LIVE_TRADING={CONFIRM_LIVE_TRADING}")
    log.info(f"POLL_SECONDS={POLL_SECONDS}")
    log.info(f"SERIES_PREFIX={SERIES_PREFIX}")
    log.info(f"API_BASE={API_BASE}")
    log.info(f"SUBACCOUNT={SUBACCOUNT}")

    kc = KalshiClient(API_BASE, KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_B64, subaccount=SUBACCOUNT)
    kc.discover_prefix()

    # Balance check (debug prints the raw response so we can finalize parsing if needed)
    bal_resp = kc.get_balance()
    cash = parse_cash_usd(bal_resp)
    if cash is None:
        log.warning("Auth OK (balance endpoint 200), but cash not parseable. Proceeding with cash=UNKNOWN.")
    else:
        log.info(f"Auth OK. cash=${cash:.2f}")

    while True:
        try:
            now_et = et_now()
            tickers = choose_target_tickers(SERIES_PREFIX)

            # Pull markets list for sanity/debug, but we don’t rely on it exclusively
            markets_resp = kc.get_markets(SERIES_PREFIX)
            markets = extract_markets_list(markets_resp)

            if DEBUG_JSON:
                debug_dump("markets_raw", markets_resp)
                # Also show a tiny sample of matched tickers
                matched = [m.get("ticker") for m in markets if is_dict(m) and market_matches_series(m, SERIES_PREFIX)]
                log.info(f"[DEBUG] matched_markets_count={len(matched)} sample={matched[:5]}")

            chosen = None
            for tk in tickers:
                # If markets list contains it and it's open, prioritize it
                m = next((x for x in markets if is_dict(x) and x.get("ticker") == tk), None)
                if m is not None and is_market_open(m):
                    chosen = tk
                    break
                # If not found / status unknown, we still try orderbook to see if quotes exist
                if chosen is None:
                    chosen = tk  # optimistic; we’ll validate via orderbook
                    break

            if not chosen:
                log.warning("Could not choose any ticker; sleeping.")
                time.sleep(POLL_SECONDS)
                continue

            # Orderbook check
            ob = kc.get_orderbook(chosen, depth=1)

            yes_bid = parse_best_bid_from_orderbook(ob, "yes")
            no_bid = parse_best_bid_from_orderbook(ob, "no")

            yes_px = clamp_price(yes_bid)
            no_px = clamp_price(no_bid)

            log.info(
                f"Heartbeat ET now={now_et.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                f"market={chosen} | yes_bid={yes_bid} no_bid={no_bid} | "
                f"placing yes@{yes_px} no@{no_px}"
            )

            # If there are no quotes at all (both None), we skip trading this cycle.
            if yes_bid is None and no_bid is None:
                log.warning(f"No quotes available for {chosen} (both sides).")
                time.sleep(POLL_SECONDS)
                continue

            if not should_trade():
                log.warning("Trading disabled or not confirmed. Set ENABLE_TRADING=true and CONFIRM_LIVE_TRADING=true to trade.")
                time.sleep(POLL_SECONDS)
                continue

            # Compute contract counts from USD budget
            yes_ct = calc_contracts_for_usd(yes_px, USD_PER_SIDE)
            no_ct = calc_contracts_for_usd(no_px, USD_PER_SIDE)

            yes_ct = min(yes_ct, MAX_CONTRACTS_PER_SIDE)
            no_ct = min(no_ct, MAX_CONTRACTS_PER_SIDE)

            if yes_ct <= 0 and no_ct <= 0:
                log.warning("USD_PER_SIDE too small for current prices; not placing orders.")
                time.sleep(POLL_SECONDS)
                continue

            # Place both orders (market-making)
            if yes_ct > 0:
                resp_yes = kc.place_order(chosen, "yes", yes_px, yes_ct)
                debug_dump("order_resp_yes", resp_yes)
                log.info(f"Placed YES: ticker={chosen} price={yes_px} count={yes_ct}")

            if no_ct > 0:
                resp_no = kc.place_order(chosen, "no", no_px, no_ct)
                debug_dump("order_resp_no", resp_no)
                log.info(f"Placed NO: ticker={chosen} price={no_px} count={no_ct}")

        except Exception as e:
            log.error(f"Loop error: {repr(e)}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()