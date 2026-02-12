# shib_bot.py
# Kalshi rolling 1H SHIB — Find mispriced contracts, hold to close, scale bankroll
#
# STRATEGY:
# - Arms at T-2400s (40 min) to OBSERVE market, SHIB price, trends, book
# - Buys mispriced contracts in 5-25 min window where model has info advantage
# - Trusts the MARKET (orderbook) over the model near settlement
# - Requires 3%+ real edge — only enters when model sees genuine mispricing
# - HOLDS TO SETTLEMENT — collect the full payout for being right
# - Dump is ABORT ONLY — safety net, not a regular exit
# - Scales bankroll: wins compound via quarter-Kelly, 24 markets/day
#
# KEY SETTINGS:
# - TIME-DEPENDENT PROB: 90% if >15min, 86% if 8-15min, 83% if <8min
# - EDGE_MIN=0.03 (3% real edge — no penny-picking)
# - MAX_ENTRY_PRICE=96¢ (force real edge)
# - KELLY=0.25 (quarter-Kelly — smoother equity curve)
# - SHIB-AWARE BAIL: only dump if SHIB has moved against us, not book noise
# - MULTI-TIMEFRAME TRENDS: 120-min + 60-min SpotTrend, per-minute ProbTrend
# - BANKROLL STOPS: max loss = 3% of balance or 8% settlement loss cap
# - Dump abort: 6% reversal, 30s patience, catastrophic 20¢ backstop

import os
import time
import base64
import logging
import math
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


# -----------------------------
# HARD BOOT BANNER (so you ALWAYS see something if python starts)
# -----------------------------
print(f"BOOT: shib_bot.py loaded at {datetime.now(timezone.utc).isoformat()}Z", flush=True)


# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-shib-bot")
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

SERIES_TICKER = getenv_first(["SERIES", "KALSHI_SERIES", "KALSHI_SERIES_TICKER"], "KXSHIBA")
EVENT_TICKER = getenv_first(["EVENT_TICKER", "KALSHI_EVENT_TICKER"], "<auto>")
MARKET_OVERRIDE = getenv_first(["MARKET_OVERRIDE", "KALSHI_MARKET_OVERRIDE"], "<none>")

POLL_SECONDS = env_float("POLL_SECONDS", 1.0)  # Check every second for dumps
META_REFRESH_SECONDS = env_float("META_REFRESH", 10.0)

DRY_RUN = env_bool("DRY_RUN", False)
ENABLE_TRADING = env_bool("ENABLE_TRADING", True)
POST_ONLY = env_bool("POST_ONLY", False)  # Use market orders for faster fills
YES_ONLY = env_bool("YES_ONLY", False)    # Trade both YES and NO — double the addressable markets

ORDER_QTY = env_int("ORDER_QTY", 1)

COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/SHIB-USD/spot"

BOOTSTRAP_CANCEL_OPEN_ORDERS = env_bool("BOOTSTRAP_CANCEL_OPEN_ORDERS", True)

# -------------- HOLD-TO-CLOSE STRATEGY (HARDWIRED) --------------
# Philosophy: arm early to OBSERVE price/trends/book. Buy when confident,
# but EARLY ENOUGH that the book is still liquid (asks exist).
# Even a few cents edge is fine — buy MORE contracts.
# Hold to settlement. Dump is abort-only. Trade every market possible.
#
# KEY INSIGHT: Waiting until the last few minutes to buy means the book is LOCKED (no asks).
# Placing a 99¢ bid with no sellers = zero fills = zero profit. Enter at
# T-1500s when probability is high AND the book still has liquidity.
#
# TWO PHASES (1-hour market):
#   OBSERVE (40min → 25min before close): gather trend data, watch book, DON'T buy
#   BUY     (25min → 10s before close):   make the call, place the order, hold
OBSERVE_START_SECONDS = 2400  # Start watching at 40min — gather trend + prob data
BUY_START_SECONDS = 1500      # Can enter from T-1500s (25 min) — book has liquidity, grab it before it locks up
ENTRY_LAST_SECONDS = 10       # Can enter up to 10s before close (need time to fill)
FILL_WAIT_SECONDS = 20
ALLOW_TAKER_AT_LAST = True
CANCEL_UNFILLED_AT_CLOSE = True

PROB_MIN = 0.83  # 83%+ to enter in last 8 min — slightly lower bar, EV cap is the real protection
EDGE_MIN = 0.03  # 3% minimum edge — only enter with real mispricing, not penny edges
MAX_ENTRY_PRICE_CENTS = 96  # At 96¢ entry, gain 4¢/win, need ~24 wins per loss
FEE_CENTS_PER_CONTRACT = 0

# -------------- TIME-DEPENDENT CERTAINTY (within the 25-min buy window) --------
# Buy window is 25min → 10s before close. Require more certainty at the start
# of the buy window (SHIB still has time to move), relax near the end.
# NOTE: observation phase (40min → 25min) gathers data but never buys.
PROB_EARLY_ENTRY_SECONDS = 900   # 15-25 min to close = "early" part of buy window
PROB_EARLY_MIN = 0.90            # >15min: need 90%+ — SHIB has time to move
PROB_MID_ENTRY_SECONDS = 480     # 8-15 min to close = "mid"
PROB_MID_MIN = 0.86              # 8-15min: need 86%+
# <8 min = PROB_MIN (0.83) — market has priced in the outcome, EV cap protects

# -------------- PROBABILITY TREND DETECTION (confirm borderline trades) --------
# When prob is borderline (80-89%), require momentum confirmation.
# When prob is high (90%+), the outcome speaks for itself — skip trend checks.
PROB_TREND_WINDOW_SECONDS = 180   # Look at last 180 seconds of probability (wider for hourly)
PROB_TREND_MIN_SAMPLES = 10       # Need at least 10 samples (~100s at 1/sec)
PROB_TREND_THRESHOLD = 0.08       # 8% swing in one direction = trend signal
PROB_TREND_MIN_CURRENT = 0.80     # Current prob must be ≥80% for trend-based entries
PROB_TREND_ENTRY_ENABLED = True   # Enable trend-based entries (for borderline trades)
REQUIRE_TREND_ALIGNMENT = True    # Prob trend must match SHIB spot trend (borderline only)
# HIGH-CERTAINTY FAST LANE: if prob is this high, skip trend/momentum checks entirely
# Rationale: 90%+ prob means SHIB is well inside the range. The outcome is decisive.
# Don't wait for trend alignment when the outcome is clear.
# TIME-DEPENDENT: early in buy window, require higher prob (92%) for fast lane.
# Near close (<8 min), 85% is enough because the market has priced in the outcome.
PROB_FAST_LANE_THRESHOLD = 0.90   # ≥90% prob = buy immediately, no trend check needed
PROB_FAST_LANE_LATE_THRESHOLD = 0.85  # ≥85% prob in last 8 min = fast lane (market is decisive)

# CONFIRMATION HOLD: require signal to be stable for N seconds before early entry
# Prevents snap entries on transient orderbook spikes at T-1500s.
# At T-900s to T-480s, prob must have been on the same side for this many seconds.
CONFIRMATION_HOLD_SECONDS = 30   # Signal must persist for 30s before early commitment (wider for hourly)
CONFIRMATION_HOLD_MIN_TIME = 480  # Only require confirmation hold above 8 min to close

# SHIB price ~$0.00001-0.00003 — sigma is proportionally tiny in absolute $
# Dynamic sigma (USE_DYNAMIC_SIGMA=True) self-calibrates from Coinbase candles
SPOT_SIGMA_USD_PER_SQRT_SEC = 0.000000005  # Fallback: ~5e-9 $/√sec for SHIB

# -------------- KELLY BANKROLL SIZING (HARDWIRED) --------------
# Philosophy: size by BANKROLL FRACTION using Kelly criterion.
# As you win, your bankroll grows → position size grows automatically.
# As you lose, bankroll shrinks → position size shrinks (self-protecting).
# No streak counters needed — compounding is baked into the math.
#
# Kelly fraction = p_true - (1 - p_true) / ((1 - price) / price)
# where p_true = model probability, price = entry cost / 100.
# Full Kelly is optimal but volatile; quarter-Kelly gives smoother equity curve.
KELLY_MULTIPLIER = 0.25     # Quarter-Kelly — smaller bets, smoother equity curve, survives loss streaks
KELLY_FLOOR_FRACTION = 0.05 # Minimum 5% of bankroll when we decide to trade at all
KELLY_CAP_FRACTION = 0.50   # Never risk more than 50% of bankroll in one trade
MAX_CONTRACTS = 100          # Hard cap — safety limit (bankroll fraction is the real cap)
MIN_CONTRACTS = 1           # Floor
MIN_FREE_USD_TO_TRADE = 5.0
# Legacy constants (kept for backward compat in safety checks)
BASE_CONTRACTS = 3
CONTRACT_INCREMENT = 1
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
# SHIB has actually moved against us AND the book confirms it. A book spike
# while SHIB is on our side is NOT a reason to bail.
DUMP_PROB_FLIP = 0.60  # Floor: if prob drops to 60% AND SHIB confirms, bail (was 50% — too late, already lost 40¢+)
DUMP_PROB_DROP_PERCENT = 1.0  # Disabled
DUMP_MARKET_FLIP_THRESHOLD = 0.50  # Floor
DUMP_MIN_TIME_REMAINING = 8   # Can bail until 8s before settlement (was 15s — more time to dump)
DUMP_ON_PRICE_DANGER = False  # Disabled - trust SHIB price, not book noise

# -------------- SHIB-AWARE BAIL (the key fix: don't dump winners) ---------------
# Before ANY bail trigger fires, check: is SHIB on our side of the boundary?
# YES side: spot > lo + buffer → SHIB is safely above range floor → HOLD
# NO side:  spot < hi - buffer → SHIB is safely below range ceiling → HOLD
# If SHIB is on our side, the book is lying (thin book, spike, manipulation).
# ONLY bail if SHIB has actually crossed or is dangerously close to boundary.
DUMP_BTC_SAFE_BUFFER_EARLY = env_float("DUMP_SAFE_BUFFER_EARLY", 0.0000003)  # >5min to close: ~1.5% of SHIB price
DUMP_BTC_SAFE_BUFFER_LATE = env_float("DUMP_SAFE_BUFFER_LATE", 0.0000001)    # <5min to close: ~0.5% of SHIB price
DUMP_BTC_SAFE_CUTOFF_SECONDS = 300   # Boundary between early/late buffer (5 min for hourly markets)

# -------------- REVERSAL BAIL (only after SHIB check fails) --------------------
DUMP_ON_PROB_REVERSAL = True   # Still enabled as safety net
DUMP_REVERSAL_THRESHOLD = 0.06  # 6% drop from peak — bail fast (was 8% — still too slow, 6% catches reversals earlier)
DUMP_REVERSAL_THRESHOLD_PROFIT = 0.04  # 4% when profitable — protect gains aggressively (was 6%)
DUMP_PROFIT_TIGHTEN_ABOVE_ENTRY = 0.03  # Tighten after 3%+ gain (was 5% — start protecting earlier)
DUMP_REVERSAL_MIN_SAMPLES = 5
DUMP_EARLY_EXIT_ENABLED = True
# Reversal during early settling phase uses a wider threshold (not blocked entirely)
DUMP_REVERSAL_THRESHOLD_SETTLING = 0.10  # 10% drop in first 30s = something is very wrong, bail even early

# -------------- BANKROLL-PROPORTIONAL LOSS CAP (scales with your balance) -----
# Never lose more than X% of current balance on a single trade.
# At $35: max loss = $1.75.  At $350: max loss = $17.50.  Scales naturally.
# This fires BEFORE the fixed catastrophic stop and replaces it as the primary cap.
DUMP_MAX_LOSS_FRACTION_OF_BALANCE = 0.03  # 3% of current balance = max single-trade loss (was 5% — too much at small bankroll)
# Also cap at 50% of position cost — if you paid $3, max loss is $1.50
DUMP_MAX_LOSS_FRACTION_OF_POSITION = 0.50  # Never lose more than 50% of what you put in
# ENTRY-SIDE cap: worst case = settlement loss = full entry cost.
# With the EV price cap (price ≤ prob), entries are always +EV, so we can
# afford to size up.  15% of $22 = $3.30 → 3 contracts at 97c.
# As bankroll grows to $220: $33 → 34 contracts at 97c.
MAX_SETTLEMENT_LOSS_FRACTION = 0.08  # Max 8% of balance at risk per trade — one loss hurts but doesn't wreck you

# -------------- BAIL TIMING (hold to close — but bail fast when it's wrong) ----
DUMP_GRACE_PERIOD_SECONDS = 10      # 10s grace period (was 15s — start monitoring sooner)
DUMP_PROACTIVE_AFTER_SECONDS = 30   # Proactive bail after 30s (was 60s — detect reversals earlier, bankroll cap covers the gap)

# -------------- HARD P&L STOP (last-resort backstop) -------------------------
DUMP_MAX_LOSS_CENTS_PER_CONTRACT = 10  # Hard stop after SHIB check (was 15¢ — tighter to salvage more)
# CATASTROPHIC STOP: fires BEFORE SHIB check — absolute max loss regardless of anything
# Prevents a $2.65 loss when the hard stop is supposed to cap at 10¢/contract
DUMP_CATASTROPHIC_LOSS_CENTS = 20      # If losing >20¢/contract, bail no matter what (was 30¢ — too much damage)

# -------------- WINDOWED PEAK TRACKING (avoid false reversals from book spikes) -----
# All-time peak ratchets up on thin-book spikes (e.g., 99% for 3 seconds) creating
# false reversal signals when prob returns to normal (e.g., 94% looks like 5% drop).
# Use a rolling window max instead: peak = max(prob over last N seconds).
DUMP_PEAK_WINDOW_SECONDS = 30  # Use max prob over last 30s as "peak" (not all-time)

# -------------- RAPID DROP BAIL (emergency exit on fast moves) -------------------
# If probability drops very fast (>4% in 10s), something is seriously wrong.
# Bail even during settling period — fast drops mean SHIB is actively moving against us.
DUMP_RAPID_DROP_THRESHOLD = 0.04   # 4% drop in the rapid window = emergency
DUMP_RAPID_DROP_WINDOW_SECONDS = 10  # Look at last 10 seconds for rapid drops

# -------------- FLIP AFTER DUMP (double-dip: dump losing side, buy winning side) ----
# If we bail because SHIB moved against us, the OTHER side is now the high-prob winner.
# Instead of just eating the loss, flip to the other side and hold THAT to settlement.
# Example: bought YES at 94¢, SHIB tanks, dump YES at 40¢ (lose 54¢), buy NO at 60¢,
#          NO settles at $1 → +40¢. Net loss 14¢ instead of 54¢.
# Safety: the flip still checks probability and price, but with a LOWER bar
#         than a fresh entry — this is a recovery play, not a new trade.
#         We already took the loss; the question is "can I claw some back?"
FLIP_AFTER_DUMP = True              # Enable flip-to-other-side after bail
FLIP_MIN_TIME_REMAINING = 15        # Just need time to place the order and settle
FLIP_MIN_PROB = 0.60                # Lower bar: 60% on other side is enough for recovery
FLIP_MAX_ENTRY_PRICE = 99           # Edge = settlement payout, even 1¢/contract at scale

# -------------- LAST-MINUTE SCALP (compound on near-certain outcomes) -----------
# With <120s left and SHIB far from the strike, the outcome is locked.
# Buy a boatload of contracts at 98-99¢ and collect 1-2¢/contract at settlement.
# Key safety: distance from strike.  If SHIB is far from the boundary with 120s left,
# it cannot reverse.  Volatility gate is the real safety check.
#
# Risk/reward at 99¢ × 33 contracts:
#   Win (99.5%+ of the time): +$0.33
#   Lose (SHIB reverses in 120s): -$32.67
# Over 24 markets/day: scalp income compounds.
SCALP_ENABLED = True
SCALP_MAX_SECONDS = 120            # Only scalp in the last 120 seconds (wider window for hourly)
SCALP_MIN_SECONDS = 10             # Don't scalp in the last 10s (order might not fill)
SCALP_MIN_DISTANCE_USD = 0.0000002 # SHIB must be ~1% from strike (vol gate is the real safety)
# Distance tiers: farther from strike = more aggressive sizing
# Each tier: (min_distance_usd, bankroll_fraction)  — scaled for SHIB price
SCALP_DISTANCE_TIERS = [
    (0.000001,  0.85),   # ~5% from strike: extremely safe, size up hard
    (0.0000005, 0.65),   # ~2.5% from strike: very safe, go bigger
    (0.0000003, 0.45),   # ~1.5% from strike: safe, meaningful size
    (0.0000002, 0.25),   # ~1% from strike: moderate — compound the edge
]
SCALP_MAX_ENTRY_PRICE = 99        # Max 99¢ — even 1¢/contract × many contracts at scale
SCALP_MIN_PROB = 0.80             # Low bar — distance + volatility gate is the real safety, not blend prob
SCALP_MAX_LOSS_FRACTION = 0.05    # Never risk more than 5% of cash on a scalp (was 15% — too much when stacked with main entry)

# -------------- COMBINED POSITION RISK CAP (main entry + scalp) --------------
# The main entry risks up to MAX_SETTLEMENT_LOSS_FRACTION (8%) and the scalp
# risks up to SCALP_MAX_LOSS_FRACTION (5%) independently.  Without a combined
# cap, a single market can lose 13% of bankroll when both positions go wrong.
# This cap ensures the TOTAL risk across all positions in one market never
# exceeds a single threshold.  The scalp logic checks existing position cost
# and only uses whatever room remains under this cap.
COMBINED_POSITION_RISK_CAP = 0.10  # Max 10% of bankroll at risk per market (main + scalp combined)

# -------------- BRACKET ARBITRAGE (buy all 3, dump 2, hold 1) ----------------
# When an event has 3 range brackets (lo-mid, mid-hi, hi-top), exactly ONE must
# settle YES at $1.00.  If we buy YES on all 3 cheaply enough (total < 100¢),
# we're guaranteed profit regardless of outcome.
#
# Strategy:
#   1. Near settlement, identify all 3 sibling brackets for the same event
#   2. Buy YES on all 3 at the ask (or post limit orders)
#   3. As the winner becomes clear, dump the 2 losers while they still have bids
#   4. Hold the winner to settlement at $1.00
#
# The edge comes from total ask < 100¢ (guaranteed arb) or from being able to
# identify the winner early enough to dump losers while they still have residual
# value (reducing net cost below 100¢).
BRACKET_ARB_ENABLED = env_bool("BRACKET_ARB_ENABLED", True)
BRACKET_ARB_MAX_TOTAL_CENTS = env_int("BRACKET_ARB_MAX_TOTAL_CENTS", 97)   # Max total to pay for 3 YES (97¢ = 3¢ guaranteed profit)
BRACKET_ARB_SOFT_TOTAL_CENTS = env_int("BRACKET_ARB_SOFT_TOTAL_CENTS", 103)  # Allow up to 103¢ if we can dump losers to recover
BRACKET_ARB_MAX_SECONDS = env_int("BRACKET_ARB_MAX_SECONDS", 600)           # Enter within last 10 minutes
BRACKET_ARB_MIN_SECONDS = env_int("BRACKET_ARB_MIN_SECONDS", 15)            # Need at least 15s to get fills
BRACKET_ARB_MAX_SINGLE_PRICE = env_int("BRACKET_ARB_MAX_SINGLE_PRICE", 50)  # Don't pay more than 50¢ for any single bracket (low prices only)
BRACKET_ARB_BANKROLL_FRACTION = env_float("BRACKET_ARB_BANKROLL_FRACTION", 0.08)  # Max 8% of bankroll per bracket set
BRACKET_ARB_DUMP_PROB_THRESHOLD = env_float("BRACKET_ARB_DUMP_PROB_THRESHOLD", 0.10)  # Dump when a bracket's YES prob < 10%
BRACKET_ARB_WINNER_PROB_THRESHOLD = env_float("BRACKET_ARB_WINNER_PROB_THRESHOLD", 0.85)  # Bracket is "winner" when prob > 85%
BRACKET_ARB_MIN_BRACKETS = env_int("BRACKET_ARB_MIN_BRACKETS", 3)  # Need all 3 brackets to arb

# -------------- A-LEVEL ADDITIONS --------------
USE_MARKET_IMPLIED = env_bool("USE_MARKET_IMPLIED", True)
MODEL_BLEND_ALPHA = env_float("MODEL_BLEND_ALPHA", 0.20)  # Was 0.75 — model is ~50/50 at >5min, drowns out 95% market signal

REQUIRE_DIVERGENCE = env_bool("REQUIRE_DIVERGENCE", False)
MIN_DIVERGENCE = env_float("MIN_DIVERGENCE", 0.015)

USE_DYNAMIC_SIGMA = env_bool("USE_DYNAMIC_SIGMA", True)
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/SHIB-USD/candles"
CANDLES_GRANULARITY_SEC = env_int("CANDLES_GRANULARITY_SEC", 60)
CANDLES_LOOKBACK = env_int("CANDLES_LOOKBACK", 10)
SIGMA_FLOOR = env_float("SIGMA_FLOOR", 0.000000001)   # ~1e-9 for SHIB
SIGMA_CEIL = env_float("SIGMA_CEIL", 0.00000005)      # ~5e-8 for SHIB

MAX_SPREAD_CENTS_TO_TRADE = env_int("MAX_SPREAD_CENTS_TO_TRADE", 12)  # Wider to allow entry when book is thinner early on
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
SETTLEMENT_LOCK_SECONDS = env_int("SETTLEMENT_LOCK_SECONDS", 480)    # Last 8 min only — earlier window still needs trend/prob checks (scaled for hourly)
SETTLEMENT_LOCK_MIN_PROB = env_float("SETTLEMENT_LOCK_MIN_PROB", 0.85)  # blend prob — lower bar, EV cap (price ≤ prob) is the real protection
SETTLEMENT_LOCK_MAX_PRICE = env_int("SETTLEMENT_LOCK_MAX_PRICE", 99)   # edge = settlement
SETTLEMENT_LOCK_MIN_BID = env_int("SETTLEMENT_LOCK_MIN_BID", 90)      # locked book: if bid ≥ 90¢ but no ask, join bid queue

LAST_CHANCE_TIME_SEC = env_int("LAST_CHANCE_TIME_SEC", 60)  # Last chance window wider for hourly
LAST_CHANCE_MIN_PROB = env_float("LAST_CHANCE_MIN_PROB", 0.85)

BOUNDARY_BUFFER_USD = env_float("BOUNDARY_BUFFER_USD", 0.0000002)  # ~1% of SHIB price — SHIB-aware bail is the real safety net during hold
LATE_ENTRY_PROB_BOOST = env_float("LATE_ENTRY_PROB_BOOST", 0.0)  # No boost — EV price cap is the real protection
LATE_ENTRY_TIME_SEC = env_int("LATE_ENTRY_TIME_SEC", 15)

# -------------- TREND TRACKING (know what SHIB is doing) --------------
TREND_WINDOW_MINUTES = 120         # Long-term trend: 120 min (~2 hourly markets)
TREND_SHORT_WINDOW_MINUTES = 60    # Short-term trend: 60 min (~1 hourly market)
TREND_SAMPLE_INTERVAL_SECONDS = 30  # Record spot every 30s
TREND_STRONG_THRESHOLD = 0.0000005  # ~2.5% move in window = strong trend (scaled for SHIB)
TREND_MODERATE_THRESHOLD = 0.0000002  # ~1% move = moderate trend (scaled for SHIB)
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

    return infer_close_ts_from_ticker(ticker, interval_minutes=60)


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
    skipped_statuses = {}
    for m in markets:
        status = str(m.get("status", "")).lower()
        if status and status not in ("open", "active"):
            skipped_statuses[status] = skipped_statuses.get(status, 0) + 1
            continue
        ot = get_ts(m, "open_time") or get_ts(m, "open_ts") or get_ts(m, "open_timestamp")
        ct = get_ts(m, "close_time") or get_ts(m, "close_ts") or get_ts(m, "close_timestamp")
        # If timestamps are ISO strings, parse them
        if ot is None:
            for k in ("open_time", "open_ts", "open_timestamp"):
                v = m.get(k)
                if isinstance(v, str) and v:
                    parsed = _parse_iso_to_epoch_s(v)
                    if parsed is not None:
                        ot = parsed
                        break
        if ct is None:
            for k in ("close_time", "close_ts", "close_timestamp"):
                v = m.get(k)
                if isinstance(v, str) and v:
                    parsed = _parse_iso_to_epoch_s(v)
                    if parsed is not None:
                        ct = parsed
                        break
        # Last resort: infer close_ts from ticker
        ticker = m.get("ticker") or m.get("market_ticker") or ""
        if ct is None and ticker:
            ct = infer_close_ts_from_ticker(ticker, interval_minutes=60)
        candidates.append((ot, ct, m))
    if skipped_statuses:
        log.info(f"[PICK] skipped statuses: {skipped_statuses}")

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

    log.info(f"[PICK] total_markets={len(markets)} candidates={len(candidates)} active={len(active)} future={len(future)} past={len(past)}")

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
def fetch_shib_spot_usd(session: requests.Session, timeout: float = 5.0) -> Optional[float]:
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


def get_order(client: KalshiClient, order_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single order by ID. Returns the order dict or None on error."""
    try:
        resp = client.request("GET", f"/portfolio/orders/{order_id}")
        if isinstance(resp, dict):
            return resp.get("order", resp)
        return None
    except Exception as e:
        log.warning(f"[ORDER] get_order({order_id}) failed: {e}")
        return None


# Fill check constants
FILL_CHECK_DELAY = 2.0       # seconds to wait before checking fill
FILL_CHECK_RETRIES = 2       # how many times to re-check before giving up
FILL_CHECK_INTERVAL = 2.0    # seconds between re-checks


def wait_for_fill(client: KalshiClient, order_id: str, market: str) -> Tuple[str, int]:
    """Wait briefly for an order to fill. Returns (status, filled_qty).

    status: 'filled', 'partial', 'resting', 'canceled', 'unknown'
    filled_qty: number of contracts that actually filled (0 if none).
    """
    time.sleep(FILL_CHECK_DELAY)

    for attempt in range(1, FILL_CHECK_RETRIES + 1):
        order = get_order(client, order_id)
        if order is None:
            # API error — check position as fallback
            try:
                pos = abs(parse_position_for_market(get_positions(client), market))
                if pos > 0:
                    log.info(f"[FILL] Order lookup failed but found position={pos} in {market}")
                    return "filled", pos
            except Exception:
                pass
            return "unknown", 0

        status = order.get("status", "unknown")
        remaining = order.get("remaining_count", order.get("count", 0))
        total = order.get("count", 0)

        # Kalshi statuses: resting, canceled, executed (fully filled), partial
        if status == "executed":
            log.info(f"[FILL] Order {order_id} fully filled: {total} contracts")
            return "filled", total
        if remaining == 0 and total > 0:
            # Fully filled even if status label differs
            log.info(f"[FILL] Order {order_id} filled (remaining=0): {total} contracts")
            return "filled", total
        if 0 < remaining < total:
            filled = total - remaining
            log.info(f"[FILL] Order {order_id} partial fill: {filled}/{total} contracts")
            return "partial", filled
        if status == "canceled":
            log.info(f"[FILL] Order {order_id} was canceled")
            return "canceled", 0
        # Still resting — wait and retry
        if attempt < FILL_CHECK_RETRIES:
            time.sleep(FILL_CHECK_INTERVAL)

    # Still resting after all retries
    log.warning(f"[FILL] Order {order_id} still resting after {FILL_CHECK_DELAY + FILL_CHECK_RETRIES * FILL_CHECK_INTERVAL}s")
    return "resting", 0


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
    # Legacy contract-count tracking (sizing now uses Kelly bankroll fraction)
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
        NOTE: Sizing now uses Kelly bankroll fraction (auto-compounds via balance).
        consecutive_wins/losses persist for stats/logging."""
        self.market_wins = 0
        self.market_losses = 0
        # consecutive_wins/losses intentionally NOT reset — useful for stats
        # Clear per-market pause (but NOT daily hard stop)
        if not self.is_daily_stopped:
            self.is_paused = False
            self.pause_reason = None
        log.warning(
            f"[SESSION] Market reset: bankroll=${self.current_balance_usd:.2f} streak={self.consecutive_wins}W "
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
            f"bankroll=${self.current_balance_usd:.2f} "
            f"W/L={self.total_wins}/{self.total_losses} "
            f"streak={self.consecutive_wins}W/{self.consecutive_losses}L"
        )

    def _scale_up(self):
        """Win: legacy counter (sizing now uses Kelly bankroll fraction)."""
        self.current_contracts = min(self.current_contracts + CONTRACT_INCREMENT, MAX_CONTRACTS)

    def _scale_down(self):
        """Loss: legacy counter (sizing now uses Kelly bankroll fraction)."""
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
    Tracks SHIB spot price over a rolling window to detect trends.
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
        - YES bet = we think SHIB will stay ABOVE lo (or in range)
        - NO bet = we think SHIB will stay BELOW hi (or in range)
        - If SHIB is trending UP strongly and we want NO → against trend
        - If SHIB is trending DOWN strongly and we want YES → against trend
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
          EARLY (15-25 min): Require active trend match — SHIB still has time to swing.
          MID   (8-15 min): Allow flat trend — stable high prob is enough certainty.
          LATE  (<8 min):  Skip trend entirely — probability IS the outcome now.
        Always: Trend matching our side = GO. Trend against us = BLOCK (unless late).
        """
        if not PROB_TREND_ENTRY_ENABLED:
            return False, "trend_entry_disabled"

        # LATE window (<3 min): probability speaks for itself, skip trend check
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

        # MID window (3-5 min): flat trend is acceptable — stable certainty
        if secs_to_close <= PROB_EARLY_ENTRY_SECONDS and direction == "flat":
            return True, f"mid_flat({current_prob:.0%}/{seconds:.0f}s, {secs_to_close}s left)"

        # EARLY window (5-7 min): flat is NOT enough, need active trend match
        if direction == "flat":
            return False, f"early_need_trend({current_prob:.0%}, flat/{secs_to_close}s left)"

        # Trend is AGAINST us → BLOCK
        return False, f"trend_against({direction}/{change:+.2f})"

    def side_confirmed_for(self, side: str, min_prob: float = 0.85) -> float:
        """
        How many seconds has the probability been consistently on this side
        above min_prob? Returns 0 if no confirmation.
        Used to prevent snap entries on transient orderbook spikes.
        """
        if len(self.samples) < 3:
            return 0.0
        now = self.samples[-1][0]
        confirmed_since = now
        for t, p in reversed(self.samples):
            p_for_side = p if side == "yes" else (1.0 - p)
            if p_for_side < min_prob:
                break
            confirmed_since = t
        return now - confirmed_since

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
    # Windowed peak tracking: list of (timestamp, prob) for rolling max computation
    # Avoids false reversal signals from thin-book spikes ratcheting up all-time peak
    prob_history: list = field(default_factory=list)  # List[Tuple[float, float]]

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
        # Gradual transition from model-weighted to market-weighted across buy window:
        #   >420s: MODEL_BLEND_ALPHA (for observation — model still useful for trend)
        #   60-420s: linear ramp from MODEL_BLEND_ALPHA down to 0% (market taking over)
        #   <60s: 100% market (book IS the probability)
        #
        # KEY FIX: The old 75% model weight at >120s was catastrophic. At T-400s with
        # a 15-min market, the BS model says 50/50 (σ√t > boundary gap), but the market
        # is 95% on one side. 75% model weight drags blend to 62% — below all thresholds.
        # Now we ramp from 420s→60s so the market signal dominates in the buy window.
        if secs_to_close <= 60:
            alpha = 0.0   # 100% market — book IS the probability in the last minute
        elif secs_to_close <= BUY_START_SECONDS:
            # Linear ramp across buy window: at BUY_START alpha=MODEL_BLEND_ALPHA, at 60s alpha=0
            alpha = float(MODEL_BLEND_ALPHA) * (secs_to_close - 60) / float(BUY_START_SECONDS - 60)
        else:
            alpha = float(MODEL_BLEND_ALPHA)  # Outside buy window: model for observation
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
        effective_prob_min = PROB_EARLY_MIN   # >5min: need 92%+
    elif secs_to_close > PROB_MID_ENTRY_SECONDS:
        effective_prob_min = PROB_MID_MIN     # 3-5min: need 88%+
    else:
        effective_prob_min = PROB_MIN         # <3min: 85%+ — market has priced in the outcome
        
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

    # MARKET CONVICTION OVERRIDE: When the book shows ≥85% on one side inside the
    # buy window, override the blend probability to trust the market.  The BS model
    # is nearly useless at >5 min (σ√t > boundary gap → 50/50), but the market has
    # already priced the outcome.  This prevents the model from blocking trades the
    # orderbook clearly supports.
    MARKET_CONVICTION_THRESHOLD = 0.85
    if p_mkt is not None and secs_to_close <= BUY_START_SECONDS:
        p_yes_mkt = float(p_mkt)
        p_no_mkt = 1.0 - p_yes_mkt
        if p_yes_mkt >= MARKET_CONVICTION_THRESHOLD and p_yes_blend < p_yes_mkt:
            log.info(
                f"[MKT CONVICTION] YES: book={p_yes_mkt:.1%} > blend={p_yes_blend:.1%}, "
                f"overriding blend to market"
            )
            p_yes_blend = p_yes_mkt
            p_no_blend = 1.0 - p_yes_blend
            edge_yes = compute_edge(p_yes_blend, yes_px, FEE_CENTS_PER_CONTRACT) if yes_px is not None else -1e9
            edge_no = compute_edge(p_no_blend, no_px, FEE_CENTS_PER_CONTRACT) if no_px is not None else -1e9
            # Re-evaluate ok_yes/ok_no with new blend
            ok_yes = (
                yes_px is not None and ok_book_yes
                and (p_yes_blend >= effective_prob_min) and (edge_yes >= EDGE_MIN)
                and (yes_px <= MAX_ENTRY_PRICE_CENTS) and div_gate_yes
            )
            ok_no = (
                no_px is not None and ok_book_no
                and (p_no_blend >= effective_prob_min) and (edge_no >= EDGE_MIN)
                and (no_px <= MAX_ENTRY_PRICE_CENTS) and div_gate_no
            )
        elif p_no_mkt >= MARKET_CONVICTION_THRESHOLD and p_no_blend < p_no_mkt:
            log.info(
                f"[MKT CONVICTION] NO: book={p_no_mkt:.1%} > blend={p_no_blend:.1%}, "
                f"overriding blend to market"
            )
            p_no_blend = p_no_mkt
            p_yes_blend = 1.0 - p_no_blend
            edge_yes = compute_edge(p_yes_blend, yes_px, FEE_CENTS_PER_CONTRACT) if yes_px is not None else -1e9
            edge_no = compute_edge(p_no_blend, no_px, FEE_CENTS_PER_CONTRACT) if no_px is not None else -1e9
            # Re-evaluate ok_yes/ok_no with new blend
            ok_yes = (
                yes_px is not None and ok_book_yes
                and (p_yes_blend >= effective_prob_min) and (edge_yes >= EDGE_MIN)
                and (yes_px <= MAX_ENTRY_PRICE_CENTS) and div_gate_yes
            )
            ok_no = (
                no_px is not None and ok_book_no
                and (p_no_blend >= effective_prob_min) and (edge_no >= EDGE_MIN)
                and (no_px <= MAX_ENTRY_PRICE_CENTS) and div_gate_no
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

        # LOCKED BOOK HANDLING: When ask is None (nobody selling), but bid is
        # high (≥95¢), the book is "locked" — outcome is decided, just no sellers.
        # Place a limit buy at the bid price to join the queue.  If anyone market-
        # sells, we get filled.  If not, we simply don't fill — zero risk.
        # This turns "no ask to hit" from "can't trade" into "join the queue."
        if yes_px is None and yes_bid is not None and yes_bid >= SETTLEMENT_LOCK_MIN_BID:
            yes_px = clamp_int(yes_bid, 1, 99)
            edge_yes = compute_edge(p_yes_blend, yes_px, FEE_CENTS_PER_CONTRACT)
            log.info(f"[LOCKED BOOK] YES: no ask, using bid={yes_bid}¢ as limit price")
        if no_px is None and no_bid is not None and no_bid >= SETTLEMENT_LOCK_MIN_BID:
            no_px = clamp_int(no_bid, 1, 99)
            edge_no = compute_edge(p_no_blend, no_px, FEE_CENTS_PER_CONTRACT)
            log.info(f"[LOCKED BOOK] NO: no ask, using bid={no_bid}¢ as limit price")

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

    # YES_ONLY: Master one direction before adding the other.
    # Block all NO entries — overrides high-certainty and settlement lock too.
    if YES_ONLY and ok_no and not ok_yes:
        log.info(f"[YES_ONLY] Blocking NO entry (edge={edge_no:.4f} prob={p_no_blend:.1%}) — YES_ONLY mode")
    if YES_ONLY:
        ok_no = False

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
def _spot_is_safe(side: str, spot: float, lo: Optional[float], hi: Optional[float],
                  secs_to_close: int) -> Tuple[bool, float]:
    """
    Check if SHIB spot price is safely on our side of the market boundary.
    Returns (is_safe, buffer_distance_usd).

    This is THE key check: if SHIB is on our side, the book is lying.
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
        # YES wins if SHIB stays ABOVE lo. Safe if spot > lo + buffer.
        distance = spot - lo
        return distance >= buffer, distance
    elif side == "no" and hi is not None:
        # NO wins if SHIB stays BELOW hi (range market). Safe if spot < hi - buffer.
        distance = hi - spot
        return distance >= buffer, distance
    elif side == "no" and lo is not None:
        # NO wins if SHIB drops BELOW lo (up-or-down market, no hi).
        # Safe if spot < lo - buffer (SHIB is well below the strike).
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

    KEY PRINCIPLE: Before any bail trigger fires, check if SHIB is on our side
    of the boundary. If it is, the book is lying — HOLD. Only bail when
    SHIB has actually moved against us.

    BANKROLL PROTECTION: Never lose more than 5% of balance or 50% of position
    cost on a single trade. This fires before SHIB check — no position justifies
    blowing up the bankroll.

    Returns: (should_dump, reason)
    """
    if not ENABLE_DUMP:
        return False, None

    if secs_to_close < DUMP_MIN_TIME_REMAINING:
        return False, "too_close_to_settlement"

    # LATE-ENTRY HOLD: if <60s to close, outcome is mostly decided.
    # Hold to settlement — don't let dump logic sell a near-certain winner.
    # For earlier entries, normal dump logic applies — SHIB can still move.
    # CRITICAL FIX: Even within hold window, if SHIB is near the boundary,
    # allow dump logic to run. Blindly holding while SHIB drifts toward the
    # strike is how big losses happen.
    HOLD_TO_SETTLE_SECONDS = 60  # Only suppress dumps in the last 60s (wider for hourly)
    HOLD_SPOT_DANGER_BUFFER = 0.0000003  # If SHIB is within ~1.5% of boundary, DON'T suppress dumps
    if st.entry_time > 0 and secs_to_close <= HOLD_TO_SETTLE_SECONDS:
        # Check if SHIB is dangerously close to boundary — if so, let dump logic run
        btc_safe_for_hold, btc_hold_dist = _spot_is_safe(st.side, spot, lo, hi, secs_to_close)
        if not btc_safe_for_hold or btc_hold_dist < HOLD_SPOT_DANGER_BUFFER:
            # SHIB is near the boundary — DON'T suppress dumps, let normal logic decide
            log.warning(
                f"[HOLD OVERRIDE] SHIB near boundary (dist={btc_hold_dist:.10f} < {HOLD_SPOT_DANGER_BUFFER:.10f}) "
                f"with {secs_to_close}s left — allowing dump checks"
            )
            # Fall through to normal dump logic below
        else:
            # SHIB is safely on our side — hold to settlement
            # Still bail on catastrophic loss (bankroll protection)
            if st.entry_price_cents is not None and st.qty > 0 and current_balance_usd > 0:
                exit_price_est = int((p_yes_blend if st.side == "yes" else p_no_blend) * 100)
                loss_per_contract = st.entry_price_cents - exit_price_est
                total_loss_usd = (loss_per_contract * st.qty) / 100.0
                max_loss_balance = current_balance_usd * DUMP_MAX_LOSS_FRACTION_OF_BALANCE
                if total_loss_usd > max_loss_balance:
                    return True, f"late_entry_bankroll_cap_${total_loss_usd:.2f}>${max_loss_balance:.2f}"
            return False, f"late_entry_hold_to_settle_t={secs_to_close}s_dist=${btc_hold_dist:.0f}"

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

    # Update peak probability tracking (all-time, for logging)
    if current_prob > st.peak_prob_for_side:
        st.peak_prob_for_side = current_prob

    # Record prob sample for windowed peak tracking
    now_ts = time.time()
    st.prob_history.append((now_ts, current_prob))
    # Prune old samples outside the peak window
    cutoff_ts = now_ts - DUMP_PEAK_WINDOW_SECONDS
    st.prob_history = [(t, p) for t, p in st.prob_history if t >= cutoff_ts]

    # Windowed peak: max prob over last N seconds (avoids thin-book spike ratcheting)
    windowed_peak = max(p for _, p in st.prob_history) if st.prob_history else current_prob
    drop_from_peak = windowed_peak - current_prob
    in_settling = time_in_trade < DUMP_PROACTIVE_AFTER_SECONDS

    # Rapid drop detection: check if prob dropped fast in the last 10s
    rapid_cutoff = now_ts - DUMP_RAPID_DROP_WINDOW_SECONDS
    rapid_samples = [(t, p) for t, p in st.prob_history if t >= rapid_cutoff]
    rapid_drop = 0.0
    if len(rapid_samples) >= 3:  # Need at least 3 samples for meaningful signal
        rapid_peak = max(p for _, p in rapid_samples)
        rapid_drop = rapid_peak - current_prob

    # =============================================================
    # === BANKROLL-PROPORTIONAL STOP: fires BEFORE SHIB check ===
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
                    f"overrides SHIB safety, protecting bankroll"
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
    # === CATASTROPHIC STOP: absolute backstop, fires BEFORE SHIB check ===
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
                f"overrides SHIB safety, capping damage"
            )
            return True, f"catastrophic_{catastrophic_loss}c_per_contract"

    # =============================================================
    # === SHIB SAFETY CHECK: THE MASTER OVERRIDE ===
    # If SHIB is on our side of the boundary, DO NOT BAIL.
    # The book can spike, the probability can drop on a thin book,
    # but if SHIB is safely on our side with time left, we WIN.
    # =============================================================
    btc_safe, btc_distance = _spot_is_safe(st.side, spot, lo, hi, secs_to_close)
    if btc_safe:
        # SHIB is on our side — this is a winner. Hold no matter what the book says.
        phase = "settling" if in_settling else "active"
        return False, (
            f"btc_safe_{phase}_dist=${btc_distance:.0f}_"
            f"prob={current_prob:.0%}_peak={st.peak_prob_for_side:.0%}_drop={drop_from_peak:.0%}"
        )

    # === Below here: SHIB is NOT safely on our side — bail checks apply ===

    # --- RAPID DROP BAIL: SHIB is against us AND prob dropped fast ---
    # Emergency exit: if prob dropped >4% in the last 10s, SHIB is actively moving
    # against us. Fire even during settling period — speed matters here.
    if rapid_drop >= DUMP_RAPID_DROP_THRESHOLD:
        log.warning(
            f"[BAIL RAPID DROP] SHIB NOT safe (dist={btc_distance:.10f}) AND "
            f"prob dropped {rapid_drop:.1%} in last {DUMP_RAPID_DROP_WINDOW_SECONDS}s "
            f"(current={current_prob:.1%}) — emergency exit"
        )
        return True, f"rapid_drop_{rapid_drop:.1%}_in_{DUMP_RAPID_DROP_WINDOW_SECONDS}s"

    # --- HARD P&L STOP: SHIB is against us AND losing big ---
    if st.entry_price_cents is not None:
        exit_price_est = int(current_prob * 100)
        unrealized_loss_per_contract = st.entry_price_cents - exit_price_est
        if unrealized_loss_per_contract >= DUMP_MAX_LOSS_CENTS_PER_CONTRACT:
            log.warning(
                f"[BAIL HARD STOP] SHIB NOT safe (dist={btc_distance:.10f}) AND "
                f"losing ~{unrealized_loss_per_contract}¢/contract "
                f"(entry={st.entry_price_cents}¢ est_exit={exit_price_est}¢) — salvaging"
            )
            return True, f"hard_stop_{unrealized_loss_per_contract}c_per_contract"

    # --- REVERSAL BAIL: SHIB is against us AND prob has dropped significantly ---
    # Now fires during settling too (with wider threshold) — was completely blocked before.
    # Windowed peak (last 30s) is used instead of all-time peak to avoid false signals.
    if DUMP_ON_PROB_REVERSAL and DUMP_EARLY_EXIT_ENABLED:
        gain_above_entry = windowed_peak - entry_prob
        if gain_above_entry >= DUMP_PROFIT_TIGHTEN_ABOVE_ENTRY:
            effective_threshold = DUMP_REVERSAL_THRESHOLD_PROFIT
        else:
            effective_threshold = DUMP_REVERSAL_THRESHOLD

        # During settling (first 30s), use wider threshold — only bail on big reversals
        if in_settling:
            effective_threshold = DUMP_REVERSAL_THRESHOLD_SETTLING

        if drop_from_peak >= effective_threshold:
            phase_label = "settling" if in_settling else "active"
            log.warning(
                f"[BAIL REVERSAL] SHIB NOT safe (dist={btc_distance:.10f}) AND "
                f"prob dropped {drop_from_peak:.1%} from windowed peak "
                f"({windowed_peak:.1%} -> {current_prob:.1%}) phase={phase_label} "
                f"threshold={effective_threshold:.1%} — salvaging"
            )
            return True, f"reversal_{current_prob:.0%}_from_wpeak_{windowed_peak:.0%}_{phase_label}"

    # --- FLOOR: SHIB is against us AND prob is at coin-flip ---
    if current_prob < DUMP_PROB_FLIP:
        log.warning(
            f"[BAIL FLOOR] SHIB NOT safe (dist={btc_distance:.10f}) AND "
            f"prob={current_prob:.1%} < {DUMP_PROB_FLIP:.0%} — salvaging"
        )
        return True, f"floor_{current_prob:.0%}<{DUMP_PROB_FLIP:.0%}"

    # Still holding
    phase = "settling" if in_settling else "active"
    return False, (
        f"{phase}_prob={current_prob:.0%}_wpeak={windowed_peak:.0%}_peak={st.peak_prob_for_side:.0%}"
        f"_drop={drop_from_peak:.0%}_rdrop={rapid_drop:.0%}_shib_dist={btc_distance:.10f}"
    )


def kelly_fraction(p_true: float, entry_cents: int) -> float:
    """Kelly criterion fraction for binary Kalshi contracts.

    Returns the optimal fraction of bankroll to wager.
    f* = p_true - (1 - p_true) / b
    where b = net_payout / wager = (100 - entry_cents) / entry_cents.

    At p_true=0.99, entry=97¢: f* = 0.99 - 0.01/0.0309 = 0.666 (66.6%)
    At p_true=0.99, entry=99¢: f* = 0.99 - 0.01/0.0101 = 0.000 (no edge)
    At p_true=0.995, entry=97¢: f* = 0.995 - 0.005/0.0309 = 0.833 (83.3%)
    """
    if entry_cents <= 0 or entry_cents >= 100 or p_true <= 0 or p_true >= 1:
        return 0.0
    price = entry_cents / 100.0
    b = (1.0 - price) / price  # net odds: win $0.03 on $0.97 bet = 0.0309
    q = 1.0 - p_true
    f = p_true - q / b
    return max(0.0, f)


def compute_qty_from_bankroll(
    available_usd: Optional[float],
    entry_cents: int,
    edge_net: float,
    p_gate: float,
    session: Optional[SessionState] = None
) -> int:
    """Compute order quantity using Kelly criterion bankroll sizing.

    Primary model: Kelly fraction of available bankroll.
    As you win, bankroll grows → buy more contracts automatically.
    As you lose, bankroll shrinks → buy fewer (self-protecting).
    No streak counter needed — compounding is in the math.
    """
    if entry_cents is None or entry_cents <= 0:
        log.warning(f"[SIZE] Invalid entry_cents={entry_cents}, falling back to MIN_CONTRACTS={MIN_CONTRACTS}")
        return MIN_CONTRACTS

    if available_usd is None or available_usd <= 0:
        log.warning(f"[SIZE] available_usd is None or <=0, falling back to MIN_CONTRACTS={MIN_CONTRACTS}")
        return MIN_CONTRACTS

    if available_usd < MIN_FREE_USD_TO_TRADE:
        log.warning(f"[SIZE] available_usd ${available_usd:.2f} < MIN_FREE_USD_TO_TRADE ${MIN_FREE_USD_TO_TRADE}, returning 0")
        return 0

    cost_per = float(entry_cents) / 100.0

    # SETTLEMENT LOCK BOOST: REMOVED.
    # Previously boosted p_gate to 0.995 for Kelly sizing when p>=0.95 at entry>=97¢.
    # This caused massive oversizing — Kelly thought we were 99.5% certain when we
    # were really 95%, leading to 40%+ bankroll bets that wiped out dozens of small wins.
    # Now Kelly uses the actual probability. If edge is real, Kelly sizes appropriately.
    # If edge is tiny, Kelly sizes small — which is correct behavior.
    sizing_p = p_gate

    # PRIMARY: Kelly criterion sizing
    kf = kelly_fraction(sizing_p, entry_cents)
    fraction = kf * KELLY_MULTIPLIER  # Quarter-Kelly by default

    # Floor: if we decided to trade, commit at least KELLY_FLOOR_FRACTION
    if fraction < KELLY_FLOOR_FRACTION:
        log.info(
            f"[SIZE] Kelly fraction {fraction:.3f} (raw={kf:.3f} × {KELLY_MULTIPLIER}) "
            f"below floor, using {KELLY_FLOOR_FRACTION:.2f}"
        )
        fraction = KELLY_FLOOR_FRACTION

    # Cap: never risk more than KELLY_CAP_FRACTION in one trade
    if fraction > KELLY_CAP_FRACTION:
        fraction = KELLY_CAP_FRACTION

    # Convert fraction to contract count
    target_qty = int(available_usd * fraction / cost_per)

    # SETTLEMENT LOSS CAP: worst case = lose entire entry cost at settlement.
    # Cap so that worst-case loss never exceeds MAX_SETTLEMENT_LOSS_FRACTION of balance.
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

    log.info(
        f"[SIZE] Kelly bankroll sizing: contracts={qty} kelly_f={kf:.3f} "
        f"quarter_kelly={fraction:.3f} p_gate={p_gate:.3f} entry={entry_cents}¢ "
        f"bankroll=${available_usd:.2f} risking=${qty * cost_per:.2f} "
        f"({qty * cost_per / available_usd:.1%} of bankroll)"
    )

    return qty


def compute_scalp_qty(
    available_usd: float,
    entry_cents: int,
    distance_usd: float,
    existing_position_cost_usd: float = 0.0,
    current_balance_usd: float = 0.0,
) -> int:
    """Compute scalp order quantity based on distance from strike.

    Farther from strike = safer = more contracts.
    Uses SCALP_DISTANCE_TIERS to determine bankroll fraction.

    Combined risk cap: the scalp + existing position cost must not exceed
    COMBINED_POSITION_RISK_CAP of the current balance.  This prevents
    stacking a main entry (8% risk) and a scalp (5% risk) into 13%+ total
    exposure on a single market.
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

    # Safety cap 1: worst-case loss (all contracts go to $0) must not exceed SCALP_MAX_LOSS_FRACTION
    max_loss_usd = available_usd * SCALP_MAX_LOSS_FRACTION
    max_qty_for_loss = int(max_loss_usd / cost_per)
    if target_qty > max_qty_for_loss:
        log.info(
            f"[SCALP SIZE] Loss cap: {target_qty} -> {max_qty_for_loss} contracts "
            f"(max loss ${max_loss_usd:.2f} = {SCALP_MAX_LOSS_FRACTION:.0%} of ${available_usd:.2f})"
        )
        target_qty = max_qty_for_loss

    # Safety cap 2: COMBINED position risk cap (main entry + scalp)
    # If we already have a position costing $X, the scalp can only use
    # whatever room remains under the combined cap.
    if current_balance_usd > 0 and existing_position_cost_usd > 0:
        combined_cap_usd = current_balance_usd * COMBINED_POSITION_RISK_CAP
        remaining_risk_budget = max(0.0, combined_cap_usd - existing_position_cost_usd)
        max_qty_combined = int(remaining_risk_budget / cost_per)
        if target_qty > max_qty_combined:
            log.info(
                f"[SCALP SIZE] Combined cap: {target_qty} -> {max_qty_combined} contracts "
                f"(existing=${existing_position_cost_usd:.2f} + scalp must stay under "
                f"{COMBINED_POSITION_RISK_CAP:.0%} of ${current_balance_usd:.2f} = "
                f"${combined_cap_usd:.2f}, room=${remaining_risk_budget:.2f})"
            )
            target_qty = max_qty_combined

    target_qty = max(0, min(target_qty, MAX_CONTRACTS))

    if target_qty > 0:
        expected_profit = target_qty * (100 - entry_cents) / 100.0
        max_loss = target_qty * cost_per
        log.info(
            f"[SCALP SIZE] qty={target_qty} @ {entry_cents}¢ "
            f"(dist=${distance_usd:.0f}, frac={scalp_fraction:.0%}, "
            f"profit=${expected_profit:.2f}, risk=${max_loss:.2f}"
            f", existing_pos=${existing_position_cost_usd:.2f})"
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
    existing_position_cost_usd: float = 0.0,
    current_balance_usd: float = 0.0,
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

    # Core safety: how far is SHIB from the strike?
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

    # Volatility sanity check: can SHIB actually move `distance` in `secs_to_close`?
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

    qty = compute_scalp_qty(
        available_usd, ask_price, distance,
        existing_position_cost_usd=existing_position_cost_usd,
        current_balance_usd=current_balance_usd,
    )
    if qty <= 0:
        return False, 0, None, f"qty=0(existing_pos=${existing_position_cost_usd:.2f})"

    reason = (
        f"dist=${distance:.0f} 3σ√t=${max_expected_move:.0f} "
        f"prob={p_blend:.1%} price={ask_price}¢ qty={qty} existing_pos=${existing_position_cost_usd:.2f}"
    )
    return True, qty, ask_price, reason


# =====================================================================
# BRACKET ARBITRAGE — buy all 3, dump 2, hold 1
# =====================================================================

@dataclass
class BracketPosition:
    """Tracks a single bracket within a bracket arb set."""
    market_ticker: str
    event_ticker: str
    floor_strike: Optional[float]
    cap_strike: Optional[float]
    qty: int = 0
    entry_price_cents: Optional[int] = None
    order_id: Optional[str] = None
    is_dumped: bool = False
    dump_price_cents: Optional[int] = None
    market_obj: Optional[Dict[str, Any]] = None


@dataclass
class BracketArbState:
    """Tracks a bracket arb across all 3 (or more) sibling brackets."""
    event_ticker: Optional[str] = None
    brackets: List[BracketPosition] = field(default_factory=list)
    is_active: bool = False       # True once we've bought into brackets
    entry_time: float = 0.0
    total_entry_cost_cents: int = 0  # Sum of (entry_price * qty) across all brackets
    total_qty_per_bracket: int = 0   # Contracts per bracket (same for all)
    winner_ticker: Optional[str] = None  # Market ticker of the identified winner
    all_settled: bool = False

    def reset(self):
        self.event_ticker = None
        self.brackets = []
        self.is_active = False
        self.entry_time = 0.0
        self.total_entry_cost_cents = 0
        self.total_qty_per_bracket = 0
        self.winner_ticker = None
        self.all_settled = False

    def total_cost_cents(self) -> int:
        """Total cost in cents per contract-set (sum of all entry prices)."""
        return sum(b.entry_price_cents for b in self.brackets if b.entry_price_cents is not None)

    def total_invested_usd(self) -> float:
        """Total USD invested across all brackets."""
        return sum(
            (b.entry_price_cents or 0) * b.qty / 100.0
            for b in self.brackets
        )

    def undumped_brackets(self) -> List[BracketPosition]:
        return [b for b in self.brackets if not b.is_dumped and b.qty > 0]

    def dumped_recovery_cents(self) -> int:
        """Total cents recovered from dumping losers."""
        return sum(
            (b.dump_price_cents or 0) * b.qty
            for b in self.brackets if b.is_dumped
        )


def find_event_brackets(
    markets: List[Dict[str, Any]],
    event_ticker: str,
) -> List[Dict[str, Any]]:
    """Find all sibling bracket markets for the same event, sorted by floor_strike."""
    siblings = []
    for m in markets:
        ev = m.get("event_ticker") or (m.get("event", {}) or {}).get("ticker") or ""
        status = str(m.get("status", "")).lower()
        if ev == event_ticker and status in ("open", "active"):
            siblings.append(m)

    # Sort by floor_strike ascending (lowest bracket first)
    def sort_key(m):
        lo, hi = market_bounds_usd(m)
        return lo if lo is not None else float("inf")

    siblings.sort(key=sort_key)
    return siblings


def evaluate_bracket_arb(
    brackets: List[Dict[str, Any]],
    client,
    available_usd: float,
    secs_to_close: int,
) -> Tuple[bool, List[Tuple[str, int, Dict]], str]:
    """Evaluate whether bracket arbitrage is available.

    Returns: (should_enter, [(market_ticker, ask_cents, market_obj), ...], reason)
    """
    if not BRACKET_ARB_ENABLED:
        return False, [], "disabled"

    n = len(brackets)
    if n < BRACKET_ARB_MIN_BRACKETS:
        return False, [], f"only_{n}_brackets(need_{BRACKET_ARB_MIN_BRACKETS})"

    if secs_to_close > BRACKET_ARB_MAX_SECONDS:
        return False, [], f"too_early({secs_to_close}s>{BRACKET_ARB_MAX_SECONDS}s)"

    if secs_to_close < BRACKET_ARB_MIN_SECONDS:
        return False, [], f"too_late({secs_to_close}s<{BRACKET_ARB_MIN_SECONDS}s)"

    if available_usd < MIN_FREE_USD_TO_TRADE:
        return False, [], f"low_cash(${available_usd:.2f})"

    # Fetch orderbooks for all brackets
    bracket_prices = []  # (market_ticker, yes_ask_cents, market_obj)
    total_ask_cents = 0

    for m in brackets:
        ticker = m.get("ticker") or m.get("market_ticker") or ""
        try:
            ob = client.request("GET", f"/markets/{ticker}/orderbook")
            yes_bid, yes_ask, no_bid, no_ask = parse_best_yes_no(ob)
        except Exception as e:
            log.info(f"[BRACKET_ARB] Failed to fetch orderbook for {ticker}: {e}")
            return False, [], f"ob_fetch_failed({ticker})"

        # Use yes_ask if available, otherwise derive from no_bid
        if yes_ask is not None:
            ask_cents = int(yes_ask)
        elif no_bid is not None:
            ask_cents = 100 - int(no_bid)
        else:
            return False, [], f"no_ask({ticker})"

        if ask_cents > BRACKET_ARB_MAX_SINGLE_PRICE:
            return False, [], f"single_too_high({ticker}={ask_cents}¢>{BRACKET_ARB_MAX_SINGLE_PRICE}¢)"

        if ask_cents < 1:
            ask_cents = 1

        bracket_prices.append((ticker, ask_cents, m))
        total_ask_cents += ask_cents

    # Check if total is cheap enough
    if total_ask_cents > BRACKET_ARB_SOFT_TOTAL_CENTS:
        return False, [], f"total_too_high({total_ask_cents}¢>{BRACKET_ARB_SOFT_TOTAL_CENTS}¢)"

    guaranteed_arb = total_ask_cents <= 100
    cheap_enough = total_ask_cents <= BRACKET_ARB_MAX_TOTAL_CENTS

    reason_parts = [f"total={total_ask_cents}¢"]
    for ticker, ask, _ in bracket_prices:
        short_ticker = ticker.split("-")[-1] if "-" in ticker else ticker
        reason_parts.append(f"{short_ticker}={ask}¢")

    if guaranteed_arb:
        reason_parts.append("GUARANTEED_ARB")
    elif cheap_enough:
        reason_parts.append("CHEAP_ARB")
    else:
        reason_parts.append("SOFT_ARB(dump_to_recover)")

    reason = " ".join(reason_parts)
    log.info(f"[BRACKET_ARB] Evaluating: {reason}")

    return True, bracket_prices, reason


def compute_bracket_arb_qty(
    available_usd: float,
    bracket_prices: List[Tuple[str, int, Dict]],
) -> int:
    """Compute how many contract-sets to buy (same qty for each bracket).

    One contract-set = 1 YES on each bracket. Cost = sum of all asks.
    Payout = $1.00 (exactly one bracket wins).
    """
    total_cost_per_set = sum(ask for _, ask, _ in bracket_prices) / 100.0  # USD per set
    if total_cost_per_set <= 0:
        return 0

    budget = available_usd * BRACKET_ARB_BANKROLL_FRACTION
    max_sets = int(budget / total_cost_per_set)

    # Also cap by MAX_CONTRACTS per bracket
    max_sets = min(max_sets, MAX_CONTRACTS)

    # Ensure at least 1 set if we can afford it
    if max_sets <= 0 and budget >= total_cost_per_set:
        max_sets = 1

    if max_sets > 0:
        profit_per_set = 1.00 - total_cost_per_set
        log.info(
            f"[BRACKET_ARB] Sizing: {max_sets} sets @ ${total_cost_per_set:.3f}/set "
            f"profit/set=${profit_per_set:.3f} total_risk=${max_sets * total_cost_per_set:.2f} "
            f"({max_sets * total_cost_per_set / available_usd:.1%} of bankroll)"
        )

    return max_sets


def manage_bracket_arb_positions(
    arb: BracketArbState,
    client,
    spot: float,
    secs_to_close: int,
    sigma: float,
) -> None:
    """Monitor bracket arb positions: identify winner, dump losers, hold winner.

    Called every poll cycle while bracket arb is active.
    """
    if not arb.is_active or not arb.brackets:
        return

    undumped = arb.undumped_brackets()
    if len(undumped) <= 1:
        # Already down to the winner (or no positions left)
        return

    # Evaluate each undumped bracket: is it clearly a loser or winner?
    bracket_probs = []
    for bp in undumped:
        lo = bp.floor_strike
        hi = bp.cap_strike
        if lo is not None and hi is not None:
            # Range bracket: YES wins if lo < spot < hi
            t_eff = max(5.0, float(min(secs_to_close, 120)))
            sd = sigma * math.sqrt(t_eff)
            if sd > 0:
                p_yes = prob_yes_in_range(spot, lo, hi, sd)
            else:
                p_yes = 1.0 if lo < spot < hi else 0.0
        elif lo is not None:
            # "Above" bracket: YES wins if spot > lo
            t_eff = max(5.0, float(min(secs_to_close, 120)))
            sd = sigma * math.sqrt(t_eff)
            if sd > 0:
                z = (spot - lo) / sd
                p_yes = _norm_cdf(z)
            else:
                p_yes = 1.0 if spot > lo else 0.0
        elif hi is not None:
            # "Below" bracket: YES wins if spot < hi
            t_eff = max(5.0, float(min(secs_to_close, 120)))
            sd = sigma * math.sqrt(t_eff)
            if sd > 0:
                z = (hi - spot) / sd
                p_yes = _norm_cdf(z)
            else:
                p_yes = 1.0 if spot < hi else 0.0
        else:
            p_yes = 0.33  # Can't determine, assume equal

        bracket_probs.append((bp, p_yes))

    # Sort by probability descending — highest prob is the likely winner
    bracket_probs.sort(key=lambda x: x[1], reverse=True)

    winner_bp, winner_prob = bracket_probs[0]
    losers = bracket_probs[1:]

    log.info(
        f"[BRACKET_ARB] Monitoring: winner={winner_bp.market_ticker.split('-')[-1]} "
        f"p={winner_prob:.1%} | losers: "
        + ", ".join(f"{bp.market_ticker.split('-')[-1]}={p:.1%}" for bp, p in losers)
        + f" | t={secs_to_close}s spot={spot}"
    )

    # Dump losers when their probability drops below threshold
    for bp, p_yes in losers:
        if p_yes < BRACKET_ARB_DUMP_PROB_THRESHOLD and not bp.is_dumped:
            log.warning(
                f"[BRACKET_ARB] DUMPING loser {bp.market_ticker} "
                f"p={p_yes:.1%}<{BRACKET_ARB_DUMP_PROB_THRESHOLD:.0%} "
                f"entry={bp.entry_price_cents}¢ qty={bp.qty}"
            )
            try:
                # Market sell at 1¢ (accept any bid)
                payload = build_order_payload(
                    market_ticker=bp.market_ticker,
                    action="sell",
                    side="yes",
                    price_cents=1,
                    count=bp.qty,
                    post_only=False,
                )
                if not DRY_RUN:
                    oid = place_order(client, payload)
                    log.warning(f"[BRACKET_ARB] Dump order placed: {oid}")
                    # Check fill
                    fill_status, filled = wait_for_fill(client, oid, bp.market_ticker)
                    if fill_status in ("filled", "partial") and filled > 0:
                        # Try to get actual exit price from order
                        try:
                            order_data = get_order(client, oid)
                            exit_cents = order_data.get("yes_price") or order_data.get("no_price") or 1
                        except Exception:
                            exit_cents = 1
                        bp.dump_price_cents = int(exit_cents)
                        bp.is_dumped = True
                        log.warning(
                            f"[BRACKET_ARB] Dumped {bp.market_ticker}: "
                            f"exit={bp.dump_price_cents}¢ loss={(bp.entry_price_cents or 0) - bp.dump_price_cents}¢/contract"
                        )
                    else:
                        log.warning(f"[BRACKET_ARB] Dump not filled for {bp.market_ticker} (status={fill_status})")
                else:
                    bp.is_dumped = True
                    bp.dump_price_cents = 0
                    log.info(f"[BRACKET_ARB] DRY_RUN: would dump {bp.market_ticker}")
            except Exception as e:
                log.warning(f"[BRACKET_ARB] Dump failed for {bp.market_ticker}: {e}")

    # Track the winner
    if winner_prob >= BRACKET_ARB_WINNER_PROB_THRESHOLD:
        arb.winner_ticker = winner_bp.market_ticker
        remaining = arb.undumped_brackets()
        if len(remaining) == 1:
            log.warning(
                f"[BRACKET_ARB] Winner identified: {winner_bp.market_ticker} "
                f"p={winner_prob:.1%} — holding to settlement"
            )


def settle_bracket_arb(
    arb: BracketArbState,
    client,
    session: 'SessionState',
) -> None:
    """Settle a bracket arb — check results and record P&L for all brackets."""
    if not arb.brackets:
        return

    total_pnl_cents = 0
    for bp in arb.brackets:
        if bp.qty == 0 or bp.entry_price_cents is None:
            continue

        if bp.is_dumped:
            # Already exited — P&L = dump_price - entry_price per contract
            pnl = ((bp.dump_price_cents or 0) - bp.entry_price_cents) * bp.qty
            total_pnl_cents += pnl
            continue

        # Still held — check settlement result
        result = None
        try:
            mkt_data = client.request("GET", f"/markets/{bp.market_ticker}")
            mkt_obj = mkt_data.get("market", mkt_data) if isinstance(mkt_data, dict) else {}
            result = mkt_obj.get("result", "").lower()
        except Exception as e:
            log.warning(f"[BRACKET_ARB] Settlement check failed for {bp.market_ticker}: {e}")

        if result == "yes":
            pnl = (100 - bp.entry_price_cents) * bp.qty  # Winner!
        elif result == "no":
            pnl = -bp.entry_price_cents * bp.qty  # Loser (should have been dumped)
        else:
            pnl = 0  # Unknown — will be handled by deferred settlement
            log.warning(f"[BRACKET_ARB] Result not available for {bp.market_ticker}")

        total_pnl_cents += pnl

    pnl_usd = total_pnl_cents / 100.0
    session.record_trade(
        market=f"BRACKET_ARB:{arb.event_ticker}",
        side="yes",
        entry_price=arb.total_cost_cents() if arb.brackets else 0,
        exit_price=100,  # One bracket settles at 100
        qty=arb.total_qty_per_bracket,
        pnl_cents=total_pnl_cents,
        was_dump=False,
    )
    log.warning(
        f"[BRACKET_ARB] Settled {arb.event_ticker}: "
        f"total_pnl={total_pnl_cents}¢ (${pnl_usd:.2f}) "
        f"winner={arb.winner_ticker}"
    )

    arb.all_settled = True


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
        f"DUMP_PROB_FLIP={DUMP_PROB_FLIP} DUMP_PROB_DROP={DUMP_PROB_DROP_PERCENT} "
        f"YES_ONLY={YES_ONLY}"
    )
    log.warning(
        f"[BOOTCFG] ENTRY: fast_lane={PROB_FAST_LANE_THRESHOLD:.0%} (≥{PROB_FAST_LANE_THRESHOLD:.0%} skips trend checks) "
        f"boundary_buffer=${BOUNDARY_BUFFER_USD:.0f} trend_block={TREND_AGAINST_BLOCK}"
    )
    log.warning(
        f"[BOOTCFG] BAIL: grace={DUMP_GRACE_PERIOD_SECONDS}s settling={DUMP_PROACTIVE_AFTER_SECONDS}s "
        f"reversal={DUMP_REVERSAL_THRESHOLD:.0%} hard_stop={DUMP_MAX_LOSS_CENTS_PER_CONTRACT}¢/contract "
        f"shib_buffer_early={DUMP_BTC_SAFE_BUFFER_EARLY:.10f} shib_buffer_late={DUMP_BTC_SAFE_BUFFER_LATE:.10f}"
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
    log.warning("[HEARTBEAT] main() entered — SHIB HOURLY SCALPER is running")

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

    # Bracket arbitrage state
    bracket_arb = BracketArbState()
    _all_event_markets: List[Dict[str, Any]] = []  # Cache of all markets for current event

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

    def refresh_active_market() -> Tuple[str, str, Dict[str, Any], List[Dict[str, Any]]]:
        """Returns (event_ticker, market_ticker, market_obj, all_markets)."""
        if MARKET_OVERRIDE and MARKET_OVERRIDE not in ("<none>", "none", "None", ""):
            mt = MARKET_OVERRIDE
            try:
                snap = client.request("GET", f"/markets/{mt}")
                mobj = snap.get("market") if isinstance(snap, dict) and isinstance(snap.get("market"), dict) else (snap if isinstance(snap, dict) else {})
            except Exception:
                mobj = {}
            ev = EVENT_TICKER if EVENT_TICKER != "<auto>" else "<manual>"
            return ev, mt, mobj, [mobj] if mobj else []

        params = {"series_ticker": SERIES_TICKER, "status": "open", "limit": 200}
        resp = client.request("GET", "/markets", params=params)
        markets = resp.get("markets", []) if isinstance(resp, dict) else []
        if not markets:
            raise RuntimeError(f"No open markets returned for series_ticker={SERIES_TICKER}")
        ev, mt, mobj = pick_active_market(markets)
        return ev, mt, mobj, markets

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

    ev, mt, mobj, _all_event_markets = refresh_active_market()
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
                f"SESSION: pnl=${session.daily_pnl_usd:.2f} bankroll=${session.current_balance_usd:.2f} "
                f"W/L={session.total_wins}/{session.total_losses} "
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
                ev2, mt2, mobj2, _all_event_markets = refresh_active_market()
                if mt2 != st.market:
                    old_market = st.market
                    log.warning(f"[ROLL] {old_market} -> {mt2}")

                    # Record P&L for settled position (if we had one)
                    if st.traded_this_market and st.entry_price_cents is not None and st.side is not None:
                        # Reconcile st.qty with actual position before computing P&L.
                        # Resting orders may not have filled (or only partially filled).
                        try:
                            actual_pos = abs(parse_position_for_market(get_positions(client), old_market))
                            if actual_pos != st.qty:
                                log.warning(
                                    f"[RECON] Position mismatch: st.qty={st.qty} actual={actual_pos} "
                                    f"— using actual for P&L"
                                )
                                st.qty = actual_pos
                            # Cancel any resting orders for this market
                            if getattr(st, 'order_id', None):
                                cancel_order_status(client, st.order_id)
                        except Exception as e:
                            log.warning(f"[RECON] Position check failed: {e} — using st.qty={st.qty}")

                        if st.qty == 0:
                            log.warning(f"[ROLL] No position filled in {old_market} — skipping P&L")
                            session.reset_for_new_market()
                            ev, market_ticker, market_obj = ev2, mt2, mobj2
                            st = BotState(market=mt2)
                            last_meta = now
                            continue

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

                    # Settle bracket arb if active
                    if bracket_arb.is_active:
                        log.warning(f"[BRACKET_ARB] Market rolling — settling bracket arb for {bracket_arb.event_ticker}")
                        settle_bracket_arb(bracket_arb, client, session)
                        bracket_arb.reset()

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
                spot = fetch_shib_spot_usd(http)
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
                                        and not (YES_ONLY and flip_side == "no")  # Don't flip to NO in YES_ONLY mode
                                    )

                                    # Two paths: model agrees (prob >= 60%) or market confident (price >= 80¢)
                                    if can_flip:
                                        market_confident = flip_price >= 80
                                        model_agrees = flip_prob >= FLIP_MIN_PROB
                                        if not market_confident and not model_agrees:
                                            can_flip = False

                                    if can_flip:
                                        flip_edge = flip_prob - (flip_price / 100.0)

                                        # Kelly bankroll sizing for flip
                                        try:
                                            avail_usd, _ = get_balance_usd(client)
                                            if avail_usd and avail_usd > 0:
                                                flip_qty = compute_qty_from_bankroll(
                                                    avail_usd, int(flip_price),
                                                    edge_net=flip_edge, p_gate=flip_prob,
                                                    session=session,
                                                )
                                            else:
                                                flip_qty = MIN_CONTRACTS
                                        except Exception:
                                            flip_qty = MIN_CONTRACTS

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

                                            # Verify flip fill
                                            flip_fill_status, flip_filled = wait_for_fill(client, flip_oid, st.market)
                                            if flip_fill_status in ("filled", "partial") and flip_filled > 0:
                                                st.sm = SM.HOLD
                                                st.side = flip_side
                                                st.entry_price_cents = int(flip_price)
                                                st.entry_time = time.time()
                                                st.entry_model_prob = p_yes_model if flip_side == "yes" else (1.0 - p_yes_model)
                                                st.entry_market_prob = p_mkt
                                                st.entry_spot_price = spot
                                                st.qty = flip_filled  # Actual filled qty
                                                st.peak_prob_for_side = flip_prob
                                                st.prob_history = []  # Reset windowed peak tracking for flipped position
                                                st.has_flipped = True
                                                st.traded_this_market = True
                                                if flip_filled < flip_qty:
                                                    log.warning(f"[FLIP] Partial fill: {flip_filled}/{flip_qty} — canceling remainder")
                                                    cancel_order_status(client, flip_oid)
                                                else:
                                                    log.warning(f"[FLIP] Fill confirmed: {flip_filled} contracts")
                                            else:
                                                log.warning(f"[FLIP] Order NOT filled (status={flip_fill_status}) — canceling")
                                                cancel_order_status(client, flip_oid)
                                                st.sm = SM.DUMPED
                                                st.side = None
                                                st.entry_price_cents = None
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
                                        if YES_ONLY and flip_side == "no":
                                            reason_parts.append("yes_only_mode")
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

                            # Compute existing position cost for combined risk cap
                            existing_pos_cost = 0.0
                            if st.entry_price_cents is not None and st.qty > 0:
                                existing_pos_cost = (st.entry_price_cents * st.qty) / 100.0

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
                                existing_position_cost_usd=existing_pos_cost,
                                current_balance_usd=session.current_balance_usd,
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
                                            scalp_qty = compute_scalp_qty(
                                                scalp_avail, scalp_px, scalp_dist,
                                                existing_position_cost_usd=existing_pos_cost,
                                                current_balance_usd=session.current_balance_usd,
                                            )
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
                                                expected_profit = scalp_qty * (100 - scalp_px) / 100.0
                                                log.warning(
                                                    f"[SCALP] PLACED order={scalp_oid} BUY {st.side.upper()} "
                                                    f"@ {scalp_px}¢ × {scalp_qty} "
                                                    f"(expect +${expected_profit:.2f} at settlement)"
                                                )
                                                # Quick fill check for scalp (less time since we're near close)
                                                scalp_fill_status, scalp_filled = wait_for_fill(client, scalp_oid, st.market)
                                                if scalp_fill_status in ("filled", "partial") and scalp_filled > 0:
                                                    st.has_scalped = True
                                                    log.warning(f"[SCALP] Fill confirmed: {scalp_filled}/{scalp_qty} contracts")
                                                    if scalp_filled < scalp_qty:
                                                        cancel_order_status(client, scalp_oid)
                                                else:
                                                    log.warning(f"[SCALP] NOT filled (status={scalp_fill_status}) — canceling")
                                                    cancel_order_status(client, scalp_oid)
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
        # BRACKET ARBITRAGE — buy all 3 brackets, dump 2 losers, hold 1 winner
        # Runs as an alternative strategy when conditions are right.
        # Does NOT conflict with regular single-market trading (separate state).
        # ============================================================
        if BRACKET_ARB_ENABLED and secs_to_close is not None:
            # --- MANAGE active bracket arb positions ---
            if bracket_arb.is_active:
                spot_arb = fetch_shib_spot_usd(http)
                if spot_arb is not None:
                    sigma_arb = get_sigma_cached(http)
                    manage_bracket_arb_positions(
                        bracket_arb, client, spot_arb, secs_to_close, sigma_arb,
                    )

                # Check if market is rolling (bracket arb needs settlement)
                if secs_to_close <= 0:
                    settle_bracket_arb(bracket_arb, client, session)
                    bracket_arb.reset()

            # --- EVALUATE new bracket arb opportunity ---
            elif (
                not st.traded_this_market
                and not bracket_arb.is_active
                and secs_to_close <= BRACKET_ARB_MAX_SECONDS
                and secs_to_close >= BRACKET_ARB_MIN_SECONDS
            ):
                # Find sibling brackets for this event
                event_tk = st.event
                if event_tk and _all_event_markets:
                    siblings = find_event_brackets(_all_event_markets, event_tk)
                    if len(siblings) >= BRACKET_ARB_MIN_BRACKETS:
                        should_arb, bracket_prices, arb_reason = evaluate_bracket_arb(
                            siblings, client, session.current_balance_usd, secs_to_close,
                        )

                        if should_arb and bracket_prices:
                            qty_per_bracket = compute_bracket_arb_qty(
                                session.current_balance_usd, bracket_prices,
                            )

                            if qty_per_bracket > 0:
                                log.warning(
                                    f"[BRACKET_ARB] ENTERING — {len(bracket_prices)} brackets × {qty_per_bracket} contracts | {arb_reason}"
                                )

                                # Buy YES on all brackets
                                all_filled = True
                                bracket_arb.event_ticker = event_tk
                                bracket_arb.total_qty_per_bracket = qty_per_bracket
                                bracket_arb.entry_time = time.time()

                                for ticker, ask_cents, mobj_b in bracket_prices:
                                    lo_b, hi_b = market_bounds_usd(mobj_b)
                                    bp = BracketPosition(
                                        market_ticker=ticker,
                                        event_ticker=event_tk,
                                        floor_strike=lo_b,
                                        cap_strike=hi_b,
                                        market_obj=mobj_b,
                                    )

                                    try:
                                        payload = build_order_payload(
                                            market_ticker=ticker,
                                            action="buy",
                                            side="yes",
                                            price_cents=ask_cents,
                                            count=qty_per_bracket,
                                            post_only=False,
                                        )
                                        if not DRY_RUN:
                                            oid = place_order(client, payload)
                                            fill_status, filled = wait_for_fill(client, oid, ticker)
                                            if fill_status in ("filled", "partial") and filled > 0:
                                                bp.qty = filled
                                                bp.entry_price_cents = ask_cents
                                                bp.order_id = oid
                                                log.warning(
                                                    f"[BRACKET_ARB] Filled {ticker}: {filled} @ {ask_cents}¢"
                                                )
                                            else:
                                                log.warning(
                                                    f"[BRACKET_ARB] NOT filled {ticker} (status={fill_status}) — canceling"
                                                )
                                                cancel_order_status(client, oid)
                                                all_filled = False
                                        else:
                                            bp.qty = qty_per_bracket
                                            bp.entry_price_cents = ask_cents
                                            log.info(f"[BRACKET_ARB] DRY_RUN: would buy {ticker} @ {ask_cents}¢ × {qty_per_bracket}")
                                    except Exception as e:
                                        log.warning(f"[BRACKET_ARB] Order failed for {ticker}: {e}")
                                        all_filled = False

                                    bracket_arb.brackets.append(bp)

                                # Check if we got any fills
                                filled_brackets = [b for b in bracket_arb.brackets if b.qty > 0]
                                if filled_brackets:
                                    bracket_arb.is_active = True
                                    # Mark the primary market as traded so we don't also enter directionally
                                    st.traded_this_market = True
                                    total_cost = bracket_arb.total_invested_usd()
                                    log.warning(
                                        f"[BRACKET_ARB] Active: {len(filled_brackets)}/{len(bracket_prices)} brackets filled "
                                        f"total_cost=${total_cost:.2f} "
                                        f"guaranteed_pnl=${bracket_arb.total_qty_per_bracket * 1.00 - total_cost:.2f}"
                                    )
                                else:
                                    log.warning(f"[BRACKET_ARB] No brackets filled — aborting")
                                    bracket_arb.reset()

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
        # Runs from 12min → 7min before close
        # ============================================================
        spot = fetch_shib_spot_usd(http)
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
        # PHASE 3: BUY WINDOW — last 7 min, make the call
        # By now we have 5 minutes of trend data to inform the decision
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
        # Three paths:
        #   FAST LANE (≥90% prob, or ≥85% in last 3 min): buy immediately
        #   CONFIRMED (85-90% prob, >3 min left): need stable signal for 15s
        #   STANDARD  (<85% prob): borderline → need trend + momentum
        #
        # KEY FIX: Settlement lock (180s) is now separate from buy window (420s).
        # Early window entries (420s-180s) must pass trend/confirmation checks.
        # This prevents snap entries on transient 85% spikes at T-420s that
        # flip to the wrong side when SHIB moves.
        # ================================================================
        current_prob_for_side = p_yes_blend if chosen_side == "yes" else p_no_blend
        in_settlement_lock = secs_to_close is not None and secs_to_close <= SETTLEMENT_LOCK_SECONDS

        # Time-dependent fast lane threshold
        if secs_to_close <= SETTLEMENT_LOCK_SECONDS:
            fast_lane_thresh = PROB_FAST_LANE_LATE_THRESHOLD  # 85% in last 3 min
        else:
            fast_lane_thresh = PROB_FAST_LANE_THRESHOLD  # 90% earlier

        fast_lane = current_prob_for_side >= fast_lane_thresh

        # CONFIRMATION HOLD: early in buy window, require signal stability
        # Even if fast lane fires, check that the signal has been consistent
        if fast_lane and secs_to_close > CONFIRMATION_HOLD_MIN_TIME:
            confirmed_secs = prob_trend.side_confirmed_for(chosen_side, fast_lane_thresh)
            if confirmed_secs < CONFIRMATION_HOLD_SECONDS:
                fast_lane = False
                log.info(
                    f"[CONFIRM] {chosen_side.upper()} prob={current_prob_for_side:.1%} "
                    f"but only confirmed for {confirmed_secs:.0f}s < {CONFIRMATION_HOLD_SECONDS}s "
                    f"— waiting for stable signal before early entry"
                )

        if fast_lane:
            reason = (f"prob={current_prob_for_side:.1%} ≥ {fast_lane_thresh:.0%}"
                      if current_prob_for_side >= fast_lane_thresh
                      else f"settlement_lock t={secs_to_close}s prob={current_prob_for_side:.1%}")
            log.warning(
                f"[FAST LANE] {chosen_side.upper()} {reason} "
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

            # --- CROSS-VALIDATE: prob trend must match SHIB spot trend ---
            if REQUIRE_TREND_ALIGNMENT and prob_trend_ok:
                prob_change, prob_dir, _, _ = prob_trend.get_trend()
                spot_move, spot_dir, _ = trend.get_trend()
                spot_short_move, spot_short_dir, _ = trend_short.get_trend()

                misaligned = False
                if chosen_side == "yes" and "down" in spot_dir and abs(spot_move) > TREND_MODERATE_THRESHOLD:
                    misaligned = True
                    mismatch_reason = f"prob_yes but SHIB 120m={spot_dir}({spot_move:+.10f})"
                elif chosen_side == "no" and "up" in spot_dir and abs(spot_move) > TREND_MODERATE_THRESHOLD:
                    misaligned = True
                    mismatch_reason = f"prob_no but SHIB 120m={spot_dir}({spot_move:+.10f})"
                elif chosen_side == "yes" and "down" in spot_short_dir and abs(spot_short_move) > TREND_MODERATE_THRESHOLD:
                    misaligned = True
                    mismatch_reason = f"prob_yes but SHIB 60m={spot_short_dir}({spot_short_move:+.10f})"
                elif chosen_side == "no" and "up" in spot_short_dir and abs(spot_short_move) > TREND_MODERATE_THRESHOLD:
                    misaligned = True
                    mismatch_reason = f"prob_no but SHIB 60m={spot_short_dir}({spot_short_move:+.10f})"

                if misaligned:
                    log.warning(f"[ALIGNMENT] BLOCKED — {mismatch_reason}. Prob trend may be manipulation, not signal.")
                    time.sleep(POLL_SECONDS)
                    continue

                log.info(f"[ALIGNMENT] OK — prob {prob_dir} aligns with SHIB 120m={spot_dir} 60m={spot_short_dir}")

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
        # AGGRESSIVE FILL: use taker orders when probability is high enough.
        # Getting filled is worth more than saving maker/taker spread.
        # Not participating costs 100% of the edge; crossing the spread costs 1-2¢.
        # TIME-DEPENDENT: require higher prob for taker early (avoid crossing spread on uncertain signals)
        taker_prob_thresh = 0.90 if secs_to_close > SETTLEMENT_LOCK_SECONDS else 0.85
        if p_gate >= taker_prob_thresh:
            use_post_only = False
            log.info(f"[TAKER] Using taker order — p={p_gate:.4f} ≥ {taker_prob_thresh:.0%}, fills > maker savings")
        elif secs_to_close < LAST_CHANCE_TIME_SEC and p_gate >= LAST_CHANCE_MIN_PROB:
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
            log.warning(f"[DRY] would place: BUY {chosen_side} @ {chosen_px}¢ qty={qty} (bankroll=${available_usd:.2f} streak={session.consecutive_wins}W)")
            st.traded_this_market = True
            st.sm = SM.HOLD
            time.sleep(POLL_SECONDS)
            continue

        try:
            oid = place_order(client, payload)
            log.warning(
                f"[ORDER] PLACED {st.market} order_id={oid} BUY {chosen_side.upper()} @ {chosen_px}¢ qty={qty} "
                f"edge={edge_net:.4f} p_gate={p_gate:.4f} bankroll=${available_usd:.2f} streak={session.consecutive_wins}W"
            )

            # Verify fill before committing state
            fill_status, filled_qty = wait_for_fill(client, oid, st.market)

            if fill_status in ("filled", "partial") and filled_qty > 0:
                st.traded_this_market = True
                st.sm = SM.HOLD
                st.side = chosen_side
                st.entry_model_prob = p_yes_model
                st.entry_market_prob = p_mkt
                st.entry_spot_price = spot
                st.entry_price_cents = int(chosen_px)
                st.entry_time = now
                st.qty = filled_qty  # Use actual filled qty, not intended
                st.peak_prob_for_side = p_yes_blend if chosen_side == "yes" else (1.0 - p_yes_blend)
                st.prob_history = []  # Reset windowed peak tracking for new position
                if filled_qty < qty:
                    log.warning(f"[FILL] Partial fill: got {filled_qty}/{qty} contracts — canceling remainder")
                    cancel_order_status(client, oid)
                else:
                    log.warning(f"[FILL] Full fill confirmed: {filled_qty} contracts")
            elif fill_status == "unknown":
                # API error — assume filled to be safe (position check will reconcile)
                st.traded_this_market = True
                st.sm = SM.HOLD
                st.side = chosen_side
                st.entry_model_prob = p_yes_model
                st.entry_market_prob = p_mkt
                st.entry_spot_price = spot
                st.entry_price_cents = int(chosen_px)
                st.entry_time = now
                st.qty = qty
                st.peak_prob_for_side = p_yes_blend if chosen_side == "yes" else (1.0 - p_yes_blend)
                st.prob_history = []  # Reset windowed peak tracking for new position
                log.warning(f"[FILL] Could not verify fill — assuming filled, position check will reconcile")
            elif use_post_only or (int(chosen_px) >= 97 and p_gate >= PROB_FAST_LANE_THRESHOLD):
                # Order resting on the book — intentional in locked-book scenarios.
                # At 97-99¢ with ≥98% prob, the book is often locked (no asks).
                # Whether post_only or LAST_CHANCE taker, there's no counterparty
                # to fill against. Let it rest — we're the highest bid. Any seller
                # fills against us. Costs nothing if it expires unfilled at settlement.
                st.traded_this_market = True
                st.sm = SM.HOLD
                st.side = chosen_side
                st.entry_model_prob = p_yes_model
                st.entry_market_prob = p_mkt
                st.entry_spot_price = spot
                st.entry_price_cents = int(chosen_px)
                st.entry_time = now
                st.qty = qty  # Intended qty — will reconcile from position on settlement
                st.order_id = oid
                st.peak_prob_for_side = p_yes_blend if chosen_side == "yes" else (1.0 - p_yes_blend)
                st.prob_history = []  # Reset windowed peak tracking for new position
                resting_reason = "maker" if use_post_only else "locked_book"
                log.warning(
                    f"[FILL] Order {oid} resting ({resting_reason}) @ {chosen_px}¢ × {qty} — "
                    f"letting it sit (highest bid, zero cost if unfilled)"
                )
            else:
                # Taker order that should have filled but didn't — cancel and retry
                log.warning(f"[FILL] Taker order {oid} NOT filled (status={fill_status}) — canceling and resetting")
                cancel_order_status(client, oid)
                # Do NOT set traded_this_market — let the bot retry next loop
        except Exception as e:
            log.warning(f"[ORDER] place failed: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception(f"FATAL: bot crashed: {e}")
        raise
