# bot.py
# Kalshi rolling 15m BTC aggressive strategy with DUMP capability
#
# WHAT THIS DOES:
# - Arms at T-600s (10 minutes before close) 
# - Trades aggressively (60% prob, 1.5% edge, up to 85¢ entry)
# - Monitors position continuously
# - DUMPS position if probability flips or price moves dangerously
# - Sizes positions at 10% of bankroll with Kelly scaling
#
# CHANGES FROM ORIGINAL:
# - Changed ENTRY_START_SECONDS from 120 -> 600 (10 min early arming)
# - Changed PROB_MIN from 0.85 -> 0.60 (trade more)
# - Changed EDGE_MIN from 0.01 -> 0.015 (1.5%)
# - Changed MAX_ENTRY_PRICE from 99 -> 85¢
# - Changed BANKROLL_FRACTION from 0.05 -> 0.10 (10%)
# - Added DUMP logic with multiple exit triggers
# - Added continuous position monitoring in HOLD state
# - Removed "one decision" constraint - now checks continuously while armed
# - ADDED DEBUG LOGGING FOR URL CONSTRUCTION

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

# -------------- AGGRESSIVE STRATEGY ENVs --------------
ENTRY_START_SECONDS = env_int("ENTRY_START_SECONDS", 600)  # CHANGED: 10 min early
ENTRY_DECISION_SECONDS = env_int("ENTRY_DECISION_SECONDS", 60)
ENTRY_LAST_SECONDS = env_int("ENTRY_LAST_SECONDS", 45)  # Stop new entries at 45s
FILL_WAIT_SECONDS = env_int("FILL_WAIT_SECONDS", 30)
ALLOW_TAKER_AT_LAST = env_bool("ALLOW_TAKER_AT_LAST", True)
CANCEL_UNFILLED_AT_CLOSE = env_bool("CANCEL_UNFILLED_AT_CLOSE", True)

PROB_MIN = env_float("PROB_MIN", 0.60)  # CHANGED: Lower threshold
EDGE_MIN = env_float("EDGE_MIN", 0.015)  # CHANGED: 1.5% edge
MAX_ENTRY_PRICE_CENTS = env_int("MAX_ENTRY_PRICE_CENTS", 85)  # CHANGED: Max 85¢
FEE_CENTS_PER_CONTRACT = env_int("FEE_CENTS_PER_CONTRACT", 0)

SPOT_SIGMA_USD_PER_SQRT_SEC = env_float("SPOT_SIGMA_USD_PER_SQRT_SEC", 12.0)

BANKROLL_FRACTION = env_float("BANKROLL_FRACTION", 0.10)  # CHANGED: 10% per trade
MIN_CONTRACTS = env_int("MIN_CONTRACTS", 1)
MAX_CONTRACTS = env_int("MAX_CONTRACTS", 50)
MIN_FREE_USD_TO_TRADE = env_float("MIN_FREE_USD_TO_TRADE", 5.0)

ONE_TRADE_PER_MARKET = env_bool("ONE_TRADE_PER_MARKET", True)
CANCEL_ALL_STRAYS_ALWAYS = env_bool("CANCEL_ALL_STRAYS_ALWAYS", True)

LOG_DECISIONS = env_bool("LOG_DECISIONS", True)
LOG_STATE_EVERY_SECONDS = env_float("LOG_STATE_EVERY_SECONDS", 10.0)

JOIN_UP_CENTS = env_int("JOIN_UP_CENTS", 0)
OB_WARN_EVERY_SECONDS = env_float("OB_WARN_EVERY_SECONDS", 2.0)

# -------------- DUMP CONFIGURATION (NEW) --------------
ENABLE_DUMP = env_bool("ENABLE_DUMP", True)
DUMP_PROB_FLIP = env_float("DUMP_PROB_FLIP", 0.45)  # Exit if prob drops below 45%
DUMP_PROB_DROP_PERCENT = env_float("DUMP_PROB_DROP_PERCENT", 0.20)  # Exit if -20% drop
DUMP_MARKET_FLIP_THRESHOLD = env_float("DUMP_MARKET_FLIP_THRESHOLD", 0.30)
DUMP_MIN_TIME_REMAINING = env_int("DUMP_MIN_TIME_REMAINING", 30)  # Never dump <30s
DUMP_ON_PRICE_DANGER = env_bool("DUMP_ON_PRICE_DANGER", True)
DUMP_PRICE_SIGMA_MULTIPLIER = env_float("DUMP_PRICE_SIGMA_MULTIPLIER", 1.5)

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
BANKROLL_FRACTION_HARD_CAP = env_float("BANKROLL_FRACTION_HARD_CAP", 0.25)

EDGE_SIZE_START = env_float("EDGE_SIZE_START", 0.02)
EDGE_SIZE_SLOPE = env_float("EDGE_SIZE_SLOPE", 2.5)

A_PLUS_PROB = env_float("A_PLUS_PROB", 0.92)
A_PLUS_EDGE = env_float("A_PLUS_EDGE", 0.03)
A_PLUS_FRACTION = env_float("A_PLUS_FRACTION", 0.15)

HIGH_CERTAINTY_PROB = env_float("HIGH_CERTAINTY_PROB", 0.98)
HIGH_CERTAINTY_TIME_SEC = env_int("HIGH_CERTAINTY_TIME_SEC", 10)
HIGH_CERTAINTY_MAX_PRICE = env_int("HIGH_CERTAINTY_MAX_PRICE", 99)

LAST_CHANCE_TIME_SEC = env_int("LAST_CHANCE_TIME_SEC", 15)
LAST_CHANCE_MIN_PROB = env_float("LAST_CHANCE_MIN_PROB", 0.90)

BOUNDARY_BUFFER_USD = env_float("BOUNDARY_BUFFER_USD", 100.0)
LATE_ENTRY_PROB_BOOST = env_float("LATE_ENTRY_PROB_BOOST", 0.05)
LATE_ENTRY_TIME_SEC = env_int("LATE_ENTRY_TIME_SEC", 20)

# Heartbeat
HEARTBEAT_SECONDS = env_float("HEARTBEAT_SECONDS", 15.0)


# -----------------------------
# Kalshi API client (RSA-PSS signing)  **WITH DEBUG**
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
        
        # DEBUG: Print what we initialized with
        print(f"DEBUG INIT: api_base={self.api_base} api_prefix={self.api_prefix}", flush=True)

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
        
        # DEBUG: Print the constructed URL
        print(f"DEBUG URL: base={self.api_base} prefix={self.api_prefix} path={path} -> full={url_with_q}", flush=True)

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
    entry_time: float = 0.0

    last_p_yes: Optional[float] = None
    last_edge_yes: Optional[float] = None
    last_edge_no: Optional[float] = None

    last_p_mkt: Optional[float] = None
    last_div_yes: Optional[float] = None
    last_sigma: Optional[float] = None
    last_p_yes_blend: Optional[float] = None


# [REST OF THE CODE CONTINUES WITH CHOOSE_TRADE, SHOULD_DUMP, COMPUTE FUNCTIONS, AND MAIN - TRUNCATED FOR LENGTH]
# THE REMAINING CODE IS IDENTICAL TO THE PREVIOUS VERSION

# Due to character limit, I'll note that the rest of the functions (choose_trade, should_dump_position, 
# compute_fraction_for_trade, compute_qty_from_bankroll, and main) remain exactly the same as the 
# previous version I provided. The ONLY changes are the two print() debug statements added to KalshiClient.
