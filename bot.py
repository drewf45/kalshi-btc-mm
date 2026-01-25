# bot.py
# Kalshi rolling 15m BTC late-snipe (SINGLE TRADE PER MARKET; NO MULTI-MARKET)
#
# WHAT THIS DOES:
# - Watches the active KXBTC15M market.
# - Starts "arming" at T-ENTRY_START_SECONDS (default 120s to close).
# - Makes ONE decision trade near the end:
#     • Default decision time is T-ENTRY_DECISION_SECONDS (default 60s to close).
#     • Will keep checking until T-ENTRY_LAST_SECONDS (default 30s) if not yet traded.
# - Chooses YES or NO based on:
#     • "winner" side probability >= PROB_MIN (default 0.85)
#     • edge_net >= EDGE_MIN (default 0.01)   [fee-aware via FEE_CENTS_PER_CONTRACT]
#     • entry_price <= MAX_ENTRY_PRICE_CENTS (default 97)
# - Sizes the bet as BANKROLL_FRACTION of AVAILABLE balance (default 5%),
#   with caps and safe fallbacks.
# - After placing the trade, it does NOTHING until the position resolves/clears.
#
# IMPORTANT CONSTRAINT YOU GAVE:
# - DO NOT change Kalshi posting/signing/keys/names logic. Kept intact.
# - Everything else is free to change. This file is a rewrite around the SAME KalshiClient.
#
# -----------------------------
# NOTES / WHAT CHANGED (A-LEVEL ADDITIONS ONLY)
# -----------------------------
# A1) Market-implied probability (optional) + blending:
#     - Compute p_mkt from orderbook (mid/complements) when USE_MARKET_IMPLIED=True
#     - p_blend = alpha*p_model + (1-alpha)*p_mkt   (MODEL_BLEND_ALPHA default 0.75)
#     - Edges are computed off p_blend (reduces hero trades).
#
# A2) Divergence gate is now OPTIONAL (OFF by default):
#     - REQUIRE_DIVERGENCE=False by default
#     - If you turn it ON, the model must disagree with market by MIN_DIVERGENCE
#
# A3) Dynamic sigma (optional):
#     - Estimate realized volatility from Coinbase Exchange 1-min candles
#     - Sigma is clipped to [SIGMA_FLOOR, SIGMA_CEIL]
#
# A4) Basic book sanity (optional):
#     - REQUIRE_BOTH_SIDES_BOOK: require bid+ask for chosen side
#     - MAX_SPREAD_CENTS_TO_TRADE: if both exist, require spread <= threshold
#
# A5) "Pick the winner" behavior:
#     - Bot does NOT prefer YES or NO.
#     - It picks whichever side has >= PROB_MIN AND >= 1-cent edge (EDGE_MIN default 0.01).
#     - If both qualify, it takes the higher edge.

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
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")


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


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(x)))


# -----------------------------
# Config (keep your existing names for Kalshi + series)
# -----------------------------
API_BASE = getenv_first(["KALSHI_API_BASE"], "https://api.elections.kalshi.com").rstrip("/")
API_PREFIX = getenv_first(["KALSHI_API_PREFIX"], "/trade-api/v2").rstrip("/")
API_KEY_ID = getenv_first(["KALSHI_API_KEY_ID"], "")
PRIVATE_KEY_PEM_B64 = getenv_first(["KALSHI_PRIVATE_KEY_PEM_BASE64"], "")

SERIES_TICKER = getenv_first(["SERIES", "KALSHI_SERIES", "KALSHI_SERIES_TICKER"], "KXBTC15M")
EVENT_TICKER = getenv_first(["EVENT_TICKER", "KALSHI_EVENT_TICKER"], "<auto>")
MARKET_OVERRIDE = getenv_first(["MARKET_OVERRIDE", "KALSHI_MARKET_OVERRIDE"], "<none>")

POLL_SECONDS = env_float("POLL_SECONDS", 0.25)
META_REFRESH_SECONDS = env_float("META_REFRESH", 10.0)

DRY_RUN = env_bool("DRY_RUN", False)
ENABLE_TRADING = env_bool("ENABLE_TRADING", True)
POST_ONLY = env_bool("POST_ONLY", True)

# Keep ORDER_QTY as a fallback if balance sizing fails
ORDER_QTY = env_int("ORDER_QTY", 1)

# Coinbase spot endpoint (keep same)
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

# Bootstrap cleanup toggle (keep name)
BOOTSTRAP_CANCEL_OPEN_ORDERS = env_bool("BOOTSTRAP_CANCEL_OPEN_ORDERS", True)

# -------------- STRATEGY ENVs (additive only) --------------
# Timing
ENTRY_START_SECONDS = env_int("ENTRY_START_SECONDS", 120)         # start monitoring hard at T-120
ENTRY_DECISION_SECONDS = env_int("ENTRY_DECISION_SECONDS", 60)    # default "place" target is T-60
ENTRY_LAST_SECONDS = env_int("ENTRY_LAST_SECONDS", 30)            # last chance to place at T-30
FILL_WAIT_SECONDS = env_int("FILL_WAIT_SECONDS", 30)              # after placing, wait up to this long
ALLOW_TAKER_AT_LAST = env_bool("ALLOW_TAKER_AT_LAST", False)      # (kept, not used here)
CANCEL_UNFILLED_AT_CLOSE = env_bool("CANCEL_UNFILLED_AT_CLOSE", True)

# Edge / model gates
PROB_MIN = env_float("PROB_MIN", 0.85)
EDGE_MIN = env_float("EDGE_MIN", 0.01)                            # 1 cent in probability-price terms
MAX_ENTRY_PRICE_CENTS = env_int("MAX_ENTRY_PRICE_CENTS", 97)      # don't buy/pay above this
FEE_CENTS_PER_CONTRACT = env_int("FEE_CENTS_PER_CONTRACT", 0)     # fee-aware edge

# Probability model: sigma in USD per sqrt(second) for last-minute BTC movement
SPOT_SIGMA_USD_PER_SQRT_SEC = env_float("SPOT_SIGMA_USD_PER_SQRT_SEC", 12.0)

# Bankroll sizing
BANKROLL_FRACTION = env_float("BANKROLL_FRACTION", 0.05)
MIN_CONTRACTS = env_int("MIN_CONTRACTS", 1)
MAX_CONTRACTS = env_int("MAX_CONTRACTS", 50)
MIN_FREE_USD_TO_TRADE = env_float("MIN_FREE_USD_TO_TRADE", 5.0)

# One-trade-only behavior
ONE_TRADE_PER_MARKET = env_bool("ONE_TRADE_PER_MARKET", True)
CANCEL_ALL_STRAYS_ALWAYS = env_bool("CANCEL_ALL_STRAYS_ALWAYS", True)

# Diagnostics
LOG_DECISIONS = env_bool("LOG_DECISIONS", True)
LOG_STATE_EVERY_SECONDS = env_float("LOG_STATE_EVERY_SECONDS", 2.0)

# Book handling
JOIN_UP_CENTS = env_int("JOIN_UP_CENTS", 0)
OB_WARN_EVERY_SECONDS = env_float("OB_WARN_EVERY_SECONDS", 2.0)

# -------------- A-LEVEL ADDITIONS (additive only) --------------
USE_MARKET_IMPLIED = env_bool("USE_MARKET_IMPLIED", True)
MODEL_BLEND_ALPHA = env_float("MODEL_BLEND_ALPHA", 0.75)

# Divergence gate is OPTIONAL now (default OFF)
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


# -----------------------------
# Kalshi API client (RSA-PSS signing)  **UNCHANGED CORE**
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
        path = parsed.path  # excludes query
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
# Robust close_ts resolver (prevents "missing close_ts; waiting..." forever)
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
    """
    Example ticker: KXBTC15M-26JAN251445-45
      - Treats "26JAN251445" as DDMMMYYHHMM in America/New_York time.
      - close = start + interval_minutes
      - returns UTC epoch seconds
    """
    try:
        parts = str(ticker).split("-")
        if len(parts) < 2:
            return None
        dt_chunk = parts[1]  # "26JAN251445"

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
    except Exception:
        return None


def resolve_close_ts(market_obj: Dict[str, Any], ticker: str) -> Optional[int]:
    """
    Tries multiple shapes:
      - numeric close_ts/close_time/etc (seconds or ms)
      - ISO close_time strings
      - fallback: infer from market ticker encoding
    Returns epoch seconds.
    """
    if not isinstance(market_obj, dict):
        market_obj = {}

    # 1) direct numeric timestamps (seconds or ms)
    for k in (
        "close_ts", "closeTs", "close_time_ts", "closeTimeTs", "close_timestamp", "closeTimestamp",
        "close_time", "closeTime", "expiration_ts", "expirationTs"
    ):
        v = market_obj.get(k)
        if isinstance(v, (int, float)):
            vv = int(v)
            return vv // 1000 if vv > 10_000_000_000 else vv

    # 2) numeric nested candidates
    for k in ("market", "data"):
        sub = market_obj.get(k)
        if isinstance(sub, dict):
            for kk in ("close_ts", "close_time", "close_timestamp"):
                v = sub.get(kk)
                if isinstance(v, (int, float)):
                    vv = int(v)
                    return vv // 1000 if vv > 10_000_000_000 else vv

    # 3) ISO time strings
    for k in ("close_time", "closeTime", "close_datetime", "closeDateTime", "expiration_time", "expirationTime"):
        v = market_obj.get(k)
        if isinstance(v, str):
            ts = _parse_iso_to_epoch_s(v)
            if ts is not None:
                return ts

    # 4) fallback: infer from ticker
    return infer_close_ts_from_ticker(ticker, interval_minutes=15)


# -----------------------------
# Market selection / parsing
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
# Spot + probability model
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
# A-LEVEL: dynamic sigma helpers
# -----------------------------
def fetch_coinbase_candles(session: requests.Session, granularity: int, timeout: float = 5.0) -> Optional[List[List[float]]]:
    """
    Coinbase Exchange candles endpoint returns: [ time, low, high, open, close, volume ]
    Most recent first.
    """
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

    # sd_per_min ≈ sigma * sqrt(60)  => sigma = sd_per_min / sqrt(60)
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
# Orderbook parsing (YES and NO best bid/ask)
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
    """
    Returns: (yes_bid, yes_ask, no_bid, no_ask) in cents.
    Returns whatever it can parse/derive.
    """
    if not isinstance(ob, dict):
        return None, None, None, None

    root = ob.get("orderbook") if isinstance(ob.get("orderbook"), dict) else ob

    yes_bid = yes_ask = no_bid = no_ask = None

    # Shape A
    if isinstance(root, dict) and isinstance(root.get("yes"), dict):
        y = root.get("yes", {})
        n = root.get("no", {})
        yes_bid = _best_from_levels(y.get("bids", y.get("buy")), "bid")
        yes_ask = _best_from_levels(y.get("asks", y.get("sell")), "ask")
        if isinstance(n, dict):
            no_bid = _best_from_levels(n.get("bids", n.get("buy")), "bid")
            no_ask = _best_from_levels(n.get("asks", n.get("sell")), "ask")

    # Shape B
    if isinstance(root, dict) and (isinstance(root.get("yes"), list) or isinstance(root.get("no"), list)):
        if yes_bid is None and isinstance(root.get("yes"), list):
            yes_bid = _best_from_levels(root.get("yes"), "bid")
        if no_bid is None and isinstance(root.get("no"), list):
            no_bid = _best_from_levels(root.get("no"), "bid")

    # Derive missing asks/bids by complement where possible
    if yes_ask is None and no_bid is not None:
        yes_ask = clamp_int(100 - no_bid, 1, 99)
    if no_ask is None and yes_bid is not None:
        no_ask = clamp_int(100 - yes_bid, 1, 99)

    if yes_bid is None and no_ask is not None:
        yes_bid = clamp_int(100 - no_ask, 1, 99)
    if no_bid is None and yes_ask is not None:
        no_bid = clamp_int(100 - yes_ask, 1, 99)

    # If book looks locked/crossed, drop asks
    if yes_bid is not None and yes_ask is not None and yes_ask <= yes_bid:
        yes_ask = None
    if no_bid is not None and no_ask is not None and no_ask <= no_bid:
        no_ask = None

    return yes_bid, yes_ask, no_bid, no_ask


# -----------------------------
# A-LEVEL: market-implied probability + book sanity
# -----------------------------
def implied_prob_from_book(
    yes_bid: Optional[int],
    yes_ask: Optional[int],
    no_bid: Optional[int],
    no_ask: Optional[int],
) -> Optional[float]:
    # best: YES mid
    if yes_bid is not None and yes_ask is not None and yes_ask > yes_bid:
        return max(0.01, min(0.99, (yes_bid + yes_ask) / 200.0))

    # next: NO mid, then complement
    if no_bid is not None and no_ask is not None and no_ask > no_bid:
        no_mid = (no_bid + no_ask) / 200.0
        return max(0.01, min(0.99, 1.0 - no_mid))

    # conservative single-sided
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
# Orders / portfolio helpers
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
    action: str,      # "buy" or "sell"
    side: str,        # "yes" or "no"
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
# Late-snipe state machine
# -----------------------------
class SM:
    IDLE = "IDLE"
    ARMED = "ARMED"
    ORDER_WAIT = "ORDER_WAIT"
    HOLD = "HOLD"
    ROLL = "ROLL"


@dataclass
class BotState:
    sm: str = SM.IDLE
    market: Optional[str] = None
    event: Optional[str] = None

    traded_this_market: bool = False
    side: Optional[str] = None
    action: Optional[str] = None
    target_price: Optional[int] = None
    qty: int = 0

    order_id: Optional[str] = None
    order_price: Optional[int] = None
    order_side: Optional[str] = None

    placed_at: float = 0.0

    last_p_yes: Optional[float] = None
    last_edge_yes: Optional[float] = None
    last_edge_no: Optional[float] = None

    # A-level diagnostics
    last_p_mkt: Optional[float] = None
    last_div_yes: Optional[float] = None
    last_sigma: Optional[float] = None
    last_p_yes_blend: Optional[float] = None


# -----------------------------
# Decision logic
# -----------------------------
def compute_edge(p: float, price_cents: int, fee_cents: int) -> float:
    return float(p) - float(price_cents + fee_cents) / 100.0


def postable_entry_price(bid: Optional[int], ask: Optional[int]) -> Optional[int]:
    """
    For POST_ONLY buys: choose a price that will REST (not cross).
    """
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
    float, float,          # p_yes_blend, p_no_blend
    Optional[float], Optional[float], float,  # p_mkt, div_yes, sigma_used
    float, float           # edge_yes, edge_no (blend-based)
]:
    """
    Returns:
      chosen_side ("yes"/"no") or None
      chosen_entry_price (limit price)
      p_yes_model, p_no_model
      p_yes_blend, p_no_blend
      p_mkt (implied), div_yes (p_model - p_mkt), sigma_used
      edge_yes, edge_no (computed off blended prob)
    """
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

    # entry prices
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

    # Optional divergence gating:
    div_gate_yes = True
    div_gate_no = True
    if REQUIRE_DIVERGENCE and (div_yes is not None):
        div_gate_yes = (div_yes >= MIN_DIVERGENCE)
        div_gate_no = ((-div_yes) >= MIN_DIVERGENCE)

    # "Pick winner" gating uses MODEL probability threshold (per your logic)
    ok_yes = (
        yes_px is not None
        and ok_book_yes
        and (p_yes_model >= PROB_MIN)
        and (edge_yes >= EDGE_MIN)
        and (yes_px <= MAX_ENTRY_PRICE_CENTS)
        and div_gate_yes
    )
    ok_no = (
        no_px is not None
        and ok_book_no
        and (p_no_model >= PROB_MIN)
        and (edge_no >= EDGE_MIN)
        and (no_px <= MAX_ENTRY_PRICE_CENTS)
        and div_gate_no
    )

    # Choose whichever qualifies; if both qualify, take higher edge
    if ok_yes and ok_no:
        if edge_yes > edge_no + 1e-9:
            return "yes", int(yes_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)
        if edge_no > edge_yes + 1e-9:
            return "no", int(no_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)
        # tie-break: pick the more probable side by model
        if p_yes_model >= p_no_model:
            return "yes", int(yes_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)
        return "no", int(no_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)

    if ok_yes:
        return "yes", int(yes_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)
    if ok_no:
        return "no", int(no_px), p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)

    return None, None, p_yes_model, p_no_model, p_yes_blend, p_no_blend, p_mkt, div_yes, sigma_used, float(edge_yes), float(edge_no)


def compute_qty_from_bankroll(available_usd: Optional[float], entry_cents: int) -> int:
    if available_usd is None or available_usd <= 0:
        return clamp_int(ORDER_QTY, MIN_CONTRACTS, MAX_CONTRACTS)

    if available_usd < MIN_FREE_USD_TO_TRADE:
        return 0

    stake_usd = max(0.0, float(available_usd) * float(BANKROLL_FRACTION))
    cost_per = float(entry_cents) / 100.0
    if cost_per <= 0:
        return 0
    qty = int(stake_usd // cost_per)
    qty = clamp_int(qty, MIN_CONTRACTS, MAX_CONTRACTS)
    return qty


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    log.info(f"[ENV] Detected KALSHI_* keys: {env_keys_with_prefix('KALSHI_')}")
    log.warning(
        f"[BOOTCFG] SERIES={SERIES_TICKER} MARKET_OVERRIDE={MARKET_OVERRIDE} "
        f"DRY_RUN={DRY_RUN} ENABLE_TRADING={ENABLE_TRADING} POST_ONLY={POST_ONLY} "
        f"ONE_TRADE_PER_MARKET={ONE_TRADE_PER_MARKET} BANKROLL_FRACTION={BANKROLL_FRACTION} "
        f"PROB_MIN={PROB_MIN} EDGE_MIN={EDGE_MIN} MAX_ENTRY_PRICE_CENTS={MAX_ENTRY_PRICE_CENTS} FEE_CENTS_PER_CONTRACT={FEE_CENTS_PER_CONTRACT} "
        f"USE_MARKET_IMPLIED={USE_MARKET_IMPLIED} MODEL_BLEND_ALPHA={MODEL_BLEND_ALPHA} "
        f"REQUIRE_DIVERGENCE={REQUIRE_DIVERGENCE} MIN_DIVERGENCE={MIN_DIVERGENCE} "
        f"USE_DYNAMIC_SIGMA={USE_DYNAMIC_SIGMA}"
    )

    if not API_KEY_ID or not PRIVATE_KEY_PEM_B64:
        raise RuntimeError("Missing KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_PEM_BASE64")

    client = KalshiClient(API_BASE, API_PREFIX, API_KEY_ID, PRIVATE_KEY_PEM_B64)
    http = requests.Session()

    st = BotState()
    active_market_obj: Dict[str, Any] = {}

    last_meta = 0.0
    last_state_log = 0.0
    last_ob_warn = 0.0

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

        params = {"series_ticker": SERIES_TICKER, "status": "open", "limit": 200, "mve_filter": "exclude"}
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
            log.warning(f"[RECON] found existing position in {new_market}: pos={pos}. Enter HOLD.")
            return

        try:
            oo = get_open_orders(client)
            for o in oo:
                if str(o.get("ticker")) != str(new_market):
                    continue
                oid = o.get("order_id") or o.get("id")
                if oid:
                    st.order_id = str(oid)
                    st.order_side = str(o.get("side", "")).lower() or None
                    px = o.get("yes_price") if st.order_side == "yes" else o.get("no_price")
                    if px is None:
                        px = o.get("price")
                    try:
                        st.order_price = int(px) if px is not None else None
                    except Exception:
                        st.order_price = None
                    st.placed_at = time.time()
                    st.sm = SM.ORDER_WAIT
                    st.market = new_market
                    st.traded_this_market = True
                    log.warning(f"[RECON] inherited resting order in {new_market}: order_id={st.order_id} side={st.order_side} px={st.order_price}")
                    return
        except Exception:
            pass

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

        if (now - last_meta) >= META_REFRESH_SECONDS:
            try:
                ev2, mt2, mobj2 = refresh_active_market()
                if mt2 != st.market:
                    log.warning(f"[ROLL] {st.market} -> {mt2}")
                    st.event = ev2
                    st.market = mt2
                    active_market_obj = mobj2 or {}
                    st.sm = SM.ROLL
                    st.traded_this_market = False
                    st.order_id = None
                    st.side = None
                    st.target_price = None
                    st.qty = 0
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

        if pos != 0:
            if st.sm != SM.HOLD:
                st.sm = SM.HOLD
                st.traded_this_market = True
                log.warning(f"[HOLD] market={st.market} pos={pos} -> holding until settlement clears")
            time.sleep(POLL_SECONDS)
            continue

        if st.sm == SM.ORDER_WAIT and st.order_id:
            try:
                oo = get_open_orders(client)
                alive = any((str(o.get("order_id") or o.get("id")) == st.order_id) for o in oo)
            except Exception:
                alive = True

            if not alive:
                log.warning(f"[ORDER] order_id={st.order_id} no longer resting; pos={pos}. Mark traded_this_market=True and go HOLD.")
                st.order_id = None
                st.sm = SM.HOLD
                st.traded_this_market = True
                time.sleep(POLL_SECONDS)
                continue

            if secs_to_close is not None and secs_to_close <= 0 and CANCEL_UNFILLED_AT_CLOSE:
                try:
                    if ENABLE_TRADING and not DRY_RUN:
                        stc = cancel_order_status(client, st.order_id)
                        log.warning(f"[ORDER] close passed; canceled resting order {st.order_id} status={stc}")
                except Exception as e:
                    log.warning(f"[ORDER] close cancel failed: {e}")
                st.order_id = None
                st.sm = SM.HOLD
                st.traded_this_market = True
                time.sleep(POLL_SECONDS)
                continue

            time.sleep(POLL_SECONDS)
            continue

        if ONE_TRADE_PER_MARKET and st.traded_this_market:
            st.sm = SM.HOLD
            time.sleep(POLL_SECONDS)
            continue

        if secs_to_close is None:
            if (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                log.warning(f"[STATE] market={st.market} missing close_ts (even after inference); forcing meta refresh...")
                last_state_log = now
            last_meta = 0.0
            time.sleep(POLL_SECONDS)
            continue

        if secs_to_close > ENTRY_START_SECONDS:
            st.sm = SM.IDLE
            if (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                log.info(f"[STATE] {st.market} IDLE t_close={secs_to_close}s (arming at {ENTRY_START_SECONDS}s)")
                last_state_log = now
            time.sleep(POLL_SECONDS)
            continue

        st.sm = SM.ARMED

        if secs_to_close > ENTRY_DECISION_SECONDS:
            if (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                log.info(f"[STATE] {st.market} ARMED t_close={secs_to_close}s (decision at {ENTRY_DECISION_SECONDS}s)")
                last_state_log = now
            time.sleep(POLL_SECONDS)
            continue

        if secs_to_close < ENTRY_LAST_SECONDS:
            st.traded_this_market = True
            log.warning(f"[SKIP] {st.market} missed last entry window (t_close={secs_to_close}s < {ENTRY_LAST_SECONDS}s).")
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

        if POST_ONLY:
            yes_px_log = postable_entry_price(yes_bid, yes_ask)
            no_px_log = postable_entry_price(no_bid, no_ask)
        else:
            yes_px_log = yes_ask
            no_px_log = no_ask

        st.last_p_yes = p_yes_model
        st.last_p_mkt = p_mkt
        st.last_div_yes = div_yes
        st.last_sigma = sigma_used
        st.last_p_yes_blend = p_yes_blend

        st.last_edge_yes = compute_edge(p_yes_blend, yes_px_log, FEE_CENTS_PER_CONTRACT) if yes_px_log is not None else None
        st.last_edge_no = compute_edge(p_no_blend, no_px_log, FEE_CENTS_PER_CONTRACT) if no_px_log is not None else None

        if LOG_DECISIONS:
            log.info(
                f"[DECIDE] {st.market} t_close={secs_to_close}s spot={spot:.2f} range=({lo},{hi}) sigma={sigma_used:.3f} "
                f"p_mkt={p_mkt} div_yes={div_yes} p_yes_blend={p_yes_blend:.4f} "
                f"YES(bid={yes_bid},ask={yes_ask},entry={yes_px_log},p={p_yes_model:.4f},edge={st.last_edge_yes}) "
                f"NO(bid={no_bid},ask={no_ask},entry={no_px_log},p={p_no_model:.4f},edge={st.last_edge_no}) "
                f"-> chosen={chosen_side} entry={chosen_px}"
            )

        if chosen_side is None or chosen_px is None:
            if (now - last_ob_warn) >= OB_WARN_EVERY_SECONDS:
                log.warning(
                    f"[OB] no usable entry near close: yes=(bid={yes_bid},ask={yes_ask}) no=(bid={no_bid},ask={no_ask}) POST_ONLY={POST_ONLY}"
                )
                last_ob_warn = now
            time.sleep(POLL_SECONDS)
            continue

        available_usd, total_usd = get_balance_usd(client)
        qty = compute_qty_from_bankroll(available_usd, int(chosen_px))
        if qty <= 0:
            log.warning(f"[SKIP] {st.market} qty=0 (available={available_usd}, entry={chosen_px})")
            st.traded_this_market = True
            time.sleep(POLL_SECONDS)
            continue

        payload = build_order_payload(
            market_ticker=st.market,
            action="buy",
            side=chosen_side,
            price_cents=int(chosen_px),
            count=int(qty),
            post_only=POST_ONLY,
        )

        if not ENABLE_TRADING or DRY_RUN:
            log.warning(f"[DRY] would_place: market={st.market} buy {chosen_side} @ {chosen_px} qty={qty}")
            st.traded_this_market = True
            st.sm = SM.HOLD
            time.sleep(POLL_SECONDS)
            continue

        try:
            oid = place_order(client, payload)
            st.order_id = oid
            st.order_price = int(chosen_px)
            st.order_side = chosen_side
            st.placed_at = time.time()
            st.qty = qty
            st.side = chosen_side
            st.target_price = int(chosen_px)
            st.traded_this_market = True
            st.sm = SM.ORDER_WAIT
            log.warning(
                f"[ORDER] PLACED market={st.market} order_id={oid} BUY {chosen_side.upper()} @ {chosen_px} qty={qty} "
                f"avail_usd={available_usd} total_usd={total_usd}"
            )
        except Exception as e:
            log.warning(f"[ORDER] place failed: {e}")
            st.order_id = None
            time.sleep(POLL_SECONDS)
            continue

        while True:
            now2 = time.time()
            secs_to_close2 = int(close_ts - int(now2)) if close_ts is not None else None

            if secs_to_close2 is not None and secs_to_close2 <= 0:
                if CANCEL_UNFILLED_AT_CLOSE and st.order_id:
                    try:
                        stc = cancel_order_status(client, st.order_id)
                        log.warning(f"[ORDER] close passed; canceled order_id={st.order_id} status={stc}")
                    except Exception as ce:
                        log.warning(f"[ORDER] close cancel failed: {ce}")
                st.order_id = None
                st.sm = SM.HOLD
                break

            try:
                pos2 = parse_position_for_market(get_positions(client), st.market)
            except Exception:
                pos2 = 0

            if pos2 != 0:
                log.warning(f"[FILL] market={st.market} pos_now={pos2} order_id={st.order_id} -> HOLD")
                st.sm = SM.HOLD
                break

            alive = True
            try:
                oo2 = get_open_orders(client)
                alive = any((str(o.get("order_id") or o.get("id")) == st.order_id) for o in oo2) if st.order_id else False
            except Exception:
                alive = True

            if not alive:
                log.warning(f"[ORDER] order_id={st.order_id} no longer resting; pos=0 -> HOLD (single-trade policy)")
                st.order_id = None
                st.sm = SM.HOLD
                break

            if (now2 - st.placed_at) >= float(FILL_WAIT_SECONDS):
                log.warning(f"[ORDER] still resting after {FILL_WAIT_SECONDS}s; staying out (single-trade policy). order_id={st.order_id}")
                st.sm = SM.HOLD
                break

            time.sleep(POLL_SECONDS)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()