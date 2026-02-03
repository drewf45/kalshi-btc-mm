# bot.py
# Kalshi rolling 15m BTC SCALPER - Trade every market with edge
#
# STRATEGY:
# - Arms at T-360s (6 minutes before close) for early edge capture
# - Trades aggressively: 55% prob, 1% edge, up to 90¢ entry
# - Scales bankroll UP on wins, pulls back on losses
# - DUMPS fast if probability flips or drawdown hits limits
# - Session-level loss limits to protect capital
#
# KEY SETTINGS:
# - ENTRY_START_SECONDS=360 (6 min window)
# - ENTRY_LAST_SECONDS=30 (trade until 30s before close)
# - PROB_MIN=0.55 (trade more markets)
# - EDGE_MIN=0.01 (1% minimum edge)
# - MAX_ENTRY_PRICE=90¢ (allow higher entries)
# - BANKROLL_FRACTION=0.15 (15% base sizing)
# - Bankroll scaling: increase on wins, decrease on losses
# - Session loss limit: stop trading if down too much

import os
import time
import base64
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding


# -----------------------------
# HARD BOOT BANNER (so you ALWAYS see something if python starts)
# -----------------------------
print(f"BOOT: bot.py loaded at {datetime.now(timezone.utc).isoformat()}Z", flush=True)


# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")
log.warning("BOOT: logger initialized", extra={})

# Early debug: Check if credentials exist
print(f"DEBUG: KALSHI_API_KEY_ID exists: {bool(os.getenv('KALSHI_API_KEY_ID'))}", flush=True)
print(f"DEBUG: KALSHI_PRIVATE_KEY_PEM_BASE64 exists: {bool(os.getenv('KALSHI_PRIVATE_KEY_PEM_BASE64'))}", flush=True)
print(f"DEBUG: KALSHI_API_BASE = {os.getenv('KALSHI_API_BASE', 'not set')}", flush=True)


# -----------------------------
# Env helpers (keep style)
# -----------------------------
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


def env_keys_with_prefix(prefix: str) -> List[str]:
    return sorted([k for k in os.environ.keys() if k.startswith(prefix)])


def now_ms() -> int:
    return int(time.time() * 1000)


def clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(x)))


def clamp_float(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


# -----------------------------
# Config (MODIFIED FOR AGGRESSIVE STRATEGY)
# -----------------------------
API_BASE = getenv_first(["KALSHI_API_BASE"], "https://api.elections.kalshi.com").rstrip("/")
API_PREFIX = getenv_first(["KALSHI_API_PREFIX"], "/trade-api/v2").rstrip("/")
API_KEY_ID = getenv_first(["KALSHI_API_KEY_ID"], "")
PRIVATE_KEY_PEM_B64 = getenv_first(["KALSHI_PRIVATE_KEY_PEM_BASE64"], "")

SERIES_TICKER = getenv_first(["SERIES", "KALSHI_SERIES", "KALSHI_SERIES_TICKER"], "KXBTC15M")
EVENT_TICKER = getenv_first(["EVENT_TICKER", "KALSHI_EVENT_TICKER"], "<auto>")
MARKET_OVERRIDE = getenv_first(["MARKET_OVERRIDE", "KALSHI_MARKET_OVERRIDE"], "<none>")

POLL_SECONDS = env_float("POLL_SECONDS", 1.0)  # Check every second for dumps
META_REFRESH_SECONDS = env_float("META_REFRESH", 10.0)

DRY_RUN = env_bool("DRY_RUN", False)
ENABLE_TRADING = env_bool("ENABLE_TRADING", True)
POST_ONLY = env_bool("POST_ONLY", False)  # Use market orders for faster fills

ORDER_QTY = env_int("ORDER_QTY", 1)

COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

BOOTSTRAP_CANCEL_OPEN_ORDERS = env_bool("BOOTSTRAP_CANCEL_OPEN_ORDERS", True)

# -------------- SCALPER STRATEGY (HARDWIRED) --------------
ENTRY_START_SECONDS = 360  # 6 min before close - HARDWIRED
ENTRY_DECISION_SECONDS = 60
ENTRY_LAST_SECONDS = 30  # Trade until 30s before close - HARDWIRED
FILL_WAIT_SECONDS = 20
ALLOW_TAKER_AT_LAST = True
CANCEL_UNFILLED_AT_CLOSE = True

PROB_MIN = 0.55  # Lower threshold = more trades - HARDWIRED
EDGE_MIN = 0.01  # 1% minimum edge - HARDWIRED
MAX_ENTRY_PRICE_CENTS = 90  # Allow up to 90¢ - HARDWIRED
FEE_CENTS_PER_CONTRACT = 0

SPOT_SIGMA_USD_PER_SQRT_SEC = 12.0

BANKROLL_FRACTION = 0.15  # 15% base per trade - HARDWIRED
MIN_CONTRACTS = 1
MAX_CONTRACTS = 100  # Allow bigger positions
MIN_FREE_USD_TO_TRADE = 5.0

# -------------- BANKROLL SCALING (HARDWIRED) --------------
ENABLE_BANKROLL_SCALING = True
SCALING_WIN_MULTIPLIER = 1.25  # +25% after win
SCALING_LOSS_MULTIPLIER = 0.70  # -30% after loss
SCALING_MIN_FRACTION = 0.05  # Floor at 5%
SCALING_MAX_FRACTION = 0.35  # Cap at 35%

# -------------- SESSION LOSS LIMITS (HARDWIRED) --------------
ENABLE_SESSION_LIMITS = True
SESSION_MAX_LOSS_USD = 50.0  # Stop if down $50
SESSION_MAX_LOSS_PERCENT = 0.20  # Or 20% of starting
SESSION_CONSECUTIVE_LOSSES_LIMIT = 5  # Pause after 5 losses
SESSION_COOLDOWN_MINUTES = 15  # Cooldown after limit hit

ONE_TRADE_PER_MARKET = env_bool("ONE_TRADE_PER_MARKET", True)
CANCEL_ALL_STRAYS_ALWAYS = env_bool("CANCEL_ALL_STRAYS_ALWAYS", True)

LOG_DECISIONS = env_bool("LOG_DECISIONS", True)
LOG_STATE_EVERY_SECONDS = env_float("LOG_STATE_EVERY_SECONDS", 10.0)

JOIN_UP_CENTS = env_int("JOIN_UP_CENTS", 0)
OB_WARN_EVERY_SECONDS = env_float("OB_WARN_EVERY_SECONDS", 2.0)

# -------------- DUMP CONFIGURATION (HARDWIRED - less aggressive) --------------
ENABLE_DUMP = True
DUMP_PROB_FLIP = 0.35  # Exit if prob drops below 35% - HARDWIRED
DUMP_PROB_DROP_PERCENT = 0.30  # Exit on 30% drop (less aggressive) - HARDWIRED
DUMP_MARKET_FLIP_THRESHOLD = 0.30
DUMP_MIN_TIME_REMAINING = 15  # Can dump closer to settlement
DUMP_ON_PRICE_DANGER = True
DUMP_PRICE_SIGMA_MULTIPLIER = 1.5  # Less sensitive to price swings

# -------------- SMART DUMP (cut losses, let winners ride) --------------
DUMP_IF_LOSING_CENTS = 25  # Dump if underwater by 25¢+ (more room)
DUMP_PROTECT_PROFIT_CENTS = 15  # Lock in 15¢+ profit

# -------------- A-LEVEL ADDITIONS --------------
USE_MARKET_IMPLIED = env_bool("USE_MARKET_IMPLIED", True)
MODEL_BLEND_ALPHA = env_float("MODEL_BLEND_ALPHA", 0.75)

REQUIRE_DIVERGENCE = env_bool("REQUIRE_DIVERGENCE", False)
MIN_DIVERGENCE = env_float("MIN_DIVERGENCE", 0.015)

USE_DYNAMIC_SIGMA = env_bool("USE_DYNAMIC_SIGMA", True)
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
CANDLES_GRANULARITY_SEC = env_int("CANDLES_GRANULARITY_SEC", 60)
CANDLES_LOOKBACK = env_int("CANDLES_LOOKBACK", 10)
SIGMA_FLOOR = env_float("SIGMA_FLOOR", 6.0)
SIGMA_CEIL = env_float("SIGMA_CEIL", 40.0)

MAX_SPREAD_CENTS_TO_TRADE = env_int("MAX_SPREAD_CENTS_TO_TRADE", 8)
REQUIRE_BOTH_SIDES_BOOK = env_bool("REQUIRE_BOTH_SIDES_BOOK", False)

SIGMA_REFRESH_SECONDS = env_float("SIGMA_REFRESH_SECONDS", 5.0)
_last_sigma_ts: float = 0.0
_last_sigma_val: float = SPOT_SIGMA_USD_PER_SQRT_SEC

# -------------- SIZING + PROB GATE --------------
PROB_GATE_USE_BLEND = env_bool("PROB_GATE_USE_BLEND", True)
BANKROLL_FRACTION_HARD_CAP = env_float("BANKROLL_FRACTION_HARD_CAP", 0.40)  # Allow up to 40%

EDGE_SIZE_START = env_float("EDGE_SIZE_START", 0.015)  # Start scaling earlier
EDGE_SIZE_SLOPE = env_float("EDGE_SIZE_SLOPE", 3.0)  # Steeper scaling

A_PLUS_PROB = env_float("A_PLUS_PROB", 0.85)  # Lower bar for A+ trades
A_PLUS_EDGE = env_float("A_PLUS_EDGE", 0.025)
A_PLUS_FRACTION = env_float("A_PLUS_FRACTION", 0.25)  # Go bigger on A+ setups

HIGH_CERTAINTY_PROB = env_float("HIGH_CERTAINTY_PROB", 0.95)  # Slightly lower
HIGH_CERTAINTY_TIME_SEC = env_int("HIGH_CERTAINTY_TIME_SEC", 15)
HIGH_CERTAINTY_MAX_PRICE = env_int("HIGH_CERTAINTY_MAX_PRICE", 99)

LAST_CHANCE_TIME_SEC = env_int("LAST_CHANCE_TIME_SEC", 20)
LAST_CHANCE_MIN_PROB = env_float("LAST_CHANCE_MIN_PROB", 0.85)

BOUNDARY_BUFFER_USD = env_float("BOUNDARY_BUFFER_USD", 75.0)  # Tighter buffer
LATE_ENTRY_PROB_BOOST = env_float("LATE_ENTRY_PROB_BOOST", 0.03)
LATE_ENTRY_TIME_SEC = env_int("LATE_ENTRY_TIME_SEC", 30)

# Heartbeat
HEARTBEAT_SECONDS = env_float("HEARTBEAT_SECONDS", 15.0)


# -----------------------------
# Kalshi API client (RSA-PSS signing)  **UNCHANGED**
# -----------------------------
class KalshiClient:
    def __init__(self, api_base: str, api_prefix: str, key_id: str, private_key_pem_b64: str):
        self.api_base = api_base.rstrip("/")
        self.api_prefix = api_prefix if api_prefix.startswith("/") else f"/{api_prefix}"
        self.key_id = key_id

        if not private_key_pem_b64:
            raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_BASE64")

        pem_bytes = base64.b64decode(private_key_pem_b64)
        self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)

        self.session = requests.Session()

    def _sign_headers(self, method: str, full_url: str) -> Dict[str, str]:
        ts = str(now_ms())
        parsed = urlparse(full_url)
        path = parsed.path
        msg = f"{ts}{method.upper()}{path}".encode("utf-8")

        sig = self.private_key.sign(
            msg,
            asy_padding.PSS(
                mgf=asy_padding.MGF1(hashes.SHA256()),
                salt_length=asy_padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(sig).decode("utf-8")

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Any:
        if not path.startswith("/"):
            path = "/" + path

        url = f"{self.api_base}{self.api_prefix}{path}"
        url_with_q = url + "?" + urlencode(params) if params else url

        headers = self._sign_headers(method, url)
        headers["Accept"] = "application/json"
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        resp = self.session.request(
            method=method.upper(),
            url=url_with_q,
            headers=headers,
            json=json_body,
            timeout=timeout,
        )

        if resp.status_code >= 400:
            body = resp.text or ""
            raise RuntimeError(f"HTTP {resp.status_code} {path}: body={body}")

        if resp.content:
            return resp.json()
        return None


# -----------------------------
# Robust close_ts resolver **UNCHANGED**
# -----------------------------
NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_iso_to_epoch_s(s: str) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp())
    except Exception:
        return None


def infer_close_ts_from_ticker(ticker: str, interval_minutes: int = 15) -> Optional[int]:
    if not ticker or ticker is None:
        log.warning(f"[TICKER] infer_close_ts received None/empty ticker")
        return None
        
    try:
        parts = str(ticker).split("-")
        if len(parts) < 2:
            return None
        dt_chunk = parts[1]

        day = int(dt_chunk[0:2])
        mon = MONTHS[dt_chunk[2:5].upper()]
        yy = int(dt_chunk[5:7])
        year = 2000 + yy
        hh = int(dt_chunk[7:9])
        mm = int(dt_chunk[9:11])

        start_local = datetime(year, mon, day, hh, mm, tzinfo=NY)
        close_local = start_local + timedelta(minutes=int(interval_minutes))
        close_utc = close_local.astimezone(UTC)
        return int(close_utc.timestamp())
    except Exception as e:
        log.warning(f"[TICKER] Failed to parse ticker '{ticker}': {e}")
        return None


def resolve_close_ts(market_obj: Dict[str, Any], ticker: str) -> Optional[int]:
    if not isinstance(market_obj, dict):
        market_obj = {}

    for k in (
        "close_ts", "closeTs", "close_time_ts", "closeTimeTs", "close_timestamp", "closeTimestamp",
        "close_time", "closeTime", "expiration_ts", "expirationTs"
    ):
        v = market_obj.get(k)
        if isinstance(v, (int, float)):
            vv = int(v)
            return vv // 1000 if vv > 10_000_000_000 else vv

    for k in ("market", "data"):
        sub = market_obj.get(k)
        if isinstance(sub, dict):
            for kk in ("close_ts", "close_time", "close_timestamp"):
                v = sub.get(kk)
                if isinstance(v, (int, float)):
                    vv = int(v)
                    return vv // 1000 if vv > 10_000_000_000 else vv

    for k in ("close_time", "closeTime", "close_datetime", "closeDateTime", "expiration_time", "expirationTime"):
        v = market_obj.get(k)
        if isinstance(v, str):
            ts = _parse_iso_to_epoch_s(v)
            if ts is not None:
                return ts

    return infer_close_ts_from_ticker(ticker, interval_minutes=15)


# -----------------------------
# Market selection / parsing **UNCHANGED**
# -----------------------------
def pick_active_market(markets: List[Dict[str, Any]]) -> Tuple[str, str, Dict[str, Any]]:
    now_ts = int(time.time())

    def get_ts(obj: Dict[str, Any], key: str) -> Optional[int]:
        v = obj.get(key)
        if v is None:
            return None
        try:
            vv = int(v)
            return vv // 1000 if vv > 10_000_000_000 else vv
        except Exception:
            return None

    candidates = []
    for m in markets:
        status = str(m.get("status", "")).lower()
        if status and status != "open":
            continue
        ot = get_ts(m, "open_time") or get_ts(m, "open_ts") or get_ts(m, "open_timestamp")
        ct = get_ts(m, "close_time") or get_ts(m, "close_ts") or get_ts(m, "close_timestamp")
        candidates.append((ot, ct, m))

    active = []
    future = []
    for ot, ct, m in candidates:
        if ot is not None and ct is not None and ot <= now_ts < ct:
            active.append((ct, m))
        elif ct is not None and ct > now_ts:
            future.append((ct, m))

    if active:
        active.sort(key=lambda x: x[0])
        chosen = active[0][1]
    elif future:
        future.sort(key=lambda x: x[0])
        chosen = future[0][1]
    else:
        chosen = markets[0] if markets else {}
        if not chosen:
            raise RuntimeError("No markets available to pick from.")

    market_ticker = chosen.get("ticker") or chosen.get("market_ticker")
    event_ticker = chosen.get("event_ticker") or chosen.get("event", {}).get("ticker") or chosen.get("event_ticker")

    if not market_ticker or not event_ticker:
        raise RuntimeError(f"Could not determine event/market ticker from market object: {chosen}")

    return str(event_ticker), str(market_ticker), chosen


def extract_close_ts(market_obj: Dict[str, Any], market_ticker: str) -> Optional[int]:
    return resolve_close_ts(market_obj, market_ticker)


def market_bounds_usd(market_obj: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    lo = None
    hi = None
    for lo_key in ("floor_strike", "lower_strike", "strike_lower", "floor"):
        if lo_key in market_obj:
            try:
                lo = float(market_obj[lo_key])
                break
            except Exception:
                lo = None
    for hi_key in ("cap_strike", "upper_strike", "strike_upper", "cap"):
        if hi_key in market_obj:
            try:
                hi = float(market_obj[hi_key])
                break
            except Exception:
                hi = None
    return lo, hi


# -----------------------------
# Spot + probability model **UNCHANGED**
# -----------------------------
def fetch_btc_spot_usd(session: requests.Session, timeout: float = 5.0) -> Optional[float]:
    try:
        r = session.get(COINBASE_SPOT_URL, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        amt = data.get("data", {}).get("amount")
        if amt is None:
            return None
        return float(amt)
    except Exception:
        return None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_yes_in_range(mean: float, lo: Optional[float], hi: Optional[float], sd: float) -> float:
    if sd <= 0:
        sd = 1e-9
    if lo is None and hi is None:
        return 0.5
    if lo is not None and hi is not None:
        z_hi = (hi - mean) / sd
        z_lo = (lo - mean) / sd
        return max(0.0, min(1.0, _norm_cdf(z_hi) - _norm_cdf(z_lo)))
    if lo is not None:
        z = (lo - mean) / sd
        return max(0.0, min(1.0, 1.0 - _norm_cdf(z)))
    z = (hi - mean) / sd
    return max(0.0, min(1.0, _norm_cdf(z)))


# -----------------------------
# A-LEVEL: dynamic sigma helpers **UNCHANGED**
# -----------------------------
def fetch_coinbase_candles(session: requests.Session, granularity: int, timeout: float = 5.0) -> Optional[List[List[float]]]:
    try:
        params = {"granularity": int(granularity)}
        r = session.get(COINBASE_CANDLES_URL, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        return data
    except Exception:
        return None


def realized_sigma_usd_per_sqrt_sec(session: requests.Session) -> Optional[float]:
    candles = fetch_coinbase_candles(session, CANDLES_GRANULARITY_SEC)
    if not candles or len(candles) < 3:
        return None

    take = candles[: max(3, int(CANDLES_LOOKBACK))]
    take_sorted = sorted(take, key=lambda x: float(x[0]))

    closes: List[float] = []
    for c in take_sorted:
        try:
            closes.append(float(c[4]))
        except Exception:
            continue

    if len(closes) < 3:
        return None

    diffs: List[float] = []
    for i in range(1, len(closes)):
        diffs.append(closes[i] - closes[i - 1])

    if len(diffs) < 2:
        return None

    mean = sum(diffs) / len(diffs)
    var = sum((x - mean) ** 2 for x in diffs) / max(1, (len(diffs) - 1))
    sd_per_min = math.sqrt(max(0.0, var))

    sigma = sd_per_min / math.sqrt(60.0)
    return float(sigma)


def get_sigma_cached(http: requests.Session) -> float:
    global _last_sigma_ts, _last_sigma_val
    now = time.time()
    if not USE_DYNAMIC_SIGMA:
        return float(SPOT_SIGMA_USD_PER_SQRT_SEC)
    if (now - _last_sigma_ts) < float(SIGMA_REFRESH_SECONDS):
        return float(_last_sigma_val)

    rs = realized_sigma_usd_per_sqrt_sec(http)
    if rs is None or rs <= 0:
        _last_sigma_val = float(SPOT_SIGMA_USD_PER_SQRT_SEC)
    else:
        _last_sigma_val = float(max(SIGMA_FLOOR, min(SIGMA_CEIL, rs)))
    _last_sigma_ts = now
    return float(_last_sigma_val)


# -----------------------------
# Orderbook parsing **UNCHANGED**
# -----------------------------
def _best_from_levels(levels: Any, want: str) -> Optional[int]:
    if not isinstance(levels, list) or not levels:
        return None
    best: Optional[int] = None
    for lv in levels:
        p = None
        if isinstance(lv, (list, tuple)) and len(lv) >= 1:
            try:
                p = int(lv[0])
            except Exception:
                p = None
        elif isinstance(lv, dict):
            for k in ("price", "yes_price", "p"):
                if k in lv:
                    try:
                        p = int(lv[k])
                        break
                    except Exception:
                        p = None
        if p is None:
            continue
        if best is None:
            best = p
        else:
            best = max(best, p) if want == "bid" else min(best, p)
    if best is None:
        return None
    return clamp_int(best, 1, 99)


def parse_best_yes_no(ob: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    if not isinstance(ob, dict):
        return None, None, None, None

    root = ob.get("orderbook") if isinstance(ob.get("orderbook"), dict) else ob

    yes_bid = yes_ask = no_bid = no_ask = None

    if isinstance(root, dict) and isinstance(root.get("yes"), dict):
        y = root.get("yes", {})
        n = root.get("no", {})
        yes_bid = _best_from_levels(y.get("bids", y.get("buy")), "bid")
        yes_ask = _best_from_levels(y.get("asks", y.get("sell")), "ask")
        if isinstance(n, dict):
            no_bid = _best_from_levels(n.get("bids", n.get("buy")), "bid")
            no_ask = _best_from_levels(n.get("asks", n.get("sell")), "ask")

    if isinstance(root, dict) and (isinstance(root.get("yes"), list) or isinstance(root.get("no"), list)):
        if yes_bid is None and isinstance(root.get("yes"), list):
            yes_bid = _best_from_levels(root.get("yes"), "bid")
        if no_bid is None and isinstance(root.get("no"), list):
            no_bid = _best_from_levels(root.get("no"), "bid")

    if yes_ask is None and no_bid is not None:
        yes_ask = clamp_int(100 - no_bid, 1, 99)
    if no_ask is None and yes_bid is not None:
        no_ask = clamp_int(100 - yes_bid, 1, 99)

    if yes_bid is None and no_ask is not None:
        yes_bid = clamp_int(100 - no_ask, 1, 99)
    if no_bid is None and yes_ask is not None:
        no_bid = clamp_int(100 - yes_ask, 1, 99)

    if yes_bid is not None and yes_ask is not None and yes_ask <= yes_bid:
        yes_ask = None
    if no_bid is not None and no_ask is not None and no_ask <= no_bid:
        no_ask = None

    return yes_bid, yes_ask, no_bid, no_ask


# -----------------------------
# A-LEVEL: market-implied probability + book sanity **UNCHANGED**
# -----------------------------
def implied_prob_from_book(
    yes_bid: Optional[int],
    yes_ask: Optional[int],
    no_bid: Optional[int],
    no_ask: Optional[int],
) -> Optional[float]:
    if yes_bid is not None and yes_ask is not None and yes_ask > yes_bid:
        return max(0.01, min(0.99, (yes_bid + yes_ask) / 200.0))
    if no_bid is not None and no_ask is not None and no_ask > no_bid:
        no_mid = (no_bid + no_ask) / 200.0
        return max(0.01, min(0.99, 1.0 - no_mid))
    if yes_bid is not None:
        return max(0.01, min(0.99, yes_bid / 100.0))
    if yes_ask is not None:
        return max(0.01, min(0.99, yes_ask / 100.0))
    return None


def spread_ok(bid: Optional[int], ask: Optional[int]) -> bool:
    if bid is None or ask is None:
        return not REQUIRE_BOTH_SIDES_BOOK
    return (ask - bid) <= int(MAX_SPREAD_CENTS_TO_TRADE)


# -----------------------------
# Orders / portfolio helpers **UNCHANGED**
# -----------------------------
def get_open_orders(client: KalshiClient) -> List[Dict[str, Any]]:
    resp = client.request("GET", "/portfolio/orders", params={"status": "resting", "limit": 200})
    if isinstance(resp, dict):
        return resp.get("orders", [])
    return resp if isinstance(resp, list) else []


def cancel_order_status(client: KalshiClient, order_id: str) -> str:
    try:
        client.request("DELETE", f"/portfolio/orders/{order_id}")
        return "canceled"
    except RuntimeError as e:
        msg = str(e)
        if ("HTTP 404" in msg) or ("not_found" in msg):
            return "not_found"
        raise


def place_order(client: KalshiClient, payload: Dict[str, Any]) -> str:
    resp = client.request("POST", "/portfolio/orders", json_body=payload)
    if isinstance(resp, dict):
        if "order" in resp and isinstance(resp["order"], dict) and resp["order"].get("order_id"):
            return str(resp["order"]["order_id"])
        if resp.get("order_id"):
            return str(resp["order_id"])
    raise RuntimeError(f"Unexpected create order response: {resp}")


def get_positions(client: KalshiClient) -> List[Dict[str, Any]]:
    resp = client.request("GET", "/portfolio/positions", params={"limit": 200})
    if isinstance(resp, dict):
        for k in ("positions", "market_positions", "portfolio_positions"):
            if k in resp and isinstance(resp[k], list):
                return resp[k]
    if isinstance(resp, list):
        return resp
    return []


def parse_position_for_market(positions: List[Dict[str, Any]], market_ticker: str) -> int:
    mt = str(market_ticker)
    for p in positions:
        t = p.get("ticker") or p.get("market_ticker") or p.get("contract_ticker")
        if not t or str(t) != mt:
            continue
        for k in ("position", "net_position", "yes_position", "net_yes_position", "qty", "count"):
            if k in p:
                try:
                    return int(p[k])
                except Exception:
                    continue
        return 0
    return 0


def get_balance_usd(client: KalshiClient) -> Tuple[Optional[float], Optional[float]]:
    try:
        resp = client.request("GET", "/portfolio/balance")
    except Exception:
        return None, None
    if not isinstance(resp, dict):
        return None, None

    base = resp.get("balance") if isinstance(resp.get("balance"), dict) else resp

    cand_available = [
        "available_balance",
        "available",
        "available_cash",
        "available_funds",
        "free_collateral",
        "available_collateral",
    ]
    cand_total = [
        "balance",
        "total_balance",
        "total",
        "equity",
        "account_value",
    ]

    av = None
    tot = None

    for k in cand_available:
        if k in base:
            try:
                av = float(base[k])
                break
            except Exception:
                pass
    for k in cand_total:
        if k in base:
            try:
                tot = float(base[k])
                break
            except Exception:
                pass

    return av, tot


def build_order_payload(
    market_ticker: str,
    action: str,
    side: str,
    price_cents: int,
    count: int,
    post_only: bool,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "ticker": market_ticker,
        "action": action,
        "side": side,
        "type": "limit",
        "count": int(count),
    }
    if side == "yes":
        body["yes_price"] = int(price_cents)
    else:
        body["no_price"] = int(price_cents)
    if post_only:
        body["post_only"] = True
    return body


def cancel_all_strays_for_market(client: KalshiClient, market_ticker: str) -> None:
    try:
        oo = get_open_orders(client)
    except Exception as e:
        log.warning(f"[CLEAN] failed to fetch open orders: {e}")
        return
    killed = 0
    for o in oo:
        if str(o.get("ticker")) != str(market_ticker):
            continue
        oid = o.get("order_id") or o.get("id")
        if not oid:
            continue
        try:
            st = cancel_order_status(client, str(oid))
            killed += 1
            log.warning(f"[CLEAN] {market_ticker} canceled stray order_id={oid} status={st}")
        except Exception as ce:
            log.warning(f"[CLEAN] cancel failed order_id={oid}: {ce}")
    if killed:
        log.warning(f"[CLEAN] {market_ticker} canceled {killed} stray orders")


# -----------------------------
# State machine (MODIFIED FOR DUMP LOGIC)
# -----------------------------
class SM:
    IDLE = "IDLE"
    ARMED = "ARMED"
    ORDER_WAIT = "ORDER_WAIT"
    HOLD = "HOLD"  # Now actively monitors for dump conditions
    DUMPED = "DUMPED"  # NEW: Position was dumped early
    ROLL = "ROLL"
    COOLDOWN = "COOLDOWN"  # Paused due to session limits


@dataclass
class SessionState:
    """Track session-level P&L and scaling"""
    starting_balance_usd: float = 0.0
    current_pnl_usd: float = 0.0

    # Win/loss tracking
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    consecutive_wins: int = 0

    # Bankroll scaling
    current_fraction: float = BANKROLL_FRACTION  # Dynamic fraction

    # Trade history (last N for analysis)
    recent_trades: List[Dict[str, Any]] = None

    # Session limits
    is_paused: bool = False
    pause_until: float = 0.0  # Unix timestamp
    pause_reason: Optional[str] = None

    # Markets traded this session
    markets_traded: int = 0

    def __post_init__(self):
        if self.recent_trades is None:
            self.recent_trades = []

    def record_trade(self, market: str, side: str, entry_price: int, exit_price: Optional[int],
                     qty: int, pnl_cents: int, was_dump: bool = False):
        """Record a completed trade and update scaling"""
        pnl_usd = pnl_cents / 100.0
        self.current_pnl_usd += pnl_usd
        self.markets_traded += 1

        trade = {
            "market": market,
            "side": side,
            "entry": entry_price,
            "exit": exit_price,
            "qty": qty,
            "pnl_cents": pnl_cents,
            "pnl_usd": pnl_usd,
            "was_dump": was_dump,
            "ts": time.time(),
        }
        self.recent_trades.append(trade)
        if len(self.recent_trades) > 50:
            self.recent_trades = self.recent_trades[-50:]

        if pnl_cents > 0:
            self.wins += 1
            self.consecutive_wins += 1
            self.consecutive_losses = 0
            self._scale_up()
        else:
            self.losses += 1
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            self._scale_down()

        self._check_session_limits()

        log.warning(
            f"[SESSION] Trade recorded: pnl=${pnl_usd:.2f} total=${self.current_pnl_usd:.2f} "
            f"W/L={self.wins}/{self.losses} streak={self.consecutive_wins}W/{self.consecutive_losses}L "
            f"fraction={self.current_fraction:.2%}"
        )

    def _scale_up(self):
        """Increase bankroll fraction after win"""
        if not ENABLE_BANKROLL_SCALING:
            return
        new_frac = self.current_fraction * SCALING_WIN_MULTIPLIER
        self.current_fraction = min(new_frac, SCALING_MAX_FRACTION)

    def _scale_down(self):
        """Decrease bankroll fraction after loss"""
        if not ENABLE_BANKROLL_SCALING:
            return
        new_frac = self.current_fraction * SCALING_LOSS_MULTIPLIER
        self.current_fraction = max(new_frac, SCALING_MIN_FRACTION)

    def _check_session_limits(self):
        """Check if we should pause trading"""
        if not ENABLE_SESSION_LIMITS:
            return

        # Check absolute loss limit
        if self.current_pnl_usd <= -SESSION_MAX_LOSS_USD:
            self._pause(f"max_loss_${SESSION_MAX_LOSS_USD}")
            return

        # Check percentage loss limit
        if self.starting_balance_usd > 0:
            pct_loss = -self.current_pnl_usd / self.starting_balance_usd
            if pct_loss >= SESSION_MAX_LOSS_PERCENT:
                self._pause(f"max_loss_{SESSION_MAX_LOSS_PERCENT:.0%}")
                return

        # Check consecutive losses
        if self.consecutive_losses >= SESSION_CONSECUTIVE_LOSSES_LIMIT:
            self._pause(f"consecutive_losses_{self.consecutive_losses}")
            return

    def _pause(self, reason: str):
        """Pause trading for cooldown period"""
        self.is_paused = True
        self.pause_until = time.time() + (SESSION_COOLDOWN_MINUTES * 60)
        self.pause_reason = reason
        # Reset fraction to minimum after hitting limits
        self.current_fraction = SCALING_MIN_FRACTION
        log.warning(f"[SESSION] PAUSED: {reason} - cooldown until {datetime.fromtimestamp(self.pause_until)}")

    def check_can_trade(self) -> Tuple[bool, Optional[str]]:
        """Check if we can trade. Returns (can_trade, reason_if_not)"""
        if not self.is_paused:
            return True, None

        if time.time() >= self.pause_until:
            self.is_paused = False
            self.pause_reason = None
            self.consecutive_losses = 0  # Reset streak after cooldown
            log.warning("[SESSION] Cooldown ended, resuming trading")
            return True, None

        remaining = int(self.pause_until - time.time())
        return False, f"paused:{self.pause_reason} ({remaining}s remaining)"

    def get_current_fraction(self) -> float:
        """Get the current bankroll fraction to use"""
        return self.current_fraction


@dataclass
class BotState:
    sm: str = SM.IDLE
    market: Optional[str] = None
    event: Optional[str] = None

    traded_this_market: bool = False
    side: Optional[str] = None  # "yes" or "no" - which side we're holding
    action: Optional[str] = None
    target_price: Optional[int] = None
    qty: int = 0

    order_id: Optional[str] = None
    order_price: Optional[int] = None
    order_side: Optional[str] = None

    placed_at: float = 0.0

    # Entry conditions (for dump logic)
    entry_model_prob: Optional[float] = None
    entry_market_prob: Optional[float] = None
    entry_spot_price: Optional[float] = None
    entry_price_cents: Optional[int] = None  # Track entry price for P&L
    entry_time: float = 0.0

    last_p_yes: Optional[float] = None
    last_edge_yes: Optional[float] = None
    last_edge_no: Optional[float] = None

    last_p_mkt: Optional[float] = None
    last_div_yes: Optional[float] = None
    last_sigma: Optional[float] = None
    last_p_yes_blend: Optional[float] = None


# -----------------------------
# Decision logic (MODIFIED FOR CONTINUOUS TRADING + DUMP)
# -----------------------------
def compute_edge(p: float, price_cents: int, fee_cents: int) -> float:
    return float(p) - float(price_cents + fee_cents) / 100.0


def postable_entry_price(bid: Optional[int], ask: Optional[int]) -> Optional[int]:
    if bid is None and ask is None:
        return None
    if bid is None:
        px = int(ask) - 1
        return clamp_int(px, 1, 99) if px >= 1 else None
    if ask is None:
        return clamp_int(int(bid), 1, 99)
    max_rest = int(ask) - 1
    if max_rest < 1:
        return None
    px = int(bid) + int(JOIN_UP_CENTS)
    px = min(px, max_rest)
    return clamp_int(px, 1, 99)


def choose_trade(
    http: requests.Session,
    spot: float,
    lo: Optional[float],
    hi: Optional[float],
    secs_to_close: int,
    yes_bid: Optional[int],
    yes_ask: Optional[int],
    no_bid: Optional[int],
    no_ask: Optional[int],
) -> Tuple[
    Optional[str], Optional[int],
    float, float,
    float, float,
    Optional[float], Optional[float], float,
    float, float
]:
    t_eff = max(5.0, float(min(secs_to_close, 120)))
    sigma_used = float(get_sigma_cached(http))
    sd = sigma_used * math.sqrt(t_eff)

    p_yes_model = prob_yes_in_range(spot, lo, hi, sd)
    p_no_model = 1.0 - p_yes_model

    p_mkt = implied_prob_from_book(yes_bid, yes_ask, no_bid, no_ask) if USE_MARKET_IMPLIED else None

    if p_mkt is not None:
        p_yes_blend = float(MODEL_BLEND_ALPHA) * p_yes_model + (1.0 - float(MODEL_BLEND_ALPHA)) * float(p_mkt)
    else:
        p_yes_blend = p_yes_model
    p_yes_blend = max(0.0, min(1.0, p_yes_blend))
    p_no_blend = 1.0 - p_yes_blend

    if POST_ONLY:
        yes_px = postable_entry_price(yes_bid, yes_ask)
        no_px = postable_entry_price(no_bid, no_ask)
    else:
        yes_px = yes_ask
        no_px = no_ask

    ok_book_yes = spread_ok(yes_bid, yes_ask)
    ok_book_no = spread_ok(no_bid, no_ask)

    edge_yes = compute_edge(p_yes_blend, yes_px, FEE_CENTS_PER_CONTRACT) if yes_px is not None else -1e9
    edge_no = compute_edge(p_no_blend, no_px, FEE_CENTS_PER_CONTRACT) if no_px is not None else -1e9

    div_yes = None
    if p_mkt is not None:
        div_yes = p_yes_model - p_mkt

    div_gate_yes = True
    div_gate_no = True
    if REQUIRE_DIVERGENCE and (div_yes is not None):
        div_gate_yes = (div_yes >= MIN_DIVERGENCE)
        div_gate_no = ((-div_yes) >= MIN_DIVERGENCE)

    # MODIFIED: Use blended probability for gating
    effective_prob_min = PROB_MIN
    if secs_to_close < LATE_ENTRY_TIME_SEC:
        effective_prob_min = PROB_MIN + LATE_ENTRY_PROB_BOOST
        
    ok_yes = (
        yes_px is not None
        and ok_book_yes
        and (p_yes_blend >= effective_prob_min)  # Use blend for gate
        and (edge_yes >= EDGE_MIN)
        and (yes_px <= MAX_ENTRY_PRICE_CENTS)
        and div_gate_yes
    )
    ok_no = (
        no_px is not None
        and ok_book_no
        and (p_no_blend >= effective_prob_min)  # Use blend for gate
        and (edge_no >= EDGE_MIN)
        and (no_px <= MAX_ENTRY_PRICE_CENTS)
        and div_gate_no
    )

    # Boundary buffer protection
    if lo is not None and spot < (lo + BOUNDARY_BUFFER_USD):
        if ok_yes:
            log.warning(f"[BOUNDARY] Spot ${spot:.2f} too close to lower bound ${lo:.2f}, blocking YES")
        ok_yes = False
    
    if hi is not None and spot > (hi - BOUNDARY_BUFFER_USD):
        if ok_no:
            log.warning(f"[BOUNDARY] Spot ${spot:.2f} too close to upper bound ${hi:.2f}, blocking NO")
        ok_no = False

    # High-certainty override
    if secs_to_close < HIGH_CERTAINTY_TIME_SEC:
        yes_boundary_ok = (lo is None) or (spot >= lo + BOUNDARY_BUFFER_USD)
        no_boundary_ok = (hi is None) or (spot <= hi - BOUNDARY_BUFFER_USD)
        
        if p_yes_model >= HIGH_CERTAINTY_PROB and yes_px is not None and yes_px <= HIGH_CERTAINTY_MAX_PRICE and yes_boundary_ok:
            ok_yes = True
            log.info(f"[OVERRIDE] YES high-certainty (p={p_yes_model:.4f}, price={yes_px})")
        if p_no_model >= HIGH_CERTAINTY_PROB and no_px is not None and no_px <= HIGH_CERTAINTY_MAX_PRICE and no_boundary_ok:
            ok_no = True
            log.info(f"[OVERRIDE] NO high-certainty (p={p_no_model:.4f}, price={no_px})")

    if ok_yes and ok_no:
        if edge_yes > edge_no + 1e-9:
            return "yes", int(yes_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)
        if edge_no > edge_yes + 1e-9:
            return "no", int(no_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)
        if p_yes_model >= p_no_model:
            return "yes", int(yes_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)
        return "no", int(no_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)

    if ok_yes:
        return "yes", int(yes_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)
    if ok_no:
        return "no", int(no_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)

    return None, None, p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)


# NEW: Dump decision logic
def should_dump_position(
    st: BotState,
    p_yes_blend: float,
    p_no_blend: float,
    p_mkt: Optional[float],
    spot: float,
    lo: Optional[float],
    hi: Optional[float],
    sigma: float,
    secs_to_close: int,
) -> Tuple[bool, Optional[str]]:
    """
    Check if we should dump our position
    Returns: (should_dump, reason)
    """
    if not ENABLE_DUMP:
        return False, None
        
    if secs_to_close < DUMP_MIN_TIME_REMAINING:
        return False, "too_close_to_settlement"
    
    if st.entry_model_prob is None:
        return False, "no_entry_data"
    
    # Determine current probability for our side
    if st.side == "yes":
        current_prob = p_yes_blend
        entry_prob = st.entry_model_prob
    else:
        current_prob = p_no_blend
        entry_prob = 1.0 - st.entry_model_prob
    
    # TRIGGER 1: Probability flipped below threshold
    if current_prob < DUMP_PROB_FLIP:
        return True, f"prob_flip_{current_prob:.3f}"
    
    # TRIGGER 2: Probability dropped significantly
    prob_drop = entry_prob - current_prob
    if prob_drop > DUMP_PROB_DROP_PERCENT:
        return True, f"prob_drop_{prob_drop:.3f}"
    
    # TRIGGER 3: Market probability flipped
    if USE_MARKET_IMPLIED and p_mkt is not None:
        market_prob = p_mkt if st.side == "yes" else (1.0 - p_mkt)
        if market_prob < DUMP_MARKET_FLIP_THRESHOLD:
            return True, f"market_flip_{market_prob:.3f}"
    
    # TRIGGER 4: Bitcoin price danger zone
    if DUMP_ON_PRICE_DANGER:
        if st.side == "yes":
            # We bet BTC will be ABOVE threshold
            if lo is not None:
                danger_price = lo - (sigma * DUMP_PRICE_SIGMA_MULTIPLIER)
                if spot < danger_price:
                    distance = lo - spot
                    return True, f"price_danger_YES_${distance:.0f}_below"
        
        elif st.side == "no":
            # We bet BTC will be BELOW threshold
            if hi is not None:
                danger_price = hi + (sigma * DUMP_PRICE_SIGMA_MULTIPLIER)
                if spot > danger_price:
                    distance = spot - hi
                    return True, f"price_danger_NO_${distance:.0f}_above"
    
    return False, None


def compute_fraction_for_trade(edge_net: float, p_gate: float, session_fraction: Optional[float] = None) -> float:
    """Compute position sizing fraction based on edge, probability, and session state"""
    # Use session-adjusted fraction if provided, otherwise use default
    base = session_fraction if session_fraction is not None else float(BANKROLL_FRACTION)

    if (p_gate >= float(A_PLUS_PROB)) and (edge_net >= float(A_PLUS_EDGE)):
        frac = max(base, float(A_PLUS_FRACTION))
    else:
        bump = float(EDGE_SIZE_SLOPE) * max(0.0, float(edge_net) - float(EDGE_SIZE_START))
        frac = base + bump

    frac = min(frac, float(BANKROLL_FRACTION_HARD_CAP))
    frac = clamp_float(frac, 0.0, 0.99)
    return float(frac)


def compute_qty_from_bankroll(
    available_usd: Optional[float],
    entry_cents: int,
    edge_net: float,
    p_gate: float,
    session: Optional[SessionState] = None
) -> int:
    """Compute order quantity based on bankroll, edge, and session state"""
    if entry_cents is None or entry_cents <= 0:
        log.warning(f"[SIZE] Invalid entry_cents={entry_cents}, falling back to ORDER_QTY={ORDER_QTY}")
        return clamp_int(ORDER_QTY, MIN_CONTRACTS, MAX_CONTRACTS)

    if available_usd is None or available_usd <= 0:
        log.warning(f"[SIZE] available_usd is None or <=0, falling back to ORDER_QTY={ORDER_QTY}")
        return clamp_int(ORDER_QTY, MIN_CONTRACTS, MAX_CONTRACTS)

    if available_usd < MIN_FREE_USD_TO_TRADE:
        log.warning(f"[SIZE] available_usd ${available_usd:.2f} < MIN_FREE_USD_TO_TRADE ${MIN_FREE_USD_TO_TRADE}, returning 0")
        return 0

    # Get session-adjusted fraction if available
    session_fraction = session.get_current_fraction() if session else None
    frac = compute_fraction_for_trade(edge_net=edge_net, p_gate=p_gate, session_fraction=session_fraction)
    stake_usd = max(0.0, float(available_usd) * float(frac))

    cost_per = float(entry_cents) / 100.0

    if cost_per <= 0.0:
        log.warning(f"[SIZE] cost_per={cost_per} invalid, returning ORDER_QTY={ORDER_QTY}")
        return clamp_int(ORDER_QTY, MIN_CONTRACTS, MAX_CONTRACTS)

    qty = int(stake_usd // cost_per)
    qty = clamp_int(qty, MIN_CONTRACTS, MAX_CONTRACTS)

    session_info = f" (session_frac={session_fraction:.2%})" if session_fraction else ""
    log.info(f"[SIZE] avail=${available_usd:.2f} frac={frac:.4f} stake=${stake_usd:.2f} qty={qty}{session_info}")

    return qty


# -----------------------------
# Main (SCALPER with session tracking)
# -----------------------------
def main() -> None:
    log.warning(f"[ENV] Detected KALSHI_* keys: {env_keys_with_prefix('KALSHI_')}")
    log.warning(
        f"[BOOTCFG] SERIES={SERIES_TICKER} ARM_TIME={ENTRY_START_SECONDS}s "
        f"PROB_MIN={PROB_MIN} EDGE_MIN={EDGE_MIN} MAX_ENTRY={MAX_ENTRY_PRICE_CENTS}¢ "
        f"BANKROLL_FRACTION={BANKROLL_FRACTION} ENABLE_DUMP={ENABLE_DUMP} "
        f"DUMP_PROB_FLIP={DUMP_PROB_FLIP} DUMP_PROB_DROP={DUMP_PROB_DROP_PERCENT}"
    )
    log.warning(
        f"[BOOTCFG] SCALING: enabled={ENABLE_BANKROLL_SCALING} win_mult={SCALING_WIN_MULTIPLIER} "
        f"loss_mult={SCALING_LOSS_MULTIPLIER} min={SCALING_MIN_FRACTION:.0%} max={SCALING_MAX_FRACTION:.0%}"
    )
    log.warning(
        f"[BOOTCFG] LIMITS: enabled={ENABLE_SESSION_LIMITS} max_loss=${SESSION_MAX_LOSS_USD} "
        f"max_loss_pct={SESSION_MAX_LOSS_PERCENT:.0%} consec_losses={SESSION_CONSECUTIVE_LOSSES_LIMIT} "
        f"cooldown={SESSION_COOLDOWN_MINUTES}min"
    )
    log.warning("[HEARTBEAT] main() entered — SCALPER is running")

    if not API_KEY_ID or not PRIVATE_KEY_PEM_B64:
        raise RuntimeError("Missing KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_PEM_BASE64")

    client = KalshiClient(API_BASE, API_PREFIX, API_KEY_ID, PRIVATE_KEY_PEM_B64)
    http = requests.Session()

    st = BotState()
    session = SessionState()
    active_market_obj: Dict[str, Any] = {}

    # Initialize session with starting balance
    try:
        av, tot = get_balance_usd(client)
        if av is not None:
            session.starting_balance_usd = av
            log.warning(f"[SESSION] Starting balance: ${av:.2f}")
    except Exception as e:
        log.warning(f"[SESSION] Could not fetch starting balance: {e}")

    last_meta = 0.0
    last_state_log = 0.0
    last_ob_warn = 0.0
    last_heartbeat = 0.0

    def refresh_active_market() -> Tuple[str, str, Dict[str, Any]]:
        if MARKET_OVERRIDE and MARKET_OVERRIDE not in ("<none>", "none", "None", ""):
            mt = MARKET_OVERRIDE
            try:
                snap = client.request("GET", f"/markets/{mt}")
                mobj = snap.get("market") if isinstance(snap, dict) and isinstance(snap.get("market"), dict) else (snap if isinstance(snap, dict) else {})
            except Exception:
                mobj = {}
            ev = EVENT_TICKER if EVENT_TICKER != "<auto>" else "<manual>"
            return ev, mt, mobj

        params = {"series_ticker": SERIES_TICKER, "status": "open", "limit": 200}
        resp = client.request("GET", "/markets", params=params)
        markets = resp.get("markets", []) if isinstance(resp, dict) else []
        if not markets:
            raise RuntimeError(f"No open markets returned for series_ticker={SERIES_TICKER}")
        ev, mt, mobj = pick_active_market(markets)
        return ev, mt, mobj

    def reconcile_on_market_change(new_market: str) -> None:
        if BOOTSTRAP_CANCEL_OPEN_ORDERS or CANCEL_ALL_STRAYS_ALWAYS:
            try:
                cancel_all_strays_for_market(client, new_market)
            except Exception as e:
                log.warning(f"[RECON] cancel strays failed: {e}")

        try:
            pos = parse_position_for_market(get_positions(client), new_market)
        except Exception:
            pos = 0

        if pos != 0:
            st.sm = SM.HOLD
            st.market = new_market
            st.traded_this_market = True
            st.order_id = None
            # Determine which side we're holding
            st.side = "yes" if pos > 0 else "no"
            log.warning(f"[RECON] found existing position in {new_market}: pos={pos} side={st.side}. Enter HOLD.")
            return

        st.sm = SM.IDLE
        st.market = new_market
        st.traded_this_market = False
        st.order_id = None
        st.side = None
        st.target_price = None
        st.qty = 0

    ev, mt, mobj = refresh_active_market()
    active_market_obj = mobj or {}
    st.market = mt
    st.event = ev
    reconcile_on_market_change(mt)
    last_meta = time.time()

    while True:
        now = time.time()

        if (now - last_heartbeat) >= float(HEARTBEAT_SECONDS):
            log.warning(
                f"[HEARTBEAT] market={st.market} sm={st.sm} traded={st.traded_this_market} | "
                f"SESSION: pnl=${session.current_pnl_usd:.2f} W/L={session.wins}/{session.losses} "
                f"frac={session.current_fraction:.1%} paused={session.is_paused}"
            )
            last_heartbeat = now

        if (now - last_meta) >= META_REFRESH_SECONDS:
            try:
                ev2, mt2, mobj2 = refresh_active_market()
                if mt2 != st.market:
                    old_market = st.market
                    log.warning(f"[ROLL] {old_market} -> {mt2}")

                    # Record P&L for settled position (if we had one)
                    if st.traded_this_market and st.entry_price_cents is not None and st.side is not None:
                        # Try to determine settlement result
                        try:
                            old_mkt_data = client.request("GET", f"/markets/{old_market}")
                            old_mkt_obj = old_mkt_data.get("market", old_mkt_data) if isinstance(old_mkt_data, dict) else {}
                            result = old_mkt_obj.get("result", "").lower()

                            # Calculate P&L based on settlement
                            if result == "yes":
                                # YES paid 100, NO paid 0
                                if st.side == "yes":
                                    pnl_cents = (100 - st.entry_price_cents) * st.qty
                                else:
                                    pnl_cents = -st.entry_price_cents * st.qty
                            elif result == "no":
                                # YES paid 0, NO paid 100
                                if st.side == "yes":
                                    pnl_cents = -st.entry_price_cents * st.qty
                                else:
                                    pnl_cents = (100 - st.entry_price_cents) * st.qty
                            else:
                                # Unknown result, assume loss equal to entry
                                pnl_cents = -st.entry_price_cents * st.qty
                                log.warning(f"[ROLL] Unknown settlement result '{result}' for {old_market}")

                            session.record_trade(
                                market=old_market,
                                side=st.side,
                                entry_price=st.entry_price_cents,
                                exit_price=100 if (result == st.side) else 0,
                                qty=st.qty,
                                pnl_cents=pnl_cents,
                                was_dump=False,
                            )
                            log.warning(f"[ROLL] Settled {old_market}: {st.side.upper()} result={result} pnl={pnl_cents}¢")
                        except Exception as e:
                            log.warning(f"[ROLL] Could not determine settlement for {old_market}: {e}")

                    st.event = ev2
                    st.market = mt2
                    active_market_obj = mobj2 or {}
                    st.sm = SM.ROLL
                    st.traded_this_market = False
                    st.order_id = None
                    st.side = None
                    st.target_price = None
                    st.qty = 0
                    st.entry_price_cents = None
                    reconcile_on_market_change(mt2)
                else:
                    if isinstance(mobj2, dict) and mobj2:
                        active_market_obj = mobj2
                last_meta = now
            except Exception as e:
                log.warning(f"[ROLL] refresh failed: {e}")
                last_meta = now

        if not st.market:
            time.sleep(POLL_SECONDS)
            continue

        close_ts = extract_close_ts(active_market_obj, st.market)
        secs_to_close = None
        if close_ts is not None:
            secs_to_close = int(close_ts - int(time.time()))

        pos = 0
        try:
            pos = parse_position_for_market(get_positions(client), st.market)
        except Exception as e:
            log.warning(f"[INV] positions fetch failed: {e}")

        # MODIFIED: In HOLD state, check for dump conditions
        if pos != 0:
            if st.sm != SM.HOLD:
                st.sm = SM.HOLD
                st.traded_this_market = True
                st.side = "yes" if pos > 0 else "no"
                log.warning(f"[HOLD] market={st.market} pos={pos} side={st.side}")
            
            # Check dump conditions continuously
            if ENABLE_DUMP and secs_to_close is not None:
                spot = fetch_btc_spot_usd(http)
                if spot is not None:
                    try:
                        ob = client.request("GET", f"/markets/{st.market}/orderbook")
                        yes_bid, yes_ask, no_bid, no_ask = parse_best_yes_no(ob)
                        lo, hi = market_bounds_usd(active_market_obj)
                        
                        # Recalculate probabilities
                        sigma_used = get_sigma_cached(http)
                        t_eff = max(5.0, float(min(secs_to_close, 120)))
                        sd = sigma_used * math.sqrt(t_eff)
                        p_yes_model = prob_yes_in_range(spot, lo, hi, sd)
                        p_mkt = implied_prob_from_book(yes_bid, yes_ask, no_bid, no_ask)
                        
                        if p_mkt is not None:
                            p_yes_blend = MODEL_BLEND_ALPHA * p_yes_model + (1.0 - MODEL_BLEND_ALPHA) * p_mkt
                        else:
                            p_yes_blend = p_yes_model
                        p_no_blend = 1.0 - p_yes_blend
                        
                        should_dump, dump_reason = should_dump_position(
                            st, p_yes_blend, p_no_blend, p_mkt,
                            spot, lo, hi, sigma_used, secs_to_close
                        )
                        
                        if should_dump:
                            log.warning(f"[DUMP] Triggering dump: {dump_reason}")

                            # Estimate exit price (use current bid/ask)
                            if st.side == "yes":
                                exit_price_cents = yes_bid if yes_bid else 50
                            else:
                                exit_price_cents = no_bid if no_bid else 50

                            # Place opposing market order to exit
                            try:
                                exit_side = "no" if st.side == "yes" else "yes"
                                exit_payload = build_order_payload(
                                    market_ticker=st.market,
                                    action="buy",
                                    side=exit_side,
                                    price_cents=99,  # Market order
                                    count=abs(pos),
                                    post_only=False,
                                )

                                if not DRY_RUN:
                                    oid = place_order(client, exit_payload)
                                    log.warning(f"[DUMP] Placed exit order {oid} BUY {exit_side} qty={abs(pos)}")

                                    # Record P&L for dump (entry cost - exit value)
                                    if st.entry_price_cents is not None:
                                        # For YES: paid entry_price, selling at exit_price
                                        # For NO: paid entry_price, selling at exit_price
                                        # P&L = (exit_price - entry_price) * qty for winning side
                                        # But on dump we're usually losing, so:
                                        pnl_cents = (exit_price_cents - st.entry_price_cents) * abs(pos)
                                        session.record_trade(
                                            market=st.market,
                                            side=st.side,
                                            entry_price=st.entry_price_cents,
                                            exit_price=exit_price_cents,
                                            qty=abs(pos),
                                            pnl_cents=pnl_cents,
                                            was_dump=True,
                                        )
                                else:
                                    log.warning(f"[DRY] Would dump: BUY {exit_side} qty={abs(pos)}")

                                st.sm = SM.DUMPED
                                st.side = None
                                st.entry_price_cents = None

                            except Exception as e:
                                log.error(f"[DUMP] Failed to place exit order: {e}")
                        
                    except Exception as e:
                        log.warning(f"[DUMP] Check failed: {e}")
            
            time.sleep(POLL_SECONDS)
            continue

        if ONE_TRADE_PER_MARKET and st.traded_this_market:
            st.sm = SM.HOLD
            time.sleep(POLL_SECONDS)
            continue

        if secs_to_close is None:
            if (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                log.warning(f"[STATE] market={st.market} missing close_ts")
                last_state_log = now
            last_meta = 0.0
            time.sleep(POLL_SECONDS)
            continue

        # MODIFIED: Arm early (600s instead of 120s)
        if secs_to_close > ENTRY_START_SECONDS:
            st.sm = SM.IDLE
            if (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                log.info(f"[STATE] {st.market} IDLE t_close={secs_to_close}s (arming at {ENTRY_START_SECONDS}s)")
                last_state_log = now
            time.sleep(POLL_SECONDS)
            continue

        st.sm = SM.ARMED

        # MODIFIED: Stop checking continuously once we're <45s to close
        if secs_to_close < ENTRY_LAST_SECONDS:
            st.traded_this_market = True
            log.warning(f"[SKIP] {st.market} missed last entry window (t_close={secs_to_close}s)")
            time.sleep(POLL_SECONDS)
            continue

        spot = fetch_btc_spot_usd(http)
        if spot is None:
            log.warning(f"[SPOT] failed; skipping this poll")
            time.sleep(POLL_SECONDS)
            continue

        try:
            ob = client.request("GET", f"/markets/{st.market}/orderbook")
        except Exception as e:
            log.warning(f"[OB] fetch failed: {e}")
            time.sleep(POLL_SECONDS)
            continue

        yes_bid, yes_ask, no_bid, no_ask = parse_best_yes_no(ob)
        lo, hi = market_bounds_usd(active_market_obj)

        try:
            (
                chosen_side, chosen_px,
                p_yes_model, p_no_model,
                p_yes_blend, p_no_blend,
                p_mkt, div_yes, sigma_used,
                edge_yes, edge_no
            ) = choose_trade(
                http=http,
                spot=spot,
                lo=lo,
                hi=hi,
                secs_to_close=secs_to_close,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
            )
        except Exception as e:
            log.warning(f"[DECIDE] choose_trade failed: {e}")
            time.sleep(POLL_SECONDS)
            continue

        if LOG_DECISIONS:
            yes_px_log = postable_entry_price(yes_bid, yes_ask) if POST_ONLY else yes_ask
            no_px_log = postable_entry_price(no_bid, no_ask) if POST_ONLY else no_ask
            edge_yes_log = compute_edge(p_yes_blend, yes_px_log, FEE_CENTS_PER_CONTRACT) if yes_px_log is not None else None
            edge_no_log = compute_edge(p_no_blend, no_px_log, FEE_CENTS_PER_CONTRACT) if no_px_log is not None else None
            
            yes_buffer_str = f"${spot-lo:.2f}" if lo else "N/A"
            no_buffer_str = f"${hi-spot:.2f}" if hi else "N/A"
            
            log.info(
                f"[DECIDE] {st.market} t_close={secs_to_close}s spot=${spot:.2f} "
                f"yes_buffer={yes_buffer_str} no_buffer={no_buffer_str} "
                f"p_yes_blend={p_yes_blend:.4f} p_no_blend={p_no_blend:.4f} "
                f"YES(edge={edge_yes_log:.4f}) NO(edge={edge_no_log:.4f}) -> {chosen_side}@{chosen_px}"
            )

        if chosen_side is None or chosen_px is None:
            if (now - last_ob_warn) >= OB_WARN_EVERY_SECONDS:
                log.warning(f"[OB] no usable entry: yes=({yes_bid},{yes_ask}) no=({no_bid},{no_ask})")
                last_ob_warn = now
            time.sleep(POLL_SECONDS)
            continue

        # Check session limits before trading
        can_trade, pause_reason = session.check_can_trade()
        if not can_trade:
            st.sm = SM.COOLDOWN
            if (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                log.warning(f"[SESSION] Trading paused: {pause_reason}")
                last_state_log = now
            time.sleep(POLL_SECONDS)
            continue

        available_usd, total_usd = get_balance_usd(client)

        if chosen_side == "yes":
            edge_net = float(edge_yes)
            p_gate = float(p_yes_blend)
        else:
            edge_net = float(edge_no)
            p_gate = float(p_no_blend)

        qty = compute_qty_from_bankroll(available_usd, int(chosen_px), edge_net=edge_net, p_gate=p_gate, session=session)
        if qty <= 0:
            log.warning(f"[SKIP] {st.market} qty=0")
            st.traded_this_market = True
            time.sleep(POLL_SECONDS)
            continue

        use_post_only = POST_ONLY
        if secs_to_close < LAST_CHANCE_TIME_SEC and p_gate >= LAST_CHANCE_MIN_PROB:
            use_post_only = False
            log.info(f"[LAST_CHANCE] Allowing taker at T-{secs_to_close}s (p={p_gate:.4f})")

        payload = build_order_payload(
            market_ticker=st.market,
            action="buy",
            side=chosen_side,
            price_cents=int(chosen_px),
            count=int(qty),
            post_only=use_post_only,
        )

        if not ENABLE_TRADING or DRY_RUN:
            log.warning(f"[DRY] would place: BUY {chosen_side} @ {chosen_px}¢ qty={qty} (session_frac={session.current_fraction:.2%})")
            st.traded_this_market = True
            st.sm = SM.HOLD
            time.sleep(POLL_SECONDS)
            continue

        try:
            oid = place_order(client, payload)
            st.traded_this_market = True
            st.sm = SM.HOLD
            st.side = chosen_side
            st.entry_model_prob = p_yes_model
            st.entry_market_prob = p_mkt
            st.entry_spot_price = spot
            st.entry_price_cents = int(chosen_px)  # Track entry price for P&L
            st.entry_time = now
            st.qty = qty

            log.warning(
                f"[ORDER] PLACED {st.market} order_id={oid} BUY {chosen_side.upper()} @ {chosen_px}¢ qty={qty} "
                f"edge={edge_net:.4f} p_gate={p_gate:.4f} session_frac={session.current_fraction:.2%}"
            )
        except Exception as e:
            log.warning(f"[ORDER] place failed: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception(f"FATAL: bot crashed: {e}")
        raise
