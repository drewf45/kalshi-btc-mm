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
import scoring_v10
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

print(f"BOOT: btc_bot.py v5 loaded at {datetime.now(timezone.utc).isoformat()}Z", flush=True)

# ======================== LOGGING ============================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-btc")
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
BOT_ID          = "BTC"
ASSET           = "BTC"
SERIES_TICKER   = getenv_first(["SERIES", "KALSHI_SERIES"], "KXBTC15M")
API_BASE        = getenv_first(["KALSHI_API_BASE"], "https://api.elections.kalshi.com").rstrip("/")
API_PREFIX      = getenv_first(["KALSHI_API_PREFIX"], "/trade-api/v2").rstrip("/")
API_KEY_ID      = getenv_first(["KALSHI_API_KEY_ID"], "")
PRIVATE_KEY_PEM_B64 = getenv_first(["KALSHI_PRIVATE_KEY_PEM_BASE64"], "")
MARKET_OVERRIDE = getenv_first(["MARKET_OVERRIDE", "KALSHI_MARKET_OVERRIDE"], "<none>")
DRY_RUN         = env_bool("DRY_RUN", False)

# ── PARTICIPATION CONSTANTS ───────────────────────────────────
# BTC whipsaws $100+ per 5-min candle — needs higher certainty than ETH/SOL
WATCH_WINDOW_SECONDS = 180     # Start watching 180s before close — enter while book has depth
CONFIRM_THRESHOLD    = 94      # BTC-specific: raised from 90¢ — whipsaw requires stronger signal
CONFIRM_CHECKS       = 5       # BTC-specific: raised from 4 — need more consecutive ticks
# Dynamic: requires 4 ticks at T=180s, reduces by 1 every 30s → min 1 at T=90s
CONFIRM_STEP         = 30         # seconds per confirm reduction (15m bots: every 30s)
def required_confirms(secs_to_close: float) -> int:
    elapsed = max(0, WATCH_WINDOW_SECONDS - secs_to_close)
    reduction = int(elapsed / CONFIRM_STEP)
    return max(1, CONFIRM_CHECKS - reduction)
SAFETY_NET_SECONDS   = 10      # Fallback: buy best side at T=10s if no position yet
POLL_SECONDS         = 1.0     # Orderbook poll interval
META_REFRESH_SECONDS = 10.0    # Active market refresh interval
MAX_RISK_PCT         = 0.20    # 20% of live balance per trade (hard cap)
MIN_BALANCE_USD      = 5.00    # Don't trade if balance drops below this
EXPIRY_BUFFER_SEC    = 10      # Cancel resting orders this many seconds before close

# ── RISK GUARDS (tunable via env vars, never full block unless truly terrible) ──
# 1. Threshold proximity — BTC raised: $101 avg 5m range means being close to threshold is fatal
PROXIMITY_SKIP_PCT  = env_float("PROXIMITY_SKIP_PCT",  0.50)  # BTC: raised 0.20→0.50% — skip if within 0.50% of threshold
PROXIMITY_HALF_PCT  = env_float("PROXIMITY_HALF_PCT",  1.50)  # BTC: raised 0.75→1.50% — half size within 1.50%
# 2. Price velocity — how much can coin move during watch window before we skip?
VELOCITY_SKIP_USD   = env_float("VELOCITY_SKIP_USD",   0.0)   # 0 = log only, no skip yet (needs data)
VELOCITY_HALF_USD   = env_float("VELOCITY_HALF_USD",   0.0)   # 0 = disabled
# 3. Confidence slope — if bid drops this many cents from peak during confirm, skip
SLOPE_DROP_SKIP     = env_int("SLOPE_DROP_SKIP",        4)    # BTC: tightened 6→4¢ — less slope tolerance

# ── CORRELATED-RISK & VOLATILE-WINDOW GUARDS ─────────────────
VELOCITY_SKIP_PCT       = env_float("VELOCITY_SKIP_PCT",       0.20)   # BTC: tightened 0.30→0.20% — less velocity tolerance
VOLATILE_THRESHOLD_BONUS = env_int("VOLATILE_THRESHOLD_BONUS",  3)     # +3¢ during afternoon/evening (→93¢)
MORNING_THRESHOLD_BONUS  = env_int("MORNING_THRESHOLD_BONUS",   7)     # +7¢ during 8-10AM (→97¢)
# Volatile windows: 8-10AM (market open), 12-2PM (US/London), 3-4PM, 8-10PM (Asia)
VOLATILE_WINDOWS_ET     = [(8, 10), (12, 14), (15, 16), (20, 22)]
# Size scaling by number of OTHER 15M bots in same direction: [0 others, 1, 2, 3+]
CORR_SCALE              = [1.0, 0.75, 0.55, 0.40]

# ── COINBASE SPOT + CANDLES ───────────────────────────────────
SPOT_URL    = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"

# ── TREND AWARENESS ──────────────────────────────────────────
TREND_WINDOW_CANDLES = 12     # 12 x 5-min = 1hr lookback
TREND_BIAS_BONUS     = 5      # +5¢ against-trend entries
TREND_REFRESH_SEC    = 300    # refresh every 5 min
_trend_state: Dict = {"direction": "neutral", "updated": 0.0, "pct": 0.0}

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
    # GATE: API position check (TRADED_TICKERS gate removed — caller sets it before retry loop)
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
        "post_only":       False,
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


# ======================== TREND AWARENESS ====================
def fetch_trend(http: requests.Session) -> str:
    """Pull last hour of 5-min candles — compute trend AND volatility.

    Volatility insight: The orderbook price (e.g. NO @ 92¢) reflects the market's
    *current* confidence. But it doesn't account for how far the asset can move in
    the remaining 15 minutes. A $101 avg 5-min candle range on BTC means a 92¢ NO
    has a real win probability closer to 80% — the market price OVERSTATES certainty
    for high-volatility assets. We discount the threshold accordingly:
      - Low vol  (avg range < $50):   +0¢  — market price is accurate
      - Med vol  ($50–$150):          +2¢  — modest discount
      - High vol ($150–$300):         +4¢  — significant discount
      - Extreme  (> $300):            +6¢  — orderbook price is unreliable
    """
    try:
        r = http.get(CANDLES_URL, params={"granularity": 300}, timeout=5)
        r.raise_for_status()
        candles = r.json()
        if len(candles) < TREND_WINDOW_CANDLES:
            return "neutral"

        # Trend: 1h direction
        recent_close = float(candles[0][4])
        old_close    = float(candles[TREND_WINDOW_CANDLES - 1][4])
        pct = (recent_close - old_close) / old_close * 100
        _trend_state["pct"] = pct
        direction = "bearish" if pct < -0.5 else "bullish" if pct > 0.5 else "neutral"
        _trend_state["direction"] = direction
        _trend_state["updated"]   = time.time()

        # Volatility: avg high-low range per 5-min candle (last 6 = 30 min)
        ranges = [abs(float(c[2]) - float(c[3])) for c in candles[:6]]
        avg_range = sum(ranges) / len(ranges)
        _trend_state["avg_range"] = avg_range

        # Volatility bonus — discount orderbook price for BTC's actual move risk
        if avg_range > 300:
            vol_bonus = 6
            vol_label = "EXTREME"
        elif avg_range > 150:
            vol_bonus = 4
            vol_label = "HIGH"
        elif avg_range > 50:
            vol_bonus = 2
            vol_label = "MED"
        else:
            vol_bonus = 0
            vol_label = "LOW"
        _trend_state["vol_bonus"] = vol_bonus

        log.warning(
            f"[TREND] {direction.upper()} 1h={pct:+.2f}% | "
            f"vol={vol_label} avg_range=${avg_range:.0f} → +{vol_bonus}¢ threshold discount"
        )
        return direction
    except Exception as e:
        log.warning(f"[TREND] fetch failed: {e}")
        return _trend_state.get("direction", "neutral")

def get_trend() -> str:
    return _trend_state.get("direction", "neutral")

def get_vol_bonus() -> int:
    """Volatility discount on orderbook confidence — higher vol = require higher bid."""
    return _trend_state.get("vol_bonus", 0)


# ======================== VOLATILE & CORR HELPERS ============
def is_volatile_window() -> bool:
    """True if current ET time falls in a known high-volatility window."""
    h = datetime.now(ZoneInfo("America/New_York")).hour
    return any(start <= h < end for start, end in VOLATILE_WINDOWS_ET)

def effective_threshold(side: str = "yes") -> int:
    """Volatility-adjusted bid threshold.

    Layers (additive):
      1. Base: CONFIRM_THRESHOLD (94¢ for BTC)
      2. Volatile window bonus: +3¢ afternoons/evenings, +7¢ morning
      3. Trend bias: +5¢ if entering against prevailing trend
      4. Vol discount: +0-6¢ based on avg 5-min candle range
         (corrects for orderbook price overstating certainty in high-vol conditions)
    """
    h = datetime.now(ZoneInfo("America/New_York")).hour
    if 8 <= h < 10:
        base = CONFIRM_THRESHOLD + MORNING_THRESHOLD_BONUS
    elif is_volatile_window():
        base = CONFIRM_THRESHOLD + VOLATILE_THRESHOLD_BONUS
    else:
        base = CONFIRM_THRESHOLD
    # Trend bias — against-trend entries need extra certainty
    trend = get_trend()
    if (side == "yes" and trend == "bearish") or (side == "no" and trend == "bullish"):
        base = min(99, base + TREND_BIAS_BONUS)
        log.info(f"[TREND-GUARD] {trend.upper()} — {side.upper()} threshold +{TREND_BIAS_BONUS}¢ → {base}¢")
    # Volatility discount — orderbook price overstates certainty when vol is high
    vol_bonus = get_vol_bonus()
    if vol_bonus > 0:
        base = min(99, base + vol_bonus)
        log.info(f"[VOL-GUARD] avg_range=${_trend_state.get('avg_range',0):.0f} — threshold +{vol_bonus}¢ → {base}¢")
    return base

def correlated_bot_count(client: "KalshiClient", side: str, own_coin: str) -> int:
    """Count OTHER 15M bots currently holding an open position in the same direction."""
    try:
        positions = get_positions(client)
        count = 0
        for p in positions:
            ticker = p.get("ticker") or p.get("market_ticker", "")
            if "15M" not in ticker:
                continue
            if own_coin.upper() in ticker.upper():
                continue  # skip own coin
            pos = p.get("position", 0)
            if pos == 0:
                continue
            other_side = "yes" if pos > 0 else "no"
            if other_side == side:
                count += 1
        return count
    except Exception:
        return 0

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
    watch_start_price:  Optional[float] = None
    tick_history:       list          = field(default_factory=list)
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
    log.warning(f"[BOT] BTC 15m — PARTICIPATION-FIRST V5")
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
    last_trend     = 0.0

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
        # Only block on existing position if the market is still ACTIVE and tradeable.
        # If market is determined/finalized (pending settlement from previous deploy),
        # treat as fresh — don't skip the window due to a pending payout.
        market_still_active = False
        if pos != 0:
            try:
                mkt_resp = client.request("GET", f"/markets/{new_ticker}")
                mkt_status = (mkt_resp.get("market") or mkt_resp).get("status", "")
                market_still_active = mkt_status == "active"
            except Exception:
                market_still_active = True  # assume active if can't check
        if pos != 0 and market_still_active:
            st.traded_this_market = True
            st.side = "yes" if pos > 0 else "no"
            st.qty  = abs(pos)
            TRADED_TICKERS.add(new_ticker)
            log.warning(f"[RECON] Existing position on {new_ticker}: {st.side} x{st.qty}")
        else:
            if pos != 0:
                log.warning(f"[RECON] Position on {new_ticker} but market not active — ignoring (pending settlement)")
            st.traded_this_market = False
            st.side               = None
            st.qty                = 0
            st.order_id           = None
            st.entry_price_cents  = None
        st.certainty_counter = 0
        st.certainty_side    = None
        st.watch_active      = False
        st.watch_start_price = None
        st.tick_history      = []

    # ── MAIN LOOP ─────────────────────────────────────────────
    while True:
        _watchdog_ts[0] = time.time()

        # ── Refresh trend (every 5 min) ──────────────────────
        now = time.time()
        if now - last_trend > TREND_REFRESH_SEC:
            fetch_trend(http)
            last_trend = now

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
            st.watch_start_price = fetch_spot(http)
            spot_str = f"${st.watch_start_price:.2f}" if st.watch_start_price else "n/a"
            log.warning(f"[WATCH-START] {st.market} t={secs_to_close:.0f}s spot={spot_str}")

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
            _threshold_yes = effective_threshold("yes")
            _threshold_no  = effective_threshold("no")
            yes_certain = yes_bid is not None and yes_bid >= _threshold_yes
            no_certain  = no_bid  is not None and no_bid  >= _threshold_no

            if yes_certain:
                if st.certainty_side == "yes":
                    st.certainty_counter += 1
                else:
                    st.certainty_side    = "yes"
                    st.certainty_counter = 1
                    st.tick_history      = []
                st.tick_history.append(yes_bid)
                log.info(f"[WATCH] YES@{yes_bid}¢ counter={st.certainty_counter}/{required_confirms(secs_to_close)} t={secs_to_close:.0f}s")
            elif no_certain:
                if st.certainty_side == "no":
                    st.certainty_counter += 1
                else:
                    st.certainty_side    = "no"
                    st.certainty_counter = 1
                    st.tick_history      = []
                st.tick_history.append(no_bid)
                log.info(f"[WATCH] NO@{no_bid}¢ counter={st.certainty_counter}/{required_confirms(secs_to_close)} t={secs_to_close:.0f}s")
            else:
                if st.certainty_counter > 0:
                    log.info(f"[RESET] Dropped below threshold — counter reset")
                st.certainty_counter = 0
                st.certainty_side    = None
                time.sleep(POLL_SECONDS)
                continue

            if st.certainty_counter < required_confirms(secs_to_close):
                time.sleep(POLL_SECONDS)
                continue

        # ── CONFIRMED: enter ─────────────────────────────────
        side = st.certainty_side

        # Counterparty check — verify opposite side has liquidity before entering
        # If YES=None when buying NO (or vice versa), order will never fill
        try:
            cp_ob = client.request("GET", f"/markets/{st.market}/orderbook")
            cp_yb, _, cp_nb, _ = parse_best_yes_no(cp_ob)
            opposite_liquid = cp_nb if side == "yes" else cp_yb
            if opposite_liquid is None:
                log.warning(f"[NO-COUNTERPARTY] {st.market} want {side.upper()} but opposite side empty — waiting for liquidity")
                st.traded_this_market = False
                st.certainty_counter  = 0
                st.certainty_side     = None
                time.sleep(POLL_SECONDS)
                continue
        except Exception as e:
            log.warning(f"[COUNTERPARTY-CHECK] {e} — proceeding anyway")

        # Refresh live balance before sizing
        try:
            cash, pv = get_balance_usd(client)
            if cash is not None:
                st.live_balance_usd = cash
        except Exception as e:
            log.warning(f"[BALANCE-REFRESH] {e}")


        # ── V10 SCORING — gate entry on live signal quality ──
        _avg_range  = _trend_state.get("avg_range", 0)
        _own_trend  = get_trend()
        _btc_trend  = _own_trend  # BTC IS the master signal — own trend == BTC trend
        try:
            from scoring_v10 import evaluate_entry as _v10_eval
            _v10_result = _v10_eval(
                asset        = ASSET,
                side         = side,
                price_cents  = buy_price,
                own_trend    = _own_trend,
                btc_trend    = _btc_trend,
                avg_range    = _avg_range,
                secs_to_close= secs_to_close,
                live_balance = st.live_balance_usd,
            )
            if _v10_result is None:
                log.warning(f"[V10-SKIP] {st.market} {side.upper()}@{buy_price}¢ — score below threshold, skipping")
                TRADED_TICKERS.add(st.market)
                st.traded_this_market = True
                time.sleep(POLL_SECONDS)
                continue
            _, v10_contracts, v10_tier, v10_score = _v10_result
            log.warning(f"[V10-ENTER] {st.market} {side.upper()}@{buy_price}¢ tier={v10_tier} score={v10_score:.3f} contracts={v10_contracts}")
        except Exception as _v10_err:
            log.warning(f"[V10-ERROR] {_v10_err} — falling back to standard sizing")
            v10_contracts = None
            v10_tier = None

        init_bid   = (yes_bid if side == "yes" else no_bid) or 99
        init_ask   = (yes_ask if side == "yes" else no_ask)
        buy_price  = min((init_ask + 1) if init_ask is not None else init_bid, 99)

        # ── RISK GUARDS (run BEFORE sizing) ──────────────────
        risk_size_multiplier = 1.0

        # Guard 1: Confidence slope — bid declining from peak = weakening signal
        if len(st.tick_history) >= 2:
            peak_bid = max(st.tick_history)
            latest_bid = st.tick_history[-1]
            drop = peak_bid - latest_bid
            if drop >= SLOPE_DROP_SKIP:
                log.warning(
                    f"[SLOPE-SKIP] {st.market} bid dropped {drop}¢ from peak {peak_bid}¢ → {latest_bid}¢ "
                    f"— signal weakening, skipping"
                )
                TRADED_TICKERS.add(st.market)
                st.traded_this_market = True
                time.sleep(POLL_SECONDS)
                continue

        # Guard 2: Threshold proximity — price too close to strike = flash wick risk
        spot_check = fetch_spot(http)
        if spot_check:
            _, threshold = market_bounds_usd(st.market_obj)
            if threshold is not None and threshold > 0:
                prox_pct = abs(spot_check - threshold) / threshold * 100
                if prox_pct <= PROXIMITY_SKIP_PCT:
                    log.warning(
                        f"[PROXIMITY-SKIP] {st.market} spot=${spot_check:.4f} threshold=${threshold:.4f} "
                        f"proximity={prox_pct:.3f}% ≤ {PROXIMITY_SKIP_PCT}% — too close, skipping"
                    )
                    TRADED_TICKERS.add(st.market)
                    st.traded_this_market = True
                    time.sleep(POLL_SECONDS)
                    continue
                elif prox_pct <= PROXIMITY_HALF_PCT:
                    log.warning(
                        f"[PROXIMITY-HALF] {st.market} spot=${spot_check:.4f} threshold=${threshold:.4f} "
                        f"proximity={prox_pct:.3f}% ≤ {PROXIMITY_HALF_PCT}% — halving size"
                    )
                    risk_size_multiplier = 0.5

        # Guard 3: Price velocity (pct-based) — direction-aware momentum risk
        # Only skip if velocity is AGAINST our position:
        #   YES entry: price falling toward threshold = bad
        #   NO entry:  price rising toward threshold = bad
        #   Confirming direction = let it ride
        if spot_check and st.watch_start_price:
            delta     = spot_check - st.watch_start_price
            abs_delta = abs(delta)
            pct       = abs_delta / st.watch_start_price * 100
            arrow     = "↑" if delta > 0 else "↓"
            log.warning(
                f"[PRICE-SANITY] watch_start=${st.watch_start_price:.2f} "
                f"now=${spot_check:.2f} move={arrow}${abs_delta:.2f} ({pct:.3f}%)"
            )
            velocity_against = (side == "yes" and delta < 0) or (side == "no" and delta > 0)
            if pct >= VELOCITY_SKIP_PCT and velocity_against:
                log.warning(
                    f"[VELOCITY-SKIP] {st.market} spot moved {pct:.3f}% ≥ {VELOCITY_SKIP_PCT}% "
                    f"AGAINST {side.upper()} — skipping"
                )
                TRADED_TICKERS.add(st.market)
                st.traded_this_market = True
                time.sleep(POLL_SECONDS)
                continue
            elif pct >= VELOCITY_SKIP_PCT:
                log.warning(
                    f"[VELOCITY-CONFIRM] {st.market} spot moved {pct:.3f}% "
                    f"CONFIRMING {side.upper()} — proceeding"
                )
        elif spot_check:
            log.warning(f"[PRICE-SANITY] now=${spot_check:.2f} (no watch_start recorded)")

        # Guard 4: Correlated direction — scale down if other bots entering same way
        corr_count = correlated_bot_count(client, side, BOT_ID)
        if corr_count > 0:
            scale_idx = min(corr_count, len(CORR_SCALE) - 1)
            risk_size_multiplier = min(risk_size_multiplier, CORR_SCALE[scale_idx])
            log.warning(
                f"[CORR-SCALE] {corr_count} other bot(s) entering {side.upper()} — "
                f"size multiplier → {risk_size_multiplier:.2f}"
            )

        # Guard 5: Volatile window context log
        if is_volatile_window():
            log.warning(
                f"[VOLATILE-WINDOW] active — threshold was {effective_threshold()}¢ "
                f"(+{VOLATILE_THRESHOLD_BONUS}¢), size multiplier={risk_size_multiplier:.2f}"
            )

        # ── SIZE (calculated AFTER all guards have set multiplier) ───────────
        position_size = st.live_balance_usd * MAX_RISK_PCT * risk_size_multiplier
        order_qty     = max(1, min(500, int(position_size / (buy_price / 100))))

        if order_qty * buy_price / 100 < 1.00:
            log.info(f"[SKIP] Cost < $1.00 — skip")
            TRADED_TICKERS.add(st.market)
            st.traded_this_market = True
            time.sleep(POLL_SECONDS)
            continue

        log.warning(
            f"[ENTER] {st.market} {side.upper()}@{buy_price}¢ "
            f"qty={order_qty} cost=${order_qty * buy_price / 100:.2f} "
            f"bal=${st.live_balance_usd:.2f} mult={risk_size_multiplier:.2f} t={secs_to_close:.0f}s"
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
            _watchdog_ts[0] = time.time()  # keep watchdog alive during retry
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
            log.warning(f"[MISS] {st.market} — entered but unfilled. Treat as lost market.")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error(f"[FATAL] {e}", exc_info=True)
        sys.exit(1)
