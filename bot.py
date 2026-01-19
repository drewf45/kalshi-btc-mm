#!/usr/bin/env python3
import os
import json
import time
import base64
import logging
import datetime as dt
from typing import Any, Dict, Optional, Tuple, List

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")

# ----------------------------
# Helpers
# ----------------------------
def is_dict(x: Any) -> bool:
    return isinstance(x, dict)

def is_list(x: Any) -> bool:
    return isinstance(x, list)

def first_item(x: Any) -> Any:
    return x[0] if is_list(x) and x else None

def env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return v
    return default

def env_bool(*names: str, default: bool = False) -> bool:
    v = env_first(*names)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "t", "yes", "y", "on")

def env_int(*names: str, default: int) -> int:
    v = env_first(*names)
    if v is None:
        return default
    try:
        return int(str(v).strip())
    except Exception:
        return default

def env_float(*names: str, default: float) -> float:
    v = env_first(*names)
    if v is None:
        return default
    try:
        return float(str(v).strip())
    except Exception:
        return default

def now_et() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=-5)))

def usd_to_cents(u: float) -> int:
    return int(round(float(u) * 100))

def cents_to_usd(c: int) -> float:
    return float(c) / 100.0

def parse_json_safe(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None

def summarize_shape(x: Any) -> str:
    """Keys/types only; never logs secrets/values."""
    if is_dict(x):
        ks = sorted(list(x.keys()))
        return f"dict keys={ks[:30]}{'...' if len(ks) > 30 else ''}"
    if is_list(x):
        return f"list len={len(x)} first={summarize_shape(first_item(x))}"
    if x is None:
        return "None"
    return type(x).__name__

# ----------------------------
# Kalshi Client (RSA signing)
# ----------------------------
class KalshiClient:
    def __init__(self, api_base: str, key_id: str, private_key_pem_b64: str, subaccount: Optional[str] = None):
        self.api_base = api_base.rstrip("/")
        self.key_id = key_id
        self.subaccount = subaccount

        pem_bytes = base64.b64decode(private_key_pem_b64)
        self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        self.api_prefix: Optional[str] = None

    def _sign(self, method: str, path_with_query: str, body: str) -> Dict[str, str]:
        ts = str(int(time.time()))
        message = (ts + method.upper() + path_with_query + body).encode("utf-8")

        signature = self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode("utf-8")

        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }
        if self.subaccount:
            headers["KALSHI-SUBACCOUNT"] = self.subaccount
        return headers

    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    def request(self, method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None) -> requests.Response:
        body = "" if json_body is None else json.dumps(json_body, separators=(",", ":"))

        if params:
            parts = [f"{k}={params[k]}" for k in sorted(params.keys())]
            qs = "&".join(parts)
            path_for_sig = f"{path}?{qs}"
        else:
            path_for_sig = path

        headers = self._sign(method, path_for_sig, body)
        url = self._url(path)

        return self.session.request(
            method=method.upper(),
            url=url,
            params=params,
            data=None if json_body is None else body,
            headers=headers,
            timeout=20,
        )

    def get(self, path: str, params: Optional[dict] = None) -> requests.Response:
        return self.request("GET", path, params=params, json_body=None)

    def post(self, path: str, json_body: dict) -> requests.Response:
        return self.request("POST", path, params=None, json_body=json_body)

    def discover_prefix(self) -> str:
        candidates = ["/trade-api/v2", "/trade-api/v1", "/trade-api", "/v2", "/v1", ""]
        last = None
        for pref in candidates:
            test_path = f"{pref}/markets"
            r = self.get(test_path, params={"limit": 1})
            last = r
            if r.status_code == 200:
                self.api_prefix = pref
                log.info(f"Discovered API prefix: {pref or '/'} (probe {test_path}?limit=1 -> 200)")
                return pref
        raise RuntimeError(f"Could not discover API prefix. Last={last.status_code if last else 'NA'} {last.text[:200] if last else 'NA'}")

    def p(self, suffix: str) -> str:
        if self.api_prefix is None:
            self.discover_prefix()
        if not suffix.startswith("/"):
            suffix = "/" + suffix
        return f"{self.api_prefix}{suffix}"

# ----------------------------
# Cash parsing (robust)
# ----------------------------
def find_cash_recursively(node: Any) -> Optional[float]:
    if node is None:
        return None

    if is_list(node):
        for item in node[:25]:
            got = find_cash_recursively(item)
            if got is not None:
                return got
        return None

    if not is_dict(node):
        return None

    # direct common keys (USD)
    for k in ("cash", "available_cash", "cash_available", "cash_usd", "available_cash_usd"):
        if k in node and node[k] is not None:
            try:
                return float(node[k])
            except Exception:
                pass

    # cents keys
    for k in ("cash_cents", "available_cash_cents"):
        if k in node and node[k] is not None:
            try:
                return cents_to_usd(int(node[k]))
            except Exception:
                pass

    # recurse children
    for _, v in node.items():
        got = find_cash_recursively(v)
        if got is not None:
            return got

    return None

def auth_check(kc: KalshiClient) -> Optional[float]:
    """
    Returns cash if parseable, else None.
    Your observed response: dict keys=['balance','portfolio_value','updated_ts']
    """
    paths = [
        kc.p("/portfolio/balance"),
        kc.p("/portfolio/balances"),
        kc.p("/portfolio"),
        kc.p("/account/balance"),
        kc.p("/account"),
    ]

    last_err = None
    for path in paths:
        r = kc.get(path)
        if r.status_code == 200:
            data = parse_json_safe(r)

            if is_dict(data) and "balance" in data:
                cash = find_cash_recursively(data["balance"])
                if cash is not None:
                    return cash

            cash = find_cash_recursively(data)
            if cash is not None:
                return cash

            log.info(f"Auth check OK (endpoint 200), but cash not parseable. shape={summarize_shape(data)}")
            if is_dict(data) and "balance" in data:
                log.info(f"Balance child shape: {summarize_shape(data['balance'])}")
            return None

        last_err = f"HTTP {r.status_code} {r.text[:200]}"

    raise RuntimeError(f"Auth check failed: {last_err}")

# ----------------------------
# Markets / Orderbook parsing
# ----------------------------
def list_markets(kc: KalshiClient, limit: int = 200) -> List[Dict[str, Any]]:
    r = kc.get(kc.p("/markets"), params={"limit": limit})
    if r.status_code != 200:
        raise RuntimeError(f"markets fetch failed: HTTP {r.status_code} {r.text[:200]}")
    data = parse_json_safe(r)

    markets = None
    if is_dict(data):
        markets = data.get("markets") or data.get("data") or data.get("results")
    elif is_list(data):
        markets = data

    if not is_list(markets):
        return []

    return [m for m in markets if is_dict(m)]

def list_openish_series_markets(kc: KalshiClient, series_prefix: str) -> List[Dict[str, Any]]:
    allm = list_markets(kc, limit=200)

    def status_ok(m: dict) -> bool:
        s = (m.get("status") or m.get("market_status") or "").lower()
        return s in ("open", "active", "trading", "listed", "")  # tolerate missing

    out = []
    for m in allm:
        t = m.get("ticker") or ""
        if t.startswith(series_prefix) and status_ok(m):
            out.append(m)

    out.sort(key=lambda x: (x.get("ticker") or ""), reverse=True)
    return out

def fetch_orderbook(kc: KalshiClient, ticker: str, depth: int = 1) -> Any:
    paths = [
        kc.p(f"/markets/{ticker}/orderbook"),
        kc.p(f"/markets/{ticker}/order-book"),
        kc.p(f"/markets/{ticker}/quotes"),
    ]
    for pth in paths:
        r = kc.get(pth, params={"depth": depth})
        if r.status_code == 200:
            return parse_json_safe(r)
    return None

def parse_level(level: Any) -> Tuple[Optional[int], Optional[int]]:
    """
    Supports:
      - {"price": 51, "quantity": 10}
      - {"p": 51, "q": 10}
      - [51, 10]
    """
    if is_dict(level):
        px = level.get("price", level.get("p"))
        qty = level.get("quantity", level.get("qty", level.get("q")))
        try:
            px_i = int(px) if px is not None else None
        except Exception:
            px_i = None
        try:
            qty_i = int(qty) if qty is not None else None
        except Exception:
            qty_i = None
        return px_i, qty_i

    # ✅ THIS is the line that was broken in your deployed file.
    if is_list(level) and len(level) >= 2:
        try:
            px_i = int(level[0])
        except Exception:
            px_i = None
        try:
            qty_i = int(level[1])
        except Exception:
            qty_i = None
        return px_i, qty_i

    return None, None

def pick_side_node(ob: Any, side: str) -> Optional[dict]:
    if ob is None:
        return None
    if is_list(ob):
        ob = first_item(ob)
    if not is_dict(ob):
        return None

    for wrapper in ("orderbook", "book", "data", "result"):
        if wrapper in ob and is_dict(ob[wrapper]):
            ob = ob[wrapper]
            break

    if side in ob and is_dict(ob[side]):
        return ob[side]
    if side.upper() in ob and is_dict(ob[side.upper()]):
        return ob[side.upper()]

    if "contracts" in ob and is_dict(ob["contracts"]):
        c = ob["contracts"]
        if side.upper() in c and is_dict(c[side.upper()]):
            return c[side.upper()]
        if side in c and is_dict(c[side]):
            return c[side]

    if side in ob and is_list(ob[side]):
        x = first_item(ob[side])
        return x if is_dict(x) else None

    return None

def best_bid_ask(ob_json: Any, side: str) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    s = pick_side_node(ob_json, side)
    if not s:
        return (None, None, None, None)

    bids = s.get("bids") or s.get("bid") or []
    asks = s.get("asks") or s.get("ask") or []

    if not bids and "buy" in s:
        bids = s.get("buy") or []
    if not asks and "sell" in s:
        asks = s.get("sell") or []

    if not is_list(bids):
        bids = []
    if not is_list(asks):
        asks = []

    bid_px, bid_qty = (None, None)
    ask_px, ask_qty = (None, None)

    if bids:
        bid_px, bid_qty = parse_level(bids[0])
    if asks:
        ask_px, ask_qty = parse_level(asks[0])

    return (bid_px, bid_qty, ask_px, ask_qty)

def market_has_quotes(kc: KalshiClient, ticker: str) -> bool:
    ob = fetch_orderbook(kc, ticker, depth=1)
    ybp, _, yap, _ = best_bid_ask(ob, "yes")
    nbp, _, nap, _ = best_bid_ask(ob, "no")
    return any(v is not None for v in (ybp, yap, nbp, nap))

def pick_market_with_quotes(kc: KalshiClient, series_prefix: str) -> Optional[str]:
    markets = list_openish_series_markets(kc, series_prefix)
    if markets:
        log.info(f"Found {len(markets)} markets for series {series_prefix}. Checking quotes...")

    for m in markets[:50]:
        t = m.get("ticker")
        if not t:
            continue
        if market_has_quotes(kc, t):
            return t

    if markets:
        sample = markets[0].get("ticker")
        ob = fetch_orderbook(kc, sample, depth=1) if sample else None
        log.warning(f"Could not detect quotes. Sample orderbook shape for {sample}: {summarize_shape(ob)}")

    return None

# ----------------------------
# Orders
# ----------------------------
def place_order(kc: KalshiClient, ticker: str, side: str, action: str, price: int, quantity: int) -> Optional[dict]:
    payload = {
        "ticker": ticker,
        "side": side,
        "action": action,
        "type": "limit",
        "price": int(price),
        "quantity": int(quantity),
    }
    r = kc.post(kc.p("/orders"), payload)
    if r.status_code not in (200, 201):
        log.warning(f"Order failed {ticker} {side} {action} px={price} qty={quantity}: HTTP {r.status_code} {r.text[:200]}")
        return None
    return parse_json_safe(r)

# ----------------------------
# State
# ----------------------------
STATE_PATH = "state.json"

def load_state() -> dict:
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st: dict):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(st, f, indent=2, sort_keys=True)
    except Exception as e:
        log.warning(f"Could not save state: {e}")

# ----------------------------
# Config (env)
# ----------------------------
API_BASE = env_first("KALSHI_API_BASE", "KALSHI_BASE_URL", default="https://api.elections.kalshi.com")

KALSHI_KEY_ID = env_first("KALSHI_KEY_ID", "KALSHI_API_KEY_ID", "KALSHI_ACCESS_KEY", default=None)
KALSHI_PRIVATE_KEY_B64 = env_first("KALSHI_PRIVATE_KEY_B64", "KALSHI_PRIVATE_KEY_PEM_BASE64", "KALSHI_PRIVATE_KEY_BASE64", default=None)
SUBACCOUNT = env_first("KALSHI_SUBACCOUNT", "SUBACCOUNT", default=None)

SERIES_PREFIX = env_first("SERIES_PREFIX", default="KXBTC15M")
POLL_SECONDS = env_int("POLL_SECONDS", default=60)

ENABLE_TRADING = env_bool("ENABLE_TRADING", default=False)
CONFIRM_LIVE_TRADING = env_bool("CONFIRM_LIVE_TRADING", "CONFIRM_LIVE", default=False)

ORDER_USD_PER_SIDE = env_float("ORDER_USD_PER_SIDE", default=1.00)
IMPROVE_TICKS = env_int("IMPROVE_TICKS", default=1)
MAX_SPREAD_CENTS = env_int("MAX_SPREAD_CENTS", default=8)
TAKE_PROFIT_CENTS = env_int("TAKE_PROFIT_CENTS", default=3)
MAX_POSITION_MARKETS = env_int("MAX_POSITION_MARKETS", default=3)
MAX_DAILY_LOSS_PCT = env_float("MAX_DAILY_LOSS_PCT", default=10.0)

def sanity_check_env():
    if not KALSHI_KEY_ID:
        raise RuntimeError("Missing env var: KALSHI_KEY_ID (or KALSHI_API_KEY_ID)")
    if not KALSHI_PRIVATE_KEY_B64:
        raise RuntimeError("Missing env var: KALSHI_PRIVATE_KEY_B64 (or KALSHI_PRIVATE_KEY_PEM_BASE64)")

def compute_qty(limit_price_cents: int) -> int:
    if limit_price_cents <= 0:
        return 0
    budget_cents = usd_to_cents(ORDER_USD_PER_SIDE)
    return int(max(0, budget_cents // limit_price_cents))

def loop_once(kc: KalshiClient, state: dict):
    today = now_et().date().isoformat()

    # Daily reset
    if state.get("day") != today:
        cash = auth_check(kc)  # may be None
        state.clear()
        state.update({
            "day": today,
            "starting_cash": cash,  # may be None
            "daily_loss_limit_usd": None if cash is None else round(cash * (MAX_DAILY_LOSS_PCT / 100.0), 2),
            "markets_traded": [],
        })
        save_state(state)

    cash_now = auth_check(kc)  # may be None
    start_cash = state.get("starting_cash", None)

    if cash_now is None or start_cash is None:
        log.warning("Cash is UNKNOWN from balance endpoint; skipping daily-loss enforcement (still trading).")
    else:
        loss = max(0.0, float(start_cash) - float(cash_now))
        limit = float(state.get("daily_loss_limit_usd") or 0.0)
        if limit > 0 and loss >= limit:
            log.warning(f"Daily loss limit hit. start=${start_cash:.2f} now=${cash_now:.2f} loss=${loss:.2f}. Not trading.")
            return

    ticker = pick_market_with_quotes(kc, SERIES_PREFIX)
    if not ticker:
        log.warning(f"No open markets with quotes found for {SERIES_PREFIX}.")
        return

    traded = state.get("markets_traded", [])
    if not is_list(traded):
        traded = []

    if ticker in traded:
        return

    ob = fetch_orderbook(kc, ticker, depth=1)
    ybp, _, yap, _ = best_bid_ask(ob, "yes")
    nbp, _, nap, _ = best_bid_ask(ob, "no")

    log.info(
        f"Heartbeat ET={now_et().strftime('%Y-%m-%d %H:%M:%S')} | market={ticker} | "
        f"yes {ybp}/{yap} no {nbp}/{nap}"
    )

    def ok_book(bid_px, ask_px):
        if bid_px is None or ask_px is None:
            return False
        if ask_px <= bid_px:
            return False
        if (ask_px - bid_px) > MAX_SPREAD_CENTS:
            return False
        return True

    yes_ok = ok_book(ybp, yap)
    no_ok = ok_book(nbp, nap)
    if not yes_ok and not no_ok:
        log.info("Both books fail spread checks or missing quotes. Skipping.")
        return

    if ENABLE_TRADING and not CONFIRM_LIVE_TRADING:
        log.warning("ENABLE_TRADING=True but CONFIRM_LIVE_TRADING=False. Not placing orders.")
        return

    if not ENABLE_TRADING:
        log.info("ENABLE_TRADING=False. Marking market as handled (no trades).")
        traded.append(ticker)
        state["markets_traded"] = traded[:MAX_POSITION_MARKETS]
        save_state(state)
        return

    def buy_then_tp(side: str, bid_px: int, ask_px: int) -> bool:
        buy_px = min(bid_px + IMPROVE_TICKS, ask_px - 1)
        qty = compute_qty(buy_px)
        if qty <= 0:
            log.info(f"{side.upper()} budget too small for price {buy_px}c. Skipping.")
            return False

        o = place_order(kc, ticker, side=side, action="buy", price=buy_px, quantity=qty)
        if not o:
            return False
        log.info(f"Placed BUY {side.upper()} {ticker} px={buy_px} qty={qty}")

        sell_px = min(99, buy_px + TAKE_PROFIT_CENTS)
        so = place_order(kc, ticker, side=side, action="sell", price=sell_px, quantity=qty)
        if so:
            log.info(f"Placed TP SELL {side.upper()} {ticker} px={sell_px} qty={qty}")
        else:
            log.warning(f"TP SELL failed for {side.upper()} {ticker}")
        return True

    placed_any = False
    if yes_ok and ybp is not None and yap is not None:
        placed_any = buy_then_tp("yes", ybp, yap) or placed_any
    if no_ok and nbp is not None and nap is not None:
        placed_any = buy_then_tp("no", nbp, nap) or placed_any

    if placed_any:
        traded.append(ticker)
        state["markets_traded"] = traded[:MAX_POSITION_MARKETS]
        save_state(state)

def main():
    log.info("=== BOT STARTED ===")
    log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    log.info(f"CONFIRM_LIVE_TRADING={CONFIRM_LIVE_TRADING}")
    log.info(f"POLL_SECONDS={POLL_SECONDS}")
    log.info(f"SERIES_PREFIX={SERIES_PREFIX}")
    log.info(f"API_BASE={API_BASE}")
    if SUBACCOUNT:
        log.info(f"SUBACCOUNT={SUBACCOUNT}")

    sanity_check_env()

    kc = KalshiClient(API_BASE, KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_B64, subaccount=SUBACCOUNT)
    kc.discover_prefix()

    cash = auth_check(kc)
    if cash is None:
        log.info("Auth OK. cash=UNKNOWN (parser mismatch).")
    else:
        log.info(f"Auth OK. cash=${cash:.2f}")

    state = load_state()

    while True:
        try:
            loop_once(kc, state)
        except Exception as e:
            log.exception(f"Loop crashed: {repr(e)}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()