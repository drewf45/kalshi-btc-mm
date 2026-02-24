# sol_bot.py — Kalshi 15-minute SOL market bot (DATA-DRIVEN V4 Feb 2026)
#
# STRATEGY (from 2,066 settled market analysis, Feb 1-19 2026):
#   - Data-driven Kelly criterion contract sizing per price bucket
#   - Asset-specific price caps with dead zone skips
#   - $25 bankroll per bot, max $6.25 risk per trade (25%)
#   - Hold to settlement — 15-minute markets, no exit logic needed
#
# IRON RULES:
#   1. One direction per market — once positioned, DONE
#   2. Data-driven contract sizing per asset per price bucket
#   3. Asset-specific price caps with dead zone skips
#   4. Never buy both YES and NO on same market
#   5. Single entry per market — TRADED_TICKERS + API check

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

from config import (
    MIN_CONFIDENCE,
    SETTLEMENT_BIAS, ASSET_CONFIG, BUY_START_SECONDS,
    ENTRY_LAST_SECONDS, POLL_SECONDS, META_REFRESH_SECONDS,
    FEE_CENTS_PER_CONTRACT, NUM_CONCURRENT_BOTS,
    ENABLE_SESSION_LIMITS, DAILY_MAX_LOSS_PERCENT,
    SESSION_CONSECUTIVE_LOSSES_LIMIT, SESSION_COOLDOWN_MINUTES,
    BALANCE_CHECK_DELAY_SECONDS,
    BOT_ALLOCATION, MIN_BOT_BALANCE, MAX_COST_PER_MARKET,
    POSTER_AMEND_INTERVAL, POSTER_EXPIRY_BUFFER,
    POSTER_OBSERVE_START_SECONDS,
)
from scoring import evaluate_entry

# ======================== BOOT BANNER ========================
print(f"BOOT: sol_bot.py loaded at {datetime.now(timezone.utc).isoformat()}Z", flush=True)

# ======================== LOGGING ============================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-sol")
log.warning("BOOT: logger initialized")

print(f"DEBUG: KALSHI_API_KEY_ID exists: {bool(os.getenv('KALSHI_API_KEY_ID'))}", flush=True)
print(f"DEBUG: KALSHI_PRIVATE_KEY_PEM_BASE64 exists: {bool(os.getenv('KALSHI_PRIVATE_KEY_PEM_BASE64'))}", flush=True)
print(f"DEBUG: KALSHI_API_BASE = {os.getenv('KALSHI_API_BASE', 'not set')}", flush=True)

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

def env_keys_with_prefix(prefix: str) -> List[str]:
    return sorted([k for k in os.environ.keys() if k.startswith(prefix)])

def now_ms() -> int:
    return int(time.time() * 1000)

def clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(x)))

# ======================== ASSET CONFIG =======================
BOT_ID = "SOL"
ASSET = "SOL"
ASSET_CFG = ASSET_CONFIG[ASSET]
SERIES_TICKER = getenv_first(["SERIES", "KALSHI_SERIES", "KALSHI_SERIES_TICKER"], ASSET_CFG['series_ticker'])
COINBASE_SPOT_URL = ASSET_CFG['spot_url']
COINBASE_CANDLES_URL = ASSET_CFG['candles_url']
SPOT_SIGMA_USD_PER_SQRT_SEC = ASSET_CFG['default_sigma']
SIGMA_FLOOR = ASSET_CFG['sigma_floor']
SIGMA_CEIL = ASSET_CFG['sigma_ceil']
BOUNDARY_BUFFER_USD = ASSET_CFG['boundary_buffer_usd']

# Fast local guard: prevents re-entry during API lag
TRADED_TICKERS: set = set()

# ======================== API CONFIG =========================
API_BASE = getenv_first(["KALSHI_API_BASE"], "https://api.elections.kalshi.com").rstrip("/")
API_PREFIX = getenv_first(["KALSHI_API_PREFIX"], "/trade-api/v2").rstrip("/")
API_KEY_ID = getenv_first(["KALSHI_API_KEY_ID"], "")
PRIVATE_KEY_PEM_B64 = getenv_first(["KALSHI_PRIVATE_KEY_PEM_BASE64"], "")
MARKET_OVERRIDE = getenv_first(["MARKET_OVERRIDE", "KALSHI_MARKET_OVERRIDE"], "<none>")
EVENT_TICKER = getenv_first(["EVENT_TICKER", "KALSHI_EVENT_TICKER"], "<auto>")

# ======================== TRADING CONFIG =====================
DRY_RUN = env_bool("DRY_RUN", False)
ENABLE_TRADING = env_bool("ENABLE_TRADING", True)
POST_ONLY = env_bool("POST_ONLY", False)  # Taker by default for cheap contracts
LOG_STATE_EVERY_SECONDS = env_float("LOG_STATE_EVERY_SECONDS", 10.0)

# ── WATCH-CONFIRM STRATEGY CONSTANTS ──
WATCH_WINDOW_SECONDS = 90    # Start watching at 90 seconds left
CONFIRM_THRESHOLD = 96       # Cents — either side must hold this
CONFIRM_CHECKS = 5           # Consecutive checks above threshold before buying
HEARTBEAT_SECONDS = env_float("HEARTBEAT_SECONDS", 15.0)

# Sigma (volatility) caching
USE_DYNAMIC_SIGMA = env_bool("USE_DYNAMIC_SIGMA", True)
CANDLES_GRANULARITY_SEC = env_int("CANDLES_GRANULARITY_SEC", 60)
CANDLES_LOOKBACK = env_int("CANDLES_LOOKBACK", 10)
SIGMA_REFRESH_SECONDS = env_float("SIGMA_REFRESH_SECONDS", 5.0)
_last_sigma_ts: float = 0.0
_last_sigma_val: float = SPOT_SIGMA_USD_PER_SQRT_SEC

# Model blending
USE_MARKET_IMPLIED = env_bool("USE_MARKET_IMPLIED", True)
MODEL_BLEND_ALPHA = env_float("MODEL_BLEND_ALPHA", 0.20)


# ======================== KALSHI API CLIENT ==================
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
            asy_padding.PSS(mgf=asy_padding.MGF1(hashes.SHA256()), salt_length=asy_padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(sig).decode("utf-8")
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                json_body: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.api_base}{self.api_prefix}{path}"
        url_with_q = url + "?" + urlencode(params) if params else url
        headers = self._sign_headers(method, url)
        headers["Accept"] = "application/json"
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resp = self.session.request(method=method.upper(), url=url_with_q, headers=headers,
                                    json=json_body, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path}: body={resp.text or ''}")
        if resp.content:
            return resp.json()
        return None


# ======================== TIMESTAMP / MARKET HELPERS =========
NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

def _parse_iso_to_epoch_s(s: str) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp())
    except Exception:
        return None

def infer_close_ts_from_ticker(ticker: str, interval_minutes: int = 15) -> Optional[int]:
    if not ticker:
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
    for k in ("close_ts", "closeTs", "close_time_ts", "closeTimeTs", "close_timestamp",
              "closeTimestamp", "close_time", "closeTime", "expiration_ts", "expirationTs"):
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
    for k in ("close_time", "closeTime", "close_datetime", "closeDateTime",
              "expiration_time", "expirationTime"):
        v = market_obj.get(k)
        if isinstance(v, str):
            ts = _parse_iso_to_epoch_s(v)
            if ts is not None:
                return ts
    return infer_close_ts_from_ticker(ticker, interval_minutes=15)

def extract_close_ts(market_obj: Dict[str, Any], market_ticker: str) -> Optional[int]:
    return resolve_close_ts(market_obj, market_ticker)

def pick_active_market(markets: List[Dict[str, Any]]) -> Tuple[str, str, Dict[str, Any]]:
    now_ts = int(time.time())

    def get_ts(obj, key):
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
        if status and status not in ("open", "active"):
            continue
        ot = get_ts(m, "open_time") or get_ts(m, "open_ts") or get_ts(m, "open_timestamp")
        ct = get_ts(m, "close_time") or get_ts(m, "close_ts") or get_ts(m, "close_timestamp")
        if ot is None:
            for k in ("open_time", "open_ts", "open_timestamp"):
                v = m.get(k)
                if isinstance(v, str) and v:
                    parsed = _parse_iso_to_epoch_s(v)
                    if parsed:
                        ot = parsed
                        break
        if ct is None:
            for k in ("close_time", "close_ts", "close_timestamp"):
                v = m.get(k)
                if isinstance(v, str) and v:
                    parsed = _parse_iso_to_epoch_s(v)
                    if parsed:
                        ct = parsed
                        break
        ticker = m.get("ticker") or m.get("market_ticker") or ""
        if ct is None and ticker:
            ct = infer_close_ts_from_ticker(ticker, interval_minutes=15)
        candidates.append((ot, ct, m))

    active, future, past = [], [], []
    for ot, ct, m in candidates:
        if ot is not None and ct is not None and ot <= now_ts < ct:
            active.append((ct, m))
        elif ct is not None and ct > now_ts:
            future.append((ct, m))
        else:
            past.append((ct or 0, m))

    log.info(f"[PICK] total_markets={len(markets)} candidates={len(candidates)} "
             f"active={len(active)} future={len(future)} past={len(past)}")

    if active:
        active.sort(key=lambda x: x[0])
        chosen = active[0][1]
    elif future:
        future.sort(key=lambda x: x[0])
        chosen = future[0][1]
    elif past:
        past.sort(key=lambda x: x[0], reverse=True)
        chosen = past[0][1]
        log.warning("[PICK] No active/future markets — using most recently closed")
    else:
        chosen = markets[0] if markets else {}
        if not chosen:
            raise RuntimeError("No markets available to pick from.")

    market_ticker = chosen.get("ticker") or chosen.get("market_ticker")
    event_ticker = (chosen.get("event_ticker") or
                    chosen.get("event", {}).get("ticker") or
                    chosen.get("event_ticker"))
    if not market_ticker or not event_ticker:
        raise RuntimeError(f"Could not determine event/market ticker from: {chosen}")
    return str(event_ticker), str(market_ticker), chosen

def market_bounds_usd(market_obj: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    lo = hi = None
    for lo_key in ("floor_strike", "lower_strike", "strike_lower", "floor"):
        if lo_key in market_obj:
            try:
                lo = float(market_obj[lo_key])
                break
            except Exception:
                pass
    for hi_key in ("cap_strike", "upper_strike", "strike_upper", "cap"):
        if hi_key in market_obj:
            try:
                hi = float(market_obj[hi_key])
                break
            except Exception:
                pass
    return lo, hi


# ======================== SPOT / PROBABILITY MODEL ===========
def fetch_spot_usd(session: requests.Session, timeout: float = 5.0) -> Optional[float]:
    try:
        r = session.get(COINBASE_SPOT_URL, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        amt = data.get("data", {}).get("amount")
        return float(amt) if amt is not None else None
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
        return max(0.0, min(1.0, _norm_cdf((hi - mean) / sd) - _norm_cdf((lo - mean) / sd)))
    if lo is not None:
        return max(0.0, min(1.0, 1.0 - _norm_cdf((lo - mean) / sd)))
    return max(0.0, min(1.0, _norm_cdf((hi - mean) / sd)))

def fetch_coinbase_candles(session: requests.Session, granularity: int, timeout: float = 5.0) -> Optional[List]:
    try:
        r = session.get(COINBASE_CANDLES_URL, params={"granularity": int(granularity)}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) and data else None
    except Exception:
        return None

def realized_sigma(session: requests.Session) -> Optional[float]:
    candles = fetch_coinbase_candles(session, CANDLES_GRANULARITY_SEC)
    if not candles or len(candles) < 3:
        return None
    take = sorted(candles[:max(3, CANDLES_LOOKBACK)], key=lambda x: float(x[0]))
    closes = []
    for c in take:
        try:
            closes.append(float(c[4]))
        except Exception:
            continue
    if len(closes) < 3:
        return None
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    if len(diffs) < 2:
        return None
    mean_d = sum(diffs) / len(diffs)
    var = sum((x - mean_d) ** 2 for x in diffs) / max(1, len(diffs) - 1)
    return math.sqrt(max(0.0, var)) / math.sqrt(60.0)

def get_sigma_cached(http: requests.Session) -> float:
    global _last_sigma_ts, _last_sigma_val
    now = time.time()
    if not USE_DYNAMIC_SIGMA:
        return float(SPOT_SIGMA_USD_PER_SQRT_SEC)
    if (now - _last_sigma_ts) < float(SIGMA_REFRESH_SECONDS):
        return float(_last_sigma_val)
    rs = realized_sigma(http)
    if rs is None or rs <= 0:
        _last_sigma_val = float(SPOT_SIGMA_USD_PER_SQRT_SEC)
    else:
        _last_sigma_val = float(max(SIGMA_FLOOR, min(SIGMA_CEIL, rs)))
    _last_sigma_ts = now
    return float(_last_sigma_val)


def calculate_fair_value_no(spot: float, lo: Optional[float], hi: Optional[float],
                             sigma: float, secs_to_close: float,
                             yes_bid: Optional[int], yes_ask: Optional[int],
                             no_bid: Optional[int], no_ask: Optional[int]) -> int:
    """
    Calculate model fair value for NO contracts in cents.

    Uses the same blended probability model already in the bot:
    - Gaussian model: probability YES price ends in range
    - Blended with market implied probability
    - Returns fair value in cents (1-99)

    Fair value = p_no × 100
    If model says 75% NO wins, fair value = 75¢
    If market offers NO at 65¢, that's a 10¢ discount = edge
    """
    # Step 1: Model probability
    t_eff = max(5.0, float(min(secs_to_close, 120)))
    sd = sigma * math.sqrt(t_eff)
    p_yes_model = prob_yes_in_range(spot, lo, hi, sd)

    # Step 2: Blend with market implied
    p_mkt = implied_prob_from_book(yes_bid, yes_ask, no_bid, no_ask) if USE_MARKET_IMPLIED else None
    if p_mkt is not None:
        if secs_to_close <= 60:
            alpha = 0.0
        elif secs_to_close <= BUY_START_SECONDS:
            alpha = MODEL_BLEND_ALPHA * (secs_to_close - 60) / float(BUY_START_SECONDS - 60)
        else:
            alpha = MODEL_BLEND_ALPHA
        p_yes = max(0.0, min(1.0, alpha * p_yes_model + (1.0 - alpha) * float(p_mkt)))
    else:
        p_yes = p_yes_model

    p_no = 1.0 - p_yes

    # Step 3: Convert to cents and clamp
    fair_value = int(round(p_no * 100))
    return max(1, min(99, fair_value))


# ======================== ORDERBOOK PARSING ==================
def _best_from_levels(levels: Any, want: str) -> Optional[int]:
    if not isinstance(levels, list) or not levels:
        return None
    best = None
    for lv in levels:
        p = None
        if isinstance(lv, (list, tuple)) and len(lv) >= 1:
            try:
                p = int(lv[0])
            except Exception:
                pass
        elif isinstance(lv, dict):
            for k in ("price", "yes_price", "p"):
                if k in lv:
                    try:
                        p = int(lv[k])
                        break
                    except Exception:
                        pass
        if p is None:
            continue
        if best is None:
            best = p
        else:
            best = max(best, p) if want == "bid" else min(best, p)
    return clamp_int(best, 1, 99) if best is not None else None

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

    if isinstance(root, dict) and isinstance(root.get("yes"), list):
        if yes_bid is None:
            yes_bid = _best_from_levels(root.get("yes"), "bid")
    if isinstance(root, dict) and isinstance(root.get("no"), list):
        if no_bid is None:
            no_bid = _best_from_levels(root.get("no"), "bid")

    # Derive missing prices from complement
    if yes_ask is None and no_bid is not None:
        yes_ask = clamp_int(100 - no_bid, 1, 99)
    if no_ask is None and yes_bid is not None:
        no_ask = clamp_int(100 - yes_bid, 1, 99)
    if yes_bid is None and no_ask is not None:
        yes_bid = clamp_int(100 - no_ask, 1, 99)
    if no_bid is None and yes_ask is not None:
        no_bid = clamp_int(100 - yes_ask, 1, 99)

    # Sanity: ask must be > bid
    if yes_bid is not None and yes_ask is not None and yes_ask <= yes_bid:
        yes_ask = None
    if no_bid is not None and no_ask is not None and no_ask <= no_bid:
        no_ask = None

    return yes_bid, yes_ask, no_bid, no_ask

def implied_prob_from_book(yes_bid, yes_ask, no_bid, no_ask) -> Optional[float]:
    if yes_bid is not None and yes_ask is not None and yes_ask > yes_bid:
        return max(0.01, min(0.99, (yes_bid + yes_ask) / 200.0))
    if no_bid is not None and no_ask is not None and no_ask > no_bid:
        return max(0.01, min(0.99, 1.0 - (no_bid + no_ask) / 200.0))
    if yes_bid is not None:
        return max(0.01, min(0.99, yes_bid / 100.0))
    if yes_ask is not None:
        return max(0.01, min(0.99, yes_ask / 100.0))
    return None


# ======================== ORDER / POSITION API ===============
def get_positions(client: KalshiClient) -> List[Dict[str, Any]]:
    resp = client.request("GET", "/portfolio/positions", params={"limit": 200})
    if isinstance(resp, dict):
        for k in ("positions", "market_positions", "portfolio_positions"):
            if k in resp and isinstance(resp[k], list):
                return resp[k]
    return resp if isinstance(resp, list) else []

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
        if "HTTP 404" in str(e) or "not_found" in str(e):
            return "not_found"
        raise

def place_order(client: KalshiClient, payload: Dict[str, Any]) -> str:
    """Place order with IRON RULE enforcement."""
    ticker = payload.get("ticker", "")

    if payload.get("action") == "buy":
        # IRON RULE 1: check existing position via API
        if ticker:
            try:
                existing = abs(parse_position_for_market(get_positions(client), ticker))
                if existing > 0:
                    log.warning(f"[RULE 1] Already hold {existing} on {ticker} — BLOCKED")
                    return "BLOCKED_BY_RULE_1"
            except Exception as e:
                log.warning(f"[RULE 1] Position check failed: {e} — proceeding with caution")

    resp = client.request("POST", "/portfolio/orders", json_body=payload)
    if isinstance(resp, dict):
        if "order" in resp and isinstance(resp["order"], dict) and resp["order"].get("order_id"):
            return str(resp["order"]["order_id"])
        if resp.get("order_id"):
            return str(resp["order_id"])
    raise RuntimeError(f"Unexpected create order response: {resp}")

def get_order(client: KalshiClient, order_id: str) -> Optional[Dict[str, Any]]:
    try:
        resp = client.request("GET", f"/portfolio/orders/{order_id}")
        return resp.get("order", resp) if isinstance(resp, dict) else None
    except Exception as e:
        log.warning(f"[ORDER] get_order({order_id}) failed: {e}")
        return None

FILL_CHECK_DELAY = 2.0
FILL_CHECK_RETRIES = 2
FILL_CHECK_INTERVAL = 2.0

def wait_for_fill(client: KalshiClient, order_id: str, market: str) -> Tuple[str, int]:
    """Wait for order fill. Returns (status, filled_qty)."""
    time.sleep(FILL_CHECK_DELAY)
    for attempt in range(1, FILL_CHECK_RETRIES + 1):
        order = get_order(client, order_id)
        if order is None:
            try:
                pos = abs(parse_position_for_market(get_positions(client), market))
                if pos > 0:
                    log.info(f"[FILL] Order lookup failed but position={pos}")
                    return "filled", pos
            except Exception:
                pass
            return "unknown", 0

        status = order.get("status", "unknown")
        remaining = order.get("remaining_count", order.get("count", 0))
        total = order.get("count", 0)

        if status == "executed" or (remaining == 0 and total > 0):
            log.info(f"[FILL] Order {order_id} filled: {total}")
            return "filled", total
        if 0 < remaining < total:
            return "partial", total - remaining
        if status == "canceled":
            return "canceled", 0
        if attempt < FILL_CHECK_RETRIES:
            time.sleep(FILL_CHECK_INTERVAL)

    return "resting", 0

def get_balance_usd(client: KalshiClient) -> Tuple[Optional[float], Optional[float]]:
    try:
        resp = client.request("GET", "/portfolio/balance")
    except Exception as e:
        log.warning(f"[BALANCE] API exception: {e}")
        return None, None
    if not isinstance(resp, dict):
        return None, None
    balance_cents = resp.get("balance")
    portfolio_cents = resp.get("portfolio_value", 0)
    if balance_cents is not None:
        cash = float(balance_cents) / 100.0
        portfolio = float(portfolio_cents) / 100.0
        total = cash + portfolio
        log.info(f"[BALANCE] cash=${cash:.2f} portfolio=${portfolio:.2f} total=${total:.2f}")
        return cash, portfolio
    return None, None

def cancel_all_strays_for_market(client: KalshiClient, market_ticker: str) -> None:
    try:
        oo = get_open_orders(client)
    except Exception:
        return
    for o in oo:
        if str(o.get("ticker")) != str(market_ticker):
            continue
        oid = o.get("order_id") or o.get("id")
        if oid:
            try:
                cancel_order_status(client, str(oid))
                log.warning(f"[CLEAN] Canceled stray {oid} on {market_ticker}")
            except Exception:
                pass

def build_order_payload(market_ticker: str, side: str, price_cents: int,
                        count: int = 1, close_ts: Optional[int] = None) -> Dict[str, Any]:
    body = {
        "ticker": market_ticker,
        "action": "buy",
        "side": side,
        "type": "limit",
        "count": max(1, int(count)),
        "client_order_id": f"{BOT_ID}-{uuid.uuid4().hex[:12]}",
        "post_only": False,
        "time_in_force": "good_till_canceled",
    }
    if side == "yes":
        body["yes_price"] = int(price_cents)
    else:
        body["no_price"] = int(price_cents)
    if close_ts is not None:
        expiry = int(close_ts) - POSTER_EXPIRY_BUFFER
        if expiry > int(time.time()):
            body["expiration_ts"] = expiry
    return body


def amend_order(client: KalshiClient, order_id: str, new_price_cents: int,
                remaining_count: int, side: str = "no") -> Optional[Dict[str, Any]]:
    """
    Amend resting limit order price.
    Handles both YES and NO sides.
    """
    try:
        price_key = "yes_price" if side == "yes" else "no_price"
        payload = {
            "count": int(remaining_count),
            price_key: int(new_price_cents),
        }
        resp = client.request("POST", f"/portfolio/orders/{order_id}/amend",
                              json_body=payload)
        if isinstance(resp, dict):
            return resp.get("order", resp)
        return None
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return None  # Order already filled or expired
        log.warning(f"[AMEND] Failed for {order_id}: {e}")
        return None
    except Exception as e:
        log.warning(f"[AMEND] Unexpected error for {order_id}: {e}")
        return None


def validate_order(client: KalshiClient, ticker: str, side: str,
                   quantity: int, price_cents: int) -> Tuple[bool, str]:
    """
    Hard validation gate. Called immediately before EVERY order placement.
    Returns (allowed, reason). FAIL-CLOSED: if any check errors, block.
    """
    # GATE 0: Fast local guard — catches re-entry during API lag
    if ticker in TRADED_TICKERS:
        return False, f"Already in TRADED_TICKERS (fast guard)"

    # GATE 1: Cost cap
    proposed_cost = quantity * (price_cents / 100.0)
    if proposed_cost > MAX_COST_PER_MARKET:
        return False, f"{quantity}ct × {price_cents}¢ = ${proposed_cost:.2f} > ${MAX_COST_PER_MARKET} max"

    # GATE 2: Existing position check (API, not local state) — FAIL-CLOSED
    try:
        existing = abs(parse_position_for_market(get_positions(client), ticker))
        if existing > 0:
            TRADED_TICKERS.add(ticker)  # Sync fast guard with API reality
            return False, f"Already own {existing}ct on {ticker}"
    except Exception as e:
        log.warning(f"[VALIDATE] Position check failed: {e} — BLOCKING (fail-closed)")
        return False, f"Position check failed: {e}"

    return True, "OK"


def run_gap_amend_loop(client, order_id: str,
                        market_ticker: str, close_ts: float,
                        get_ob_func, get_spot_func,
                        lo: float, hi: float,
                        http_session, side: str) -> int:
    """
    Gap-based amend loop.
    Tracks book price upward as market catches up.
    Cancels if score drops below threshold.
    Cancels explicitly when expiry buffer triggers.

    side: 'yes' or 'no'
    """
    last_amend = time.time()
    current_price = None

    order = get_order(client, order_id)
    if order:
        current_price = (order.get("yes_price") if side == "yes"
                        else order.get("no_price"))

    log.info(
        f"[GAP-AMEND] Start {order_id} side={side} "
        f"price={current_price}¢"
    )

    while True:
        now = time.time()
        secs_to_close = close_ts - now

        # ── EXPIRY BUFFER — cancel and exit ──────────────────
        if secs_to_close <= POSTER_EXPIRY_BUFFER:
            log.warning(
                f"[GAP-AMEND] Expiry buffer reached "
                f"({secs_to_close:.0f}s left) — canceling {order_id}"
            )
            cancel_order_status(client, order_id)
            break

        # ── CHECK ORDER STATUS ────────────────────────────────
        order = get_order(client, order_id)
        if order is None:
            break

        status = order.get("status", "")
        fill_count = order.get("fill_count", 0)
        remaining = order.get("remaining_count", 0)

        if status in ("executed", "canceled") or remaining == 0:
            log.info(
                f"[GAP-AMEND] Done: status={status} "
                f"filled={fill_count}"
            )
            break

        # ── AMEND INTERVAL ────────────────────────────────────
        if (now - last_amend) >= POSTER_AMEND_INTERVAL:
            try:
                ob = get_ob_func()
                spot = get_spot_func()
                yes_bid, yes_ask, no_bid, no_ask = parse_best_yes_no(ob)

                if spot is None:
                    last_amend = now
                    time.sleep(POLL_SECONDS)
                    continue

                sigma = get_sigma_cached(http_session)
                fair_no = calculate_fair_value_no(
                    spot, lo, hi, sigma, secs_to_close,
                    yes_bid, yes_ask, no_bid, no_ask
                )
                p_no = fair_no / 100.0
                p_yes = 1.0 - p_no

                fair_value = int(round((p_yes if side == "yes" else p_no) * 100))
                book_ask = yes_ask if side == "yes" else no_ask
                gap = (fair_value - book_ask) if book_ask is not None else 0

                # V9 re-evaluation using scoring module
                if book_ask is not None:
                    re_entry = evaluate_entry(
                        ASSET, side, book_ask, fair_value,
                        float(secs_to_close), 1000.0
                    )
                else:
                    re_entry = None

                log.info(
                    f"[GAP-AMEND] Re-eval: side={side} "
                    f"fair={fair_value}¢ book_ask={book_ask}¢ "
                    f"gap={gap:+d}¢ v9={'pass' if re_entry else 'fail'}"
                )

                # ── CANCEL IF V9 SCORE BELOW THRESHOLD ───────────
                if re_entry is None:
                    log.warning(
                        f"[GAP-AMEND] V9 score below threshold "
                        f"— canceling {order_id}"
                    )
                    cancel_order_status(client, order_id)
                    break

                new_post_price, _, _, _, _ = re_entry

                # ── AMEND TO V9 POST PRICE ──────────
                if book_ask is not None:
                    new_price = new_post_price

                    if new_price != current_price and remaining > 0:
                        result = amend_order(
                            client, order_id,
                            new_price, remaining, side
                        )
                        if result is not None:
                            log.warning(
                                f"[GAP-AMEND] {market_ticker} "
                                f"{current_price}¢ → {new_price}¢ "
                                f"gap={gap:+d}¢"
                            )
                            current_price = new_price
                        else:
                            break

            except Exception as e:
                log.warning(f"[GAP-AMEND] Error: {e}")

            last_amend = now

        time.sleep(POLL_SECONDS)

    final = get_order(client, order_id)
    filled = int(final.get("fill_count", 0)) if final else 0
    log.info(f"[GAP-AMEND] Exit {order_id} filled={filled}")
    return filled


def _sum_qty_from_levels(levels: Any) -> int:
    """Sum up contract quantities from orderbook levels."""
    if not levels or not isinstance(levels, list):
        return 0
    total_qty = 0
    for lv in levels:
        qty = 0
        if isinstance(lv, (list, tuple)) and len(lv) >= 2:
            try:
                qty = int(lv[1])
            except Exception:
                pass
        elif isinstance(lv, dict):
            for k in ("quantity", "qty", "count", "size"):
                if k in lv:
                    try:
                        qty = int(lv[k])
                        break
                    except Exception:
                        pass
        total_qty += qty
    return total_qty


def get_orderbook_depth(ob: Dict[str, Any], side: str) -> int:
    """
    Check how many contracts we can BUY on the given side.

    Kalshi orderbook: {"orderbook": {"yes": [[price, qty], ...], "no": [...]}}
    The "yes" array = YES bids, "no" array = NO bids.
    To BUY YES, we need YES asks, which come from NO bids (opposite side).
    To BUY NO, we need NO asks, which come from YES bids (opposite side).
    """
    if not isinstance(ob, dict):
        return 0
    root = ob.get("orderbook") if isinstance(ob.get("orderbook"), dict) else ob
    if not isinstance(root, dict):
        return 0

    # Look at OPPOSITE side — their bids are our asks
    opposite = "no" if side == "yes" else "yes"
    levels = root.get(opposite)

    if isinstance(levels, dict):
        # Dict format with bids/asks keys
        levels = levels.get("bids", levels.get("buy", []))
    elif not isinstance(levels, list):
        levels = []

    return _sum_qty_from_levels(levels)


# ======================== SESSION STATE ======================
@dataclass
class SessionState:
    starting_balance_usd: float = 0.0
    current_balance_usd: float = 0.0
    daily_pnl_usd: float = 0.0
    total_wins: int = 0
    total_losses: int = 0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    is_paused: bool = False
    pause_until: float = 0.0
    is_daily_stopped: bool = False
    pending_balance_check_at: float = 0.0
    recent_trades: List[Dict[str, Any]] = field(default_factory=list)

    def record_trade(self, market: str, side: str, entry_price: int,
                     exit_price: int, qty: int, pnl_cents: int):
        pnl_usd = pnl_cents / 100.0
        self.daily_pnl_usd += pnl_usd
        self.current_balance_usd += pnl_usd
        self.recent_trades.append({
            "market": market, "side": side, "entry": entry_price,
            "exit": exit_price, "qty": qty, "pnl_cents": pnl_cents,
            "ts": time.time(),
        })
        if len(self.recent_trades) > 50:
            self.recent_trades = self.recent_trades[-50:]

        if pnl_cents > 0:
            self.total_wins += 1
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.total_losses += 1
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            self._check_consecutive_limit()

        self.schedule_balance_check()
        wr = (self.total_wins / max(1, self.total_wins + self.total_losses)) * 100
        log.warning(
            f"[SETTLE] pnl=${pnl_usd:+.2f} daily=${self.daily_pnl_usd:+.2f} "
            f"bal=${self.current_balance_usd:.2f} W/L={self.total_wins}/{self.total_losses} ({wr:.0f}%)"
        )
        self._check_daily_stop()

    def schedule_balance_check(self):
        self.pending_balance_check_at = time.time() + BALANCE_CHECK_DELAY_SECONDS

    def needs_balance_check(self) -> bool:
        return self.pending_balance_check_at > 0 and time.time() >= self.pending_balance_check_at

    def clear_balance_check(self):
        self.pending_balance_check_at = 0.0

    def update_balance(self, bal: float):
        self.current_balance_usd = bal
        self.daily_pnl_usd = bal - self.starting_balance_usd

    def _check_daily_stop(self):
        if not ENABLE_SESSION_LIMITS or self.starting_balance_usd <= 0:
            return
        loss_pct = -self.daily_pnl_usd / self.starting_balance_usd
        if loss_pct >= DAILY_MAX_LOSS_PERCENT:
            self.is_daily_stopped = True
            self.is_paused = True
            log.warning(f"[SESSION] DAILY HARD STOP: lost {loss_pct:.0%}")

    def _check_consecutive_limit(self):
        if not ENABLE_SESSION_LIMITS:
            return
        if self.consecutive_losses >= SESSION_CONSECUTIVE_LOSSES_LIMIT:
            self.is_paused = True
            self.pause_until = time.time() + (SESSION_COOLDOWN_MINUTES * 60)
            log.warning(f"[SESSION] PAUSED: {self.consecutive_losses} consecutive losses")

    def check_can_trade(self) -> Tuple[bool, Optional[str]]:
        if self.is_daily_stopped:
            return False, "DAILY_HARD_STOP"
        if not self.is_paused:
            return True, None
        if self.pause_until > 0 and time.time() >= self.pause_until:
            self.is_paused = False
            self.consecutive_losses = 0
            return True, None
        remaining = int(self.pause_until - time.time()) if self.pause_until > 0 else 0
        return False, f"cooldown ({remaining}s)"


# ======================== BOT STATE ==========================
@dataclass
class BotState:
    market: Optional[str] = None
    event: Optional[str] = None
    traded_this_market: bool = False
    side: Optional[str] = None
    qty: int = 0
    entry_price_cents: Optional[int] = None
    order_id: Optional[str] = None
    last_evaluated_yes_ask: Optional[int] = None
    last_evaluated_no_ask: Optional[int] = None
    # Watch-confirm state
    certainty_counter: int = 0
    certainty_side: Optional[str] = None
    watch_active: bool = False
    # Deferred settlement
    pending_settlement_market: Optional[str] = None
    pending_settlement_side: Optional[str] = None
    pending_settlement_entry_price: Optional[int] = None
    pending_settlement_qty: int = 0
    pending_settlement_ts: float = 0.0


# ======================== HEALTH SERVER ======================
HEALTH_CHECK_PORT = int(os.environ.get("PORT", "10000"))

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, fmt, *args):
        pass

def _start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", HEALTH_CHECK_PORT), _HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.warning(f"[HEALTH] Listening on port {HEALTH_CHECK_PORT}")
    except Exception as e:
        log.warning(f"[HEALTH] Could not start: {e}")


# ======================== MAIN ===============================
def main() -> None:
    _start_health_server()
    log.warning(f"[ENV] Detected KALSHI_* keys: {env_keys_with_prefix('KALSHI_')}")
    log.warning("=" * 70)
    log.warning(f"[RULES] {ASSET} — WATCH-CONFIRM STRATEGY (Feb 2026)")
    log.warning(f"[RULES] RULE 1: Watch window = last {WATCH_WINDOW_SECONDS}s (90s)")
    log.warning(f"[RULES] RULE 2: Confirm threshold = {CONFIRM_THRESHOLD}c on either side")
    log.warning(f"[RULES] RULE 3: {CONFIRM_CHECKS} consecutive checks → buy at ask+1")
    log.warning(f"[RULES] RULE 4: Instant entry at 99c — no ticks needed")
    log.warning(f"[RULES] RULE 5: Size = cash/4 (halved at 5am EST)")
    log.warning(f"[RULES] RULE 6: Single entry per market — no stacking")
    log.warning("=" * 70)

    # Watchdog
    _watchdog_ts = [time.time()]
    def _watchdog():
        while True:
            time.sleep(60)
            if time.time() - _watchdog_ts[0] > 300:
                log.error("[WATCHDOG] Stale >5min — restart")
                os._exit(1)
    threading.Thread(target=_watchdog, daemon=True).start()

    if not API_KEY_ID or not PRIVATE_KEY_PEM_B64:
        raise RuntimeError("Missing KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_PEM_BASE64")

    client = KalshiClient(API_BASE, API_PREFIX, API_KEY_ID, PRIVATE_KEY_PEM_B64)
    http = requests.Session()
    st = BotState()
    session = SessionState()

    try:
        av, _ = get_balance_usd(client)
        if av is not None:
            session.starting_balance_usd = av
            session.current_balance_usd = av
            log.warning(f"[SESSION] Starting balance: ${av:.2f}")
    except Exception as e:
        log.warning(f"[SESSION] Could not fetch starting balance: {e}")

    last_meta = 0.0
    last_state_log = 0.0
    last_heartbeat = 0.0

    def refresh_active_market():
        if MARKET_OVERRIDE not in ("<none>", "none", "None", ""):
            mt = MARKET_OVERRIDE
            try:
                snap = client.request("GET", f"/markets/{mt}")
                mobj = snap.get("market", snap) if isinstance(snap, dict) else {}
            except Exception:
                mobj = {}
            ev = EVENT_TICKER if EVENT_TICKER != "<auto>" else "<manual>"
            return ev, mt, mobj
        params = {"series_ticker": SERIES_TICKER, "status": "open", "limit": 200}
        resp = client.request("GET", "/markets", params=params)
        markets = resp.get("markets", []) if isinstance(resp, dict) else []
        if not markets:
            raise RuntimeError(f"No open markets for {SERIES_TICKER}")
        return pick_active_market(markets)

    def reconcile_on_market_change(new_market: str):
        cancel_all_strays_for_market(client, new_market)
        try:
            pos = parse_position_for_market(get_positions(client), new_market)
        except Exception:
            pos = 0
        if pos != 0:
            st.traded_this_market = True
            st.side = "yes" if pos > 0 else "no"
            st.qty = abs(pos)
            TRADED_TICKERS.add(new_market)  # Sync fast guard with API
            log.warning(f"[RECON] Position on {new_market}: {st.side} x{st.qty} — HOLD")
            return
        st.traded_this_market = False
        st.side = None
        st.qty = 0
        st.order_id = None
        st.entry_price_cents = None
        st.last_evaluated_yes_ask = None
        st.last_evaluated_no_ask = None
        st.certainty_counter = 0
        st.certainty_side = None
        st.watch_active = False

    ev, mt, mobj = refresh_active_market()
    active_market_obj = mobj or {}
    st.market = mt
    st.event = ev
    reconcile_on_market_change(mt)
    last_meta = time.time()
    # ======================== MAIN LOOP ======================
    while True:
        now = time.time()
        _watchdog_ts[0] = now

        # Heartbeat
        if (now - last_heartbeat) >= HEARTBEAT_SECONDS:
            wr = (session.total_wins / max(1, session.total_wins + session.total_losses)) * 100
            log.warning(
                f"[HEARTBEAT] market={st.market} traded={st.traded_this_market} "
                f"traded_set={len(TRADED_TICKERS)} | "
                f"pnl=${session.daily_pnl_usd:+.2f} bal=${session.current_balance_usd:.2f} "
                f"W/L={session.total_wins}/{session.total_losses} ({wr:.0f}%)"
            )
            last_heartbeat = now

        # Deferred settlement
        if st.pending_settlement_market is not None:
            pending_age = now - st.pending_settlement_ts
            if pending_age > 600:
                log.warning(f"[SETTLE] Giving up on {st.pending_settlement_market}")
                st.pending_settlement_market = None
            elif int(now) % 30 < POLL_SECONDS + 1:
                try:
                    pend_data = client.request("GET", f"/markets/{st.pending_settlement_market}")
                    pend_obj = pend_data.get("market", pend_data) if isinstance(pend_data, dict) else {}
                    result = pend_obj.get("result", "").lower()
                    if result in ("yes", "no"):
                        s = st.pending_settlement_side
                        e = st.pending_settlement_entry_price
                        q = st.pending_settlement_qty
                        pnl = (100 - e) * q if result == s else -e * q
                        session.record_trade(st.pending_settlement_market, s, e,
                                             100 if result == s else 0, q, pnl)
                        log.warning(
                            f"[SETTLE] Deferred: {st.pending_settlement_market} {s.upper()} "
                            f"result={result} pnl={pnl}¢"
                        )
                        st.pending_settlement_market = None
                except Exception:
                    pass

        # Balance check
        if session.needs_balance_check():
            try:
                bal, _ = get_balance_usd(client)
                if bal is not None:
                    session.update_balance(bal)
                session.clear_balance_check()
            except Exception:
                pass

        # Market refresh
        if (now - last_meta) >= META_REFRESH_SECONDS:
            try:
                ev2, mt2, mobj2 = refresh_active_market()
                if mt2 != st.market:
                    old_market = st.market
                    log.warning(f"[ROLL] {old_market} -> {mt2}")

                    if st.traded_this_market and st.entry_price_cents is not None and st.side and st.qty > 0:
                        result = None
                        for retry in range(3):
                            try:
                                old_data = client.request("GET", f"/markets/{old_market}")
                                old_obj = old_data.get("market", old_data) if isinstance(old_data, dict) else {}
                                result = old_obj.get("result", "").lower()
                                if result in ("yes", "no"):
                                    break
                                time.sleep(10.0)
                            except Exception:
                                time.sleep(10.0)

                        if result in ("yes", "no"):
                            pnl = ((100 - st.entry_price_cents) * st.qty if result == st.side
                                   else -st.entry_price_cents * st.qty)
                            session.record_trade(old_market, st.side, st.entry_price_cents,
                                                 100 if result == st.side else 0, st.qty, pnl)
                            log.warning(
                                f"[SETTLE] {old_market} {st.side.upper()} @ {st.entry_price_cents}¢ "
                                f"result={result} pnl={pnl}¢"
                            )
                        else:
                            st.pending_settlement_market = old_market
                            st.pending_settlement_side = st.side
                            st.pending_settlement_entry_price = st.entry_price_cents
                            st.pending_settlement_qty = st.qty
                            st.pending_settlement_ts = time.time()
                            log.warning(f"[ROLL] Deferring settlement for {old_market}")

                    TRADED_TICKERS.discard(old_market)
                    st.event = ev2
                    st.market = mt2
                    active_market_obj = mobj2 or {}
                    st.traded_this_market = False
                    st.side = None
                    st.qty = 0
                    st.order_id = None
                    st.entry_price_cents = None
                    st.last_evaluated_yes_ask = None
                    st.last_evaluated_no_ask = None
                    st.certainty_counter = 0
                    st.certainty_side = None
                    st.watch_active = False
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
        secs_to_close = int(close_ts - int(time.time())) if close_ts else None

        # Check API position
        pos = 0
        try:
            pos = parse_position_for_market(get_positions(client), st.market)
        except Exception:
            pass

        # HOLD: already positioned
        if pos != 0:
            # Always sync side/qty from API — handles case where order filled
            # but fill branch didn't set st.side (e.g., filled_qty=0 race)
            api_side = "yes" if pos > 0 else "no"
            api_qty = abs(pos)
            if not st.traded_this_market:
                st.traded_this_market = True
                TRADED_TICKERS.add(st.market)
                log.warning(f"[HOLD] {st.market} {api_side.upper()} x{api_qty}")
            if st.side is None:
                st.side = api_side
            st.qty = api_qty
            if secs_to_close is not None and (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                log.info(f"[HOLD] {st.market} {st.side.upper()} x{st.qty} t={secs_to_close}s")
                last_state_log = now
            time.sleep(POLL_SECONDS)
            continue

        # Already traded
        if st.traded_this_market:
            time.sleep(POLL_SECONDS)
            continue

        if secs_to_close is None:
            last_meta = 0.0
            time.sleep(POLL_SECONDS)
            continue

        # Fetch spot
        spot = fetch_spot_usd(http)
        if spot is None:
            time.sleep(POLL_SECONDS)
            continue

        # Fetch orderbook
        try:
            ob = client.request("GET", f"/markets/{st.market}/orderbook")
        except Exception as e:
            log.warning(f"[OB] failed: {e}")
            time.sleep(POLL_SECONDS)
            continue

        yes_bid, yes_ask, no_bid, no_ask = parse_best_yes_no(ob)
        lo, hi = market_bounds_usd(active_market_obj)

        # Calculate probability
        sigma = get_sigma_cached(http)
        t_eff = max(5.0, float(min(secs_to_close, 120)))
        sd = sigma * math.sqrt(t_eff)
        p_yes_model = prob_yes_in_range(spot, lo, hi, sd)

        p_mkt = implied_prob_from_book(yes_bid, yes_ask, no_bid, no_ask) if USE_MARKET_IMPLIED else None
        if p_mkt is not None:
            if secs_to_close <= 60:
                alpha = 0.0
            elif secs_to_close <= BUY_START_SECONDS:
                alpha = MODEL_BLEND_ALPHA * (secs_to_close - 60) / float(BUY_START_SECONDS - 60)
            else:
                alpha = MODEL_BLEND_ALPHA
            p_yes = max(0.0, min(1.0, alpha * p_yes_model + (1.0 - alpha) * float(p_mkt)))
        else:
            p_yes = p_yes_model
        p_no = 1.0 - p_yes

        # FAST GUARD: ALWAYS first — before any other check
        if st.market in TRADED_TICKERS:
            if not st.traded_this_market:
                st.traded_this_market = True
                log.warning(f"[SKIP-FAST] {st.market} in TRADED_TICKERS — blocking re-entry")
            time.sleep(POLL_SECONDS)
            continue

        # Too late — close window
        if secs_to_close < ENTRY_LAST_SECONDS:
            if secs_to_close < 0:
                last_meta = 0.0
                time.sleep(10.0)
            else:
                TRADED_TICKERS.add(st.market)
                st.traded_this_market = True
            continue

        # ── GAP POSTER V6 ────────────────────────────────────────────
        # Observe entire market. Post when model leads book by MIN_GAP.
        # Both sides valid. Tiered sizing. Gap = fair_value - book_ask.
        if not st.traded_this_market:

            # Session check
            can_trade, reason = session.check_can_trade()
            if not can_trade:
                log.warning(f"[SESSION] Paused: {reason}")
                time.sleep(POLL_SECONDS)
                continue

            # Balance floor check (balance kept fresh by needs_balance_check guard)
            if session.current_balance_usd < MIN_BOT_BALANCE:
                log.warning(f"[SKIP] Balance ${session.current_balance_usd:.2f} < floor")
                time.sleep(POLL_SECONDS)
                continue

            # ── WATCH-CONFIRM ENTRY ──────────────────────────────────────
            # Watch final 5 min. If either side holds bid >= 92¢ for 4
            # consecutive checks, buy at ask. Size: 20% of live balance.

            # Outside watch window — reset and wait
            if secs_to_close > WATCH_WINDOW_SECONDS:
                if st.certainty_counter > 0:
                    log.info(f"[RESET] {st.market} outside watch window — counter cleared")
                    st.certainty_counter = 0
                    st.certainty_side = None
                st.watch_active = False
                time.sleep(POLL_SECONDS)
                continue

            # First tick inside watch window — log activation
            if not st.watch_active:
                st.watch_active = True
                log.warning(f"[WATCH-START] {st.market} entering 90s window at t={secs_to_close:.0f}s")

            # Tick heartbeat inside watch window
            log.info(
                f"[TICK] {st.market} t={secs_to_close:.0f}s "
                f"yes={yes_bid}¢ no={no_bid}¢"
            )

            # Instant entry at 99¢ — no confirmation ticks needed
            instant_side = None
            if yes_bid is not None and yes_bid >= 99:
                instant_side = 'yes'
            elif no_bid is not None and no_bid >= 99:
                instant_side = 'no'
            if instant_side is not None:
                log.warning(
                    f"[INSTANT] {st.market} {instant_side.upper()} locked at "
                    f"{'yes=' + str(yes_bid) if instant_side == 'yes' else 'no=' + str(no_bid)}¢ "
                    f"— entering immediately t={secs_to_close:.0f}s"
                )
                st.certainty_side = instant_side
                st.certainty_counter = CONFIRM_CHECKS  # skip straight to entry

            # Check which side is at or above threshold
            yes_certain = yes_bid is not None and yes_bid >= CONFIRM_THRESHOLD
            no_certain = no_bid is not None and no_bid >= CONFIRM_THRESHOLD

            if yes_certain:
                if st.certainty_side == 'yes':
                    st.certainty_counter += 1
                else:
                    st.certainty_side = 'yes'
                    st.certainty_counter = 1
                log.info(
                    f"[WATCH] {st.market} yes={yes_bid}¢ "
                    f"counter={st.certainty_counter}/{CONFIRM_CHECKS} t={secs_to_close:.0f}s"
                )
            elif no_certain:
                if st.certainty_side == 'no':
                    st.certainty_counter += 1
                else:
                    st.certainty_side = 'no'
                    st.certainty_counter = 1
                log.info(
                    f"[WATCH] {st.market} no={no_bid}¢ "
                    f"counter={st.certainty_counter}/{CONFIRM_CHECKS} t={secs_to_close:.0f}s"
                )
            else:
                # Below threshold — reset
                if st.certainty_counter > 0:
                    log.info(f"[RESET] {st.market} dropped below {CONFIRM_THRESHOLD}¢ — counter reset t={secs_to_close:.0f}s")
                st.certainty_counter = 0
                st.certainty_side = None
                time.sleep(POLL_SECONDS)
                continue

            # Not enough consecutive checks yet
            if st.certainty_counter < CONFIRM_CHECKS:
                time.sleep(POLL_SECONDS)
                continue

            # ── CONFIRMED: retry on each tick until filled, pivot on flip ──
            side = st.certainty_side

            # Fetch live balance once before the retry loop
            try:
                live_bal, _ = get_balance_usd(client)
                if live_bal is not None:
                    session.update_balance(live_bal)
                    log.info(f"[BALANCE-REFRESH] cash=${live_bal:.2f}")
            except Exception as bal_err:
                log.warning(f"[BALANCE-REFRESH] Failed to fetch live balance: {bal_err}")

            # Sizing: cash/4, halved during 5am EST (London open)
            hour_est = (datetime.utcnow().hour - 5) % 24
            is_london_open = (hour_est == 5)
            init_price = (yes_bid if side == 'yes' else no_bid) or 99
            position_size = session.current_balance_usd / 4
            if is_london_open:
                position_size = position_size / 2
                log.info(f"[5AM] London open hour — half sizing: ${position_size:.2f}")
            order_qty = int(position_size / (init_price / 100))
            order_qty = max(1, min(500, order_qty))

            if session.current_balance_usd < (init_price / 100):
                log.warning(
                    f"[SKIP] {st.market} — balance ${session.current_balance_usd:.2f} "
                    f"can't afford 1ct at {init_price}c"
                )
                time.sleep(POLL_SECONDS)
                continue

            log.warning(
                f"[RETRY-LOOP] {st.market} ENTERING {side.upper()} "
                f"qty={order_qty} bal=${session.current_balance_usd:.2f} "
                f"t={secs_to_close:.0f}s"
            )

            # Lock market immediately to prevent re-entry
            TRADED_TICKERS.add(st.market)
            st.traded_this_market = True
            st.side = side
            st.qty = 0

            resting_oid = None
            total_filled = 0
            attempt = 0
            last_price = None
            current_side = side
            deadline = close_ts - 5

            while time.time() < deadline:
                attempt += 1

                # Fetch fresh orderbook
                try:
                    fresh_ob = client.request("GET", f"/markets/{st.market}/orderbook")
                    yb, ya, nb, na = parse_best_yes_no(fresh_ob)
                except Exception as ob_err:
                    log.warning(f"[RETRY] #{attempt} orderbook fetch failed: {ob_err}")
                    time.sleep(POLL_SECONDS)
                    continue

                # Read both sides' bids
                yes_price = yb
                no_price = nb

                # Check for side flip: other side >= threshold AND current side < threshold
                other_side = 'no' if current_side == 'yes' else 'yes'
                current_bid = yes_price if current_side == 'yes' else no_price
                other_bid = no_price if current_side == 'yes' else yes_price

                if (other_bid is not None and other_bid >= CONFIRM_THRESHOLD and
                        (current_bid is None or current_bid < CONFIRM_THRESHOLD)):
                    # Market flipped — cancel resting order and pivot
                    if resting_oid:
                        try:
                            cancel_order_status(client, resting_oid)
                            # Capture any partial fills before canceling
                            order_info = client.request("GET", f"/portfolio/orders/{resting_oid}")
                            if order_info:
                                filled_now = order_info.get("quantity_filled", 0) or 0
                                if filled_now > total_filled:
                                    total_filled = filled_now
                        except Exception:
                            pass
                        resting_oid = None

                    log.warning(
                        f"[PIVOT] {st.market} #{attempt} flipped {current_side.upper()}→{other_side.upper()} "
                        f"cur_bid={current_bid} other_bid={other_bid} filled_so_far={total_filled}"
                    )
                    current_side = other_side
                    st.side = current_side
                    last_price = None
                    time.sleep(POLL_SECONDS)
                    continue

                # Determine buy price on current side: ask + 1¢
                if current_side == 'yes':
                    current_ask = ya
                else:
                    current_ask = na

                if current_ask is not None:
                    buy_price = min(current_ask + 1, 99)
                else:
                    buy_price = 99

                # Check if resting order already filled
                if resting_oid:
                    try:
                        order_info = client.request("GET", f"/portfolio/orders/{resting_oid}")
                        if order_info:
                            filled_now = order_info.get("quantity_filled", 0) or 0
                            status = order_info.get("status", "")
                            if filled_now >= order_qty or status == "filled":
                                total_filled = filled_now
                                log.warning(
                                    f"[FILLED] {st.market} #{attempt} "
                                    f"filled={total_filled}ct {current_side.upper()} order={resting_oid}"
                                )
                                resting_oid = None
                                break
                            if filled_now > 0:
                                total_filled = filled_now
                    except Exception:
                        pass

                # Cancel resting order if price changed
                if resting_oid and buy_price != last_price:
                    try:
                        cancel_order_status(client, resting_oid)
                        log.info(f"[CANCEL] {st.market} #{attempt} canceled order {resting_oid} (price moved {last_price}→{buy_price})")
                    except Exception as ce:
                        log.warning(f"[CANCEL] #{attempt} failed: {ce}")
                    # Re-check fills after cancel
                    try:
                        order_info = client.request("GET", f"/portfolio/orders/{resting_oid}")
                        if order_info:
                            filled_now = order_info.get("quantity_filled", 0) or 0
                            if filled_now > total_filled:
                                total_filled = filled_now
                    except Exception:
                        pass
                    resting_oid = None

                # If fully filled from partial checks, stop
                if total_filled >= order_qty:
                    log.warning(f"[FILLED] {st.market} fully filled {total_filled}ct")
                    break

                # Place new order if no resting order
                if not resting_oid:
                    remaining_qty = order_qty - total_filled
                    if remaining_qty <= 0:
                        break
                    cost = remaining_qty * buy_price / 100
                    log.warning(
                        f"[RETRY] {st.market} #{attempt} {current_side.upper()}@{buy_price}¢ "
                        f"qty={remaining_qty} cost=${cost:.2f} t={close_ts - time.time():.0f}s"
                    )
                    try:
                        payload = build_order_payload(
                            st.market, current_side, buy_price, remaining_qty,
                            close_ts=close_ts
                        )
                        oid = place_order(client, payload)

                        if oid and not oid.startswith("BLOCKED") and not oid.startswith("ERROR"):
                            resting_oid = oid
                            last_price = buy_price
                            st.order_id = oid
                            st.entry_price_cents = buy_price
                        else:
                            log.warning(f"[ORDER-FAIL] #{attempt} {oid}")
                    except Exception as e:
                        err = str(e)
                        if "insufficient_balance" in err or "insufficient balance" in err.lower():
                            log.warning(f"[RETRY] #{attempt} insufficient balance — stopping retry loop")
                            try:
                                live_bal, _ = get_balance_usd(client)
                                if live_bal is not None:
                                    session.update_balance(live_bal)
                            except Exception:
                                pass
                            break
                        else:
                            log.warning(f"[RETRY] #{attempt} order exception: {e}")

                time.sleep(POLL_SECONDS)

            # Cancel any resting order at end of loop
            if resting_oid:
                try:
                    cancel_order_status(client, resting_oid)
                    log.info(f"[CANCEL-FINAL] {st.market} canceled resting order {resting_oid}")
                    # Check final fill count
                    order_info = client.request("GET", f"/portfolio/orders/{resting_oid}")
                    if order_info:
                        filled_now = order_info.get("quantity_filled", 0) or 0
                        if filled_now > total_filled:
                            total_filled = filled_now
                except Exception:
                    pass

            st.qty = total_filled
            st.side = current_side
            st.entry_price_cents = last_price

            if total_filled > 0:
                payout = total_filled * (100 - (last_price or 99)) / 100.0
                log.warning(
                    f"[RESULT] {st.market} "
                    f"filled={total_filled}ct {current_side.upper()} @ {last_price}¢ "
                    f"potential_profit=${payout:.2f} attempts={attempt}"
                )
            else:
                log.info(
                    f"[RESULT] {st.market} 0 fills after {attempt} attempts — "
                    f"order expired or not matched"
                )

            time.sleep(POLL_SECONDS)
            continue
        # ── END POSTER ──────────────────────────────────────────────


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception(f"FATAL: {e} | {ASSET} V9 ALLOC=${BOT_ALLOCATION}")
        sys.exit(1)
