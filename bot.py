# bot.py
# Kalshi rolling 15m BTC — Certain wins, hold to close, scale bankroll
#
# STRATEGY:
# - Arms at T-720s (12 min) to OBSERVE market, BTC price, trends, book
# - Buys in the LAST 60 SECONDS — outcome is decided, book shows the winner
# - Trusts the MARKET (orderbook) over the model near settlement
# - Even a few cents edge per contract is fine — buy MORE contracts
# - HOLDS TO SETTLEMENT — collect the full payout for being right
# - Dump is ABORT ONLY — safety net, not a regular exit
# - Scales bankroll: wins compound, size grows, 96 markets/day
#
# KEY SETTINGS:
# - TIME-DEPENDENT PROB: 92% if >5min, 85% if 3-5min, 80% if <3min
# - EDGE_MIN=0.02 (small edge OK — volume over 96 markets compounds)
# - MAX_ENTRY_PRICE=95¢ (allow buying if certainty supports the price)
# - CONTRACT SCALING: start at 3, +1 per win, -1 per loss (floor at base)
# - BTC-AWARE BAIL: only dump if BTC has moved against us, not book noise
# - MULTI-TIMEFRAME TRENDS: 60-min + 30-min SpotTrend, per-minute ProbTrend
# - BANKROLL STOPS: max loss = 5% of balance or 50% of position cost
# - Dump abort: 12% reversal, 60s patience, catastrophic 30¢ backstop

import os
import time
import base64
import logging
import math
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
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

# -------------- HOLD-TO-CLOSE STRATEGY (HARDWIRED) --------------
# Philosophy: arm early to OBSERVE price/trends/book. Buy only when CERTAIN
# of the outcome. Even a few cents edge is fine — buy MORE contracts.
# Hold to settlement. Dump is abort-only. Trade every market possible.
#
# TWO PHASES:
#   OBSERVE (12min → 5min before close): gather trend data, watch book, DON'T buy
#   BUY     (5min → 15s before close):   make the call, place the order, hold
#
# Why: 92% probability at 12 minutes means NOTHING — BTC moves $300 in 12 min.
#      92% probability at 3 minutes is reliable — BTC can't move far enough.
#      The observation window builds high-quality trend data so the buy decision is informed.
OBSERVE_START_SECONDS = 720  # Start watching at 12min — gather trend + prob data
BUY_START_SECONDS = 120      # Can enter from T-120s — settlement lock is primary path, but don't get locked out of thin books
ENTRY_LAST_SECONDS = 5       # Can enter up to 5s before close (need time to fill)
FILL_WAIT_SECONDS = 20
ALLOW_TAKER_AT_LAST = True
CANCEL_UNFILLED_AT_CLOSE = True

PROB_MIN = 0.92  # 92%+ to enter — with market-weighted blend in last 60s, book at 92c = entry
EDGE_MIN = 0.02  # 2% minimum edge — even small discounts compound over 96 markets/day
MAX_ENTRY_PRICE_CENTS = 99  # Edge comes from settlement — even 1¢/contract is profit at scale
FEE_CENTS_PER_CONTRACT = 0

# -------------- TIME-DEPENDENT CERTAINTY (within the 5-min buy window) --------
# Buy window is 5min → 15s before close. Require more certainty at the start
# of the buy window (BTC still has time to move), relax near the end.
# NOTE: observation phase (12min → 5min) gathers data but never buys.
PROB_EARLY_ENTRY_SECONDS = 240   # 4-5 min to close = "early" part of buy window
PROB_EARLY_MIN = 0.92            # >4min: need 92%+ (BTC still has time to move)
PROB_MID_ENTRY_SECONDS = 120     # 2-4 min to close = "mid"
PROB_MID_MIN = 0.88              # 2-4min: need 88%+
# <2 min = PROB_MIN (0.85) — EV cap protects against overpaying

# -------------- PROBABILITY TREND DETECTION (confirm borderline trades) --------
# When prob is borderline (80-89%), require momentum confirmation.
# When prob is high (90%+), the outcome speaks for itself — skip trend checks.
PROB_TREND_WINDOW_SECONDS = 90    # Look at last 90 seconds of probability
PROB_TREND_MIN_SAMPLES = 8        # Need at least 8 samples (~80s at 1/sec)
PROB_TREND_THRESHOLD = 0.08       # 8% swing in one direction = trend signal
PROB_TREND_MIN_CURRENT = 0.88     # Current prob must be ≥88% — raised from 80%
PROB_TREND_ENTRY_ENABLED = True   # Enable trend-based entries (for borderline trades)
REQUIRE_TREND_ALIGNMENT = True    # Prob trend must match BTC spot trend (borderline only)
# HIGH-CERTAINTY FAST LANE: if prob is this high, skip trend/momentum checks entirely
# Rationale: 90% prob means BTC is well inside the range. You don't need momentum
# confirmation when the outcome is already near-certain. Just buy and hold.
PROB_FAST_LANE_THRESHOLD = 0.90   # ≥90% prob = buy immediately, no trend check needed

SPOT_SIGMA_USD_PER_SQRT_SEC = 12.0

# -------------- CONTRACT-COUNT SCALING (HARDWIRED) --------------
# Philosophy: size by CONTRACT COUNT, not percentage.
# Start with BASE_CONTRACTS. Each win adds 1 contract. Each loss resets to base.
# Over 96 markets/day this compounds: win 10 in a row = 10 extra contracts.
# Simple, predictable, no bankroll-fraction math needed.
BASE_CONTRACTS = 3          # Start each session buying 3 contracts
CONTRACT_INCREMENT = 1      # Add 1 contract per consecutive win
MAX_CONTRACTS = 25          # Hard cap — absolute max per order, any code path
MIN_CONTRACTS = 1           # Floor
MIN_FREE_USD_TO_TRADE = 5.0
# Legacy fraction-based sizing (kept for A+ trade logic and safety checks)
BANKROLL_FRACTION = 0.40
SCALING_MIN_FRACTION = 0.15
SCALING_MAX_FRACTION = 0.50

# -------------- SESSION LOSS LIMITS (HARDWIRED) --------------
ENABLE_SESSION_LIMITS = True
DAILY_MAX_LOSS_PERCENT = 0.75  # HARD STOP: never lose more than 75% of starting balance
SESSION_CONSECUTIVE_LOSSES_LIMIT = 5  # Pause after 5 consecutive losses in one market
SESSION_COOLDOWN_MINUTES = 15  # Cooldown after consecutive loss limit hit
BALANCE_CHECK_DELAY_SECONDS = 300  # Wait 5 min after settlement to fetch true balance

ONE_TRADE_PER_MARKET = env_bool("ONE_TRADE_PER_MARKET", True)
CANCEL_ALL_STRAYS_ALWAYS = env_bool("CANCEL_ALL_STRAYS_ALWAYS", True)

LOG_DECISIONS = env_bool("LOG_DECISIONS", True)
LOG_STATE_EVERY_SECONDS = env_float("LOG_STATE_EVERY_SECONDS", 10.0)

JOIN_UP_CENTS = env_int("JOIN_UP_CENTS", 0)
OB_WARN_EVERY_SECONDS = env_float("OB_WARN_EVERY_SECONDS", 2.0)

# -------------- BAIL CONFIGURATION (LAST RESORT — salvage only when truly cooked) ----
ENABLE_DUMP = True
# Philosophy: we entered with high conviction and hold to close. Bail ONLY if
# BTC has actually moved against us AND the book confirms it. A book spike
# while BTC is $150 on our side is NOT a reason to bail.
DUMP_PROB_FLIP = 0.50  # Floor: if prob hits coin-flip AND BTC confirms, bail
DUMP_PROB_DROP_PERCENT = 1.0  # Disabled
DUMP_MARKET_FLIP_THRESHOLD = 0.50  # Floor
DUMP_MIN_TIME_REMAINING = 8   # Can bail until 8s before settlement (was 15s — more time to dump)
DUMP_ON_PRICE_DANGER = False  # Disabled - trust BTC price, not book noise

# -------------- BTC-AWARE BAIL (the key fix: don't dump winners) ---------------
# Before ANY bail trigger fires, check: is BTC on our side of the boundary?
# YES side: spot > lo + buffer → BTC is safely above range floor → HOLD
# NO side:  spot < hi - buffer → BTC is safely below range ceiling → HOLD
# If BTC is on our side, the book is lying (thin book, spike, manipulation).
# ONLY bail if BTC has actually crossed or is dangerously close to boundary.
DUMP_BTC_SAFE_BUFFER_EARLY = 125.0   # >2min to close: need $125 buffer to suppress bail (raised from $75)
DUMP_BTC_SAFE_BUFFER_LATE = 75.0     # <2min to close: need $75 buffer (raised from $30 — BTC moves $30-50 routinely)
DUMP_BTC_SAFE_CUTOFF_SECONDS = 120   # Boundary between early/late buffer

# -------------- REVERSAL BAIL (only after BTC check fails) --------------------
DUMP_ON_PROB_REVERSAL = True   # Still enabled as safety net
DUMP_REVERSAL_THRESHOLD = 0.08  # 8% drop from peak — bail fast (was 12%, still too slow)
DUMP_REVERSAL_THRESHOLD_PROFIT = 0.06  # 6% when profitable — protect gains (was 10%)
DUMP_PROFIT_TIGHTEN_ABOVE_ENTRY = 0.05  # Tighten after 5%+ gain (was 8%)
DUMP_REVERSAL_MIN_SAMPLES = 5
DUMP_EARLY_EXIT_ENABLED = True

# -------------- BANKROLL-PROPORTIONAL LOSS CAP (scales with your balance) -----
# Never lose more than X% of current balance on a single trade.
# At $35: max loss = $1.75.  At $350: max loss = $17.50.  Scales naturally.
# This fires BEFORE the fixed catastrophic stop and replaces it as the primary cap.
DUMP_MAX_LOSS_FRACTION_OF_BALANCE = 0.05  # 5% of current balance = max single-trade loss (dump-side)
# Also cap at 50% of position cost — if you paid $3, max loss is $1.50
DUMP_MAX_LOSS_FRACTION_OF_POSITION = 0.50  # Never lose more than 50% of what you put in
# ENTRY-SIDE cap: worst case = settlement loss = full entry cost.
# With the EV price cap (price ≤ prob), entries are always +EV, so we can
# afford to size up.  30% of $35 = $10.50 → 10 contracts at 97c.
# As bankroll grows to $350: $105 → 100+ contracts.
MAX_SETTLEMENT_LOSS_FRACTION = 0.30  # Max 30% of balance at risk per trade — this IS the compounding engine

# -------------- BAIL TIMING (hold to close — but bail fast when it's wrong) ----
DUMP_GRACE_PERIOD_SECONDS = 15      # 15s grace period (was 30s — too slow)
DUMP_PROACTIVE_AFTER_SECONDS = 60   # Proactive bail after 1 min (was 2 min — let bankroll cap handle early bail)

# -------------- HARD P&L STOP (last-resort backstop) -------------------------
DUMP_MAX_LOSS_CENTS_PER_CONTRACT = 15  # Hard stop (fires after BTC check)
# CATASTROPHIC STOP: fires BEFORE BTC check — absolute max loss regardless of anything
# Prevents a $2.65 loss when the hard stop is supposed to cap at 15¢/contract
DUMP_CATASTROPHIC_LOSS_CENTS = 30      # If losing >30¢/contract, bail no matter what

# -------------- FLIP AFTER DUMP (double-dip: dump losing side, buy winning side) ----
# If we bail because BTC moved against us, the OTHER side is now the high-prob winner.
# Instead of just eating the loss, flip to the other side and hold THAT to settlement.
# Example: bought YES at 94¢, BTC tanks, dump YES at 40¢ (lose 54¢), buy NO at 60¢,
#          NO settles at $1 → +40¢. Net loss 14¢ instead of 54¢.
# Safety: the flip still checks probability and price, but with a LOWER bar
#         than a fresh entry — this is a recovery play, not a new trade.
#         We already took the loss; the question is "can I claw some back?"
FLIP_AFTER_DUMP = True              # Enable flip-to-other-side after bail
FLIP_MIN_TIME_REMAINING = 15        # Just need time to place the order and settle
FLIP_MIN_PROB = 0.60                # Lower bar: 60% on other side is enough for recovery
FLIP_MAX_ENTRY_PRICE = 99           # Edge = settlement payout, even 1¢/contract at scale

# -------------- LAST-MINUTE SCALP (compound on near-certain outcomes) -----------
# With <60s left and BTC far from the strike, the outcome is locked.
# Buy a boatload of contracts at 98-99¢ and collect 1-2¢/contract at settlement.
# Key safety: distance from strike.  If BTC is $300 above the floor with 60s left,
# it CANNOT reverse.  sigma * sqrt(60) ≈ $93 at 12σ — $300 is >3x the max move.
#
# Risk/reward at 99¢ × 33 contracts:
#   Win (99.5%+ of the time): +$0.33
#   Lose (BTC reverses $300+ in 60s): -$32.67
# Over 96 markets/day: ~$31/day extra income if hit rate matches.
SCALP_ENABLED = True
SCALP_MAX_SECONDS = 60             # Only scalp in the last 60 seconds
SCALP_MIN_SECONDS = 5              # Don't scalp in the last 5s (order might not fill)
SCALP_MIN_DISTANCE_USD = 50.0      # BTC must be ≥$50 from strike (lowered — vol gate is the real safety)
# Distance tiers: farther from strike = more aggressive sizing
# Each tier: (min_distance_usd, bankroll_fraction)
SCALP_DISTANCE_TIERS = [
    (400.0, 0.70),   # $400+ from strike: extremely safe, go big
    (200.0, 0.50),   # $200-400: very safe
    (100.0, 0.30),   # $100-200: safe
    (50.0,  0.15),   # $50-100: moderate — compound the edge
]
SCALP_MAX_ENTRY_PRICE = 97        # Max 97¢ — ensures ≥3¢ profit/contract at settlement
SCALP_MIN_PROB = 0.80             # Low bar — distance + volatility gate is the real safety, not blend prob
SCALP_MAX_LOSS_FRACTION = 0.15    # Never risk more than 15% of cash on a scalp

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
BANKROLL_FRACTION_HARD_CAP = env_float("BANKROLL_FRACTION_HARD_CAP", 0.50)  # Allow up to 50%

EDGE_SIZE_START = env_float("EDGE_SIZE_START", 0.015)  # Start scaling earlier
EDGE_SIZE_SLOPE = env_float("EDGE_SIZE_SLOPE", 3.0)  # Steeper scaling

A_PLUS_PROB = env_float("A_PLUS_PROB", 0.90)  # A+ = extremely certain outcome
A_PLUS_EDGE = env_float("A_PLUS_EDGE", 0.03)  # A+ = even small edge at 90%+ prob is golden
A_PLUS_FRACTION = env_float("A_PLUS_FRACTION", 0.40)  # Go big — 90%+ prob is as sure as it gets

HIGH_CERTAINTY_PROB = env_float("HIGH_CERTAINTY_PROB", 0.95)  # Slightly lower
HIGH_CERTAINTY_TIME_SEC = env_int("HIGH_CERTAINTY_TIME_SEC", 15)
HIGH_CERTAINTY_MAX_PRICE = env_int("HIGH_CERTAINTY_MAX_PRICE", 99)

# SETTLEMENT LOCK: near expiry, model edge is unreliable because it blends a
# conservative BS estimate against market price.  With <2 min left the market
# price IS the probability.  If blend prob is high, buy even with thin/no edge.
SETTLEMENT_LOCK_SECONDS = env_int("SETTLEMENT_LOCK_SECONDS", 120)    # <2 min
SETTLEMENT_LOCK_MIN_PROB = env_float("SETTLEMENT_LOCK_MIN_PROB", 0.85)  # blend prob — EV cap (price ≤ prob) is the real protection
SETTLEMENT_LOCK_MAX_PRICE = env_int("SETTLEMENT_LOCK_MAX_PRICE", 99)   # edge = settlement

LAST_CHANCE_TIME_SEC = env_int("LAST_CHANCE_TIME_SEC", 20)
LAST_CHANCE_MIN_PROB = env_float("LAST_CHANCE_MIN_PROB", 0.85)

BOUNDARY_BUFFER_USD = env_float("BOUNDARY_BUFFER_USD", 50.0)  # $50 buffer — BTC-aware bail is the real safety net during hold
LATE_ENTRY_PROB_BOOST = env_float("LATE_ENTRY_PROB_BOOST", 0.0)  # No boost — EV price cap is the real protection
LATE_ENTRY_TIME_SEC = env_int("LATE_ENTRY_TIME_SEC", 15)

# -------------- TREND TRACKING (know what BTC is doing) --------------
TREND_WINDOW_MINUTES = 60          # Long-term trend: 60 min (~4 markets)
TREND_SHORT_WINDOW_MINUTES = 30    # Short-term trend: 30 min (~2 markets)
TREND_SAMPLE_INTERVAL_SECONDS = 30  # Record spot every 30s
TREND_STRONG_THRESHOLD = 100.0     # $100+ move in window = strong trend
TREND_MODERATE_THRESHOLD = 50.0    # $50+ move = moderate trend
TREND_AGAINST_EDGE_BOOST = 0.02    # Require 2% extra edge to trade against trend
TREND_AGAINST_BLOCK = False        # Don't hard-block — require extra edge instead (trade every market)
TREND_WITH_EDGE_DISCOUNT = 0.005   # Reduce required edge by 0.5% when trading with trend

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
    past = []
    for ot, ct, m in candidates:
        ticker = m.get("ticker") or m.get("market_ticker") or "?"
        if ot is not None and ct is not None and ot <= now_ts < ct:
            active.append((ct, m))
        elif ct is not None and ct > now_ts:
            future.append((ct, m))
        else:
            past.append((ct or 0, m))

    log.info(f"[PICK] candidates={len(candidates)} active={len(active)} future={len(future)} past={len(past)}")

    if active:
        active.sort(key=lambda x: x[0])
        chosen = active[0][1]
    elif future:
        future.sort(key=lambda x: x[0])
        chosen = future[0][1]
    elif past:
        # All markets are closed — pick the one that closed most recently
        # (closest to rolling into the next market)
        past.sort(key=lambda x: x[0], reverse=True)
        chosen = past[0][1]
        log.warning(f"[PICK] No active/future markets — using most recently closed")
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
    except Exception as e:
        log.warning(f"[BALANCE] API exception: {e}")
        return None, None
    if not isinstance(resp, dict):
        log.warning(f"[BALANCE] Response not dict: {type(resp)} = {resp}")
        return None, None

    # Kalshi API returns: {'balance': 2512, 'portfolio_value': 0, 'updated_ts': ...}
    # balance is in CENTS, need to convert to dollars
    balance_cents = resp.get("balance")
    portfolio_cents = resp.get("portfolio_value", 0)

    if balance_cents is not None:
        available_usd = float(balance_cents) / 100.0
        total_usd = float(balance_cents + portfolio_cents) / 100.0
        log.info(f"[BALANCE] {balance_cents}¢ available (${available_usd:.2f}), portfolio={portfolio_cents}¢")
        return available_usd, total_usd

    log.warning(f"[BALANCE] Could not find 'balance' in response: {resp}")
    return None, None


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
    """Track daily P&L from real balance, reset per-market stats on each roll"""
    # Daily tracking (persists across markets, only resets on bot restart)
    starting_balance_usd: float = 0.0  # Balance when bot started
    current_balance_usd: float = 0.0   # Last known real balance from API
    daily_pnl_usd: float = 0.0         # True P&L = current_balance - starting_balance
    total_markets: int = 0
    total_wins: int = 0
    total_losses: int = 0

    # Per-market tracking (resets on each market roll)
    market_wins: int = 0
    market_losses: int = 0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    current_fraction: float = BANKROLL_FRACTION  # Legacy, kept for safety checks
    # Contract-count scaling: the core sizing model
    current_contracts: int = BASE_CONTRACTS

    # Session limits
    is_paused: bool = False
    pause_until: float = 0.0
    pause_reason: Optional[str] = None
    is_daily_stopped: bool = False  # HARD STOP - never unpauses

    # Balance refresh
    pending_balance_check_at: float = 0.0  # When to fetch balance after settlement

    # Trade history
    recent_trades: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.recent_trades is None:
            self.recent_trades = []

    def reset_for_new_market(self):
        """Reset per-market state on each market roll. Daily state persists.
        NOTE: consecutive_wins and current_contracts PERSIST across markets —
        that's the whole point of contract-count scaling over 96 markets/day."""
        self.market_wins = 0
        self.market_losses = 0
        # consecutive_wins/losses intentionally NOT reset — they span markets
        # current_contracts intentionally NOT reset — grows with streak
        # Clear per-market pause (but NOT daily hard stop)
        if not self.is_daily_stopped:
            self.is_paused = False
            self.pause_reason = None
        log.warning(
            f"[SESSION] Market reset: contracts={self.current_contracts} streak={self.consecutive_wins}W "
            f"daily_pnl=${self.daily_pnl_usd:.2f} W/L={self.total_wins}/{self.total_losses}"
        )

    def update_balance(self, balance_usd: float):
        """Update true P&L from actual account balance"""
        self.current_balance_usd = balance_usd
        self.daily_pnl_usd = balance_usd - self.starting_balance_usd
        log.warning(
            f"[SESSION] Balance update: ${balance_usd:.2f} "
            f"(started=${self.starting_balance_usd:.2f}, daily_pnl=${self.daily_pnl_usd:.2f})"
        )
        # Check daily hard stop
        self._check_daily_stop()

    def schedule_balance_check(self):
        """Schedule a balance check after settlement"""
        self.pending_balance_check_at = time.time() + BALANCE_CHECK_DELAY_SECONDS
        log.info(f"[SESSION] Balance check scheduled in {BALANCE_CHECK_DELAY_SECONDS}s")

    def needs_balance_check(self) -> bool:
        """Check if it's time to fetch balance"""
        return self.pending_balance_check_at > 0 and time.time() >= self.pending_balance_check_at

    def clear_balance_check(self):
        self.pending_balance_check_at = 0.0

    def record_trade(self, market: str, side: str, entry_price: int, exit_price: Optional[int],
                     qty: int, pnl_cents: int, was_dump: bool = False):
        """Record a completed trade"""
        pnl_usd = pnl_cents / 100.0
        self.daily_pnl_usd += pnl_usd  # Immediate P&L update (balance check will correct later)
        self.current_balance_usd += pnl_usd  # Estimate balance until real check
        self.total_markets += 1

        trade = {
            "market": market, "side": side, "entry": entry_price,
            "exit": exit_price, "qty": qty, "pnl_cents": pnl_cents,
            "pnl_usd": pnl_usd, "was_dump": was_dump, "ts": time.time(),
        }
        self.recent_trades.append(trade)
        if len(self.recent_trades) > 50:
            self.recent_trades = self.recent_trades[-50:]

        if pnl_cents > 0:
            self.total_wins += 1
            self.market_wins += 1
            self.consecutive_wins += 1
            self.consecutive_losses = 0
            self._scale_up()
        else:
            self.total_losses += 1
            self.market_losses += 1
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            self._scale_down()

        # Check consecutive loss limit (per-market)
        self._check_consecutive_limit()

        # Schedule balance check to get true P&L
        self.schedule_balance_check()

        log.warning(
            f"[SESSION] Trade recorded: pnl=${pnl_usd:.2f} daily_pnl=${self.daily_pnl_usd:.2f} "
            f"W/L={self.total_wins}/{self.total_losses} "
            f"streak={self.consecutive_wins}W/{self.consecutive_losses}L "
            f"next_contracts={self.current_contracts}"
        )

    def _scale_up(self):
        """Win: add 1 contract. Simple compounding over 96 markets/day."""
        self.current_contracts = min(self.current_contracts + CONTRACT_INCREMENT, MAX_CONTRACTS)

    def _scale_down(self):
        """Loss: step down by 1 contract (floor at base).

        Old behavior: full reset to BASE_CONTRACTS on every loss.
        Problem: going 26-7 overnight kept contracts at 3-4 because
        scattered losses kept resetting the count.

        New behavior: lose 1 contract per loss. A 26-7 record means
        26 - 7 = 19 net wins → contracts = BASE + 19 = 22.
        This lets the compounding engine actually work over 96 markets/day.
        """
        self.current_contracts = max(BASE_CONTRACTS, self.current_contracts - CONTRACT_INCREMENT)

    def _check_daily_stop(self):
        """HARD STOP: never lose more than 75% of starting balance"""
        if not ENABLE_SESSION_LIMITS or self.starting_balance_usd <= 0:
            return
        loss_pct = -self.daily_pnl_usd / self.starting_balance_usd
        if loss_pct >= DAILY_MAX_LOSS_PERCENT:
            self.is_daily_stopped = True
            self.is_paused = True
            self.pause_reason = f"DAILY_HARD_STOP_lost_{loss_pct:.0%}"
            log.warning(
                f"[SESSION] DAILY HARD STOP: lost {loss_pct:.0%} of starting balance "
                f"(${self.starting_balance_usd:.2f} -> ${self.current_balance_usd:.2f}). "
                f"Bot will NOT trade until restart."
            )

    def _check_consecutive_limit(self):
        """Pause on consecutive losses within a market (temporary cooldown)"""
        if not ENABLE_SESSION_LIMITS:
            return
        if self.consecutive_losses >= SESSION_CONSECUTIVE_LOSSES_LIMIT:
            self.is_paused = True
            self.pause_until = time.time() + (SESSION_COOLDOWN_MINUTES * 60)
            self.pause_reason = f"consecutive_losses_{self.consecutive_losses}"
            self.current_contracts = BASE_CONTRACTS  # Reset to base on consecutive loss pause
            log.warning(f"[SESSION] PAUSED: {self.consecutive_losses} consecutive losses - cooldown {SESSION_COOLDOWN_MINUTES}min")

    def check_can_trade(self) -> Tuple[bool, Optional[str]]:
        """Check if we can trade"""
        # Daily hard stop is permanent until restart
        if self.is_daily_stopped:
            return False, f"DAILY_HARD_STOP (lost 75%+ of starting balance)"

        if not self.is_paused:
            return True, None

        if self.pause_until > 0 and time.time() >= self.pause_until:
            self.is_paused = False
            self.pause_reason = None
            self.consecutive_losses = 0
            log.warning("[SESSION] Cooldown ended, resuming trading")
            return True, None

        remaining = int(self.pause_until - time.time()) if self.pause_until > 0 else 0
        return False, f"paused:{self.pause_reason} ({remaining}s remaining)"

    def get_current_fraction(self) -> float:
        """Get the current bankroll fraction to use (legacy, for safety checks)"""
        return self.current_fraction

    def get_current_contracts(self) -> int:
        """Get how many contracts to buy this market."""
        return self.current_contracts


class SpotTrend:
    """
    Tracks BTC spot price over a rolling window to detect trends.
    Helps the bot avoid trading against strong momentum.
    """
    def __init__(self, window_minutes: int = TREND_WINDOW_MINUTES,
                 sample_interval: int = TREND_SAMPLE_INTERVAL_SECONDS):
        self.window_seconds = window_minutes * 60
        self.sample_interval = sample_interval
        self.samples: List[Tuple[float, float]] = []  # (timestamp, spot_price)
        self.last_sample_time: float = 0.0

    def record(self, spot: float) -> None:
        """Record a spot price sample (rate-limited by sample_interval)"""
        now = time.time()
        if (now - self.last_sample_time) < self.sample_interval:
            return
        self.samples.append((now, spot))
        self.last_sample_time = now
        # Prune old samples outside the window
        cutoff = now - self.window_seconds
        self.samples = [(t, p) for t, p in self.samples if t >= cutoff]

    def get_trend(self) -> Tuple[float, str, int]:
        """
        Returns: (move_usd, direction, num_samples)
        - move_usd: price change from oldest to newest sample (positive = up)
        - direction: "up", "down", or "flat"
        - num_samples: how many data points we have
        """
        if len(self.samples) < 2:
            return 0.0, "flat", len(self.samples)

        oldest_price = self.samples[0][1]
        newest_price = self.samples[-1][1]
        move = newest_price - oldest_price

        if abs(move) >= TREND_STRONG_THRESHOLD:
            direction = "strong_up" if move > 0 else "strong_down"
        elif abs(move) >= TREND_MODERATE_THRESHOLD:
            direction = "up" if move > 0 else "down"
        else:
            direction = "flat"

        return move, direction, len(self.samples)

    def get_window_minutes(self) -> float:
        """How many minutes of data we actually have"""
        if len(self.samples) < 2:
            return 0.0
        return (self.samples[-1][0] - self.samples[0][0]) / 60.0

    def trade_alignment(self, side: str, lo: Optional[float], hi: Optional[float],
                        spot: float) -> str:
        """
        Check if a proposed trade aligns with the trend.
        Returns: "with", "against", or "neutral"

        Logic:
        - YES bet = we think BTC will stay ABOVE lo (or in range)
        - NO bet = we think BTC will stay BELOW hi (or in range)
        - If BTC is trending UP strongly and we want NO → against trend
        - If BTC is trending DOWN strongly and we want YES → against trend
        """
        move, direction, _ = self.get_trend()

        if direction == "flat":
            return "neutral"

        trending_up = "up" in direction
        trending_down = "down" in direction

        if side == "yes" and trending_down:
            return "against"
        if side == "no" and trending_up:
            return "against"
        if side == "yes" and trending_up:
            return "with"
        if side == "no" and trending_down:
            return "with"

        return "neutral"

    def summary(self) -> str:
        """Short string for logging"""
        move, direction, n = self.get_trend()
        mins = self.get_window_minutes()
        if n < 2:
            return "trend=N/A(warming)"
        return f"trend={direction}(${move:+.0f}/{mins:.0f}min/{n}pts)"


class ProbTrend:
    """
    Tracks probability trend within a single market to detect momentum.
    Enables buying when probability is steadily climbing one direction,
    even if it hasn't hit 90% yet — catch the move early while prices are good.
    """
    def __init__(self):
        self.samples: List[Tuple[float, float]] = []  # (timestamp, p_yes_blend)
        self.market_ticker: Optional[str] = None

    def reset(self, market_ticker: str) -> None:
        """Reset for a new market"""
        self.samples = []
        self.market_ticker = market_ticker

    def record(self, p_yes_blend: float) -> None:
        """Record a probability sample (every poll)"""
        now = time.time()
        self.samples.append((now, p_yes_blend))
        # Prune old samples outside the window
        cutoff = now - PROB_TREND_WINDOW_SECONDS
        self.samples = [(t, p) for t, p in self.samples if t >= cutoff]

    def get_trend(self) -> Tuple[float, str, int, float]:
        """
        Returns: (prob_change, direction, num_samples, seconds_of_data)
        - prob_change: how much p_yes has moved (positive = trending YES)
        - direction: "strong_yes", "yes", "strong_no", "no", or "flat"
        - num_samples: how many data points
        - seconds_of_data: time span covered
        """
        if len(self.samples) < PROB_TREND_MIN_SAMPLES:
            return 0.0, "flat", len(self.samples), 0.0

        oldest_p = self.samples[0][1]
        newest_p = self.samples[-1][1]
        change = newest_p - oldest_p
        seconds = self.samples[-1][0] - self.samples[0][0]

        if change >= PROB_TREND_THRESHOLD:
            direction = "strong_yes"
        elif change >= PROB_TREND_THRESHOLD / 2:
            direction = "yes"
        elif change <= -PROB_TREND_THRESHOLD:
            direction = "strong_no"
        elif change <= -PROB_TREND_THRESHOLD / 2:
            direction = "no"
        else:
            direction = "flat"

        return change, direction, len(self.samples), seconds

    def should_buy(self, side: str, current_prob: float, secs_to_close: int = 0) -> Tuple[bool, str]:
        """
        Check if the probability trend supports buying this side.
        Returns: (should_buy, reason)

        Time-dependent strictness (more certain early, relax as outcome clarifies):
          EARLY (4-5 min): Require active trend match — BTC still has time to swing.
          MID   (2-4 min): Allow flat trend — stable high prob is enough certainty.
          LATE  (<2 min):  Skip trend entirely — probability IS the outcome now.
        Always: Trend matching our side = GO. Trend against us = BLOCK (unless late).
        """
        if not PROB_TREND_ENTRY_ENABLED:
            return False, "trend_entry_disabled"

        # LATE window (<2 min): probability speaks for itself, skip trend check
        if secs_to_close <= PROB_MID_ENTRY_SECONDS:
            return True, f"late_window({secs_to_close}s, prob={current_prob:.0%})"

        change, direction, n_samples, seconds = self.get_trend()

        if seconds < 60:
            return False, f"need_more_data({seconds:.0f}s)"

        # Must have minimum current probability
        if current_prob < PROB_TREND_MIN_CURRENT:
            return False, f"prob_too_low({current_prob:.2f})"

        # Trend matches our side → GO at any time
        if side == "yes" and "yes" in direction:
            return True, f"trend_yes({change:+.2f}/{seconds:.0f}s)"
        if side == "no" and "no" in direction:
            return True, f"trend_no({change:+.2f}/{seconds:.0f}s)"

        # MID window (2-4 min): flat trend is acceptable — stable certainty
        if secs_to_close <= PROB_EARLY_ENTRY_SECONDS and direction == "flat":
            return True, f"mid_flat({current_prob:.0%}/{seconds:.0f}s, {secs_to_close}s left)"

        # EARLY window (4-5 min): flat is NOT enough, need active trend match
        if direction == "flat":
            return False, f"early_need_trend({current_prob:.0%}, flat/{secs_to_close}s left)"

        # Trend is AGAINST us → BLOCK
        return False, f"trend_against({direction}/{change:+.2f})"

    def summary(self) -> str:
        """Short string for logging"""
        change, direction, n, seconds = self.get_trend()
        if n < PROB_TREND_MIN_SAMPLES:
            return f"prob_trend=warming({n}/{PROB_TREND_MIN_SAMPLES})"
        return f"prob_trend={direction}({change:+.0%}/{seconds:.0f}s/{n}pts)"


@dataclass
class BotState:
    sm: str = SM.IDLE
    market: Optional[str] = None
    event: Optional[str] = None

    traded_this_market: bool = False
    has_flipped: bool = False  # True after one flip — prevent infinite flip-flop
    has_scalped: bool = False  # True after last-minute scalp — one scalp per market
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

    # Peak probability tracking (for proactive dump)
    peak_prob_for_side: float = 0.0  # Highest prob we've seen for our side since entry

    # Deferred settlement: when we can't get result at roll time, check later
    pending_settlement_market: Optional[str] = None
    pending_settlement_side: Optional[str] = None
    pending_settlement_entry_price: Optional[int] = None
    pending_settlement_qty: int = 0
    pending_settlement_was_flip: bool = False
    pending_settlement_ts: float = 0.0  # When we started waiting

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
        # Gradual transition from model-weighted to market-weighted as time runs out:
        #   >120s: 75% model / 25% market (BTC still has time to move)
        #   60-120s: linear ramp from 75% model down to 0% (outcome becoming clear)
        #   <60s: 100% market (book IS the probability)
        if secs_to_close <= 60:
            alpha = 0.0   # 100% market — book IS the probability in the last minute
        elif secs_to_close <= 120:
            # Linear ramp: at 120s alpha=MODEL_BLEND_ALPHA, at 60s alpha=0
            alpha = float(MODEL_BLEND_ALPHA) * (secs_to_close - 60) / 60.0
        else:
            alpha = float(MODEL_BLEND_ALPHA)  # 75% model, 25% market
        p_yes_blend = alpha * p_yes_model + (1.0 - alpha) * float(p_mkt)
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

    # TIME-DEPENDENT PROBABILITY GATE: earlier = need more certainty
    if secs_to_close > PROB_EARLY_ENTRY_SECONDS:
        effective_prob_min = PROB_EARLY_MIN   # >4min: need 92%+
    elif secs_to_close > PROB_MID_ENTRY_SECONDS:
        effective_prob_min = PROB_MID_MIN     # 2-4min: need 88%+
    else:
        effective_prob_min = PROB_MIN         # <2min: 92%+ — market-weighted blend, EV cap ensures price ≤ prob
        
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

    # High-certainty override (last 15 seconds)
    if secs_to_close < HIGH_CERTAINTY_TIME_SEC:
        yes_boundary_ok = (lo is None) or (spot >= lo + BOUNDARY_BUFFER_USD)
        no_boundary_ok = (hi is None) or (spot <= hi - BOUNDARY_BUFFER_USD)

        max_yes_hc = int(p_yes_blend * 100) + 1  # +1¢ spread slack
        max_no_hc = int(p_no_blend * 100) + 1
        if p_yes_blend >= HIGH_CERTAINTY_PROB and yes_px is not None and yes_px <= max_yes_hc and yes_boundary_ok:
            ok_yes = True
            log.info(f"[OVERRIDE] YES high-certainty (p={p_yes_blend:.4f}, price={yes_px}, max={max_yes_hc})")
        if p_no_blend >= HIGH_CERTAINTY_PROB and no_px is not None and no_px <= max_no_hc and no_boundary_ok:
            ok_no = True
            log.info(f"[OVERRIDE] NO high-certainty (p={p_no_blend:.4f}, price={no_px}, max={max_no_hc})")

    # SETTLEMENT LOCK: <2 min to close, model edge is unreliable.
    # Market has priced in the near-certain outcome, so our conservative blend
    # shows negative edge even when the trade is good.  Skip edge requirement
    # AND the tight model-based price cap.  The price cap should just be the
    # max entry price — the prob gate (85%) and boundary check are the real safety.
    # Settlement payout IS the edge.
    if secs_to_close <= SETTLEMENT_LOCK_SECONDS:
        yes_boundary_ok = (lo is None) or (spot >= lo + BOUNDARY_BUFFER_USD)
        no_boundary_ok = (hi is None) or (spot <= hi - BOUNDARY_BUFFER_USD)

        # Use max entry price, not model-blended cap.  The model is too conservative
        # near settlement and blocks fair-value entries at 97-99c.
        max_settle_px = SETTLEMENT_LOCK_MAX_PRICE
        if not ok_yes and p_yes_blend >= SETTLEMENT_LOCK_MIN_PROB and yes_px is not None and yes_px <= max_settle_px and yes_boundary_ok:
            ok_yes = True
            log.warning(
                f"[SETTLE LOCK] YES override: blend={p_yes_blend:.1%} price={yes_px}¢ "
                f"max={max_settle_px}¢ edge={edge_yes:.4f} t={secs_to_close}s"
            )
        if not ok_no and p_no_blend >= SETTLEMENT_LOCK_MIN_PROB and no_px is not None and no_px <= max_settle_px and no_boundary_ok:
            ok_no = True
            log.warning(
                f"[SETTLE LOCK] NO override: blend={p_no_blend:.1%} price={no_px}¢ "
                f"max={max_settle_px}¢ edge={edge_no:.4f} t={secs_to_close}s"
            )

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


# PROACTIVE DUMP: Get best exit price, don't wait until it's too late
def _btc_is_safe(side: str, spot: float, lo: Optional[float], hi: Optional[float],
                  secs_to_close: int) -> Tuple[bool, float]:
    """
    Check if BTC spot price is safely on our side of the market boundary.
    Returns (is_safe, buffer_distance_usd).

    This is THE key check: if BTC is on our side, the book is lying.
    Don't bail on a winner just because the orderbook spiked for 3 seconds.

    For "Up or Down" markets (lo only, no hi):
      YES wins if spot > lo  →  distance = spot - lo
      NO  wins if spot < lo  →  distance = lo - spot
    For range markets (both lo and hi):
      YES wins if lo < spot < hi
      NO  wins if spot outside range
    """
    buffer = DUMP_BTC_SAFE_BUFFER_LATE if secs_to_close < DUMP_BTC_SAFE_CUTOFF_SECONDS else DUMP_BTC_SAFE_BUFFER_EARLY

    if side == "yes" and lo is not None:
        # YES wins if BTC stays ABOVE lo. Safe if spot > lo + buffer.
        distance = spot - lo
        return distance >= buffer, distance
    elif side == "no" and hi is not None:
        # NO wins if BTC stays BELOW hi (range market). Safe if spot < hi - buffer.
        distance = hi - spot
        return distance >= buffer, distance
    elif side == "no" and lo is not None:
        # NO wins if BTC drops BELOW lo (up-or-down market, no hi).
        # Safe if spot < lo - buffer (BTC is well below the strike).
        distance = lo - spot
        return distance >= buffer, distance

    return False, 0.0  # Can't determine — not safe


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
    trend: Optional['SpotTrend'] = None,
    current_balance_usd: float = 0.0,
) -> Tuple[bool, Optional[str]]:
    """
    BAIL logic: last resort only. Hold to close is the goal.

    KEY PRINCIPLE: Before any bail trigger fires, check if BTC is on our side
    of the boundary. If it is, the book is lying — HOLD. Only bail when
    BTC has actually moved against us.

    BANKROLL PROTECTION: Never lose more than 5% of balance or 50% of position
    cost on a single trade. This fires before BTC check — no position justifies
    blowing up the bankroll.

    Returns: (should_dump, reason)
    """
    if not ENABLE_DUMP:
        return False, None

    if secs_to_close < DUMP_MIN_TIME_REMAINING:
        return False, "too_close_to_settlement"

    # SETTLEMENT-LOCK ENTRIES: if we entered in the last 120s, the outcome is
    # already decided.  Just hold to settlement — don't let dump logic sell a
    # near-certain winner for 99c when settlement pays $1.
    if st.entry_time > 0:
        entry_secs_remaining = secs_to_close + (time.time() - st.entry_time)
        if entry_secs_remaining <= SETTLEMENT_LOCK_SECONDS:
            # Only bail on catastrophic loss (bankroll protection), not reversals
            if st.entry_price_cents is not None and st.qty > 0 and current_balance_usd > 0:
                exit_price_est = int((p_yes_blend if st.side == "yes" else p_no_blend) * 100)
                loss_per_contract = st.entry_price_cents - exit_price_est
                total_loss_usd = (loss_per_contract * st.qty) / 100.0
                max_loss_balance = current_balance_usd * DUMP_MAX_LOSS_FRACTION_OF_BALANCE
                if total_loss_usd > max_loss_balance:
                    return True, f"late_entry_bankroll_cap_${total_loss_usd:.2f}>${max_loss_balance:.2f}"
            return False, f"late_entry_hold_to_settle_t={secs_to_close}s"

    if st.entry_model_prob is None:
        return False, "no_entry_data"

    # --- GRACE PERIOD ---
    time_in_trade = time.time() - st.entry_time if st.entry_time > 0 else 9999
    if time_in_trade < DUMP_GRACE_PERIOD_SECONDS:
        return False, f"grace_period_{time_in_trade:.0f}s/{DUMP_GRACE_PERIOD_SECONDS}s"

    # Determine current probability for our side
    if st.side == "yes":
        current_prob = p_yes_blend
        entry_prob = st.entry_model_prob
    else:
        current_prob = p_no_blend
        entry_prob = 1.0 - st.entry_model_prob

    # Update peak probability tracking
    if current_prob > st.peak_prob_for_side:
        st.peak_prob_for_side = current_prob

    drop_from_peak = st.peak_prob_for_side - current_prob
    in_settling = time_in_trade < DUMP_PROACTIVE_AFTER_SECONDS

    # =============================================================
    # === BANKROLL-PROPORTIONAL STOP: fires BEFORE BTC check ===
    # Never lose more than 5% of balance or 50% of position cost.
    # This is THE primary loss cap. Scales with bankroll naturally:
    # $35 balance → max $1.75 loss.  $350 → max $17.50.
    # =============================================================
    if st.entry_price_cents is not None and st.qty > 0:
        exit_price_est = int(current_prob * 100)
        loss_per_contract = st.entry_price_cents - exit_price_est
        total_loss_cents = loss_per_contract * st.qty
        total_loss_usd = total_loss_cents / 100.0
        position_cost_usd = (st.entry_price_cents * st.qty) / 100.0

        # Cap 1: fraction of current balance
        if current_balance_usd > 0:
            max_loss_balance = current_balance_usd * DUMP_MAX_LOSS_FRACTION_OF_BALANCE
            if total_loss_usd > max_loss_balance:
                log.warning(
                    f"[BAIL BANKROLL CAP] losing ${total_loss_usd:.2f} > "
                    f"{DUMP_MAX_LOSS_FRACTION_OF_BALANCE:.0%} of ${current_balance_usd:.2f} "
                    f"(cap=${max_loss_balance:.2f}) — {loss_per_contract}¢/ct × {st.qty}ct "
                    f"(entry={st.entry_price_cents}¢ est_exit={exit_price_est}¢) — "
                    f"overrides BTC safety, protecting bankroll"
                )
                return True, f"bankroll_cap_${total_loss_usd:.2f}>${max_loss_balance:.2f}"

        # Cap 2: fraction of position cost (never lose more than 50% of what you put in)
        max_loss_position = position_cost_usd * DUMP_MAX_LOSS_FRACTION_OF_POSITION
        if total_loss_usd > max_loss_position:
            log.warning(
                f"[BAIL POSITION CAP] losing ${total_loss_usd:.2f} > "
                f"{DUMP_MAX_LOSS_FRACTION_OF_POSITION:.0%} of position cost "
                f"${position_cost_usd:.2f} (cap=${max_loss_position:.2f}) — "
                f"{loss_per_contract}¢/ct × {st.qty}ct "
                f"(entry={st.entry_price_cents}¢ est_exit={exit_price_est}¢)"
            )
            return True, f"position_cap_${total_loss_usd:.2f}>{DUMP_MAX_LOSS_FRACTION_OF_POSITION:.0%}"

    # =============================================================
    # === CATASTROPHIC STOP: absolute backstop, fires BEFORE BTC check ===
    # Even if bankroll cap didn't fire (e.g., balance unknown), this catches
    # extreme per-contract losses.
    # =============================================================
    if st.entry_price_cents is not None:
        exit_price_est = int(current_prob * 100)
        catastrophic_loss = st.entry_price_cents - exit_price_est
        if catastrophic_loss >= DUMP_CATASTROPHIC_LOSS_CENTS:
            log.warning(
                f"[BAIL CATASTROPHIC] losing ~{catastrophic_loss}¢/contract "
                f"(entry={st.entry_price_cents}¢ est_exit={exit_price_est}¢) — "
                f"overrides BTC safety, capping damage"
            )
            return True, f"catastrophic_{catastrophic_loss}c_per_contract"

    # =============================================================
    # === BTC SAFETY CHECK: THE MASTER OVERRIDE ===
    # If BTC is on our side of the boundary, DO NOT BAIL.
    # The book can spike, the probability can drop on a thin book,
    # but if BTC is $100+ on our side with 1 minute left, we WIN.
    # =============================================================
    btc_safe, btc_distance = _btc_is_safe(st.side, spot, lo, hi, secs_to_close)
    if btc_safe:
        # BTC is on our side — this is a winner. Hold no matter what the book says.
        phase = "settling" if in_settling else "active"
        return False, (
            f"btc_safe_{phase}_dist=${btc_distance:.0f}_"
            f"prob={current_prob:.0%}_peak={st.peak_prob_for_side:.0%}_drop={drop_from_peak:.0%}"
        )

    # === Below here: BTC is NOT safely on our side — bail checks apply ===

    # --- HARD P&L STOP: BTC is against us AND losing big ---
    if st.entry_price_cents is not None:
        exit_price_est = int(current_prob * 100)
        unrealized_loss_per_contract = st.entry_price_cents - exit_price_est
        if unrealized_loss_per_contract >= DUMP_MAX_LOSS_CENTS_PER_CONTRACT:
            log.warning(
                f"[BAIL HARD STOP] BTC NOT safe (dist=${btc_distance:.0f}) AND "
                f"losing ~{unrealized_loss_per_contract}¢/contract "
                f"(entry={st.entry_price_cents}¢ est_exit={exit_price_est}¢) — salvaging"
            )
            return True, f"hard_stop_{unrealized_loss_per_contract}c_per_contract"

    # --- REVERSAL BAIL: BTC is against us AND prob has dropped significantly ---
    if DUMP_ON_PROB_REVERSAL and DUMP_EARLY_EXIT_ENABLED and not in_settling:
        gain_above_entry = st.peak_prob_for_side - entry_prob
        if gain_above_entry >= DUMP_PROFIT_TIGHTEN_ABOVE_ENTRY:
            effective_threshold = DUMP_REVERSAL_THRESHOLD_PROFIT
        else:
            effective_threshold = DUMP_REVERSAL_THRESHOLD

        if drop_from_peak >= effective_threshold:
            log.warning(
                f"[BAIL REVERSAL] BTC NOT safe (dist=${btc_distance:.0f}) AND "
                f"prob dropped {drop_from_peak:.1%} from peak "
                f"({st.peak_prob_for_side:.1%} -> {current_prob:.1%}) — salvaging"
            )
            return True, f"reversal_{current_prob:.0%}_from_peak_{st.peak_prob_for_side:.0%}"

    # --- FLOOR: BTC is against us AND prob is at coin-flip ---
    if current_prob < DUMP_PROB_FLIP:
        log.warning(
            f"[BAIL FLOOR] BTC NOT safe (dist=${btc_distance:.0f}) AND "
            f"prob={current_prob:.1%} < {DUMP_PROB_FLIP:.0%} — salvaging"
        )
        return True, f"floor_{current_prob:.0%}<{DUMP_PROB_FLIP:.0%}"

    # Still holding
    phase = "settling" if in_settling else "active"
    return False, (
        f"{phase}_prob={current_prob:.0%}_peak={st.peak_prob_for_side:.0%}"
        f"_drop={drop_from_peak:.0%}_btc_dist=${btc_distance:.0f}"
    )


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
    """Compute order quantity using contract-count scaling.

    Primary model: session.current_contracts (base + 1 per win streak).
    Safety cap: never spend more than BANKROLL_FRACTION of available balance.
    """
    if entry_cents is None or entry_cents <= 0:
        log.warning(f"[SIZE] Invalid entry_cents={entry_cents}, falling back to BASE_CONTRACTS={BASE_CONTRACTS}")
        return clamp_int(BASE_CONTRACTS, MIN_CONTRACTS, MAX_CONTRACTS)

    if available_usd is None or available_usd <= 0:
        log.warning(f"[SIZE] available_usd is None or <=0, falling back to BASE_CONTRACTS={BASE_CONTRACTS}")
        return clamp_int(BASE_CONTRACTS, MIN_CONTRACTS, MAX_CONTRACTS)

    if available_usd < MIN_FREE_USD_TO_TRADE:
        log.warning(f"[SIZE] available_usd ${available_usd:.2f} < MIN_FREE_USD_TO_TRADE ${MIN_FREE_USD_TO_TRADE}, returning 0")
        return 0

    # Primary: contract count from session (base + streak bonus)
    target_qty = session.get_current_contracts() if session else BASE_CONTRACTS

    # HIGH-PRICE SCALING: when price is high, per-contract edge is thin.
    # Scale up contracts so absolute dollar profit stays meaningful.
    # At 95¢ (5¢ edge): 1×.  At 97¢ (3¢ edge): ~2×.  At 99¢ (1¢ edge): 5×.
    settlement_edge = 100 - entry_cents
    if settlement_edge > 0 and settlement_edge < 5:
        scale_factor = max(1, round(5.0 / settlement_edge))
        scaled_qty = target_qty * scale_factor
        log.info(
            f"[SIZE] High-price scaling: {entry_cents}¢ → {settlement_edge}¢ edge → "
            f"{scale_factor}× → {target_qty} → {scaled_qty} contracts"
        )
        target_qty = scaled_qty

    # Safety cap: don't spend more than we can afford
    cost_per = float(entry_cents) / 100.0
    if cost_per > 0:
        max_affordable = int(available_usd * BANKROLL_FRACTION / cost_per)
        if target_qty > max_affordable:
            log.info(f"[SIZE] Capping qty {target_qty} -> {max_affordable} (afford cap at {BANKROLL_FRACTION:.0%} of ${available_usd:.2f})")
            target_qty = max_affordable

    # SETTLEMENT LOSS CAP: worst case = lose entire entry cost at settlement.
    # Cap so that worst-case loss never exceeds MAX_SETTLEMENT_LOSS_FRACTION of balance.
    # This is looser than the dump-side cap (5%) because settlement losses are rare
    # (76% win rate) — but it prevents a single bad trade from doing $2-5 damage.
    # At $33 balance with 10%: max worst-case = $3.30. At 97c: 3 contracts max.
    # As bankroll grows to $330: max worst-case = $33. At 97c: 34 contracts.
    if cost_per > 0 and available_usd > 0:
        max_settlement_loss = available_usd * MAX_SETTLEMENT_LOSS_FRACTION
        max_qty_for_loss_cap = int(max_settlement_loss / cost_per)
        if max_qty_for_loss_cap < MIN_CONTRACTS:
            max_qty_for_loss_cap = MIN_CONTRACTS
        if target_qty > max_qty_for_loss_cap:
            log.info(
                f"[SIZE] Settlement loss cap: {target_qty} -> {max_qty_for_loss_cap} contracts "
                f"(max loss ${max_settlement_loss:.2f} = {MAX_SETTLEMENT_LOSS_FRACTION:.0%} of ${available_usd:.2f}, "
                f"entry={entry_cents}¢)"
            )
            target_qty = max_qty_for_loss_cap

    qty = clamp_int(target_qty, MIN_CONTRACTS, MAX_CONTRACTS)

    streak = session.consecutive_wins if session else 0
    log.info(f"[SIZE] contracts={qty} (base={BASE_CONTRACTS}+{streak}wins) cost={entry_cents}¢ avail=${available_usd:.2f}")

    return qty


def compute_scalp_qty(
    available_usd: float,
    entry_cents: int,
    distance_usd: float,
) -> int:
    """Compute scalp order quantity based on distance from strike.

    Farther from strike = safer = more contracts.
    Uses SCALP_DISTANCE_TIERS to determine bankroll fraction.
    """
    if available_usd <= 0 or entry_cents <= 0 or entry_cents > 99:
        return 0

    # Find the highest-qualifying distance tier (sorted descending)
    scalp_fraction = 0.0
    for min_dist, frac in SCALP_DISTANCE_TIERS:
        if distance_usd >= min_dist:
            scalp_fraction = frac
            break

    if scalp_fraction <= 0:
        return 0

    cost_per = float(entry_cents) / 100.0
    # How many contracts can we buy with this fraction of cash?
    target_qty = int(available_usd * scalp_fraction / cost_per)

    # Safety cap: worst-case loss (all contracts go to $0) must not exceed SCALP_MAX_LOSS_FRACTION
    max_loss_usd = available_usd * SCALP_MAX_LOSS_FRACTION
    max_qty_for_loss = int(max_loss_usd / cost_per)
    if target_qty > max_qty_for_loss:
        log.info(
            f"[SCALP SIZE] Loss cap: {target_qty} -> {max_qty_for_loss} contracts "
            f"(max loss ${max_loss_usd:.2f} = {SCALP_MAX_LOSS_FRACTION:.0%} of ${available_usd:.2f})"
        )
        target_qty = max_qty_for_loss

    target_qty = max(0, min(target_qty, MAX_CONTRACTS))

    if target_qty > 0:
        expected_profit = target_qty * (100 - entry_cents) / 100.0
        max_loss = target_qty * cost_per
        log.info(
            f"[SCALP SIZE] qty={target_qty} @ {entry_cents}¢ "
            f"(dist=${distance_usd:.0f}, frac={scalp_fraction:.0%}, "
            f"profit=${expected_profit:.2f}, risk=${max_loss:.2f})"
        )

    return target_qty


def evaluate_scalp(
    st: BotState,
    side: str,
    spot: float,
    lo: Optional[float],
    hi: Optional[float],
    secs_to_close: int,
    p_blend: float,
    ask_price: Optional[int],
    available_usd: float,
    sigma: float,
) -> Tuple[bool, int, Optional[int], str]:
    """Evaluate whether to place a last-minute scalp order.

    Returns: (should_scalp, qty, price_cents, reason)
    """
    if not SCALP_ENABLED:
        return False, 0, None, "disabled"

    if st.has_scalped:
        return False, 0, None, "already_scalped"

    if secs_to_close > SCALP_MAX_SECONDS or secs_to_close < SCALP_MIN_SECONDS:
        return False, 0, None, f"time={secs_to_close}s_outside_{SCALP_MIN_SECONDS}-{SCALP_MAX_SECONDS}s"

    if ask_price is None or ask_price > SCALP_MAX_ENTRY_PRICE:
        return False, 0, None, f"price={ask_price}¢_too_high(max={SCALP_MAX_ENTRY_PRICE})"

    if p_blend < SCALP_MIN_PROB:
        return False, 0, None, f"prob={p_blend:.1%}<{SCALP_MIN_PROB:.0%}"

    # EV cap: never pay more than the probability (same rule as main entry)
    max_ev_price = int(p_blend * 100) + 1  # +1¢ spread slack (midpoint is below ask by half-spread)
    if ask_price > max_ev_price:
        return False, 0, None, f"price={ask_price}¢>prob_cap={max_ev_price}¢(p={p_blend:.1%})"

    # Core safety: how far is BTC from the strike?
    # "Up or Down" markets have only lo (no hi). NO wins when spot < lo.
    if side == "yes" and lo is not None:
        distance = spot - lo
    elif side == "no" and hi is not None:
        distance = hi - spot
    elif side == "no" and lo is not None:
        distance = lo - spot
    else:
        return False, 0, None, "no_boundary"

    if distance < SCALP_MIN_DISTANCE_USD:
        return False, 0, None, f"dist=${distance:.0f}<${SCALP_MIN_DISTANCE_USD:.0f}"

    # Volatility sanity check: can BTC actually move `distance` in `secs_to_close`?
    # 1.5σ√t covers ~93% of moves (~3.5% adverse). Combined with distance tiers
    # and EV price cap, this gives the scalp enough room to actually fire.
    # At σ=12, t=60: $139.  t=30: $99.  t=15: $70.  t=10: $57.
    max_expected_move = 1.5 * sigma * math.sqrt(float(secs_to_close))
    if distance < max_expected_move:
        return False, 0, None, (
            f"vol_unsafe: dist=${distance:.0f} < 1.5σ√t=${max_expected_move:.0f} "
            f"(σ={sigma:.1f}, t={secs_to_close}s)"
        )

    if available_usd < MIN_FREE_USD_TO_TRADE:
        return False, 0, None, f"cash=${available_usd:.2f}<${MIN_FREE_USD_TO_TRADE}"

    qty = compute_scalp_qty(available_usd, ask_price, distance)
    if qty <= 0:
        return False, 0, None, "qty=0"

    reason = (
        f"dist=${distance:.0f} 3σ√t=${max_expected_move:.0f} "
        f"prob={p_blend:.1%} price={ask_price}¢ qty={qty}"
    )
    return True, qty, ask_price, reason


# -----------------------------
# Health check server for Render deploy
# Render needs an HTTP endpoint to confirm the service is alive.
# This runs in a background thread and doesn't affect the bot.
# -----------------------------
HEALTH_CHECK_PORT = int(os.environ.get("PORT", "10000"))

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, format, *args):
        pass  # Suppress HTTP logs — they clutter the bot output

def _start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", HEALTH_CHECK_PORT), _HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        log.warning(f"[HEALTH] Listening on port {HEALTH_CHECK_PORT}")
    except Exception as e:
        log.warning(f"[HEALTH] Could not start health server: {e} (non-fatal)")


# -----------------------------
# Main (VALUE SNIPER with session tracking)
# -----------------------------
def main() -> None:
    _start_health_server()
    log.warning(f"[ENV] Detected KALSHI_* keys: {env_keys_with_prefix('KALSHI_')}")
    log.warning(
        f"[BOOTCFG] SERIES={SERIES_TICKER} OBSERVE={OBSERVE_START_SECONDS}s BUY={BUY_START_SECONDS}s "
        f"PROB_MIN={PROB_MIN} EDGE_MIN={EDGE_MIN} MAX_ENTRY={MAX_ENTRY_PRICE_CENTS}¢ "
        f"BANKROLL_FRACTION={BANKROLL_FRACTION} ENABLE_DUMP={ENABLE_DUMP} "
        f"DUMP_PROB_FLIP={DUMP_PROB_FLIP} DUMP_PROB_DROP={DUMP_PROB_DROP_PERCENT}"
    )
    log.warning(
        f"[BOOTCFG] ENTRY: fast_lane={PROB_FAST_LANE_THRESHOLD:.0%} (≥{PROB_FAST_LANE_THRESHOLD:.0%} skips trend checks) "
        f"boundary_buffer=${BOUNDARY_BUFFER_USD:.0f} trend_block={TREND_AGAINST_BLOCK}"
    )
    log.warning(
        f"[BOOTCFG] BAIL: grace={DUMP_GRACE_PERIOD_SECONDS}s settling={DUMP_PROACTIVE_AFTER_SECONDS}s "
        f"reversal={DUMP_REVERSAL_THRESHOLD:.0%} hard_stop={DUMP_MAX_LOSS_CENTS_PER_CONTRACT}¢/contract "
        f"btc_buffer_early=${DUMP_BTC_SAFE_BUFFER_EARLY:.0f} btc_buffer_late=${DUMP_BTC_SAFE_BUFFER_LATE:.0f}"
    )
    log.warning(
        f"[BOOTCFG] FLIP: enabled={FLIP_AFTER_DUMP} min_time={FLIP_MIN_TIME_REMAINING}s "
        f"min_prob={FLIP_MIN_PROB:.0%} max_price={FLIP_MAX_ENTRY_PRICE}¢ (one flip per market)"
    )
    log.warning(
        f"[BOOTCFG] SIZING: base_contracts={BASE_CONTRACTS} increment={CONTRACT_INCREMENT}/win "
        f"max={MAX_CONTRACTS} (resets to base on loss)"
    )
    log.warning(
        f"[BOOTCFG] LIMITS: enabled={ENABLE_SESSION_LIMITS} daily_hard_stop={DAILY_MAX_LOSS_PERCENT:.0%} "
        f"consec_losses={SESSION_CONSECUTIVE_LOSSES_LIMIT} cooldown={SESSION_COOLDOWN_MINUTES}min "
        f"balance_check_delay={BALANCE_CHECK_DELAY_SECONDS}s"
    )
    log.warning(
        f"[BOOTCFG] TREND: windows={TREND_WINDOW_MINUTES}min+{TREND_SHORT_WINDOW_MINUTES}min "
        f"strong=${TREND_STRONG_THRESHOLD} moderate=${TREND_MODERATE_THRESHOLD} "
        f"block_against={TREND_AGAINST_BLOCK} edge_boost={TREND_AGAINST_EDGE_BOOST}"
    )
    log.warning(
        f"[BOOTCFG] SCALP: enabled={SCALP_ENABLED} window={SCALP_MIN_SECONDS}-{SCALP_MAX_SECONDS}s "
        f"min_dist=${SCALP_MIN_DISTANCE_USD:.0f} min_prob={SCALP_MIN_PROB:.0%} "
        f"max_loss={SCALP_MAX_LOSS_FRACTION:.0%} tiers={len(SCALP_DISTANCE_TIERS)}"
    )
    log.warning("[HEARTBEAT] main() entered — SCALPER is running")

    if not API_KEY_ID or not PRIVATE_KEY_PEM_B64:
        raise RuntimeError("Missing KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_PEM_BASE64")

    client = KalshiClient(API_BASE, API_PREFIX, API_KEY_ID, PRIVATE_KEY_PEM_B64)
    http = requests.Session()

    st = BotState()
    session = SessionState()
    trend = SpotTrend()  # 60-min long-term trend
    trend_short = SpotTrend(window_minutes=TREND_SHORT_WINDOW_MINUTES)  # 30-min short-term trend
    prob_trend = ProbTrend()
    active_market_obj: Dict[str, Any] = {}

    # Initialize session with starting balance
    try:
        av, tot = get_balance_usd(client)
        if av is not None:
            session.starting_balance_usd = av
            session.current_balance_usd = av
            session.daily_pnl_usd = 0.0
            log.warning(f"[SESSION] Starting balance: ${av:.2f} (75% hard stop at ${av * 0.25:.2f})")
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
        st.has_flipped = False
        st.has_scalped = False
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
                f"SESSION: pnl=${session.daily_pnl_usd:.2f} bal=${session.current_balance_usd:.2f} "
                f"W/L={session.total_wins}/{session.total_losses} contracts={session.current_contracts} "
                f"streak={session.consecutive_wins}W | {prob_trend.summary()}"
            )
            last_heartbeat = now

        # ============================================
        # DEFERRED SETTLEMENT: re-check if we have a pending result
        # Settlement can take 4-7 minutes.  Instead of guessing wrong at
        # roll time, we stash the trade info and poll the API here until
        # the result is known, then record the correct W/L.
        # ============================================
        if st.pending_settlement_market is not None:
            pending_age = now - st.pending_settlement_ts
            # Check every ~30s, give up after 10 minutes
            if pending_age > 600:
                log.warning(
                    f"[SETTLE] Giving up on {st.pending_settlement_market} after {pending_age:.0f}s — "
                    f"recording as unknown (no W/L impact)"
                )
                st.pending_settlement_market = None
            elif int(now) % 30 < POLL_SECONDS + 1:  # Roughly every 30s
                try:
                    pend_data = client.request("GET", f"/markets/{st.pending_settlement_market}")
                    pend_obj = pend_data.get("market", pend_data) if isinstance(pend_data, dict) else {}
                    pend_result = pend_obj.get("result", "").lower()

                    if pend_result in ("yes", "no"):
                        pend_side = st.pending_settlement_side
                        pend_entry = st.pending_settlement_entry_price
                        pend_qty = st.pending_settlement_qty

                        if pend_result == pend_side:
                            pend_pnl = (100 - pend_entry) * pend_qty
                        else:
                            pend_pnl = -pend_entry * pend_qty

                        session.record_trade(
                            market=st.pending_settlement_market,
                            side=pend_side,
                            entry_price=pend_entry,
                            exit_price=100 if pend_result == pend_side else 0,
                            qty=pend_qty,
                            pnl_cents=pend_pnl,
                            was_dump=False,
                        )
                        log.warning(
                            f"[SETTLE] Deferred result resolved: {st.pending_settlement_market} "
                            f"{pend_side.upper()} result={pend_result} pnl={pend_pnl}¢ "
                            f"(took {pending_age:.0f}s)"
                        )
                        st.pending_settlement_market = None
                    else:
                        log.info(f"[SETTLE] Still waiting for {st.pending_settlement_market} result ({pending_age:.0f}s)...")
                except Exception as e:
                    log.info(f"[SETTLE] Check failed for {st.pending_settlement_market}: {e}")

        # Check if we need to fetch balance after settlement
        if session.needs_balance_check():
            try:
                bal, _ = get_balance_usd(client)
                if bal is not None:
                    session.update_balance(bal)
                session.clear_balance_check()
            except Exception as e:
                log.warning(f"[SESSION] Balance check failed: {e}")

        if (now - last_meta) >= META_REFRESH_SECONDS:
            try:
                ev2, mt2, mobj2 = refresh_active_market()
                if mt2 != st.market:
                    old_market = st.market
                    log.warning(f"[ROLL] {old_market} -> {mt2}")

                    # Record P&L for settled position (if we had one)
                    if st.traded_this_market and st.entry_price_cents is not None and st.side is not None:
                        # Try to determine settlement result (quick check, don't block long)
                        result = None
                        log.info(f"[ROLL] Checking settlement result for {old_market}...")
                        for retry in range(3):  # Quick check: 3 × 10s = 30s max
                            try:
                                old_mkt_data = client.request("GET", f"/markets/{old_market}")
                                old_mkt_obj = old_mkt_data.get("market", old_mkt_data) if isinstance(old_mkt_data, dict) else {}
                                result = old_mkt_obj.get("result", "").lower()
                                if result in ("yes", "no"):
                                    break
                                log.info(f"[ROLL] Retry {retry+1}/3: result='{result}' for {old_market}, waiting 10s...")
                                time.sleep(10.0)
                            except Exception as e:
                                log.warning(f"[ROLL] Retry {retry+1}/3 failed: {e}")
                                time.sleep(10.0)

                        # Calculate P&L based on settlement
                        pnl_cents = None
                        if result == "yes":
                            if st.side == "yes":
                                pnl_cents = (100 - st.entry_price_cents) * st.qty
                            else:
                                pnl_cents = -st.entry_price_cents * st.qty
                        elif result == "no":
                            if st.side == "yes":
                                pnl_cents = -st.entry_price_cents * st.qty
                            else:
                                pnl_cents = (100 - st.entry_price_cents) * st.qty
                        else:
                            # Settlement not available yet — DEFER, don't guess
                            log.warning(
                                f"[ROLL] Settlement result not available for {old_market} after 30s. "
                                f"Deferring — will re-check during next market."
                            )
                            st.pending_settlement_market = old_market
                            st.pending_settlement_side = st.side
                            st.pending_settlement_entry_price = st.entry_price_cents
                            st.pending_settlement_qty = st.qty
                            st.pending_settlement_was_flip = st.has_flipped
                            st.pending_settlement_ts = time.time()

                        if pnl_cents is not None:
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

                    # Reset per-market session state (keeps daily P&L intact)
                    session.reset_for_new_market()
                    prob_trend.reset(mt2)

                    st.event = ev2
                    st.market = mt2
                    active_market_obj = mobj2 or {}
                    st.sm = SM.ROLL
                    st.traded_this_market = False
                    st.has_flipped = False
                    st.has_scalped = False
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
                    trend.record(spot)
                    trend_short.record(spot)
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
                            spot, lo, hi, sigma_used, secs_to_close, trend,
                            current_balance_usd=session.current_balance_usd,
                        )

                        # Log dump check status periodically
                        if (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                            time_in_trade = time.time() - st.entry_time if st.entry_time > 0 else 0
                            our_prob = p_yes_blend if st.side == "yes" else p_no_blend
                            phase = "grace" if time_in_trade < DUMP_GRACE_PERIOD_SECONDS else "active"
                            drop_from_peak = st.peak_prob_for_side - our_prob if st.peak_prob_for_side > 0 else 0
                            log.info(
                                f"[HOLD] {st.market} {st.side.upper()} pos={pos} "
                                f"held={time_in_trade:.0f}s phase={phase} "
                                f"our_p={our_prob:.1%} peak={st.peak_prob_for_side:.1%} drop={drop_from_peak:.1%} "
                                f"dump={dump_reason or 'none'}"
                            )
                            last_state_log = now

                        if should_dump:
                            log.warning(f"[BAIL] Triggering bail: {dump_reason}")
                            dumped_side = st.side
                            dump_qty = abs(pos)

                            # Estimate exit price (use current bid/ask)
                            if st.side == "yes":
                                exit_price_cents = yes_bid if yes_bid else 50
                            else:
                                exit_price_cents = no_bid if no_bid else 50

                            # ---- STEP 1: SELL to close position ----
                            sell_ok = False
                            try:
                                exit_payload = build_order_payload(
                                    market_ticker=st.market,
                                    action="sell",
                                    side=st.side,
                                    price_cents=1,       # Market sell (accept any price)
                                    count=dump_qty,
                                    post_only=False,
                                )

                                if not DRY_RUN:
                                    oid = place_order(client, exit_payload)
                                    log.warning(f"[BAIL] SELL order placed {oid} SELL {st.side.upper()} qty={dump_qty}")
                                    sell_ok = True
                                else:
                                    log.warning(f"[DRY] Would bail: SELL {st.side.upper()} qty={dump_qty}")
                                    sell_ok = True
                            except Exception as e:
                                log.error(f"[BAIL] Failed to place sell order: {e}")

                            # Record P&L for the dump (separate from sell so it can't break flip)
                            if sell_ok and st.entry_price_cents is not None:
                                try:
                                    pnl_cents = (exit_price_cents - st.entry_price_cents) * dump_qty
                                    session.record_trade(
                                        market=st.market,
                                        side=st.side,
                                        entry_price=st.entry_price_cents,
                                        exit_price=exit_price_cents,
                                        qty=dump_qty,
                                        pnl_cents=pnl_cents,
                                        was_dump=True,
                                    )
                                except Exception as e:
                                    log.warning(f"[BAIL] P&L recording failed (non-fatal): {e}")

                            # ---- STEP 2: FLIP — buy the other side ----
                            # Completely independent from the sell. Even if P&L
                            # recording failed, we still want to flip.
                            if sell_ok:
                                try:
                                    # Re-fetch orderbook for fresh flip prices
                                    flip_ob = client.request("GET", f"/markets/{st.market}/orderbook")
                                    flip_yes_bid, flip_yes_ask, flip_no_bid, flip_no_ask = parse_best_yes_no(flip_ob)
                                    flip_side = "no" if dumped_side == "yes" else "yes"
                                    flip_prob = p_no_blend if dumped_side == "yes" else p_yes_blend
                                    flip_price = flip_no_ask if flip_side == "no" else flip_yes_ask

                                    log.warning(
                                        f"[FLIP] Evaluating: side={flip_side} prob={flip_prob:.1%} "
                                        f"price={flip_price}¢ t_close={secs_to_close}s "
                                        f"has_flipped={st.has_flipped}"
                                    )

                                    can_flip = (
                                        FLIP_AFTER_DUMP
                                        and not st.has_flipped
                                        and secs_to_close >= FLIP_MIN_TIME_REMAINING
                                        and flip_price is not None
                                        and flip_price <= FLIP_MAX_ENTRY_PRICE
                                    )

                                    # Two paths: model agrees (prob >= 60%) or market confident (price >= 80¢)
                                    if can_flip:
                                        market_confident = flip_price >= 80
                                        model_agrees = flip_prob >= FLIP_MIN_PROB
                                        if not market_confident and not model_agrees:
                                            can_flip = False

                                    if can_flip:
                                        flip_edge = flip_prob - (flip_price / 100.0)
                                        flip_qty = session.get_current_contracts()

                                        # High-price scaling for flip too
                                        flip_settle_edge = 100 - flip_price
                                        if flip_settle_edge > 0 and flip_settle_edge < 5:
                                            flip_scale = max(1, round(5.0 / flip_settle_edge))
                                            flip_qty = flip_qty * flip_scale

                                        # Safety cap on flip qty
                                        try:
                                            avail_usd, _ = get_balance_usd(client)
                                            if avail_usd and avail_usd > 0:
                                                cost_per = flip_price / 100.0
                                                max_afford = int(avail_usd * BANKROLL_FRACTION / cost_per) if cost_per > 0 else 0
                                                flip_qty = min(flip_qty, max_afford)
                                            flip_qty = max(MIN_CONTRACTS, min(flip_qty, MAX_CONTRACTS))
                                        except Exception:
                                            flip_qty = BASE_CONTRACTS

                                        flip_path = "market_confident" if (flip_price >= 80) else "model_confirmed"
                                        log.warning(
                                            f"[FLIP] Flipping to {flip_side.upper()} after bail ({flip_path}) — "
                                            f"prob={flip_prob:.1%} price={flip_price}¢ edge={flip_edge:.4f} "
                                            f"qty={flip_qty} t_close={secs_to_close}s"
                                        )

                                        if not DRY_RUN:
                                            flip_payload = build_order_payload(
                                                market_ticker=st.market,
                                                action="buy",
                                                side=flip_side,
                                                price_cents=int(flip_price),
                                                count=int(flip_qty),
                                                post_only=False,
                                            )
                                            flip_oid = place_order(client, flip_payload)
                                            log.warning(
                                                f"[FLIP] Placed flip order {flip_oid} BUY {flip_side.upper()} "
                                                f"@ {flip_price}¢ qty={flip_qty}"
                                            )

                                            # Update state for the new position
                                            st.sm = SM.HOLD
                                            st.side = flip_side
                                            st.entry_price_cents = int(flip_price)
                                            st.entry_time = time.time()
                                            st.entry_model_prob = p_yes_model if flip_side == "yes" else (1.0 - p_yes_model)
                                            st.entry_market_prob = p_mkt
                                            st.entry_spot_price = spot
                                            st.qty = flip_qty
                                            st.peak_prob_for_side = flip_prob
                                            st.has_flipped = True
                                            st.traded_this_market = True
                                        else:
                                            log.warning(f"[DRY] Would flip: BUY {flip_side.upper()} @ {flip_price}¢ qty={flip_qty}")
                                            st.sm = SM.DUMPED
                                            st.side = None
                                            st.entry_price_cents = None
                                    else:
                                        # No flip — log why
                                        reason_parts = []
                                        if not FLIP_AFTER_DUMP:
                                            reason_parts.append("disabled")
                                        if st.has_flipped:
                                            reason_parts.append("already_flipped")
                                        if secs_to_close < FLIP_MIN_TIME_REMAINING:
                                            reason_parts.append(f"time={secs_to_close}s<{FLIP_MIN_TIME_REMAINING}s")
                                        if flip_price is not None and flip_price > FLIP_MAX_ENTRY_PRICE:
                                            reason_parts.append(f"price={flip_price}¢>{FLIP_MAX_ENTRY_PRICE}¢")
                                        if flip_price is not None and flip_price < 80 and flip_prob < FLIP_MIN_PROB:
                                            reason_parts.append(f"prob={flip_prob:.1%}<{FLIP_MIN_PROB:.0%},mkt={flip_price}¢<80¢")
                                        if flip_price is None:
                                            reason_parts.append("no_ask")

                                        log.warning(f"[FLIP] Skipped — {', '.join(reason_parts) or 'unknown'}")
                                        st.sm = SM.DUMPED
                                        st.side = None
                                        st.entry_price_cents = None

                                except Exception as e:
                                    log.error(f"[FLIP] Failed: {e}")
                                    st.sm = SM.DUMPED
                                    st.side = None
                                    st.entry_price_cents = None
                            else:
                                # Sell failed — can't flip
                                st.sm = SM.DUMPED
                                st.side = None
                                st.entry_price_cents = None

                        # ---- LAST-MINUTE SCALP (inside dump check, only if NOT dumping) ----
                        # When we're holding and NOT bailing, check if we should pile on
                        # extra contracts in the final seconds for near-free profit.
                        if not should_dump and not st.has_scalped and secs_to_close is not None:
                            our_prob = p_yes_blend if st.side == "yes" else p_no_blend

                            # Get ask price for our side. When the outcome is near-certain,
                            # the ask dries up (nobody sells the winning side). Fall back to
                            # deriving from the opposite side's bid: yes_price = 100 - no_bid.
                            # If BOTH sides are empty (common near settlement), fall back to
                            # SCALP_MAX_ENTRY_PRICE (99c) — the safety gates in evaluate_scalp
                            # (prob ≥ 95%, distance, volatility) already ensure the outcome is locked.
                            if st.side == "yes":
                                scalp_ask = yes_ask if yes_ask is not None else (100 - no_bid if no_bid is not None else None)
                            else:
                                scalp_ask = no_ask if no_ask is not None else (100 - yes_bid if yes_bid is not None else None)
                            if scalp_ask is None:
                                scalp_ask = SCALP_MAX_ENTRY_PRICE
                            # EV cap: never bid more than the probability
                            scalp_ask = min(scalp_ask, int(our_prob * 100) + 1)  # +1¢ spread slack

                            should_scalp, scalp_qty, scalp_px, scalp_reason = evaluate_scalp(
                                st=st,
                                side=st.side,
                                spot=spot,
                                lo=lo,
                                hi=hi,
                                secs_to_close=secs_to_close,
                                p_blend=our_prob,
                                ask_price=scalp_ask,
                                available_usd=session.current_balance_usd,
                                sigma=sigma_used,
                            )

                            if should_scalp:
                                log.warning(
                                    f"[SCALP] GO — {st.side.upper()} @ {scalp_px}¢ × {scalp_qty} "
                                    f"t_close={secs_to_close}s | {scalp_reason}"
                                )

                                try:
                                    # Fetch fresh cash balance for the scalp order
                                    scalp_avail, _ = get_balance_usd(client)
                                    if scalp_avail is not None and scalp_avail >= MIN_FREE_USD_TO_TRADE:
                                        # Cap scalp so total position (existing + scalp) ≤ MAX_CONTRACTS
                                        existing_pos = abs(pos)  # pos from the HOLD loop
                                        scalp_room = max(0, MAX_CONTRACTS - existing_pos)
                                        if scalp_room <= 0:
                                            log.info(f"[SCALP] Skipped — position already at {existing_pos} (max={MAX_CONTRACTS})")
                                            scalp_qty = 0
                                            st.has_scalped = True
                                        else:
                                            # Re-evaluate qty with fresh balance
                                            if st.side == "yes" and lo is not None:
                                                scalp_dist = spot - lo
                                            elif st.side == "no" and hi is not None:
                                                scalp_dist = hi - spot
                                            elif st.side == "no" and lo is not None:
                                                scalp_dist = lo - spot
                                            else:
                                                scalp_dist = 0.0
                                            scalp_qty = compute_scalp_qty(scalp_avail, scalp_px, scalp_dist)
                                            scalp_qty = min(scalp_qty, scalp_room)  # Enforce position cap

                                        if scalp_qty > 0:
                                            scalp_payload = build_order_payload(
                                                market_ticker=st.market,
                                                action="buy",
                                                side=st.side,
                                                price_cents=int(scalp_px),
                                                count=int(scalp_qty),
                                                post_only=False,  # Market order — need guaranteed fill
                                            )

                                            if not DRY_RUN:
                                                scalp_oid = place_order(client, scalp_payload)
                                                st.has_scalped = True
                                                expected_profit = scalp_qty * (100 - scalp_px) / 100.0
                                                log.warning(
                                                    f"[SCALP] PLACED order={scalp_oid} BUY {st.side.upper()} "
                                                    f"@ {scalp_px}¢ × {scalp_qty} "
                                                    f"(expect +${expected_profit:.2f} at settlement)"
                                                )
                                            else:
                                                st.has_scalped = True
                                                log.warning(f"[DRY SCALP] Would buy {st.side.upper()} @ {scalp_px}¢ × {scalp_qty}")
                                        else:
                                            log.info(f"[SCALP] Skipped — qty=0 after fresh balance (${scalp_avail:.2f})")
                                    else:
                                        log.info(f"[SCALP] Skipped — insufficient cash (${scalp_avail:.2f})")
                                except Exception as e:
                                    log.warning(f"[SCALP] Order failed: {e}")
                                    st.has_scalped = True  # Don't retry on failure

                            elif SCALP_MIN_SECONDS <= secs_to_close <= SCALP_MAX_SECONDS:
                                # Always log scalp evaluation in the window (not rate-limited)
                                # so we can see why it's not firing
                                log.info(f"[SCALP] Not yet (t={secs_to_close}s): {scalp_reason}")

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

        # ============================================================
        # PHASE 1: IDLE — too early to even observe
        # ============================================================
        if secs_to_close > OBSERVE_START_SECONDS:
            st.sm = SM.IDLE
            if (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                log.info(f"[STATE] {st.market} IDLE t_close={secs_to_close}s (observe at {OBSERVE_START_SECONDS}s)")
                last_state_log = now
            time.sleep(POLL_SECONDS)
            continue

        # ============================================================
        # PHASE 2: OBSERVE — gather trend data, watch book, DON'T buy
        # Runs from 12min → 5min before close
        # ============================================================
        spot = fetch_btc_spot_usd(http)
        if spot is not None:
            trend.record(spot)
            trend_short.record(spot)
        if spot is None:
            log.warning(f"[SPOT] failed; skipping this poll")
            time.sleep(POLL_SECONDS)
            continue

        # Always fetch orderbook + compute probability (for trend tracking)
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

        # Record probability for trend detection (every poll — observe AND buy phases)
        prob_trend.record(p_yes_blend)

        # If still in observe window: log what we see, but DON'T place orders
        if secs_to_close > BUY_START_SECONDS:
            st.sm = SM.ARMED
            if (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                trend_move, trend_dir, _ = trend.get_trend()
                trend_s_move, trend_s_dir, _ = trend_short.get_trend()
                log.info(
                    f"[OBSERVE] {st.market} t_close={secs_to_close}s (buy at {BUY_START_SECONDS}s) "
                    f"spot=${spot:.2f} p_yes={p_yes_blend:.1%} p_no={p_no_blend:.1%} "
                    f"60m={trend_dir}(${trend_move:+.0f}) 30m={trend_s_dir}(${trend_s_move:+.0f}) "
                    f"| {prob_trend.summary()}"
                )
                last_state_log = now
            time.sleep(POLL_SECONDS)
            continue

        # ============================================================
        # PHASE 3: BUY WINDOW — last 5 min, make the call
        # By now we have 7+ minutes of trend data to inform the decision
        # ============================================================
        st.sm = SM.ARMED

        if secs_to_close < ENTRY_LAST_SECONDS:
            if pos == 0 and secs_to_close < 0:
                # Market already closed, no position — force roll and wait
                log.warning(f"[SKIP] {st.market} already closed (t_close={secs_to_close}s), no position — waiting for next market")
                last_meta = 0.0  # Trigger meta refresh on next loop
                time.sleep(10.0)  # Wait 10s between retries, not 1s — the next market may not exist yet
            else:
                st.traded_this_market = True
                log.warning(f"[SKIP] {st.market} missed last entry window (t_close={secs_to_close}s)")
                time.sleep(POLL_SECONDS)
            continue

        if LOG_DECISIONS:
            yes_px_log = postable_entry_price(yes_bid, yes_ask) if POST_ONLY else yes_ask
            no_px_log = postable_entry_price(no_bid, no_ask) if POST_ONLY else no_ask
            edge_yes_log = compute_edge(p_yes_blend, yes_px_log, FEE_CENTS_PER_CONTRACT) if yes_px_log is not None else None
            edge_no_log = compute_edge(p_no_blend, no_px_log, FEE_CENTS_PER_CONTRACT) if no_px_log is not None else None

            yes_buffer_str = f"${spot-lo:.2f}" if lo else "N/A"
            no_buffer_str = f"${hi-spot:.2f}" if hi else "N/A"
            edge_yes_str = f"{edge_yes_log:.4f}" if edge_yes_log is not None else "N/A"
            edge_no_str = f"{edge_no_log:.4f}" if edge_no_log is not None else "N/A"

            log.info(
                f"[DECIDE] {st.market} t_close={secs_to_close}s spot=${spot:.2f} "
                f"p_yes={p_yes_blend:.2f} p_no={p_no_blend:.2f} "
                f"YES(edge={edge_yes_str}) NO(edge={edge_no_str}) -> {chosen_side}@{chosen_px} "
                f"| {prob_trend.summary()}"
            )

        if chosen_side is None or chosen_px is None:
            if (now - last_ob_warn) >= OB_WARN_EVERY_SECONDS:
                log.warning(f"[OB] no usable entry: yes=({yes_bid},{yes_ask}) no=({no_bid},{no_ask})")
                last_ob_warn = now
            time.sleep(POLL_SECONDS)
            continue

        # ================================================================
        # ENTRY FILTER PIPELINE
        # Two paths:
        #   FAST LANE (≥90% prob): outcome is near-certain → buy immediately
        #   STANDARD  (<90% prob): borderline → need trend + momentum confirmation
        # This lets us trade every market when it's obvious, but stay cautious
        # when the outcome is unclear.
        # ================================================================
        current_prob_for_side = p_yes_blend if chosen_side == "yes" else p_no_blend
        fast_lane = current_prob_for_side >= PROB_FAST_LANE_THRESHOLD

        if fast_lane:
            log.warning(
                f"[FAST LANE] {chosen_side.upper()} prob={current_prob_for_side:.1%} ≥ {PROB_FAST_LANE_THRESHOLD:.0%} "
                f"— skipping trend/momentum checks, outcome near-certain"
            )
        else:
            # --- MULTI-TIMEFRAME TREND CHECK (borderline trades only) ---
            alignment = trend.trade_alignment(chosen_side, lo, hi, spot)
            trend_move, trend_dir, trend_n = trend.get_trend()
            alignment_short = trend_short.trade_alignment(chosen_side, lo, hi, spot)
            trend_short_move, trend_short_dir, trend_short_n = trend_short.get_trend()

            if chosen_side == "yes":
                edge_for_trend = float(edge_yes)
            else:
                edge_for_trend = float(edge_no)

            # Check if EITHER timeframe shows opposing trend
            either_strong_against = (
                (alignment == "against" and "strong" in trend_dir) or
                (alignment_short == "against" and "strong" in trend_short_dir)
            )
            either_against = alignment == "against" or alignment_short == "against"

            if either_strong_against:
                if TREND_AGAINST_BLOCK:
                    log.warning(
                        f"[TREND] BLOCKED {chosen_side.upper()} — against strong trend "
                        f"60m={trend_dir}(${trend_move:+.0f}) 30m={trend_short_dir}(${trend_short_move:+.0f}). "
                        f"Edge={edge_for_trend:.4f} not enough to fight momentum."
                    )
                    time.sleep(POLL_SECONDS)
                    continue
                elif edge_for_trend < EDGE_MIN + TREND_AGAINST_EDGE_BOOST:
                    log.warning(
                        f"[TREND] SKIPPED {chosen_side.upper()} — against strong trend "
                        f"60m={trend_dir}(${trend_move:+.0f}) 30m={trend_short_dir}(${trend_short_move:+.0f}), "
                        f"edge={edge_for_trend:.4f} < {EDGE_MIN + TREND_AGAINST_EDGE_BOOST:.4f} required"
                    )
                    time.sleep(POLL_SECONDS)
                    continue
            elif either_against:
                if edge_for_trend < EDGE_MIN + TREND_AGAINST_EDGE_BOOST:
                    log.warning(
                        f"[TREND] SKIPPED {chosen_side.upper()} — against moderate trend "
                        f"60m={trend_dir}(${trend_move:+.0f}) 30m={trend_short_dir}(${trend_short_move:+.0f}), "
                        f"edge={edge_for_trend:.4f} < {EDGE_MIN + TREND_AGAINST_EDGE_BOOST:.4f} required"
                    )
                    time.sleep(POLL_SECONDS)
                    continue
                else:
                    log.info(f"[TREND] Trading {chosen_side.upper()} against moderate trend — edge {edge_for_trend:.4f} sufficient")
            elif alignment == "with" or alignment_short == "with":
                log.info(
                    f"[TREND] Trading WITH trend ({chosen_side.upper()}) — "
                    f"60m={trend.summary()} 30m={trend_short.summary()}"
                )

            # --- PROBABILITY TREND CHECK (borderline trades need momentum) ---
            prob_trend_ok, prob_trend_reason = prob_trend.should_buy(chosen_side, current_prob_for_side, secs_to_close)

            if PROB_TREND_ENTRY_ENABLED and not prob_trend_ok:
                if (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                    log.info(f"[PROB_TREND] Waiting for momentum: {prob_trend_reason} | {prob_trend.summary()}")
                    last_state_log = now
                time.sleep(POLL_SECONDS)
                continue

            # --- CROSS-VALIDATE: prob trend must match BTC spot trend ---
            if REQUIRE_TREND_ALIGNMENT and prob_trend_ok:
                prob_change, prob_dir, _, _ = prob_trend.get_trend()
                spot_move, spot_dir, _ = trend.get_trend()
                spot_short_move, spot_short_dir, _ = trend_short.get_trend()

                misaligned = False
                if chosen_side == "yes" and "down" in spot_dir and abs(spot_move) > 50:
                    misaligned = True
                    mismatch_reason = f"prob_yes but BTC 60m={spot_dir}(${spot_move:+.0f})"
                elif chosen_side == "no" and "up" in spot_dir and abs(spot_move) > 50:
                    misaligned = True
                    mismatch_reason = f"prob_no but BTC 60m={spot_dir}(${spot_move:+.0f})"
                elif chosen_side == "yes" and "down" in spot_short_dir and abs(spot_short_move) > 50:
                    misaligned = True
                    mismatch_reason = f"prob_yes but BTC 30m={spot_short_dir}(${spot_short_move:+.0f})"
                elif chosen_side == "no" and "up" in spot_short_dir and abs(spot_short_move) > 50:
                    misaligned = True
                    mismatch_reason = f"prob_no but BTC 30m={spot_short_dir}(${spot_short_move:+.0f})"

                if misaligned:
                    log.warning(f"[ALIGNMENT] BLOCKED — {mismatch_reason}. Prob trend may be manipulation, not signal.")
                    time.sleep(POLL_SECONDS)
                    continue

                log.info(f"[ALIGNMENT] OK — prob {prob_dir} aligns with BTC 60m={spot_dir} 30m={spot_short_dir}")

            if prob_trend_ok:
                log.warning(f"[PROB_TREND] GO signal: {prob_trend_reason} | {prob_trend.summary()}")

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
            log.warning(f"[DRY] would place: BUY {chosen_side} @ {chosen_px}¢ qty={qty} (contracts={session.current_contracts} streak={session.consecutive_wins}W)")
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
            # Initialize peak tracking for proactive dump
            st.peak_prob_for_side = p_yes_blend if chosen_side == "yes" else (1.0 - p_yes_blend)

            log.warning(
                f"[ORDER] PLACED {st.market} order_id={oid} BUY {chosen_side.upper()} @ {chosen_px}¢ qty={qty} "
                f"edge={edge_net:.4f} p_gate={p_gate:.4f} contracts={session.current_contracts} streak={session.consecutive_wins}W"
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
