# btc_bot.py — Kalshi 15-minute BTC market bot (PARTICIPATION-FIRST V5 Mar 2026)
#
# STRATEGY:
#   Buy whichever side (YES or NO) is 90%+ certain in the Kalshi book.
#   Price = win rate. 95¢ entry = ~95% win rate. Trust the market.
#   Hold to settlement — 15-minute markets, no exit logic needed.
#   Maximize participation — show up to every market, every 15 minutes, 24/7.
#
# RULES:
#   1. Watch the last 90 seconds of each 15-min window
#   2. If either side holds bid >= 90¢ for 3 consecutive 1s checks → buy
#   3. Safety net: at T=10s, no position yet → buy the highest bid if >= 90¢
#   4. Size = 20% of live balance / entry price (hard cap)
#   5. One entry per market — TRADED_TICKERS fast guard + API position check
#   6. No cooldowns, no session pauses — never miss a market
#   7. Coin price fetched for logging only — market IS the model

import os
import sys
import time
import base64
import logging
import math
import uuid
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

print(f"BOOT: xrp_daily_bot.py v5 loaded at {datetime.now(timezone.utc).isoformat()}Z", flush=True)

# ======================== LOGGING ============================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-xrp-daily")
log.warning("BOOT: logger initialized")

# ======================== ENV HELPERS ========================
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

def now_ms() -> int:
    return int(time.time() * 1000)

# ======================== CONFIGURATION ======================
BOT_ID          = "XRP"
SERIES_TICKER   = getenv_first(["SERIES", "KALSHI_SERIES"], "KXXRPD")
API_BASE        = getenv_first(["KALSHI_API_BASE"], "https://api.elections.kalshi.com").rstrip("/")
API_PREFIX      = getenv_first(["KALSHI_API_PREFIX"], "/trade-api/v2").rstrip("/")
API_KEY_ID      = getenv_first(["KALSHI_API_KEY_ID"], "")
PRIVATE_KEY_PEM_B64 = getenv_first(["KALSHI_PRIVATE_KEY_PEM_BASE64"], "")
MARKET_OVERRIDE = getenv_first(["MARKET_OVERRIDE", "KALSHI_MARKET_OVERRIDE"], "<none>")
DRY_RUN         = env_bool("DRY_RUN", False)

# ── PARTICIPATION CONSTANTS ───────────────────────────────────
WATCH_WINDOW_SECONDS = 3600      # Daily: enter in last hour before close
CONFIRM_THRESHOLD    = 91      # XRP: thin book, 91¢ min signal in watch window      # Minimum bid (cents) to consider "certain"
CONFIRM_CHECKS       = 2       # XRP: thin market, signals shorter-lived       # Consecutive ticks above threshold before buying
SAFETY_NET_SECONDS   = 120     # Daily: fire if no fill with 2min left
POLL_SECONDS         = 1.0     # Orderbook poll interval
META_REFRESH_SECONDS = 10.0    # Active market refresh interval
MAX_RISK_PCT         = 0.20    # 20% of live balance per trade (hard cap)
MIN_BALANCE_USD      = 5.00    # Don't trade if balance drops below this
EXPIRY_BUFFER_SEC    = 10      # Cancel resting orders this many seconds before close

# ── COINBASE SPOT (logging + soft sanity only) ────────────────
SPOT_URL = "https://api.coinbase.com/v2/prices/XRP-USD/spot"

# Fast local guard — prevents re-entry during API lag
TRADED_TICKERS: set = set()

# ======================== KALSHI API CLIENT ==================
class KalshiClient:
    def __init__(self, api_base: str, api_prefix: str, key_id: str, pem_b64: str):
        self.api_base   = api_base.rstrip("/")
        self.api_prefix = api_prefix if api_prefix.startswith("/") else f"/{api_prefix}"
        self.key_id     = key_id
        if not pem_b64:
            raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_BASE64")
        self.private_key = serialization.load_pem_private_key(
            base64.b64decode(pem_b64), password=None
        )
        self.session = requests.Session()

    def _sign_headers(self, method: str, full_url: str) -> Dict[str, str]:
        ts  = str(now_ms())
        path = urlparse(full_url).path
        msg = f"{ts}{method.upper()}{path}".encode("utf-8")
        sig = self.private_key.sign(
            msg,
            asy_padding.PSS(mgf=asy_padding.MGF1(hashes.SHA256()),
                            salt_length=asy_padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY":       self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def request(self, method: str, path: str,
                params: Optional[Dict] = None,
                json_body: Optional[Dict] = None,
                timeout: float = 10.0) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        url      = f"{self.api_base}{self.api_prefix}{path}"
        url_with_q = url + "?" + urlencode(params) if params else url
        headers  = self._sign_headers(method, url)
        headers["Accept"] = "application/json"
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resp = self.session.request(method=method.upper(), url=url_with_q,
                                    headers=headers, json=json_body, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path}: {resp.text or ''}")
        return resp.json() if resp.content else None


# ======================== MARKET HELPERS =====================
NY  = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def _parse_iso(s: str) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None

def infer_close_ts(ticker: str, interval_min: int = 15) -> Optional[int]:
    try:
        parts = ticker.split("-")
        if len(parts) < 2:
            return None
        d = parts[1]
        day, mon, yy = int(d[0:2]), MONTHS[d[2:5].upper()], int(d[5:7])
        hh, mm = int(d[7:9]), int(d[9:11])
        start = datetime(2000 + yy, mon, day, hh, mm, tzinfo=NY)
        return int((start + timedelta(minutes=interval_min)).astimezone(UTC).timestamp())
    except Exception:
        return None

def resolve_close_ts(mobj: Dict, ticker: str) -> Optional[int]:
    for k in ("close_ts","closeTs","close_time_ts","expiration_ts"):
        v = mobj.get(k)
        if isinstance(v, (int, float)):
            vv = int(v)
            return vv // 1000 if vv > 10_000_000_000 else vv
    for k in ("close_time","closeTime","expiration_time"):
        v = mobj.get(k)
        if isinstance(v, str):
            ts = _parse_iso(v)
            if ts:
                return ts
    return infer_close_ts(ticker)

def market_bounds_usd(mobj: Dict) -> Tuple[Optional[float], Optional[float]]:
    rules = mobj.get("rules_primary") or mobj.get("rules", {}) or {}
    if not isinstance(rules, dict): rules = {}
    lo = rules.get("lower_bound") or rules.get("lo") or rules.get("floor")
    hi = rules.get("upper_bound") or rules.get("hi") or rules.get("cap")
    try:
        lo = float(lo) if lo is not None else None
    except Exception:
        lo = None
    try:
        hi = float(hi) if hi is not None else None
    except Exception:
        hi = None
    if lo is None and hi is None:
        ticker = mobj.get("ticker", "")
        parts  = str(ticker).split("-")
        if len(parts) >= 3:
            try:
                raw = parts[2].replace("T","").replace("B","").replace("A","")
                val = float(raw.replace("_",""))
                return None, val
            except Exception:
                pass
    return lo, hi

def pick_active_market(markets: List[Dict]) -> Tuple[str, str, Dict]:
    now = time.time()
    best = None
    best_secs = float("inf")
    # Prefer status=active; fall back to any non-finalized market
    active = [m for m in markets if m.get("status") == "active"]
    candidates = active if active else [m for m in markets if m.get("status") not in ("finalized","settled","closed")]
    for m in candidates:
        ticker = m.get("ticker") or m.get("market_ticker", "")
        close_ts = resolve_close_ts(m, ticker)
        if close_ts is None:
            continue
        secs = close_ts - now
        if secs < -60:
            continue
        if secs < best_secs:
            best_secs = secs
            best = (m.get("event_ticker", ""), ticker, m)
    if best:
        return best
    m = markets[0]
    ticker = m.get("ticker", "")
    return m.get("event_ticker", ""), ticker, m


# ======================== ORDERBOOK PARSING ==================
def parse_best_yes_no(ob: Any) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """Returns (yes_bid, yes_ask, no_bid, no_ask) in cents."""
    if not isinstance(ob, dict):
        return None, None, None, None

    def best_bid(levels):
        if not isinstance(levels, list) or not levels:
            return None
        bids = []
        for lv in levels:
            p = None
            if isinstance(lv, (list, tuple)) and len(lv) >= 1:
                try: p = int(lv[0])
                except: pass
            elif isinstance(lv, dict):
                try: p = int(lv.get("price", lv.get("p", 0)))
                except: pass
            if p is not None and 1 <= p <= 99:
                bids.append(p)
        return max(bids) if bids else None

    def best_ask(levels):
        if not isinstance(levels, list) or not levels:
            return None
        asks = []
        for lv in levels:
            p = None
            if isinstance(lv, (list, tuple)) and len(lv) >= 1:
                try: p = int(lv[0])
                except: pass
            elif isinstance(lv, dict):
                try: p = int(lv.get("price", lv.get("p", 0)))
                except: pass
            if p is not None and 1 <= p <= 99:
                asks.append(p)
        return min(asks) if asks else None

    ob_data = ob.get("orderbook", ob)
    yes_bids = ob_data.get("yes", [])
    no_bids  = ob_data.get("no",  [])

    yes_bid = best_bid(yes_bids)
    no_bid  = best_bid(no_bids)
    # YES ask = 100 - min(NO bids) — the most liquid YES taker level
    # NO ask  = 100 - min(YES bids) — the most liquid NO taker level
    _no_ask_raw  = best_ask(no_bids)   # min NO bid = cheapest NO = most liquid YES ask
    _yes_ask_raw = best_ask(yes_bids)  # min YES bid = cheapest YES = most liquid NO ask
    yes_ask = (100 - _no_ask_raw)  if _no_ask_raw  is not None else None
    no_ask  = (100 - _yes_ask_raw) if _yes_ask_raw is not None else None

    return yes_bid, yes_ask, no_bid, no_ask


# ======================== PORTFOLIO API ======================
def get_balance_usd(client: KalshiClient) -> Tuple[Optional[float], Optional[float]]:
    try:
        resp = client.request("GET", "/portfolio/balance")
    except Exception as e:
        log.warning(f"[BALANCE] {e}")
        return None, None
    if not isinstance(resp, dict):
        return None, None
    cash_cents = resp.get("balance")
    pv_cents   = resp.get("portfolio_value", 0)
    if cash_cents is not None:
        cash = float(cash_cents) / 100.0
        pv   = float(pv_cents)   / 100.0
        log.info(f"[BALANCE] cash=${cash:.2f} positions=${pv:.2f} total=${cash+pv:.2f}")
        return cash, pv
    return None, None

def get_positions(client: KalshiClient) -> List[Dict]:
    resp = client.request("GET", "/portfolio/positions", params={"limit": 200})
    if isinstance(resp, dict):
        for k in ("positions", "market_positions"):
            if k in resp and isinstance(resp[k], list):
                return resp[k]
    return resp if isinstance(resp, list) else []

def parse_position_for_market(positions: List[Dict], ticker: str) -> int:
    for p in positions:
        t = p.get("ticker") or p.get("market_ticker") or ""
        if str(t) != str(ticker):
            continue
        for k in ("position", "net_position", "yes_position", "qty", "count"):
            if k in p:
                try: return int(p[k])
                except: continue
    return 0

def get_open_orders(client: KalshiClient) -> List[Dict]:
    resp = client.request("GET", "/portfolio/orders", params={"status": "resting", "limit": 200})
    if isinstance(resp, dict):
        return resp.get("orders", [])
    return resp if isinstance(resp, list) else []

def cancel_order(client: KalshiClient, order_id: str) -> str:
    try:
        client.request("DELETE", f"/portfolio/orders/{order_id}")
        return "canceled"
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return "not_found"
        raise

def cancel_strays(client: KalshiClient, ticker: str) -> None:
    try:
        for o in get_open_orders(client):
            if str(o.get("ticker", "")) == str(ticker):
                oid = o.get("order_id") or o.get("id")
                if oid:
                    try: cancel_order(client, str(oid))
                    except: pass
    except Exception:
        pass

def place_order(client: KalshiClient, ticker: str, side: str,
                price_cents: int, count: int, close_ts: Optional[int]) -> str:
    # GATE: fast local guard
    if ticker in TRADED_TICKERS:
        return "BLOCKED_FAST_GUARD"
    # GATE: API position check
    try:
        existing = abs(parse_position_for_market(get_positions(client), ticker))
        if existing > 0:
            TRADED_TICKERS.add(ticker)
            log.warning(f"[GATE] Already hold {existing}ct on {ticker} — blocked")
            return "BLOCKED_HAS_POSITION"
    except Exception as e:
        log.warning(f"[GATE] Position check failed: {e} — proceeding")

    if DRY_RUN:
        log.warning(f"[DRY-RUN] Would buy {side.upper()} {count}ct @ {price_cents}¢ on {ticker}")
        return "DRY_RUN"

    body: Dict[str, Any] = {
        "ticker":          ticker,
        "action":          "buy",
        "side":            side,
        "type":            "limit",
        "count":           max(1, int(count)),
        "client_order_id": f"{BOT_ID}-{uuid.uuid4().hex[:12]}",
        "post_only":       True,   # XRP: always maker — protect margins on thin book
        "time_in_force":   "good_till_canceled",
    }
    if side == "yes":
        body["yes_price"] = int(price_cents)
    else:
        body["no_price"] = int(price_cents)
    if close_ts is not None:
        expiry = int(close_ts) - EXPIRY_BUFFER_SEC
        if expiry > int(time.time()):
            body["expiration_ts"] = expiry

    resp = client.request("POST", "/portfolio/orders", json_body=body)
    if isinstance(resp, dict):
        order = resp.get("order", resp)
        oid = order.get("order_id") or resp.get("order_id")
        if oid:
            return str(oid)
    raise RuntimeError(f"Unexpected order response: {resp}")

def get_order(client: KalshiClient, order_id: str) -> Optional[Dict]:
    try:
        resp = client.request("GET", f"/portfolio/orders/{order_id}")
        return resp.get("order", resp) if isinstance(resp, dict) else None
    except Exception as e:
        log.warning(f"[ORDER] get {order_id}: {e}")
        return None


# ======================== SPOT PRICE (logging only) ==========
def fetch_spot(http: requests.Session) -> Optional[float]:
    try:
        r = http.get(SPOT_URL, timeout=4)
        r.raise_for_status()
        amt = r.json().get("data", {}).get("amount")
        return float(amt) if amt else None
    except Exception:
        return None


# ======================== BOT STATE ==========================
@dataclass
class BotState:
    market:             str           = ""
    event_ticker:       str           = ""
    market_obj:         Dict          = field(default_factory=dict)
    traded_this_market: bool          = False
    side:               Optional[str] = None
    qty:                int           = 0
    entry_price_cents:  Optional[int] = None
    order_id:           Optional[str] = None
    certainty_counter:  int           = 0
    certainty_side:     Optional[str] = None
    watch_active:       bool          = False
    live_balance_usd:   float         = 0.0


# ======================== HEALTH SERVER ======================
HEALTH_PORT = int(os.environ.get("PORT", "10000"))

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *a): pass

def start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.warning(f"[HEALTH] Port {HEALTH_PORT}")
    except Exception as e:
        log.warning(f"[HEALTH] Could not start: {e}")


# ======================== MAIN ===============================
def main() -> None:
    start_health_server()

    log.warning("=" * 60)
    log.warning(f"[BOT] XRP DAILY — PARTICIPATION-FIRST V5")
    log.warning(f"[BOT] Watch window:   last {WATCH_WINDOW_SECONDS}s")
    log.warning(f"[BOT] Threshold:      {CONFIRM_THRESHOLD}¢ bid on either side")
    log.warning(f"[BOT] Confirm checks: {CONFIRM_CHECKS} consecutive ticks")
    log.warning(f"[BOT] Size:           {int(MAX_RISK_PCT*100)}% of live balance")
    log.warning(f"[BOT] Min balance:    ${MIN_BALANCE_USD:.2f}")
    log.warning(f"[BOT] DRY RUN:        {DRY_RUN}")
    log.warning("=" * 60)

    print(f"DEBUG: KALSHI_API_KEY_ID exists: {bool(API_KEY_ID)}", flush=True)
    print(f"DEBUG: KALSHI_PRIVATE_KEY_PEM_BASE64 exists: {bool(PRIVATE_KEY_PEM_B64)}", flush=True)
    print(f"DEBUG: KALSHI_API_BASE = {API_BASE}", flush=True)

    if not API_KEY_ID or not PRIVATE_KEY_PEM_B64:
        raise RuntimeError("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PEM_BASE64")

    client = KalshiClient(API_BASE, API_PREFIX, API_KEY_ID, PRIVATE_KEY_PEM_B64)
    http   = requests.Session()
    st     = BotState()

    # Watchdog — restart if main loop stalls >5 min
    _watchdog_ts = [time.time()]
    def _watchdog():
        while True:
            time.sleep(60)
            if time.time() - _watchdog_ts[0] > 300:
                log.error("[WATCHDOG] Stale >5min — forcing restart")
                os._exit(1)
    threading.Thread(target=_watchdog, daemon=True).start()

    # Initial balance
    cash, pv = get_balance_usd(client)
    st.live_balance_usd = (cash or 0.0)
    log.warning(f"[START] Cash=${st.live_balance_usd:.2f} positions=${pv or 0:.2f}")

    last_meta      = 0.0
    last_state_log = 0.0

    def refresh_market():
        if MARKET_OVERRIDE not in ("<none>", "none", "None", ""):
            mt = MARKET_OVERRIDE
            try:
                snap = client.request("GET", f"/markets/{mt}")
                mobj = snap.get("market", snap) if isinstance(snap, dict) else {}
            except Exception:
                mobj = {}
            return "<manual>", mt, mobj
        params = {"series_ticker": SERIES_TICKER, "limit": 200}
        resp   = client.request("GET", "/markets", params=params)
        mkts   = resp.get("markets", []) if isinstance(resp, dict) else []
        if not mkts:
            raise RuntimeError(f"No open markets for {SERIES_TICKER}")
        return pick_active_market(mkts)

    def on_new_market(new_ticker: str):
        cancel_strays(client, new_ticker)
        try:
            pos = parse_position_for_market(get_positions(client), new_ticker)
        except Exception:
            pos = 0
        if pos != 0:
            st.traded_this_market = True
            st.side = "yes" if pos > 0 else "no"
            st.qty  = abs(pos)
            TRADED_TICKERS.add(new_ticker)
            log.warning(f"[RECON] Existing position on {new_ticker}: {st.side} x{st.qty}")
        else:
            st.traded_this_market = False
            st.side               = None
            st.qty                = 0
            st.order_id           = None
            st.entry_price_cents  = None
        st.certainty_counter = 0
        st.certainty_side    = None
        st.watch_active      = False

    # ── MAIN LOOP ─────────────────────────────────────────────
    while True:
        _watchdog_ts[0] = time.time()

        # ── Refresh active market ───────────────────────────
        now = time.time()
        if now - last_meta > META_REFRESH_SECONDS or not st.market:
            try:
                ev, ticker, mobj = refresh_market()
                if ticker != st.market:
                    log.warning(f"[MARKET] New window: {ticker}")
                    st.market      = ticker
                    st.event_ticker = ev
                    st.market_obj  = mobj
                    on_new_market(ticker)
                else:
                    st.market_obj = mobj
                last_meta = now
            except Exception as e:
                log.warning(f"[META] {e}")
                time.sleep(5)
                continue

        if not st.market:
            time.sleep(5)
            continue

        close_ts = resolve_close_ts(st.market_obj, st.market)
        secs_to_close = (close_ts - time.time()) if close_ts else None

        # ── Log periodic state ──────────────────────────────
        if time.time() - last_state_log > 10.0:
            spot = fetch_spot(http)
            lo, hi = market_bounds_usd(st.market_obj)
            spot_str = f"${spot:.2f}" if spot else "n/a"
            t_str = f"{secs_to_close:.0f}s" if secs_to_close is not None else "n/a"
            log.info(f"[STATE] {st.market} t={t_str} spot={spot_str} boundary=[{lo},{hi}] traded={st.traded_this_market}")
            last_state_log = time.time()

        # ── Already in position — just hold ─────────────────
        if st.traded_this_market:
            time.sleep(POLL_SECONDS)
            continue

        if secs_to_close is None:
            last_meta = 0.0
            time.sleep(POLL_SECONDS)
            continue

        # ── Too late ────────────────────────────────────────
        if secs_to_close < 2:
            TRADED_TICKERS.add(st.market)
            st.traded_this_market = True
            last_meta = 0.0
            time.sleep(2)
            continue

        # ── Outside watch window — wait ──────────────────────
        if secs_to_close > WATCH_WINDOW_SECONDS:
            if st.certainty_counter > 0:
                st.certainty_counter = 0
                st.certainty_side    = None
            st.watch_active = False
            time.sleep(POLL_SECONDS)
            continue

        # ── First tick in watch window ───────────────────────
        if not st.watch_active:
            st.watch_active = True
            log.warning(f"[WATCH-START] {st.market} t={secs_to_close:.0f}s")

        # ── Balance check ────────────────────────────────────
        if st.live_balance_usd < MIN_BALANCE_USD:
            log.warning(f"[SKIP] Balance ${st.live_balance_usd:.2f} < floor ${MIN_BALANCE_USD:.2f}")
            time.sleep(POLL_SECONDS)
            continue

        # ── Fetch orderbook ──────────────────────────────────
        try:
            ob = client.request("GET", f"/markets/{st.market}/orderbook")
        except Exception as e:
            log.warning(f"[OB] {e}")
            time.sleep(POLL_SECONDS)
            continue

        yes_bid, yes_ask, no_bid, no_ask = parse_best_yes_no(ob)
        log.info(f"[TICK] {st.market} t={secs_to_close:.0f}s yes={yes_bid}¢ no={no_bid}¢")

        # ── SAFETY NET: T=10s, no position yet ──────────────
        if secs_to_close <= SAFETY_NET_SECONDS:
            prices = []
            if yes_bid is not None: prices.append(("yes", yes_bid))
            if no_bid  is not None: prices.append(("no",  no_bid))
            if not prices:
                TRADED_TICKERS.add(st.market)
                st.traded_this_market = True
                time.sleep(POLL_SECONDS)
                continue
            best_side, best_price = max(prices, key=lambda x: x[1])
            # Always enter at safety net — market WILL settle yes or no
            if best_price < 51:
                log.warning(f"[SAFETY-SKIP] {st.market} best={best_side}@{best_price}¢ — coin flip, skip")
                TRADED_TICKERS.add(st.market)
                st.traded_this_market = True
                time.sleep(POLL_SECONDS)
                continue
            log.warning(f"[SAFETY-NET] {st.market} firing {best_side.upper()}@{best_price}¢ at T={secs_to_close:.0f}s")
            st.certainty_side    = best_side
            st.certainty_counter = CONFIRM_CHECKS  # skip to confirmed
        else:
            # ── WATCH-CONFIRM ────────────────────────────────
            yes_certain = yes_bid is not None and yes_bid >= CONFIRM_THRESHOLD
            no_certain  = no_bid  is not None and no_bid  >= CONFIRM_THRESHOLD

            if yes_certain:
                if st.certainty_side == "yes":
                    st.certainty_counter += 1
                else:
                    st.certainty_side    = "yes"
                    st.certainty_counter = 1
                log.info(f"[WATCH] YES@{yes_bid}¢ counter={st.certainty_counter}/{CONFIRM_CHECKS} t={secs_to_close:.0f}s")
            elif no_certain:
                if st.certainty_side == "no":
                    st.certainty_counter += 1
                else:
                    st.certainty_side    = "no"
                    st.certainty_counter = 1
                log.info(f"[WATCH] NO@{no_bid}¢ counter={st.certainty_counter}/{CONFIRM_CHECKS} t={secs_to_close:.0f}s")
            else:
                if st.certainty_counter > 0:
                    log.info(f"[RESET] Dropped below {CONFIRM_THRESHOLD}¢ — counter reset")
                st.certainty_counter = 0
                st.certainty_side    = None
                time.sleep(POLL_SECONDS)
                continue

            if st.certainty_counter < CONFIRM_CHECKS:
                time.sleep(POLL_SECONDS)
                continue

        # ── CONFIRMED: enter ─────────────────────────────────
        side = st.certainty_side

        # Refresh live balance before sizing
        try:
            cash, pv = get_balance_usd(client)
            if cash is not None:
                st.live_balance_usd = cash
        except Exception as e:
            log.warning(f"[BALANCE-REFRESH] {e}")

        init_bid   = (yes_bid if side == "yes" else no_bid) or 99
        init_ask   = (yes_ask if side == "yes" else no_ask)
        buy_price  = min((init_ask + 1) if init_ask is not None else init_bid, 99)

        position_size = st.live_balance_usd * MAX_RISK_PCT
        order_qty     = max(1, min(500, int(position_size / (buy_price / 100))))

        # Minimum trade gate ($1 cost)
        if order_qty * buy_price / 100 < 1.00:
            log.info(f"[SKIP] Proposed cost < $1.00 — skip")
            time.sleep(POLL_SECONDS)
            continue

        log.warning(
            f"[ENTER] {st.market} {side.upper()}@{buy_price}¢ "
            f"qty={order_qty} cost=${order_qty * buy_price / 100:.2f} "
            f"bal=${st.live_balance_usd:.2f} t={secs_to_close:.0f}s"
        )

        # Lock immediately
        TRADED_TICKERS.add(st.market)
        st.traded_this_market = True
        st.side               = side

        # ── ORDER RETRY LOOP ─────────────────────────────────
        resting_oid   = None
        total_filled  = 0
        attempt       = 0
        last_price    = None
        current_side  = side
        deadline      = (close_ts or (time.time() + 30)) - EXPIRY_BUFFER_SEC

        while time.time() < deadline and total_filled < order_qty:
            attempt += 1

            # Fresh orderbook
            try:
                fresh_ob = client.request("GET", f"/markets/{st.market}/orderbook")
                yb, ya, nb, na = parse_best_yes_no(fresh_ob)
            except Exception as e:
                log.warning(f"[RETRY] #{attempt} OB fail: {e}")
                time.sleep(POLL_SECONDS)
                continue

            cur_bid  = yb if current_side == "yes" else nb
            cur_ask  = ya if current_side == "yes" else na
            other_bid = nb if current_side == "yes" else yb

            # Pivot: other side is now more certain AND our side dropped
            if (other_bid is not None and other_bid >= CONFIRM_THRESHOLD and
                    (cur_bid is None or cur_bid < CONFIRM_THRESHOLD)):
                if total_filled > 0:
                    log.warning(f"[PIVOT-BLOCKED] Already filled {total_filled}ct — holding {current_side}")
                    break
                if resting_oid:
                    try:
                        cancel_order(client, resting_oid)
                        info = client.request("GET", f"/portfolio/orders/{resting_oid}")
                        if info:
                            filled_now = info.get("quantity_filled", 0) or 0
                            if filled_now > total_filled:
                                total_filled = filled_now
                    except Exception:
                        pass
                    resting_oid = None
                other = "no" if current_side == "yes" else "yes"
                log.warning(f"[PIVOT] {current_side.upper()} → {other.upper()}")
                current_side = other
                st.side      = current_side
                last_price   = None
                time.sleep(POLL_SECONDS)
                continue

            # Determine buy price (ask + 1¢, capped at 99)
            if cur_ask is not None:
                new_price = min(cur_ask + 1, 99)
            elif cur_bid is not None:
                new_price = min(cur_bid + 1, 99)
            else:
                new_price = 99

            # Check if resting order filled
            if resting_oid:
                try:
                    info = get_order(client, resting_oid)
                    if info:
                        filled_now = info.get("quantity_filled", 0) or 0
                        status     = info.get("status", "")
                        if filled_now > total_filled:
                            total_filled = filled_now
                        if status == "filled" or total_filled >= order_qty:
                            log.warning(f"[FILLED] {st.market} {total_filled}ct {current_side.upper()}")
                            resting_oid = None
                            break
                except Exception:
                    pass

            # Cancel and replace if price changed
            if resting_oid and new_price != last_price:
                try:
                    cancel_order(client, resting_oid)
                    info = client.request("GET", f"/portfolio/orders/{resting_oid}")
                    if info:
                        fn = info.get("quantity_filled", 0) or 0
                        if fn > total_filled:
                            total_filled = fn
                except Exception:
                    pass
                resting_oid = None

            if total_filled >= order_qty:
                break

            # Place new order
            if not resting_oid:
                remaining = order_qty - total_filled
                if remaining <= 0:
                    break
                cost = remaining * new_price / 100
                log.warning(f"[RETRY] #{attempt} {current_side.upper()}@{new_price}¢ qty={remaining} ${cost:.2f} t={deadline-time.time():.0f}s left")
                try:
                    oid = place_order(client, st.market, current_side, new_price, remaining, close_ts)
                    if oid and not oid.startswith("BLOCKED") and not oid.startswith("DRY") and not oid.startswith("ERROR"):
                        resting_oid = oid
                        last_price  = new_price
                        st.order_id = oid
                        st.entry_price_cents = new_price
                    else:
                        log.warning(f"[ORDER-FAIL] {oid}")
                except Exception as e:
                    err = str(e)
                    if "insufficient_balance" in err.lower():
                        log.warning(f"[RETRY] Insufficient balance — stopping")
                        try:
                            cash, pv = get_balance_usd(client)
                            if cash is not None:
                                st.live_balance_usd = cash
                        except Exception:
                            pass
                        break
                    log.warning(f"[RETRY] #{attempt} order error: {e}")

            time.sleep(POLL_SECONDS)

        # Cancel any remaining resting order
        if resting_oid:
            try:
                cancel_order(client, resting_oid)
                info = client.request("GET", f"/portfolio/orders/{resting_oid}")
                if info:
                    fn = info.get("quantity_filled", 0) or 0
                    if fn > total_filled:
                        total_filled = fn
            except Exception:
                pass

        st.qty               = total_filled
        st.side              = current_side
        st.entry_price_cents = last_price

        if total_filled > 0:
            net_win = total_filled * (100 - (last_price or 99)) / 100.0
            log.warning(
                f"[RESULT] {st.market} filled={total_filled}ct "
                f"{current_side.upper()}@{last_price}¢ "
                f"potential_win=${net_win:.2f} attempts={attempt}"
            )
        else:
            log.warning(f"[RESULT] {st.market} 0 fills after {attempt} attempts")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error(f"[FATAL] {e}", exc_info=True)
        sys.exit(1)
