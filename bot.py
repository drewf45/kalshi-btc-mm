#!/usr/bin/env python3
import os
import json
import time
import base64
import logging
import datetime as dt
from typing import Any, Dict, Optional, Tuple, List, Union

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("kalshi-bot")

# ----------------------------
# Safe helpers
# ----------------------------
def is_dict(x: Any) -> bool:
    return isinstance(x, dict)

def is_list(x: Any) -> bool:
    return isinstance(x, list)

def jget(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default

def first_item(x: Any) -> Any:
    if isinstance(x, list) and x:
        return x[0]
    return None

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

# ----------------------------
# Email (SMTP)
# ----------------------------
import smtplib
from email.message import EmailMessage

def send_email(subject: str, body: str):
    if not EMAIL_ENABLED:
        return
    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD or not EMAIL_TO:
        log.warning("EMAIL_ENABLED=True but SMTP/EMAIL_TO not fully configured. Skipping email.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    try:
        if SMTP_TLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.starttls()
                s.login(SMTP_USERNAME, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.login(SMTP_USERNAME, SMTP_PASSWORD)
                s.send_message(msg)
        log.info("Daily email sent.")
    except Exception as e:
        log.exception(f"Email send failed: {e}")

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
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
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
            parts = []
            for k in sorted(params.keys()):
                parts.append(f"{k}={params[k]}")
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
# JSON parsing
# ----------------------------
def parse_json_safe(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None

def summarize_shape(x: Any) -> str:
    """Loggable summary: keys only, types only (no values)."""
    if is_dict(x):
        keys = sorted(list(x.keys()))
        return f"dict keys={keys[:30]}{'...' if len(keys) > 30 else ''}"
    if is_list(x):
        return f"list len={len(x)} first={summarize_shape(first_item(x))}"
    if x is None:
        return "None"
    return f"{type(x).__name__}"

def extract_cash_usd(any_json: Any) -> Optional[float]:
    """
    Tries a LOT of known shapes for balance responses.
    Returns USD float if found, else None.
    """
    # If list, try first element
    if is_list(any_json):
        any_json = first_item(any_json)

    if not is_dict(any_json):
        return None

    # Common top-level keys
    candidates = [
        "cash", "available_cash", "cash_available", "availableCash",
        "cash_usd", "available_cash_usd"
    ]
    for k in candidates:
        v = any_json.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass

    # Cents variants
    cents_candidates = ["cash_cents", "available_cash_cents", "cashAvailableCents", "availableCashCents"]
    for k in cents_candidates:
        v = any_json.get(k)
        if v is not None:
            try:
                return cents_to_usd(int(v))
            except Exception:
                pass

    # Nested keys: balance / balances / portfolio_balance / data / portfolio
    nested_roots = ["balance", "balances", "portfolio_balance", "portfolio", "data", "account"]
    for root in nested_roots:
        node = any_json.get(root)
        if is_list(node):
            node = first_item(node)
        if not is_dict(node):
            continue

        # Repeat checks inside nested dict
        for k in candidates:
            v = node.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    pass
        for k in cents_candidates:
            v = node.get(k)
            if v is not None:
                try:
                    return cents_to_usd(int(v))
                except Exception:
                    pass

        # Sometimes it's like: {"cash":{"value":4980,"currency":"USD"}} or {"cash":{"cents":4980}}
        cash_obj = node.get("cash") or node.get("available_cash")
        if is_dict(cash_obj):
            for kk in ("usd", "value", "amount", "dollars"):
                if cash_obj.get(kk) is not None:
                    try:
                        return float(cash_obj.get(kk))
                    except Exception:
                        pass
            for kk in ("cents", "value_cents", "amount_cents"):
                if cash_obj.get(kk) is not None:
                    try:
                        return cents_to_usd(int(cash_obj.get(kk)))
                    except Exception:
                        pass

    return None

def auth_check(kc: KalshiClient) -> Optional[float]:
    """
    Returns cash USD float if parseable, else None (NOT 0).
    """
    paths = [
        kc.p("/portfolio/balance"),
        kc.p("/portfolio/balances"),
        kc.p("/portfolio"),
        kc.p("/account/balance"),
        kc.p("/account"),
    ]
    last_err = None
    last_shape = None

    for path in paths:
        r = kc.get(path)
        if r.status_code == 200:
            data = parse_json_safe(r)
            last_shape = summarize_shape(data)
            cash = extract_cash_usd(data)
            if cash is None:
                log.info(f"Auth check OK (endpoint 200), but cash not parseable. shape={last_shape}")
                return None
            return cash

        last_err = f"HTTP {r.status_code} {r.text[:200]}"

    raise RuntimeError(f"Auth check failed: {last_err} last_shape={last_shape}")

# ----------------------------
# Markets / orderbook
# ----------------------------
def list_open_markets(kc: KalshiClient, series_prefix: str, limit: int = 100) -> List[Dict[str, Any]]:
    r = kc.get(kc.p("/markets"), params={"limit": limit, "status": "open"})
    if r.status_code != 200:
        raise RuntimeError(f"markets fetch failed: HTTP {r.status_code} {r.text[:200]}")
    data = parse_json_safe(r)

    markets = None
    if is_dict(data):
        markets = data.get("markets") or data.get("data")
    elif is_list(data):
        markets = data

    if not is_list(markets):
        return []

    out: List[Dict[str, Any]] = []
    for m in markets:
        if not is_dict(m):
            continue
        t = m.get("ticker") or ""
        if t.startswith(series_prefix):
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

def _side_obj(maybe_side: Any) -> Optional[dict]:
    if is_dict(maybe_side):
        return maybe_side
    if is_list(maybe_side):
        x = first_item(maybe_side)
        if is_dict(x):
            return x
    return None

def parse_best_bid_ask(ob_json: Any, side: str) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    if ob_json is None:
        return (None, None, None, None)

    if is_list(ob_json):
        ob_json = first_item(ob_json)

    if not is_dict(ob_json):
        return (None, None, None, None)

    ob = ob_json.get("orderbook") if is_dict(ob_json) else None
    if ob is None:
        ob = ob_json

    if not is_dict(ob):
        return (None, None, None, None)

    s_raw = ob.get(side) or ob.get(side.upper())
    s = _side_obj(s_raw)
    if s is None:
        return (None, None, None, None)

    bids = s.get("bids")
    asks = s.get("asks")
    if not is_list(bids):
        bids = []
    if not is_list(asks):
        asks = []

    def parse_level(level: Any) -> Tuple[Optional[int], Optional[int]]:
        if not is_dict(level):
            return (None, None)
        px = level.get("price")
        qty = level.get("quantity") or level.get("qty")
        try:
            px_i = int(px) if px is not None else None
        except Exception:
            px_i = None
        try:
            qty_i = int(qty) if qty is not None else None
        except Exception:
            qty_i = None
        return (px_i, qty_i)

    bid_px, bid_qty = (None, None)
    ask_px, ask_qty = (None, None)
    if bids:
        bid_px, bid_qty = parse_level(bids[0])
    if asks:
        ask_px, ask_qty = parse_level(asks[0])

    return (bid_px, bid_qty, ask_px, ask_qty)

def market_has_quotes(kc: KalshiClient, ticker: str) -> bool:
    ob = fetch_orderbook(kc, ticker, depth=1)
    ybp, _, yap, _ = parse_best_bid_ask(ob, "yes")
    nbp, _, nap, _ = parse_best_bid_ask(ob, "no")
    return any(v is not None for v in [ybp, yap, nbp, nap])

def pick_market_with_quotes(kc: KalshiClient, series_prefix: str) -> Optional[str]:
    markets = list_open_markets(kc, series_prefix, limit=100)
    for m in markets:
        t = m.get("ticker")
        if not t:
            continue
        if market_has_quotes(kc, t):
            return t
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
# Env config
# ----------------------------
API_BASE = env_first("KALSHI_API_BASE", "KALSHI_BASE_URL", default="https://api.elections.kalshi.com")

KALSHI_KEY_ID = env_first("KALSHI_KEY_ID", "KALSHI_API_KEY_ID", "KALSHI_ACCESS_KEY", default=None)
KALSHI_PRIVATE_KEY_B64 = env_first(
    "KALSHI_PRIVATE_KEY_B64",
    "KALSHI_PRIVATE_KEY_PEM_BASE64",
    "KALSHI_PRIVATE_KEY_BASE64",
    default=None
)

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

EMAIL_ENABLED = env_bool("EMAIL_ENABLED", default=False)
EMAIL_TO = env_first("EMAIL_TO", default=None)
SMTP_HOST = env_first("SMTP_HOST", default=None)
SMTP_PORT = env_int("SMTP_PORT", default=587)
SMTP_USERNAME = env_first("SMTP_USERNAME", default=None)
SMTP_PASSWORD = env_first("SMTP_PASSWORD", default=None)
SMTP_TLS = env_bool("SMTP_TLS", default=True)

# ----------------------------
# Main logic
# ----------------------------
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
            "last_email_day": None,
        })
        save_state(state)

        # optional daily email
        if EMAIL_ENABLED and state.get("last_email_day") != today:
            send_email(
                subject=f"Kalshi bot started {today}",
                body=f"Bot started.\nSubaccount={SUBACCOUNT}\nSeries={SERIES_PREFIX}\nCash={'UNKNOWN' if cash is None else f'${cash:.2f}'}"
            )
            state["last_email_day"] = today
            save_state(state)

    # Re-check cash
    cash_now = auth_check(kc)  # may be None
    start_cash = state.get("starting_cash", None)

    # IMPORTANT FIX:
    # If cash is unknown, do NOT block trading with a fake "loss limit hit".
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
    ybp, ybq, yap, yaq = parse_best_bid_ask(ob, "yes")
    nbp, nbq, nap, naq = parse_best_bid_ask(ob, "no")

    log.info(
        f"Heartbeat ET now={now_et().strftime('%Y-%m-%d %H:%M:%S')} | market={ticker} | "
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
        log.info("Both books fail spread checks. Skipping.")
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