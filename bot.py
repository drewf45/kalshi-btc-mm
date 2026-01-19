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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("kalshi-bot")

# ----------------------------
# Helpers
# ----------------------------
def env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    """Return first env var that exists and is non-empty."""
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

def jget(obj: Any, key: str, default=None):
    """Safe .get that tolerates list/None."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default

def first_item(x: Any) -> Any:
    if isinstance(x, list) and x:
        return x[0]
    return None

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def now_et() -> dt.datetime:
    # simple ET approximation without pytz (Render + your logs used EST/ET)
    # If you need DST precision, add zoneinfo, but this is stable enough for bot ops.
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=-5)))

def iso_ts() -> str:
    return now_utc().isoformat()

def cents_to_usd(c: int) -> float:
    return float(c) / 100.0

def usd_to_cents(u: float) -> int:
    return int(round(float(u) * 100))

# ----------------------------
# Email (SMTP)
# ----------------------------
import smtplib
from email.message import EmailMessage

def send_email(subject: str, body: str):
    if not EMAIL_ENABLED:
        return

    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD or not EMAIL_TO:
        log.warning("EMAIL_ENABLED=True but SMTP/EMAIL_TO is not fully configured. Skipping email.")
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
    """
    Kalshi uses headers:
      - KALSHI-ACCESS-KEY
      - KALSHI-ACCESS-TIMESTAMP
      - KALSHI-ACCESS-SIGNATURE

    Signature is RSA-PSS over:
      <timestamp><method><path><body>
    where:
      timestamp is string epoch seconds (or ms depending on docs)
      method is uppercase (GET/POST)
      path is the request path including api prefix (e.g. /trade-api/v2/markets?limit=1) EXCLUDING domain
      body is raw body string ("" for GET)
    """

    def __init__(self, api_base: str, key_id: str, private_key_pem_b64: str, subaccount: Optional[str] = None):
        self.api_base = api_base.rstrip("/")
        self.key_id = key_id
        self.subaccount = subaccount

        # private key provided as base64 of PEM bytes (your env var screenshot)
        pem_bytes = base64.b64decode(private_key_pem_b64)
        self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

        # discovered API prefix, e.g. /trade-api/v2
        self.api_prefix = None  # type: Optional[str]

    def _sign(self, method: str, path_with_query: str, body: str) -> Dict[str, str]:
        # Kalshi expects a timestamp. Use seconds since epoch as string.
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
            # Your env var: KALSHI_SUBACCOUNT = BTC1
            headers["KALSHI-SUBACCOUNT"] = self.subaccount
        return headers

    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    def request(self, method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None) -> requests.Response:
        """
        path should already include prefix like /trade-api/v2/...
        """
        body = "" if json_body is None else json.dumps(json_body, separators=(",", ":"))
        # If params exist, requests will encode them, but signature must include the query string.
        # We'll manually build the full path with query for signing.
        if params:
            # requests will percent-encode; keep this simple and stable for signing:
            # sort keys for determinism
            parts = []
            for k in sorted(params.keys()):
                v = params[k]
                parts.append(f"{k}={v}")
            qs = "&".join(parts)
            path_for_sig = f"{path}?{qs}"
        else:
            path_for_sig = path

        headers = self._sign(method, path_for_sig, body)

        url = self._url(path)
        resp = self.session.request(
            method=method.upper(),
            url=url,
            params=params,
            data=None if json_body is None else body,
            headers=headers,
            timeout=20,
        )
        return resp

    def get(self, path: str, params: Optional[dict] = None) -> requests.Response:
        return self.request("GET", path, params=params, json_body=None)

    def post(self, path: str, json_body: dict) -> requests.Response:
        return self.request("POST", path, params=None, json_body=json_body)

    # -------- Prefix Discovery --------
    def discover_prefix(self) -> str:
        """
        Fixes your 404 problem: we probe known prefixes and select first that returns 200.
        """
        candidates = [
            "/trade-api/v2",
            "/trade-api/v1",
            "/trade-api",
            "/v2",
            "/v1",
            "",  # last resort
        ]

        for pref in candidates:
            test_path = f"{pref}/markets"
            r = self.get(test_path, params={"limit": 1})
            if r.status_code == 200:
                self.api_prefix = pref
                log.info(f"Discovered API prefix: {pref or '/'} (probe {test_path}?limit=1 -> 200)")
                return pref

        # include body for debugging
        raise RuntimeError(f"Could not discover API prefix. Last status={r.status_code} body={r.text[:300]}")

    def p(self, suffix: str) -> str:
        """prefix helper"""
        if self.api_prefix is None:
            self.discover_prefix()
        # ensure suffix starts with /
        if not suffix.startswith("/"):
            suffix = "/" + suffix
        return f"{self.api_prefix}{suffix}"

# ----------------------------
# API Wrappers (schema-safe)
# ----------------------------
def parse_json_safe(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None

def auth_check(kc: KalshiClient) -> float:
    """
    Hits balance/portfolio endpoints safely.
    Fixes 404 by using discovered prefix.
    """
    # Try common v2 endpoint first
    paths = [
        kc.p("/portfolio/balance"),
        kc.p("/portfolio/balances"),
        kc.p("/portfolio"),
        kc.p("/account/balance"),
    ]

    last_err = None
    for path in paths:
        r = kc.get(path)
        if r.status_code == 200:
            data = parse_json_safe(r)
            # balance could be in various places
            # examples: {"cash": 49.80} or {"balance": {"cash": ...}} or {"portfolio": {"cash": ...}}
            cash = None
            if isinstance(data, dict):
                cash = jget(data, "cash")
                if cash is None:
                    cash = jget(jget(data, "balance", {}), "cash")
                if cash is None:
                    cash = jget(jget(data, "portfolio", {}), "cash")
                if cash is None:
                    cash = jget(jget(data, "data", {}), "cash")

            if cash is None:
                # final fallback: if it returns cents
                cash_cents = None
                if isinstance(data, dict):
                    cash_cents = jget(data, "cash_cents") or jget(jget(data, "balance", {}), "cash_cents")
                if cash_cents is not None:
                    cash = cents_to_usd(int(cash_cents))

            if cash is None:
                # If unknown schema, still consider auth OK
                log.info("Auth check OK (balance endpoint 200), but could not parse cash. Proceeding.")
                return 0.0

            return float(cash)

        last_err = f"HTTP {r.status_code} {r.text[:200]}"

    raise RuntimeError(f"Auth check failed: {last_err}")

def list_open_markets(kc: KalshiClient, series_prefix: str, limit: int = 100) -> List[Dict[str, Any]]:
    r = kc.get(kc.p("/markets"), params={"limit": limit, "status": "open"})
    if r.status_code != 200:
        raise RuntimeError(f"markets fetch failed: HTTP {r.status_code} {r.text[:200]}")
    data = parse_json_safe(r)

    markets = None
    if isinstance(data, dict):
        markets = jget(data, "markets")
        if markets is None:
            markets = jget(data, "data")
    elif isinstance(data, list):
        markets = data

    if not isinstance(markets, list):
        return []

    out = []
    for m in markets:
        if isinstance(m, dict):
            t = jget(m, "ticker", "") or ""
            if t.startswith(series_prefix):
                out.append(m)

    # newest first by ticker string
    out.sort(key=lambda x: (jget(x, "ticker") or ""), reverse=True)
    return out

def fetch_orderbook(kc: KalshiClient, ticker: str, depth: int = 1) -> Any:
    # v2 common
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

def parse_best_bid_ask(ob_json: Any, side: str) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """
    Returns: (best_bid_price, best_bid_qty, best_ask_price, best_ask_qty) in cents/contracts
    Handles dict OR list top-level
    """
    if ob_json is None:
        return (None, None, None, None)

    if isinstance(ob_json, list):
        ob_json = first_item(ob_json)

    if not isinstance(ob_json, dict):
        return (None, None, None, None)

    # normalize: orderbook may be inside "orderbook"
    ob = jget(ob_json, "orderbook")
    if ob is None:
        ob = ob_json

    # side container could be: ob["yes"] or ob["no"]
    s = None
    if isinstance(ob, dict):
        s = ob.get(side)
        if s is None:
            s = ob.get(side.upper())
    if not isinstance(s, dict):
        return (None, None, None, None)

    bids = s.get("bids") or []
    asks = s.get("asks") or []

    def parse_px_qty(level):
        if not isinstance(level, dict):
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

    if isinstance(bids, list) and bids:
        bid_px, bid_qty = parse_px_qty(bids[0])
    if isinstance(asks, list) and asks:
        ask_px, ask_qty = parse_px_qty(asks[0])

    return (bid_px, bid_qty, ask_px, ask_qty)

def market_has_quotes(kc: KalshiClient, ticker: str) -> bool:
    ob = fetch_orderbook(kc, ticker, depth=1)
    ybp, ybq, yap, yaq = parse_best_bid_ask(ob, "yes")
    nbp, nbq, nap, naq = parse_best_bid_ask(ob, "no")
    return any(v is not None for v in [ybp, yap, nbp, nap])

def pick_market_with_quotes(kc: KalshiClient, series_prefix: str) -> Optional[str]:
    markets = list_open_markets(kc, series_prefix, limit=100)
    if not markets:
        return None

    for m in markets:
        t = jget(m, "ticker")
        if not t:
            continue
        if market_has_quotes(kc, t):
            return t
    return None

# ----------------------------
# Trading (simple scalper)
# ----------------------------
def place_order(kc: KalshiClient, ticker: str, side: str, action: str, price: int, quantity: int) -> Optional[dict]:
    """
    action: "buy" or "sell"
    side: "yes" or "no"
    """
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

def get_positions(kc: KalshiClient) -> Any:
    r = kc.get(kc.p("/portfolio/positions"), params={"limit": 200})
    if r.status_code == 200:
        return parse_json_safe(r)
    return None

def estimate_pnl_from_positions(pos_json: Any) -> Tuple[float, float]:
    """
    Returns (unrealized_pnl_usd, notional_usd) best-effort.
    Schema varies; we do safe extraction.
    """
    if pos_json is None:
        return (0.0, 0.0)

    if isinstance(pos_json, list):
        pos_list = pos_json
    elif isinstance(pos_json, dict):
        pos_list = jget(pos_json, "positions") or jget(pos_json, "data") or []
    else:
        pos_list = []

    unreal = 0.0
    notional = 0.0

    for p in pos_list:
        if not isinstance(p, dict):
            continue
        # try a few common keys
        u = p.get("unrealized_pnl") or p.get("unrealizedPnl") or p.get("unrealized_pnl_usd")
        n = p.get("notional") or p.get("notional_usd") or p.get("position_value")

        try:
            if u is not None:
                unreal += float(u)
        except Exception:
            pass
        try:
            if n is not None:
                notional += float(n)
        except Exception:
            pass

    return (unreal, notional)

# ----------------------------
# State file (helps daily email + “one trade per 15m market”)
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
# Config (env vars)
# ----------------------------
API_BASE = env_first("KALSHI_API_BASE", "KALSHI_BASE_URL", default="https://api.elections.kalshi.com")

# Accept BOTH names you used in Render:
KALSHI_KEY_ID = env_first("KALSHI_KEY_ID", "KALSHI_API_KEY_ID", "KALSHI_ACCESS_KEY", default=None)
# Accept BOTH private-key names you used:
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

# Farm-wins knobs:
ORDER_USD_PER_SIDE = env_float("ORDER_USD_PER_SIDE", default=1.00)  # you said "1 dollar per trade"
IMPROVE_TICKS = env_int("IMPROVE_TICKS", default=1)                 # join/improve by 1c
MAX_SPREAD_CENTS = env_int("MAX_SPREAD_CENTS", default=8)           # don't trade super wide spreads
MIN_ASK_QTY = env_int("MIN_ASK_QTY", default=3)                     # require some liquidity
MIN_BID_QTY = env_int("MIN_BID_QTY", default=3)

# Risk controls:
MAX_DAILY_LOSS_PCT = env_float("MAX_DAILY_LOSS_PCT", default=10.0)  # percent of starting cash
MAX_POSITION_MARKETS = env_int("MAX_POSITION_MARKETS", default=3)    # "Both market 3" interpreted as max 3 tickers

# Take profit configuration:
# NOTE: $1 profit per single 1-contract trade is not realistic on a 15m binary contract.
# We implement a configurable take-profit in cents per contract.
TAKE_PROFIT_CENTS = env_int("TAKE_PROFIT_CENTS", default=3)

# Email:
EMAIL_ENABLED = env_bool("EMAIL_ENABLED", default=False)
EMAIL_TO = env_first("EMAIL_TO", default=None)
SMTP_HOST = env_first("SMTP_HOST", default=None)
SMTP_PORT = env_int("SMTP_PORT", default=587)
SMTP_USERNAME = env_first("SMTP_USERNAME", default=None)
SMTP_PASSWORD = env_first("SMTP_PASSWORD", default=None)
SMTP_TLS = env_bool("SMTP_TLS", default=True)

# ----------------------------
# Guardrails + sanity logs
# ----------------------------
def sanity_check_env():
    if not KALSHI_KEY_ID:
        raise RuntimeError("Missing env var: KALSHI_KEY_ID (or KALSHI_API_KEY_ID)")
    if not KALSHI_PRIVATE_KEY_B64:
        raise RuntimeError("Missing env var: KALSHI_PRIVATE_KEY_B64 (or KALSHI_PRIVATE_KEY_PEM_BASE64)")

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
    if SUBACCOUNT:
        log.info(f"SUBACCOUNT={SUBACCOUNT}")

    sanity_check_env()

    kc = KalshiClient(API_BASE, KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_B64, subaccount=SUBACCOUNT)

    # Discover prefix early to prevent mid-loop surprises
    kc.discover_prefix()

    # Auth check
    cash = auth_check(kc)
    log.info(f"Auth OK. cash=${cash:.2f}")

    state = load_state()
    today_key = now_et().date().isoformat()

    # Store starting cash for daily loss checks
    if state.get("day") != today_key:
        state = {
            "day": today_key,
            "starting_cash": cash,
            "daily_loss_limit_usd": round(cash * (MAX_DAILY_LOSS_PCT / 100.0), 2),
            "markets_traded": [],
            "last_email_day": None,
        }
        save_state(state)

    # Optional: send one daily email on start (if enabled), then again when day changes
    maybe_send_daily_email(kc, state, force=False)

    while True:
        try:
            loop_once(kc, state)
        except Exception as e:
            log.error(f"Loop error: {repr(e)}")
        time.sleep(POLL_SECONDS)

def maybe_send_daily_email(kc: KalshiClient, state: dict, force: bool = False):
    if not EMAIL_ENABLED:
        return

    today = now_et().date().isoformat()
    last_sent = state.get("last_email_day")

    # send once per day (or forced)
    if force or last_sent != today:
        # best effort P&L from positions + balance
        cash = auth_check(kc)
        pos = get_positions(kc)
        unreal, notional = estimate_pnl_from_positions(pos)

        body = []
        body.append(f"Date (ET): {today}")
        body.append(f"Subaccount: {SUBACCOUNT or '(none)'}")
        body.append("")
        body.append(f"Cash balance: ${cash:.2f}")
        body.append(f"Unrealized P&L (best-effort): ${unreal:.2f}")
        body.append(f"Notional (best-effort): ${notional:.2f}")
        body.append("")
        body.append(f"Markets traded today: {len(state.get('markets_traded', []))}")
        for t in state.get("markets_traded", []):
            body.append(f" - {t}")

        send_email(
            subject=f"Kalshi BTC Bot Daily P&L ({today})",
            body="\n".join(body)
        )
        state["last_email_day"] = today
        save_state(state)

def loop_once(kc: KalshiClient, state: dict):
    # Day rollover -> email + reset
    today_key = now_et().date().isoformat()
    if state.get("day") != today_key:
        # send final email for previous day
        maybe_send_daily_email(kc, state, force=True)

        # reset state
        cash = auth_check(kc)
        state.clear()
        state.update({
            "day": today_key,
            "starting_cash": cash,
            "daily_loss_limit_usd": round(cash * (MAX_DAILY_LOSS_PCT / 100.0), 2),
            "markets_traded": [],
            "last_email_day": today_key,
        })
        save_state(state)

    # Risk: daily loss cap
    cash_now = auth_check(kc)
    start_cash = float(state.get("starting_cash", cash_now))
    loss = max(0.0, start_cash - cash_now)
    if loss >= float(state.get("daily_loss_limit_usd", 0.0)):
        log.warning(f"Daily loss limit hit. start=${start_cash:.2f} now=${cash_now:.2f} loss=${loss:.2f}. Not trading.")
        return

    # Pick an open market that has quotes
    ticker = pick_market_with_quotes(kc, SERIES_PREFIX)
    if not ticker:
        log.warning(f"No open markets with quotes found for {SERIES_PREFIX}.")
        return

    # Heartbeat: print quotes
    ob = fetch_orderbook(kc, ticker, depth=1)
    ybp, ybq, yap, yaq = parse_best_bid_ask(ob, "yes")
    nbp, nbq, nap, naq = parse_best_bid_ask(ob, "no")

    log.info(
        f"Heartbeat ET now={now_et().strftime('%Y-%m-%d %H:%M:%S %Z')} | market={ticker} | "
        f"yes {ybp}/{yap} no {nbp}/{nap}"
    )

    # Ensure we don't trade the same 15m market repeatedly unless you want to
    traded = state.get("markets_traded", [])
    if ticker in traded:
        return

    # Quote sanity: must have an ask and bid on at least one side
    if all(v is None for v in [ybp, yap, nbp, nap]):
        log.warning("No quotes available. Skipping.")
        return

    # Spread checks (prefer YES side first, else NO)
    # We'll attempt a small buy on BOTH sides only if both books are healthy.
    def ok_book(bid_px, bid_qty, ask_px, ask_qty):
        if bid_px is None or ask_px is None:
            return False
        if ask_px <= bid_px:
            return False
        spread = ask_px - bid_px
        if spread > MAX_SPREAD_CENTS:
            return False
        if (bid_qty is not None and bid_qty < MIN_BID_QTY):
            return False
        if (ask_qty is not None and ask_qty < MIN_ASK_QTY):
            return False
        return True

    yes_ok = ok_book(ybp, ybq, yap, yaq)
    no_ok = ok_book(nbp, nbq, nap, naq)

    if not yes_ok and not no_ok:
        log.info("Both books fail liquidity/spread checks. Skipping.")
        return

    # Live trading guardrail:
    if ENABLE_TRADING and not CONFIRM_LIVE_TRADING:
        log.warning("ENABLE_TRADING=True but CONFIRM_LIVE_TRADING=False. Not placing orders.")
        # Still mark as “seen” but not “traded”
        return

    if not ENABLE_TRADING:
        log.info("ENABLE_TRADING=False (paper mode). Marking market as handled without trading.")
        traded.append(ticker)
        state["markets_traded"] = traded
        save_state(state)
        return

    # Spend calc: price is cents per contract. qty = floor(usd / (ask_cents/100))
    # For a buy, you'll typically pay near bid/ask; we place a bid near bid.
    def compute_qty(limit_price_cents: int) -> int:
        if limit_price_cents <= 0:
            return 0
        budget_cents = usd_to_cents(ORDER_USD_PER_SIDE)
        # approximate contracts you can buy at that price
        qty = budget_cents // limit_price_cents
        return int(max(0, qty))

    orders_placed = []

    # Strategy: place BUY limit at (best_bid + IMPROVE_TICKS), but never >= best_ask
    def place_buy_with_tp(side: str, bid_px: int, ask_px: int, bid_qty: Optional[int], ask_qty: Optional[int]):
        buy_px = min(bid_px + IMPROVE_TICKS, ask_px - 1)
        if buy_px <= 0:
            return None

        qty = compute_qty(buy_px)
        if qty <= 0:
            log.info(f"{side.upper()} budget too small for price {buy_px}c. Skipping that side.")
            return None

        o = place_order(kc, ticker, side=side, action="buy", price=buy_px, quantity=qty)
        if o:
            log.info(f"Placed BUY {side.upper()} {ticker} px={buy_px} qty={qty}")

            # Place take-profit SELL immediately (best effort)
            sell_px = min(99, buy_px + TAKE_PROFIT_CENTS)
            so = place_order(kc, ticker, side=side, action="sell", price=sell_px, quantity=qty)
            if so:
                log.info(f"Placed TP SELL {side.upper()} {ticker} px={sell_px} qty={qty}")
            else:
                log.warning(f"TP SELL failed for {side.upper()} {ticker}")

        return o

    # If both books are good, you asked “both market” style -> try both YES and NO
    if yes_ok:
        orders_placed.append(place_buy_with_tp("yes", ybp, yap, ybq, yaq))
    if no_ok:
        orders_placed.append(place_buy_with_tp("no", nbp, nap, nbq, naq))

    # Mark market traded if we placed at least one order
    if any(o is not None for o in orders_placed):
        traded.append(ticker)
        # enforce cap “Both market 3” => max tickers per day
        traded = traded[:MAX_POSITION_MARKETS]
        state["markets_traded"] = traded
        save_state(state)

# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    main()