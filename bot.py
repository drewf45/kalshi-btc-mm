# bot.py
# Kalshi rolling 15m BTC — Find mispriced contracts, hold to close, scale bankroll
#
# STRATEGY:
# - Arms at T-720s (12 min) to OBSERVE market, BTC price, trends, book
# - Buys mispriced contracts in 3-7 min window where model has info advantage
# - Trusts the MARKET (orderbook) over the model near settlement
# - Requires 3%+ real edge — only enters when model sees genuine mispricing
# - HOLDS TO SETTLEMENT — collect the full payout for being right
# - Dump is ABORT ONLY — safety net, not a regular exit
# - Scales bankroll: wins compound via quarter-Kelly, 96 markets/day
#
# KEY SETTINGS:
# - TIME-DEPENDENT PROB: 92% if >5min, 88% if 3-5min, 85% if <3min
# - EDGE_MIN=0.03 (3% real edge — no penny-picking)
# - MAX_ENTRY_PRICE=93¢ (force real edge — 7¢/win, ~13 wins per loss)
# - KELLY=0.25 (quarter-Kelly — smoother equity curve)
# - BTC-AWARE BAIL: only dump if BTC has moved against us, not book noise
# - MULTI-TIMEFRAME TRENDS: 60-min + 30-min SpotTrend, per-minute ProbTrend
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
YES_ONLY = env_bool("YES_ONLY", False)    # Trade both YES and NO — double the addressable markets
NO_ONLY = env_bool("NO_ONLY", False)      # Trade only NO side (overrides YES_ONLY if both set)

# -------------- ASYMMETRIC SIDE REQUIREMENTS --------------------------------
# YES is filtered by the ultra-strict gate (92% prob, 60% move, 10min, 85¢+, etc).
# NO extra bonus on prob/edge in choose_trade — the ultra-strict gate is the
# real filter. Adding bonuses HERE made YES impossible (97-104% prob required)
# which killed volume. Let choose_trade pass YES through, then let the
# ultra-strict gate decide.
YES_PROB_BONUS = 0.0        # Ultra-strict gate handles YES filtering at 92% — no extra bonus here
YES_EDGE_BONUS = 0.0        # Ultra-strict gate handles YES filtering — no extra edge here
YES_MAX_ENTRY_PRICE = 96    # YES price cap in choose_trade (ultra-strict gate enforces >=92¢ minimum)
YES_REQUIRE_TREND = True    # YES always requires trend alignment — no fast lane
YES_REQUIRE_BOTH_TRENDS = True  # YES must have BOTH 60-min AND 30-min BTC trend aligned
YES_MIN_BTC_DISTANCE = 200.0    # YES only if BTC is $200+ above floor (physically locked)
YES_MAX_SECONDS = 600           # YES within last 10 min (was 60s — too restrictive, zero fills)

# -------------- ULTRA-STRICT YES GATE (4-condition simultaneous check) --------
# YES trades should be rare but nearly guaranteed wins.
# ALL 4 conditions must be true simultaneously or the trade is skipped.
# RELAXED: enter earlier for better fill rates — $0.40 hard stop is the real protection.
YES_ULTRA_MIN_PROB = 0.92       # (1) Model probability must exceed 92%
YES_ULTRA_MIN_MOVE_PCT = 0.60   # (2) BTC must have completed 60%+ of the expected range move
YES_ULTRA_MAX_SECONDS = 600     # (3) Up to 10 minutes remaining (was 420s/7min — too late, no fills)
YES_ULTRA_MIN_PRICE = 85        # (4) YES contract price must be >= 85 cents (was 92 — too close to $1, no fills)

ORDER_QTY = env_int("ORDER_QTY", 1)

COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

BOOTSTRAP_CANCEL_OPEN_ORDERS = env_bool("BOOTSTRAP_CANCEL_OPEN_ORDERS", True)

# -------------- HOLD-TO-CLOSE STRATEGY (HARDWIRED) --------------
# Philosophy: arm early to OBSERVE price/trends/book. Buy when confident,
# but EARLY ENOUGH that the book is still liquid (asks exist).
# Even a few cents edge is fine — buy MORE contracts.
# Hold to settlement. Dump is abort-only. Trade every market possible.
#
# KEY INSIGHT: Waiting until T-120s to buy means the book is LOCKED (no asks).
# Placing a 99¢ bid with no sellers = zero fills = zero profit. Enter at
# T-300s when probability is high AND the book still has liquidity.
#
# TWO PHASES:
#   OBSERVE (12min → 10min before close): gather trend data, watch book, DON'T buy
#   BUY     (10min → 5s before close):   make the call, place the order, hold
OBSERVE_START_SECONDS = 720  # Start watching at 12min — gather trend + prob data
BUY_START_SECONDS = 600      # Can enter from T-600s (10 min) — earlier entry = more liquidity = better fills
ENTRY_LAST_SECONDS = 5       # Can enter up to 5s before close (need time to fill)
FILL_WAIT_SECONDS = 20
ALLOW_TAKER_AT_LAST = True
CANCEL_UNFILLED_AT_CLOSE = True

PROB_MIN = 0.83  # 83%+ to enter in last 2 min — slightly lower bar, EV cap is the real protection
EDGE_MIN = 0.03  # 3% minimum edge — only enter with real mispricing, not penny edges
MAX_ENTRY_PRICE_CENTS = 96  # Raised from 93¢ — at 96¢ entry, gain 4¢/win, need ~24 wins per loss
FEE_CENTS_PER_CONTRACT = 0

# -------------- TIME-DEPENDENT CERTAINTY (within the 10-min buy window) -------
# Buy window is 10min → 5s before close. Require more certainty at the start
# of the buy window (BTC still has time to move), relax near the end.
# NOTE: observation phase (12min → 10min) gathers data but never buys.
PROB_EARLY_ENTRY_SECONDS = 300   # 5-10 min to close = "early" part of buy window
PROB_EARLY_MIN = 0.90            # >5min: need 90%+ (lowered from 92% — trade more markets)
PROB_MID_ENTRY_SECONDS = 180     # 3-5 min to close = "mid"
PROB_MID_MIN = 0.86              # 3-5min: need 86%+ (lowered from 88%)
# <3 min = PROB_MIN (0.83) — market has priced in the outcome, EV cap protects

# -------------- PROBABILITY TREND DETECTION (confirm borderline trades) --------
# When prob is borderline (80-89%), require momentum confirmation.
# When prob is high (90%+), the outcome speaks for itself — skip trend checks.
PROB_TREND_WINDOW_SECONDS = 90    # Look at last 90 seconds of probability
PROB_TREND_MIN_SAMPLES = 8        # Need at least 8 samples (~80s at 1/sec)
PROB_TREND_THRESHOLD = 0.08       # 8% swing in one direction = trend signal
PROB_TREND_MIN_CURRENT = 0.80     # Current prob must be ≥80% for trend-based entries
PROB_TREND_ENTRY_ENABLED = True   # Enable trend-based entries (for borderline trades)
REQUIRE_TREND_ALIGNMENT = True    # Prob trend must match BTC spot trend (borderline only)
# HIGH-CERTAINTY FAST LANE: if prob is this high, skip trend/momentum checks entirely
# Rationale: 90%+ prob means BTC is well inside the range. The outcome is decisive.
# Don't wait for trend alignment when the outcome is clear.
# TIME-DEPENDENT: early in buy window, require higher prob (92%) for fast lane.
# Near close (<3 min), 85% is enough because the market has priced in the outcome.
PROB_FAST_LANE_THRESHOLD = 0.90   # ≥90% prob = buy immediately, no trend check needed
PROB_FAST_LANE_LATE_THRESHOLD = 0.85  # ≥85% prob in last 3 min = fast lane (market is decisive)

# CONFIRMATION HOLD: require signal to be stable for N seconds before early entry
# Prevents snap entries on transient orderbook spikes at T-420s.
# At T-300s to T-180s, prob must have been on the same side for this many seconds.
CONFIRMATION_HOLD_SECONDS = 15   # Signal must persist for 15s before early commitment
CONFIRMATION_HOLD_MIN_TIME = 180  # Only require confirmation hold above 3 min to close

SPOT_SIGMA_USD_PER_SQRT_SEC = 12.0

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

# -------------- PORTFOLIO RISK CAP (2% per trade) --------------------------------
# Before placing any trade, max_risk = portfolio_balance × 0.02.
# Number of contracts must not result in potential loss exceeding max_risk.
# If Kelly suggests larger, cap at max_risk. Overrides Kelly when needed.
PORTFOLIO_MAX_RISK_FRACTION = 0.02  # 2% of portfolio balance = max risk per trade
# Legacy constants (kept for backward compat in safety checks)
BASE_CONTRACTS = 3
CONTRACT_INCREMENT = 1
BANKROLL_FRACTION = 0.40
SCALING_MIN_FRACTION = 0.15
SCALING_MAX_FRACTION = 0.50

# -------------- SESSION LOSS LIMITS (HARDWIRED) --------------
ENABLE_SESSION_LIMITS = True
DAILY_MAX_LOSS_PERCENT = 0.75  # HARD STOP: never lose more than 75% of starting balance
SESSION_CONSECUTIVE_LOSSES_LIMIT = 4  # Pause after 4 consecutive losses — sit one out, re-evaluate
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
DUMP_PROB_FLIP = 0.60  # Floor: if prob drops to 60% AND BTC confirms, bail (was 50% — too late, already lost 40¢+)
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
DUMP_BTC_SAFE_BUFFER_EARLY = 75.0    # >2min to close: need $75 buffer (was $100 — hold more winners, σ√120=$131 still provides margin)
DUMP_BTC_SAFE_BUFFER_LATE = 40.0     # <2min to close: need $40 buffer (was $50 — σ√60=$93, $40 is safe enough)
DUMP_BTC_SAFE_CUTOFF_SECONDS = 120   # Boundary between early/late buffer

# -------------- REVERSAL BAIL (only after BTC check fails) --------------------
DUMP_ON_PROB_REVERSAL = True   # Still enabled as safety net
DUMP_REVERSAL_THRESHOLD = 0.06  # 6% drop from peak — bail fast (was 8% — still too slow, 6% catches reversals earlier)
DUMP_REVERSAL_THRESHOLD_PROFIT = 0.04  # 4% when profitable — protect gains aggressively (was 6%)
DUMP_PROFIT_TIGHTEN_ABOVE_ENTRY = 0.03  # Tighten after 3%+ gain (was 5% — start protecting earlier)
DUMP_REVERSAL_MIN_SAMPLES = 5
DUMP_EARLY_EXIT_ENABLED = True
# Reversal during early settling phase uses a wider threshold (not blocked entirely)
DUMP_REVERSAL_THRESHOLD_SETTLING = 0.10  # 10% drop in first 30s = something is very wrong, bail even early

# -------------- HARD DOLLAR STOP-LOSS (absolute max loss per trade, no exceptions) -----
# This fires FIRST, before grace period, before BTC check, before everything.
# If a trade is losing more than this dollar amount, GET OUT. Period.
# $0.40 hard cap: at 70%+ WR with $0.10-0.15 avg win, one $0.40 loss = ~3 wins erased.
# Never allow a single trade to lose more than $0.40 under any circumstances.
HARD_STOP_LOSS_USD = 0.40  # Absolute max dollar loss per trade — overrides all other exit logic
SOFT_STOP_LOSS_USD = 0.30  # Soft stop: at -$0.30 unrealized, immediately market-sell to exit

# -------------- YES TIME-OF-DAY RESTRICTION ---------------------------------
# YES is net negative in 5/6 sessions. Only profitable session was overnight.
# Disable YES during daytime (8am-8pm EST) where it consistently bleeds.
# Allow YES overnight (8pm-8am EST) where it showed +$1.00 at 75% WR.
# Combined with the "physically locked" gate, YES can only fire overnight
# in the last 60s when BTC is $200+ above floor. Extremely selective.
YES_DAYTIME_DISABLED = True           # Kill YES trades during 8am-8pm EST
YES_DAYTIME_START_HOUR = 8            # 8am EST
YES_DAYTIME_END_HOUR = 20             # 8pm EST
YES_DAYTIME_TIMEZONE = "America/New_York"

# -------------- SESSION DRAWDOWN BREAKER ------------------------------------
# If down $2.00+ in a rolling 2-hour window, pause 30 min, resume at 50% size.
# Prevents cascade sessions like the -$7.93 morning.
DRAWDOWN_ENABLED = True
DRAWDOWN_MAX_LOSS_USD = 2.00          # Max loss in rolling window before pause
DRAWDOWN_WINDOW_SECONDS = 7200        # 2-hour rolling window
DRAWDOWN_PAUSE_SECONDS = 1800         # Pause for 30 minutes
DRAWDOWN_RESUME_SIZE_MULT = 0.50      # Resume at 50% position size
DRAWDOWN_RESUME_TRADES = 3            # Run 3 trades at reduced size before full size

# -------------- EARLY EXIT ON UNDERWATER POSITIONS --------------------------
# If position is down >30% of max possible loss within first 5 minutes,
# exit early — don't let underwater positions ride to expiry.
EARLY_EXIT_ENABLED = True
EARLY_EXIT_LOSS_FRACTION = 0.30       # 30% of max possible loss
EARLY_EXIT_WINDOW_SECONDS = 300       # First 5 minutes of holding

# -------------- MINIMUM EXPECTED PAYOUT (stop making penny trades) ----------
# If projected win is $0.02-$0.03, the risk/reward is terrible.
# Require minimum expected profit before entering any trade.
# expected_payout = qty × (100 - entry_cents) / 100
MIN_EXPECTED_PAYOUT_USD = 0.10  # Don't enter trades with < $0.10 projected win

# -------------- BANKROLL-PROPORTIONAL LOSS CAP (scales with your balance) -----
# Never lose more than X% of current balance on a single trade.
# At $35: max loss = $1.05.  At $350: max loss = $10.50.  Scales naturally.
# This fires BEFORE the fixed catastrophic stop and replaces it as the primary cap.
DUMP_MAX_LOSS_FRACTION_OF_BALANCE = 0.03  # 3% of current balance = max single-trade loss (was 5% — too much at small bankroll)
# Also cap at 50% of position cost — if you paid $3, max loss is $1.50
DUMP_MAX_LOSS_FRACTION_OF_POSITION = 0.50  # Never lose more than 50% of what you put in
# ENTRY-SIDE cap: worst case = settlement loss = full entry cost.
# With the EV price cap (price ≤ prob), entries are always +EV, so we can
# afford to size up.  15% of $22 = $3.30 → 3 contracts at 97c.
# As bankroll grows to $220: $33 → 34 contracts at 97c.
MAX_SETTLEMENT_LOSS_FRACTION = 0.08  # Max 8% of balance at risk per trade — one loss hurts but doesn't wreck you

# -------------- NUKE PREVENTION (cap how many wins one loss can erase) ----------
# At 96¢ entry: win=$0.04, loss=$0.96 → nuke ratio = 24:1.
# Without a cap, one bad trade erases 24 good ones.
# This limits position size so worst-case settlement loss ≤ N × expected win.
# Example at entry=96¢, NUKE_MAX_WINS_ERASED=5:
#   max_loss_allowed = 5 × (100-96)/100 × qty → qty ≤ 5 × win_per / cost_per
#   Effectively: qty ≤ 5 × ($0.04/$0.96) × bankroll_fraction → much smaller at high prices
NUKE_MAX_WINS_ERASED = 5  # One loss should never wipe more than 5 winning trades

# -------------- NO-SIDE CONTRACT CEILING (BTC-specific) -----------------------
# 5 of 6 BTC blowups were NO side. Even with Kelly + nuke cap, a high-confidence
# NO signal can result in too many contracts. Hard ceiling prevents that.
MAX_NO_CONTRACTS = 5  # Hard cap on NO contracts regardless of Kelly output

# -------------- BTC-SPECIFIC SIDE ADJUSTMENTS --------------------------------
# YES has excessive losses — reduce YES position size by 50%.
# NO is entering at insufficient confidence — raise minimum to 85%.
YES_POSITION_SIZE_MULT = 0.50    # Multiply all YES position sizes by 0.5
NO_MIN_CONFIDENCE = 0.80         # NO side requires 80% confidence (was 85% — NO is 100% profitable, let it breathe)

# -------------- TRAILING STOP ON WINNERS --------------------------------------
# Too many BTC trades go to +$0.30-$0.50 then give it all back at settlement.
# Once up $0.15, trail $0.10 below peak unrealized P&L. Lock in gains.
TRAILING_STOP_ENABLED = True
TRAILING_STOP_ACTIVATE_USD = 0.15  # Activate once unrealized P&L hits +$0.15
TRAILING_STOP_TRAIL_USD = 0.10     # Exit if P&L drops $0.10 below peak

# -------------- PORTFOLIO MILESTONE TRACKING ----------------------------------
# Track portfolio balance after every trade. Log milestones at every $5 increment.
# After $100, log profit cap hit and suggest withdrawing above $100.
MILESTONE_ENABLED = True
MILESTONE_INCREMENT = 5.0          # Log milestone every $5
MILESTONE_START = 30.0             # First milestone at $30
MILESTONE_PROFIT_CAP = 100.0      # Above $100, withdraw all excess

# -------------- BAIL TIMING (hold to close — but bail fast when it's wrong) ----
DUMP_GRACE_PERIOD_SECONDS = 10      # 10s grace period (was 15s — start monitoring sooner)
DUMP_PROACTIVE_AFTER_SECONDS = 30   # Proactive bail after 30s (was 60s — detect reversals earlier, bankroll cap covers the gap)

# -------------- HARD P&L STOP (last-resort backstop) -------------------------
DUMP_MAX_LOSS_CENTS_PER_CONTRACT = 10  # Hard stop after BTC check (was 15¢ — tighter to salvage more)
# CATASTROPHIC STOP: fires BEFORE BTC check — absolute max loss regardless of anything
# Prevents a $2.65 loss when the hard stop is supposed to cap at 10¢/contract
DUMP_CATASTROPHIC_LOSS_CENTS = 20      # If losing >20¢/contract, bail no matter what (was 30¢ — too much damage)

# -------------- WINDOWED PEAK TRACKING (avoid false reversals from book spikes) -----
# All-time peak ratchets up on thin-book spikes (e.g., 99% for 3 seconds) creating
# false reversal signals when prob returns to normal (e.g., 94% looks like 5% drop).
# Use a rolling window max instead: peak = max(prob over last N seconds).
DUMP_PEAK_WINDOW_SECONDS = 30  # Use max prob over last 30s as "peak" (not all-time)

# -------------- RAPID DROP BAIL (emergency exit on fast moves) -------------------
# If probability drops very fast (>4% in 10s), something is seriously wrong.
# Bail even during settling period — fast drops mean BTC is actively moving against us.
DUMP_RAPID_DROP_THRESHOLD = 0.04   # 4% drop in the rapid window = emergency
DUMP_RAPID_DROP_WINDOW_SECONDS = 10  # Look at last 10 seconds for rapid drops

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
    (400.0, 0.85),   # $400+ from strike: extremely safe, size up hard
    (200.0, 0.65),   # $200-400: very safe, go bigger
    (100.0, 0.45),   # $100-200: safe, meaningful size
    (50.0,  0.25),   # $50-100: moderate — compound the edge
]
SCALP_MAX_ENTRY_PRICE = 99        # Max 99¢ — even 1¢/contract × many contracts at scale
SCALP_MIN_PROB = 0.80             # Low bar — distance + volatility gate is the real safety, not blend prob
SCALP_MAX_LOSS_FRACTION = 0.15    # Never risk more than 15% of cash on a scalp

# -------------- A-LEVEL ADDITIONS --------------
USE_MARKET_IMPLIED = env_bool("USE_MARKET_IMPLIED", True)
MODEL_BLEND_ALPHA = env_float("MODEL_BLEND_ALPHA", 0.20)  # Was 0.75 — model is ~50/50 at >5min, drowns out 95% market signal

REQUIRE_DIVERGENCE = env_bool("REQUIRE_DIVERGENCE", False)
MIN_DIVERGENCE = env_float("MIN_DIVERGENCE", 0.015)

USE_DYNAMIC_SIGMA = env_bool("USE_DYNAMIC_SIGMA", True)
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
CANDLES_GRANULARITY_SEC = env_int("CANDLES_GRANULARITY_SEC", 60)
CANDLES_LOOKBACK = env_int("CANDLES_LOOKBACK", 10)
SIGMA_FLOOR = env_float("SIGMA_FLOOR", 6.0)
SIGMA_CEIL = env_float("SIGMA_CEIL", 40.0)

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
SETTLEMENT_LOCK_SECONDS = env_int("SETTLEMENT_LOCK_SECONDS", 180)    # Last 3 min only — earlier window still needs trend/prob checks
SETTLEMENT_LOCK_MIN_PROB = env_float("SETTLEMENT_LOCK_MIN_PROB", 0.85)  # blend prob — lower bar, EV cap (price ≤ prob) is the real protection
SETTLEMENT_LOCK_MAX_PRICE = env_int("SETTLEMENT_LOCK_MAX_PRICE", 99)   # edge = settlement
SETTLEMENT_LOCK_MIN_BID = env_int("SETTLEMENT_LOCK_MIN_BID", 90)      # locked book: if bid ≥ 90¢ but no ask, join bid queue

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
            ct = infer_close_ts_from_ticker(ticker, interval_minutes=15)
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


class YesHourlyTracker:
    """Track YES trade opportunities, skips, and outcomes per hour.
    Prints a summary log every hour."""

    def __init__(self):
        self.current_hour: int = -1  # Will be set on first call
        self.opportunities: int = 0
        self.skipped: int = 0
        self.skip_reasons: Dict[str, int] = {}  # condition_name -> count
        self.executed: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.net_pnl_usd: float = 0.0

    def _maybe_rotate(self):
        """Check if we've crossed into a new hour. If so, print summary and reset."""
        now_hour = datetime.now(timezone.utc).hour
        if self.current_hour == -1:
            self.current_hour = now_hour
            return
        if now_hour != self.current_hour:
            self._print_summary()
            self._reset()
            self.current_hour = now_hour

    def record_opportunity_skipped(self, failed_conditions: List[str]):
        """Record a YES opportunity that was skipped."""
        self._maybe_rotate()
        self.opportunities += 1
        self.skipped += 1
        for cond in failed_conditions:
            self.skip_reasons[cond] = self.skip_reasons.get(cond, 0) + 1

    def record_opportunity_executed(self):
        """Record a YES trade that passed all gates and was placed."""
        self._maybe_rotate()
        self.opportunities += 1
        self.executed += 1

    def record_result(self, pnl_usd: float):
        """Record a YES trade result (win or loss)."""
        self._maybe_rotate()
        if pnl_usd > 0:
            self.wins += 1
        else:
            self.losses += 1
        self.net_pnl_usd += pnl_usd

    def _print_summary(self):
        """Print hourly YES summary."""
        if self.opportunities == 0 and self.executed == 0:
            return
        # Find most common skip reason
        top_skip = "none"
        if self.skip_reasons:
            top_skip = max(self.skip_reasons, key=self.skip_reasons.get)
        skip_breakdown = ", ".join(f"{k}={v}" for k, v in sorted(self.skip_reasons.items(), key=lambda x: -x[1]))
        log.warning(
            f"[YES HOURLY] hour={self.current_hour:02d}:00 UTC | "
            f"opportunities={self.opportunities} skipped={self.skipped} executed={self.executed} | "
            f"wins={self.wins} losses={self.losses} net_pnl=${self.net_pnl_usd:.2f} | "
            f"top_skip_reason={top_skip} | breakdown: {skip_breakdown or 'none'}"
        )

    def _reset(self):
        self.opportunities = 0
        self.skipped = 0
        self.skip_reasons = {}
        self.executed = 0
        self.wins = 0
        self.losses = 0
        self.net_pnl_usd = 0.0


class FillRateTracker:
    """Track order fill rates across all order paths (main, flip, scalp).
    Prints a summary log every hour to help tune entry timing and pricing."""

    def __init__(self):
        self.current_hour: int = -1
        self.orders_posted: int = 0
        self.orders_filled: int = 0
        self.orders_partial: int = 0
        self.orders_unfilled: int = 0
        # Per-side tracking
        self.yes_posted: int = 0
        self.yes_filled: int = 0
        self.no_posted: int = 0
        self.no_filled: int = 0
        # Per-path tracking
        self.path_stats: Dict[str, Dict[str, int]] = {}  # path -> {posted, filled, unfilled}

    def _maybe_rotate(self):
        now_hour = datetime.now(timezone.utc).hour
        if self.current_hour == -1:
            self.current_hour = now_hour
            return
        if now_hour != self.current_hour:
            self._print_summary()
            self._reset()
            self.current_hour = now_hour

    def record_posted(self, side: str, path: str = "main"):
        """Record an order that was posted to the exchange."""
        self._maybe_rotate()
        self.orders_posted += 1
        if side == "yes":
            self.yes_posted += 1
        else:
            self.no_posted += 1
        if path not in self.path_stats:
            self.path_stats[path] = {"posted": 0, "filled": 0, "unfilled": 0}
        self.path_stats[path]["posted"] += 1

    def record_fill(self, side: str, path: str = "main", partial: bool = False):
        """Record an order that was filled (fully or partially)."""
        self._maybe_rotate()
        if partial:
            self.orders_partial += 1
        else:
            self.orders_filled += 1
        if side == "yes":
            self.yes_filled += 1
        else:
            self.no_filled += 1
        if path not in self.path_stats:
            self.path_stats[path] = {"posted": 0, "filled": 0, "unfilled": 0}
        self.path_stats[path]["filled"] += 1

    def record_unfilled(self, side: str, path: str = "main"):
        """Record an order that was NOT filled."""
        self._maybe_rotate()
        self.orders_unfilled += 1
        if path not in self.path_stats:
            self.path_stats[path] = {"posted": 0, "filled": 0, "unfilled": 0}
        self.path_stats[path]["unfilled"] += 1

    def _print_summary(self):
        if self.orders_posted == 0:
            return
        fill_rate = (self.orders_filled + self.orders_partial) / self.orders_posted * 100
        yes_rate = (self.yes_filled / self.yes_posted * 100) if self.yes_posted > 0 else 0
        no_rate = (self.no_filled / self.no_posted * 100) if self.no_posted > 0 else 0
        path_str = " | ".join(
            f"{p}: {s['filled']}/{s['posted']}"
            for p, s in sorted(self.path_stats.items())
        )
        log.warning(
            f"[FILL RATE] hour={self.current_hour:02d}:00 UTC | "
            f"posted={self.orders_posted} filled={self.orders_filled} "
            f"partial={self.orders_partial} unfilled={self.orders_unfilled} "
            f"rate={fill_rate:.0f}% | "
            f"YES={self.yes_filled}/{self.yes_posted}({yes_rate:.0f}%) "
            f"NO={self.no_filled}/{self.no_posted}({no_rate:.0f}%) | "
            f"paths: {path_str or 'none'}"
        )

    def _reset(self):
        self.orders_posted = 0
        self.orders_filled = 0
        self.orders_partial = 0
        self.orders_unfilled = 0
        self.yes_posted = 0
        self.yes_filled = 0
        self.no_posted = 0
        self.no_filled = 0
        self.path_stats = {}


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
    _needs_trend_reset: bool = False  # Reset trend data after cooldown

    # Drawdown breaker
    drawdown_paused: bool = False
    drawdown_resume_at: float = 0.0
    drawdown_reduced_trades: int = 0  # Count of trades at reduced size after drawdown

    # Milestone tracking
    next_milestone: float = MILESTONE_START  # Next milestone to log

    # Balance refresh
    pending_balance_check_at: float = 0.0  # When to fetch balance after settlement

    # Trade history
    recent_trades: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.recent_trades is None:
            self.recent_trades = []

    def init_milestones(self, balance: float):
        """Set next_milestone to the first $5 increment above current balance."""
        if MILESTONE_ENABLED and balance > 0:
            # Find the next $5 milestone above current balance
            self.next_milestone = (int(balance / MILESTONE_INCREMENT) + 1) * MILESTONE_INCREMENT
            log.info(
                f"[MILESTONE] Initialized: balance=${balance:.2f} "
                f"next_milestone=${self.next_milestone:.0f}"
            )

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
            "pnl_usd": pnl_usd, "was_dump": was_dump,
            "ts": time.time(), "timestamp": time.time(),
        }
        self.recent_trades.append(trade)
        if len(self.recent_trades) > 50:
            self.recent_trades = self.recent_trades[-50:]

        # Check rolling drawdown after every trade
        self.check_rolling_drawdown()

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

        # Portfolio milestone tracking
        self._check_milestones()

    def _check_milestones(self):
        """Track portfolio milestones at every $5 increment. Log withdrawal suggestions."""
        if not MILESTONE_ENABLED:
            return
        bal = self.current_balance_usd

        # Check if we crossed a milestone
        while bal >= self.next_milestone:
            log.warning(
                f"[MILESTONE_HIT] ${self.next_milestone:.0f} — withdraw $1 | "
                f"balance=${bal:.2f} daily_pnl=${self.daily_pnl_usd:.2f} "
                f"W/L={self.total_wins}/{self.total_losses}"
            )
            self.next_milestone += MILESTONE_INCREMENT

        # Profit cap: above $100, withdraw all excess
        if bal >= MILESTONE_PROFIT_CAP:
            excess = bal - MILESTONE_PROFIT_CAP
            log.warning(
                f"[PROFIT_CAP_HIT] withdraw all above ${MILESTONE_PROFIT_CAP:.0f} "
                f"(excess=${excess:.2f}) | balance=${bal:.2f}"
            )

        # Log distance to next milestone after every trade
        distance = self.next_milestone - bal
        log.info(
            f"[MILESTONE] balance=${bal:.2f} next_milestone=${self.next_milestone:.0f} "
            f"distance_to_milestone=${distance:.2f}"
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

    def check_rolling_drawdown(self) -> None:
        """Check if rolling P&L in window exceeds drawdown limit. Triggers pause if so."""
        if not DRAWDOWN_ENABLED or not self.recent_trades:
            return
        # Already in drawdown pause
        if self.drawdown_paused:
            return
        cutoff = time.time() - DRAWDOWN_WINDOW_SECONDS
        rolling_pnl = sum(
            t.get("pnl_usd", t.get("pnl_cents", 0) / 100.0)
            for t in self.recent_trades
            if t.get("timestamp", 0) >= cutoff
        )
        if rolling_pnl <= -DRAWDOWN_MAX_LOSS_USD:
            self.drawdown_paused = True
            self.drawdown_resume_at = time.time() + DRAWDOWN_PAUSE_SECONDS
            self.drawdown_reduced_trades = 0
            log.warning(
                f"[DRAWDOWN] Rolling {DRAWDOWN_WINDOW_SECONDS//60}min P&L = ${rolling_pnl:.2f} "
                f"<= -${DRAWDOWN_MAX_LOSS_USD:.2f} — PAUSING {DRAWDOWN_PAUSE_SECONDS//60}min, "
                f"then resume at {DRAWDOWN_RESUME_SIZE_MULT:.0%} size for {DRAWDOWN_RESUME_TRADES} trades"
            )

    def get_drawdown_size_multiplier(self) -> float:
        """Returns position size multiplier based on drawdown state."""
        if not self.drawdown_paused:
            return 1.0
        if self.drawdown_reduced_trades < DRAWDOWN_RESUME_TRADES:
            return DRAWDOWN_RESUME_SIZE_MULT
        return 1.0

    def check_can_trade(self) -> Tuple[bool, Optional[str]]:
        """Check if we can trade"""
        # Daily hard stop is permanent until restart
        if self.is_daily_stopped:
            return False, f"DAILY_HARD_STOP (lost 75%+ of starting balance)"

        # Drawdown breaker pause
        if self.drawdown_paused:
            if time.time() >= self.drawdown_resume_at:
                self.drawdown_paused = False
                log.warning(f"[DRAWDOWN] Pause ended — resuming at {DRAWDOWN_RESUME_SIZE_MULT:.0%} size for {DRAWDOWN_RESUME_TRADES} trades")
            else:
                remaining = int(self.drawdown_resume_at - time.time())
                return False, f"drawdown_pause ({remaining}s remaining)"

        if not self.is_paused:
            return True, None

        if self.pause_until > 0 and time.time() >= self.pause_until:
            self.is_paused = False
            self.pause_reason = None
            self.consecutive_losses = 0
            self._needs_trend_reset = True  # Signal to reset trend data on resume
            log.warning("[SESSION] Cooldown ended, resuming — trend data will be reset to re-evaluate")
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

    def reset(self) -> None:
        """Clear all trend data — forces re-observation from scratch."""
        self.samples.clear()
        self.last_sample_time = 0.0
        log.info("[SPOT_TREND] Reset — re-gathering BTC trend data")

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
          EARLY (5-7 min): Require active trend match — BTC still has time to swing.
          MID   (3-5 min): Allow flat trend — stable high prob is enough certainty.
          LATE  (<3 min):  Skip trend entirely — probability IS the outcome now.
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

    # Trailing stop: track peak unrealized P&L for locking in gains
    peak_unrealized_pnl: float = 0.0

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
        effective_prob_min = PROB_EARLY_MIN   # >5min: need 90%+
    elif secs_to_close > PROB_MID_ENTRY_SECONDS:
        effective_prob_min = PROB_MID_MIN     # 3-5min: need 86%+
    else:
        effective_prob_min = PROB_MIN         # <3min: 83%+ — market has priced in the outcome

    # ASYMMETRIC GATES: YES must clear a higher bar than NO
    yes_prob_min = effective_prob_min + YES_PROB_BONUS  # YES: same as NO here; ultra-strict gate enforces 92%
    yes_edge_min = EDGE_MIN + YES_EDGE_BONUS            # YES: same as NO here; ultra-strict gate filters
    yes_max_price = YES_MAX_ENTRY_PRICE                  # YES: capped at 96¢ (ultra-strict gate enforces >=85¢)
    no_prob_min = effective_prob_min                      # NO: standard thresholds
    no_edge_min = EDGE_MIN                               # NO: standard 3% edge

    ok_yes = (
        yes_px is not None
        and ok_book_yes
        and (p_yes_blend >= yes_prob_min)     # YES: tighter prob gate
        and (edge_yes >= yes_edge_min)        # YES: tighter edge gate
        and (yes_px <= yes_max_price)         # YES: lower price cap (91¢)
        and div_gate_yes
    )
    ok_no = (
        no_px is not None
        and ok_book_no
        and (p_no_blend >= no_prob_min)       # NO: standard prob gate
        and (edge_no >= no_edge_min)          # NO: standard edge gate
        and (no_px <= MAX_ENTRY_PRICE_CENTS)  # NO: normal price cap (96¢)
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
            # Re-evaluate ok_yes/ok_no with new blend (asymmetric gates)
            ok_yes = (
                yes_px is not None and ok_book_yes
                and (p_yes_blend >= yes_prob_min) and (edge_yes >= yes_edge_min)
                and (yes_px <= yes_max_price) and div_gate_yes
            )
            ok_no = (
                no_px is not None and ok_book_no
                and (p_no_blend >= no_prob_min) and (edge_no >= no_edge_min)
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
            # Re-evaluate ok_yes/ok_no with new blend (asymmetric gates)
            ok_yes = (
                yes_px is not None and ok_book_yes
                and (p_yes_blend >= yes_prob_min) and (edge_yes >= yes_edge_min)
                and (yes_px <= yes_max_price) and div_gate_yes
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

    # SIDE RESTRICTION: YES_ONLY or NO_ONLY modes
    if YES_ONLY and ok_no and not ok_yes:
        log.info(f"[YES_ONLY] Blocking NO entry (edge={edge_no:.4f} prob={p_no_blend:.1%}) — YES_ONLY mode")
    if YES_ONLY:
        ok_no = False
    if NO_ONLY and ok_yes and not ok_no:
        log.info(f"[NO_ONLY] Blocking YES entry (edge={edge_yes:.4f} prob={p_yes_blend:.1%}) — NO_ONLY mode")
    if NO_ONLY:
        ok_yes = False

    # Log asymmetric gate info when YES is blocked by tighter requirements
    if not ok_yes and yes_px is not None and p_yes_blend >= effective_prob_min and not YES_ONLY and not NO_ONLY:
        log.info(
            f"[YES TIGHTENED] Blocked: prob={p_yes_blend:.1%} (need {yes_prob_min:.1%}) "
            f"edge={edge_yes:.4f} (need {yes_edge_min:.2f}) price={yes_px}¢ (max {yes_max_price}¢)"
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
    yes_bid: Optional[int] = None,
    no_bid: Optional[int] = None,
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

    # =============================================================
    # === HARD DOLLAR STOP-LOSS: FIRES FIRST, NO EXCEPTIONS ===
    # Before grace period, before BTC check, before late-entry hold.
    # If the trade is losing more than HARD_STOP_LOSS_USD, get out NOW.
    #
    # CRITICAL: Use the ACTUAL BID PRICE (what we'd get if we sold now),
    # NOT the probability estimate. The prob can lag behind reality —
    # blend shows 70% when the real bid is 10¢. That's how a $2.87 loss
    # slips through a $1.00 hard stop.
    # Check BOTH prob-based AND bid-based estimates, fire on whichever is worse.
    # =============================================================
    if st.entry_price_cents is not None and st.qty > 0:
        # Method 1: probability-based estimate
        if st.side == "yes":
            _hs_prob = p_yes_blend
        else:
            _hs_prob = p_no_blend
        _hs_exit_prob = int(_hs_prob * 100)
        _hs_loss_prob = (st.entry_price_cents - _hs_exit_prob) * st.qty / 100.0

        # Method 2: actual bid price (what the market will actually pay us)
        if st.side == "yes" and yes_bid is not None:
            _hs_exit_bid = yes_bid
        elif st.side == "no" and no_bid is not None:
            _hs_exit_bid = no_bid
        else:
            _hs_exit_bid = _hs_exit_prob  # fallback to prob if no bid available
        _hs_loss_bid = (st.entry_price_cents - _hs_exit_bid) * st.qty / 100.0

        # Use the WORST case (highest loss) of both methods
        _hs_total_loss = max(_hs_loss_prob, _hs_loss_bid)
        _hs_method = "bid" if _hs_loss_bid >= _hs_loss_prob else "prob"
        _hs_exit_used = _hs_exit_bid if _hs_method == "bid" else _hs_exit_prob

        # HARD STOP: at $0.40 unrealized loss, immediately market-sell.
        # This overrides ALL other sizing and exit logic. No exceptions.
        if _hs_total_loss >= HARD_STOP_LOSS_USD:
            log.warning(
                f"[STOP_LOSS_HIT] timestamp={datetime.now(timezone.utc).isoformat()}Z "
                f"market_id={st.market} entry_price={st.entry_price_cents}¢ "
                f"exit_price={_hs_exit_used}¢ loss_amount=${_hs_total_loss:.2f} "
                f"reason=STOP_LOSS_HIT | method={_hs_method} qty={st.qty} "
                f"loss_bid=${_hs_loss_bid:.2f} loss_prob=${_hs_loss_prob:.2f}"
            )
            return True, f"STOP_LOSS_HIT_${_hs_total_loss:.2f}>=${HARD_STOP_LOSS_USD:.2f}"

        # SOFT STOP: at $0.30 unrealized loss, start exiting.
        if _hs_total_loss >= SOFT_STOP_LOSS_USD:
            log.warning(
                f"[STOP_LOSS_HIT] timestamp={datetime.now(timezone.utc).isoformat()}Z "
                f"market_id={st.market} entry_price={st.entry_price_cents}¢ "
                f"exit_price={_hs_exit_used}¢ loss_amount=${_hs_total_loss:.2f} "
                f"reason=SOFT_STOP | method={_hs_method} qty={st.qty} "
                f"loss_bid=${_hs_loss_bid:.2f} loss_prob=${_hs_loss_prob:.2f}"
            )
            return True, f"SOFT_STOP_${_hs_total_loss:.2f}>=${SOFT_STOP_LOSS_USD:.2f}"

    if secs_to_close < DUMP_MIN_TIME_REMAINING:
        return False, "too_close_to_settlement"

    # LATE-ENTRY HOLD: if <30s to close, outcome is mostly decided.
    # Hold to settlement — don't let dump logic sell a near-certain winner.
    # For earlier entries, normal dump logic applies — BTC can still move.
    # Was 60s — BTC can move $90 in 60s (σ=12, √60=7.7). 30s is $65, safer.
    # CRITICAL FIX: Even within hold window, if BTC is near the boundary,
    # allow dump logic to run. Blindly holding while BTC drifts toward the
    # strike is how -$3.84 losses happen.
    HOLD_TO_SETTLE_SECONDS = 30  # Only suppress dumps in the last 30s (was 60s)
    HOLD_BTC_DANGER_BUFFER = 75.0  # If BTC is within $75 of boundary, DON'T suppress dumps
    if st.entry_time > 0 and secs_to_close <= HOLD_TO_SETTLE_SECONDS:
        # Check if BTC is dangerously close to boundary — if so, let dump logic run
        btc_safe_for_hold, btc_hold_dist = _btc_is_safe(st.side, spot, lo, hi, secs_to_close)
        if not btc_safe_for_hold or btc_hold_dist < HOLD_BTC_DANGER_BUFFER:
            # BTC is near the boundary — DON'T suppress dumps, let normal logic decide
            log.warning(
                f"[HOLD OVERRIDE] BTC near boundary (dist=${btc_hold_dist:.0f} < ${HOLD_BTC_DANGER_BUFFER:.0f}) "
                f"with {secs_to_close}s left — allowing dump checks"
            )
            # Fall through to normal dump logic below
        else:
            # BTC is safely on our side — hold to settlement
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

    time_in_trade = time.time() - st.entry_time if st.entry_time > 0 else 9999

    # --- EARLY EXIT ON UNDERWATER POSITIONS ---
    # If down >30% of max possible loss within first 5 minutes, exit early.
    # Don't let underwater positions ride to expiry.
    if EARLY_EXIT_ENABLED and st.entry_price_cents is not None and st.qty > 0:
        if time_in_trade <= EARLY_EXIT_WINDOW_SECONDS:
            # Max possible loss = entry_price × qty (contract goes to $0)
            max_possible_loss = (st.entry_price_cents * st.qty) / 100.0
            threshold_loss = max_possible_loss * EARLY_EXIT_LOSS_FRACTION

            # Use bid price if available for accurate loss estimate
            if st.side == "yes":
                _ee_exit = yes_bid if yes_bid is not None else int(p_yes_blend * 100)
            else:
                _ee_exit = no_bid if no_bid is not None else int(p_no_blend * 100)
            _ee_loss = (st.entry_price_cents - _ee_exit) * st.qty / 100.0

            if _ee_loss >= threshold_loss:
                log.warning(
                    f"[EARLY EXIT] Down ${_ee_loss:.2f} >= {EARLY_EXIT_LOSS_FRACTION:.0%} of "
                    f"max loss ${max_possible_loss:.2f} (threshold ${threshold_loss:.2f}) "
                    f"in first {time_in_trade:.0f}s — exiting before it gets worse"
                )
                return True, f"early_exit_${_ee_loss:.2f}>={EARLY_EXIT_LOSS_FRACTION:.0%}_of_max"

    # --- GRACE PERIOD ---
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

    # --- RAPID DROP BAIL: BTC is against us AND prob dropped fast ---
    # Emergency exit: if prob dropped >4% in the last 10s, BTC is actively moving
    # against us. Fire even during settling period — speed matters here.
    if rapid_drop >= DUMP_RAPID_DROP_THRESHOLD:
        log.warning(
            f"[BAIL RAPID DROP] BTC NOT safe (dist=${btc_distance:.0f}) AND "
            f"prob dropped {rapid_drop:.1%} in last {DUMP_RAPID_DROP_WINDOW_SECONDS}s "
            f"(current={current_prob:.1%}) — emergency exit"
        )
        return True, f"rapid_drop_{rapid_drop:.1%}_in_{DUMP_RAPID_DROP_WINDOW_SECONDS}s"

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
                f"[BAIL REVERSAL] BTC NOT safe (dist=${btc_distance:.0f}) AND "
                f"prob dropped {drop_from_peak:.1%} from windowed peak "
                f"({windowed_peak:.1%} -> {current_prob:.1%}) phase={phase_label} "
                f"threshold={effective_threshold:.1%} — salvaging"
            )
            return True, f"reversal_{current_prob:.0%}_from_wpeak_{windowed_peak:.0%}_{phase_label}"

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
        f"{phase}_prob={current_prob:.0%}_wpeak={windowed_peak:.0%}_peak={st.peak_prob_for_side:.0%}"
        f"_drop={drop_from_peak:.0%}_rdrop={rapid_drop:.0%}_btc_dist=${btc_distance:.0f}"
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

    # PORTFOLIO RISK CAP: max_risk = portfolio_balance × 2%. Cap contracts so
    # potential loss (entry_cost × qty) never exceeds max_risk.
    if cost_per > 0 and available_usd > 0:
        max_risk_usd = available_usd * PORTFOLIO_MAX_RISK_FRACTION
        max_qty_risk = int(max_risk_usd / cost_per)
        if max_qty_risk < MIN_CONTRACTS:
            max_qty_risk = MIN_CONTRACTS
        if target_qty > max_qty_risk:
            log.warning(
                f"[SIZE] PORTFOLIO_RISK_CAP: suggested={target_qty} capped={max_qty_risk} "
                f"portfolio_balance=${available_usd:.2f} max_risk=${max_risk_usd:.2f} "
                f"({PORTFOLIO_MAX_RISK_FRACTION:.0%} of balance) entry={entry_cents}¢"
            )
            target_qty = max_qty_risk

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

    # NUKE PREVENTION: cap so one loss never erases more than N wins.
    # At high entry prices the win/loss asymmetry is brutal (96¢: 24:1 nuke ratio).
    # This sizes the position so that: total_loss ≤ NUKE_MAX_WINS_ERASED × total_win.
    #   total_loss = qty × entry_cents (worst case: settles at $0)
    #   total_win  = qty × (100 - entry_cents) (best case: settles at $1)
    # So: qty × entry ≤ N × qty × (100 - entry) → simplifies to entry ≤ N × (100 - entry)
    # But that's per-contract (fixed ratio). The real cap is on DOLLAR loss:
    #   max_dollar_loss = NUKE_MAX_WINS_ERASED × expected_dollar_win_per_trade
    #   expected_dollar_win = target_qty × (100 - entry_cents) / 100
    #   max_qty_nuke = NUKE_MAX_WINS_ERASED × (100 - entry_cents) / entry_cents × target_qty ... circular
    # Non-circular: cap total risk so it equals N average wins at THIS entry price:
    #   qty_nuke × cost_per ≤ NUKE_MAX_WINS_ERASED × qty_nuke × win_per → always true (it's a ratio)
    # Real fix: use the ORIGINAL target_qty as the "expected trade size" and cap actual qty:
    win_per = (100.0 - entry_cents) / 100.0
    if win_per > 0 and cost_per > 0:
        # nuke_ratio = how many wins one full loss erases at this entry price
        nuke_ratio = cost_per / win_per
        if nuke_ratio > NUKE_MAX_WINS_ERASED:
            # Scale down: if nuke_ratio is 24 and max is 5, multiply qty by 5/24
            nuke_scale = NUKE_MAX_WINS_ERASED / nuke_ratio
            nuke_qty = max(MIN_CONTRACTS, int(target_qty * nuke_scale))
            if nuke_qty < target_qty:
                log.warning(
                    f"[NUKE CAP] entry={entry_cents}¢ nuke_ratio={nuke_ratio:.1f}:1 "
                    f"(1 loss = {nuke_ratio:.0f} wins) — scaling {target_qty} -> {nuke_qty} contracts "
                    f"(×{nuke_scale:.2f}) to cap at {NUKE_MAX_WINS_ERASED} wins erased"
                )
                target_qty = nuke_qty

    # HARD STOP BACKSTOP: Even if the hard stop fails to fire (no bids, locked book,
    # API error), the position must be small enough that a FULL settlement loss
    # stays under the hard stop cap. This is the last line of defense.
    # max_qty × cost_per ≤ HARD_STOP_LOSS_USD → max_qty = HARD_STOP_LOSS_USD / cost_per
    if cost_per > 0:
        max_qty_hard_stop = int(HARD_STOP_LOSS_USD / cost_per)
        # CRITICAL: If one contract costs more than the hard stop cap, DO NOT TRADE.
        # Previously floored to MIN_CONTRACTS=1 which allowed $0.89 loss through $0.40 cap.
        if max_qty_hard_stop <= 0:
            log.warning(
                f"[HARD STOP BLOCK] cost_per=${cost_per:.2f} > HARD_STOP=${HARD_STOP_LOSS_USD:.2f} — "
                f"single contract exceeds max loss cap, CANNOT TRADE at {entry_cents}¢"
            )
            return 0
        if target_qty > max_qty_hard_stop:
            log.warning(
                f"[HARD STOP SIZE] Capping qty {target_qty} -> {max_qty_hard_stop} so full "
                f"settlement loss (${target_qty * cost_per:.2f}) stays under "
                f"${HARD_STOP_LOSS_USD:.2f} hard stop"
            )
            target_qty = max_qty_hard_stop

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

    # HARD STOP BACKSTOP: scalp settlement loss must also stay under HARD_STOP_LOSS_USD.
    # This was missing — scalps at 15% of balance could risk $5+ on a $36 bankroll.
    if cost_per > 0:
        max_qty_hard = int(HARD_STOP_LOSS_USD / cost_per)
        if max_qty_hard <= 0:
            log.warning(
                f"[SCALP HARD BLOCK] cost_per=${cost_per:.2f} > ${HARD_STOP_LOSS_USD:.2f} — "
                f"single scalp contract exceeds max loss, blocking"
            )
            return 0
        if target_qty > max_qty_hard:
            log.warning(
                f"[SCALP HARD CAP] Capping scalp {target_qty} -> {max_qty_hard} "
                f"(settlement loss ${target_qty * cost_per:.2f} > ${HARD_STOP_LOSS_USD:.2f})"
            )
            target_qty = max_qty_hard

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


def validate_position_size(
    side: str,
    entry_cents: int,
    num_contracts: int,
    existing_qty: int = 0,
    existing_entry_cents: int = 0,
    max_loss: float = HARD_STOP_LOSS_USD,
) -> int:
    """Universal last-gate validation: cap contracts so max possible loss <= max_loss.

    This runs RIGHT BEFORE every place_order BUY call. Even if every upstream
    sizing function has bugs, this makes blowups physically impossible.

    For add-on orders (scalps on existing positions), pass existing_qty and
    existing_entry_cents so the COMBINED position is capped.
    """
    if num_contracts <= 0 or entry_cents <= 0:
        return num_contracts

    cost_new = float(entry_cents) / 100.0
    # Existing position risk
    cost_existing = (float(existing_entry_cents) / 100.0) * existing_qty if existing_qty > 0 else 0.0
    # Budget remaining for new contracts
    budget = max_loss - cost_existing
    if budget <= 0:
        log.warning(
            f"[VALIDATE] BLOCKED — existing position already risks "
            f"${cost_existing:.2f} >= ${max_loss:.2f} cap"
        )
        return 0

    max_new = int(budget / cost_new) if cost_new > 0 else num_contracts

    # CRITICAL: If one contract costs more than remaining budget, DO NOT TRADE.
    # This was the root cause of -$0.89 loss through $0.40 cap.
    # Previously: "if max_new < 1: max_new = 1" which allowed trades over the cap.
    if max_new <= 0:
        log.warning(
            f"[VALIDATE] BLOCKED — cost_per=${cost_new:.2f} > budget=${budget:.2f} "
            f"(cap=${max_loss:.2f} - existing=${cost_existing:.2f}) | "
            f"side={side} entry={entry_cents}¢ — single contract exceeds max loss"
        )
        return 0

    if num_contracts > max_new:
        log.warning(
            f"[VALIDATE] SIZE_CAPPED | side={side} | entry={entry_cents}¢ | "
            f"original={num_contracts} | capped={max_new} | "
            f"existing_risk=${cost_existing:.2f} | "
            f"max_loss_before=${cost_existing + cost_new * num_contracts:.2f} | "
            f"max_loss_after=${cost_existing + cost_new * max_new:.2f} | "
            f"cap=${max_loss:.2f}"
        )
        return max_new

    return num_contracts


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
        f"DUMP_PROB_FLIP={DUMP_PROB_FLIP} DUMP_PROB_DROP={DUMP_PROB_DROP_PERCENT} "
        f"YES_ONLY={YES_ONLY} NO_ONLY={NO_ONLY}"
    )
    log.warning(
        f"[BOOTCFG] ASYMMETRIC: YES_PROB_BONUS={YES_PROB_BONUS:.0%} YES_EDGE_BONUS={YES_EDGE_BONUS:.0%} "
        f"YES_MAX_ENTRY={YES_MAX_ENTRY_PRICE}¢ YES_REQUIRE_TREND={YES_REQUIRE_TREND} "
        f"HARD_STOP=${HARD_STOP_LOSS_USD:.2f} MIN_PAYOUT=${MIN_EXPECTED_PAYOUT_USD:.2f}"
    )
    log.warning(
        f"[BOOTCFG] YES GATE: ultra_min_price={YES_ULTRA_MIN_PRICE}¢ ultra_min_prob={YES_ULTRA_MIN_PROB:.0%} "
        f"ultra_max_secs={YES_ULTRA_MAX_SECONDS}s ultra_min_move={YES_ULTRA_MIN_MOVE_PCT:.0%} "
        f"time_lock={YES_MAX_SECONDS}s | NO_MIN_CONF={NO_MIN_CONFIDENCE:.0%} "
        f"BUY_WINDOW={BUY_START_SECONDS}s"
    )
    # IMPORTANT: Flag hard stop vs YES price interaction
    _max_yes_for_hard_stop = int(HARD_STOP_LOSS_USD * 100)  # Max YES price allowing ≥1 contract
    if YES_ULTRA_MIN_PRICE > _max_yes_for_hard_stop:
        log.warning(
            f"[BOOTCFG] *** NOTE: YES min price ({YES_ULTRA_MIN_PRICE}¢) > hard stop allows "
            f"({_max_yes_for_hard_stop}¢ max for 1 contract). YES trades effectively BLOCKED "
            f"by $0.40 hard stop. This is SAFE — raise hard stop to enable YES. ***"
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
    yes_tracker = YesHourlyTracker()
    fill_tracker = FillRateTracker()
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
            session.init_milestones(av)
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
                        if pend_side == "yes":
                            yes_tracker.record_result(pend_pnl / 100.0)
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
                            if st.side == "yes":
                                yes_tracker.record_result(pnl_cents / 100.0)
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
                            # TIME-RAMPED blend (same as entry logic):
                            # Near close, trust the MARKET, not the model.
                            # The model lags and masks losses from the hard stop.
                            if secs_to_close <= 60:
                                _hold_alpha = 0.0  # 100% market in last minute
                            elif secs_to_close <= BUY_START_SECONDS:
                                _hold_alpha = float(MODEL_BLEND_ALPHA) * (secs_to_close - 60) / float(BUY_START_SECONDS - 60)
                            else:
                                _hold_alpha = float(MODEL_BLEND_ALPHA)
                            p_yes_blend = _hold_alpha * p_yes_model + (1.0 - _hold_alpha) * float(p_mkt)
                        else:
                            p_yes_blend = p_yes_model
                        p_yes_blend = max(0.0, min(1.0, p_yes_blend))
                        p_no_blend = 1.0 - p_yes_blend
                        
                        should_dump, dump_reason = should_dump_position(
                            st, p_yes_blend, p_no_blend, p_mkt,
                            spot, lo, hi, sigma_used, secs_to_close, trend,
                            current_balance_usd=session.current_balance_usd,
                            yes_bid=yes_bid, no_bid=no_bid,
                        )

                        # DUMP DIAGNOSTIC: log every cycle so we can trace stop-loss behavior
                        if st.entry_price_cents is not None and st.qty > 0:
                            if st.side == "yes":
                                _diag_bid = yes_bid if yes_bid is not None else int(p_yes_blend * 100)
                            else:
                                _diag_bid = no_bid if no_bid is not None else int(p_no_blend * 100)
                            _diag_unrealized = (_diag_bid - st.entry_price_cents) * st.qty / 100.0
                            _diag_worst_case = st.qty * st.entry_price_cents / 100.0
                            log.info(
                                f"[DUMP CHECK] market={st.market} side={st.side.upper()} "
                                f"entry={st.entry_price_cents}¢ qty={st.qty} "
                                f"bid={_diag_bid}¢ unrealized_pnl=${_diag_unrealized:.2f} "
                                f"worst_case=${_diag_worst_case:.2f} "
                                f"hard_stop=${HARD_STOP_LOSS_USD:.2f} soft_stop=${SOFT_STOP_LOSS_USD:.2f} "
                                f"t={secs_to_close}s dump={'YES' if should_dump else 'no'}"
                                + (f" reason={dump_reason}" if should_dump else "")
                            )

                        # --- TRAILING STOP ON WINNERS ---
                        # Once position is up $0.15, trail $0.10 below peak.
                        # Uses bid price for accurate P&L (not model estimate).
                        if TRAILING_STOP_ENABLED and not should_dump and st.entry_price_cents is not None and st.qty > 0:
                            if st.side == "yes":
                                _ts_bid = yes_bid if yes_bid is not None else int(p_yes_blend * 100)
                            else:
                                _ts_bid = no_bid if no_bid is not None else int(p_no_blend * 100)
                            _ts_pnl = (_ts_bid - st.entry_price_cents) * st.qty / 100.0

                            # Update peak unrealized P&L
                            if _ts_pnl > st.peak_unrealized_pnl:
                                st.peak_unrealized_pnl = _ts_pnl

                            # If we've reached the activation threshold, check trail
                            if st.peak_unrealized_pnl >= TRAILING_STOP_ACTIVATE_USD:
                                _ts_floor = st.peak_unrealized_pnl - TRAILING_STOP_TRAIL_USD
                                if _ts_pnl <= _ts_floor:
                                    should_dump = True
                                    dump_reason = (
                                        f"trailing_stop_pnl=${_ts_pnl:.2f}_peak=${st.peak_unrealized_pnl:.2f}"
                                        f"_floor=${_ts_floor:.2f}"
                                    )
                                    log.warning(
                                        f"[TRAILING STOP] P&L ${_ts_pnl:.2f} dropped below "
                                        f"${_ts_floor:.2f} (peak ${st.peak_unrealized_pnl:.2f} - "
                                        f"${TRAILING_STOP_TRAIL_USD:.2f} trail) — locking in gains"
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
                                    if st.side == "yes":
                                        yes_tracker.record_result(pnl_cents / 100.0)
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
                                        and not (NO_ONLY and flip_side == "yes")  # Don't flip to YES in NO_ONLY mode
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

                                        # BTC adjustments on flip: YES ×0.5, NO confidence check
                                        if flip_side == "yes" and YES_POSITION_SIZE_MULT < 1.0:
                                            flip_qty = max(MIN_CONTRACTS, int(flip_qty * YES_POSITION_SIZE_MULT))
                                        if flip_side == "no" and flip_prob < NO_MIN_CONFIDENCE:
                                            log.warning(f"[FLIP] SKIP NO flip — confidence {flip_prob:.1%} < {NO_MIN_CONFIDENCE:.0%}")
                                            flip_qty = 0  # Block the flip
                                        # NO cap + universal validation on flip
                                        if flip_side == "no" and flip_qty > MAX_NO_CONTRACTS:
                                            flip_qty = MAX_NO_CONTRACTS
                                        if flip_qty > 0:
                                            flip_qty = validate_position_size(flip_side, int(flip_price), flip_qty)

                                        if flip_qty <= 0:
                                            log.warning(f"[FLIP] Skipped — qty=0 after adjustments")
                                            can_flip = False

                                    if can_flip:
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
                                            fill_tracker.record_posted(flip_side, "flip")
                                            log.warning(
                                                f"[FLIP] Placed flip order {flip_oid} BUY {flip_side.upper()} "
                                                f"@ {flip_price}¢ qty={flip_qty}"
                                            )

                                            # Verify flip fill
                                            flip_fill_status, flip_filled = wait_for_fill(client, flip_oid, st.market)
                                            if flip_fill_status in ("filled", "partial") and flip_filled > 0:
                                                fill_tracker.record_fill(flip_side, "flip", partial=(flip_fill_status == "partial"))
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
                                                st.peak_unrealized_pnl = 0.0  # Reset trailing stop tracker
                                                st.has_flipped = True
                                                st.traded_this_market = True
                                                if flip_filled < flip_qty:
                                                    log.warning(f"[FLIP] Partial fill: {flip_filled}/{flip_qty} — canceling remainder")
                                                    cancel_order_status(client, flip_oid)
                                                else:
                                                    log.warning(f"[FLIP] Fill confirmed: {flip_filled} contracts")
                                            else:
                                                fill_tracker.record_unfilled(flip_side, "flip")
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
                                        if NO_ONLY and flip_side == "yes":
                                            reason_parts.append("no_only_mode")
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

                                            # UNIVERSAL VALIDATION: cap scalp so COMBINED position
                                            # (existing + scalp) can't lose more than $1.00 at settlement.
                                            scalp_qty = validate_position_size(
                                                st.side, scalp_px, scalp_qty,
                                                existing_qty=abs(pos),
                                                existing_entry_cents=st.entry_price_cents or 0,
                                            )

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
                                                fill_tracker.record_posted(st.side, "scalp")
                                                expected_profit = scalp_qty * (100 - scalp_px) / 100.0
                                                log.warning(
                                                    f"[SCALP] PLACED order={scalp_oid} BUY {st.side.upper()} "
                                                    f"@ {scalp_px}¢ × {scalp_qty} "
                                                    f"(expect +${expected_profit:.2f} at settlement)"
                                                )
                                                # Quick fill check for scalp (less time since we're near close)
                                                scalp_fill_status, scalp_filled = wait_for_fill(client, scalp_oid, st.market)
                                                if scalp_fill_status in ("filled", "partial") and scalp_filled > 0:
                                                    fill_tracker.record_fill(st.side, "scalp", partial=(scalp_fill_status == "partial"))
                                                    st.has_scalped = True
                                                    log.warning(f"[SCALP] Fill confirmed: {scalp_filled}/{scalp_qty} contracts")
                                                    if scalp_filled < scalp_qty:
                                                        cancel_order_status(client, scalp_oid)
                                                else:
                                                    fill_tracker.record_unfilled(st.side, "scalp")
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
            # EVAL LOG: show why no side was chosen (both sides failed choose_trade)
            _eval_p_yes = p_yes_blend
            _eval_p_no = p_no_blend
            _eval_yes_px = postable_entry_price(yes_bid, yes_ask) if POST_ONLY else yes_ask
            _eval_no_px = postable_entry_price(no_bid, no_ask) if POST_ONLY else no_ask
            _eval_edge_y = compute_edge(_eval_p_yes, _eval_yes_px, FEE_CENTS_PER_CONTRACT) if _eval_yes_px else -9
            _eval_edge_n = compute_edge(_eval_p_no, _eval_no_px, FEE_CENTS_PER_CONTRACT) if _eval_no_px else -9
            if (now - last_ob_warn) >= OB_WARN_EVERY_SECONDS:
                log.warning(
                    f"[EVAL SKIP] {st.market} t={secs_to_close}s NEITHER side passed | "
                    f"YES: prob={_eval_p_yes:.1%} px={_eval_yes_px} edge={_eval_edge_y:.4f} "
                    f"need_prob>={PROB_MIN:.0%} need_edge>={EDGE_MIN + YES_EDGE_BONUS:.2f} max_px={YES_MAX_ENTRY_PRICE} | "
                    f"NO: prob={_eval_p_no:.1%} px={_eval_no_px} edge={_eval_edge_n:.4f} "
                    f"need_prob>={PROB_MIN:.0%} need_edge>={EDGE_MIN:.2f} max_px={MAX_ENTRY_PRICE_CENTS} | "
                    f"book: yes=({yes_bid},{yes_ask}) no=({no_bid},{no_ask})"
                )
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
        # flip to the wrong side when BTC moves.
        # ================================================================
        current_prob_for_side = p_yes_blend if chosen_side == "yes" else p_no_blend
        in_settlement_lock = secs_to_close is not None and secs_to_close <= SETTLEMENT_LOCK_SECONDS

        # EVAL LOG: log every evaluation cycle with all gate values
        _eval_edge = float(edge_yes) if chosen_side == "yes" else float(edge_no)
        log.info(
            f"[EVAL] {st.market} t={secs_to_close}s side={chosen_side.upper()} "
            f"px={chosen_px}¢ prob={current_prob_for_side:.1%} edge={_eval_edge:.4f} "
            f"settle_lock={'Y' if in_settlement_lock else 'N'} "
            f"spot=${spot:.2f} | entering filter pipeline"
        )

        # Time-dependent fast lane threshold
        if secs_to_close <= SETTLEMENT_LOCK_SECONDS:
            fast_lane_thresh = PROB_FAST_LANE_LATE_THRESHOLD  # 85% in last 3 min
        else:
            fast_lane_thresh = PROB_FAST_LANE_THRESHOLD  # 90% earlier

        fast_lane = current_prob_for_side >= fast_lane_thresh

        # YES TIGHTENING: YES side never gets fast lane — must always prove trend alignment
        if fast_lane and chosen_side == "yes" and YES_REQUIRE_TREND:
            fast_lane = False
            log.info(
                f"[YES TIGHTENED] Blocking fast lane for YES (prob={current_prob_for_side:.1%}) "
                f"— YES must pass trend checks"
            )

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

        # =============================================================
        # === YES ULTRA-STRICT GATE (4-condition simultaneous check) ===
        # YES trades should be rare but nearly guaranteed wins.
        # ALL 4 conditions must be true simultaneously:
        #   1. Probability > 92%
        #   2. Price move completed >= 60% of range
        #   3. Less than 7 minutes remaining
        #   4. YES contract price >= 92 cents (market agrees near-certain)
        # Plus all existing physical-lock gates (daytime, trends, distance, vol).
        # =============================================================
        if chosen_side == "yes" and not YES_ONLY:
            # Compute the 4 ultra-strict conditions
            _yes_prob = current_prob_for_side
            _yes_price = int(chosen_px)  # YES contract price in cents
            _yes_mins_remaining = secs_to_close / 60.0 if secs_to_close else 99.0

            # Price move % completed: how far BTC is through the range toward YES
            # YES wins when spot > lo. Range = hi - lo. Move = spot - lo.
            _yes_range = (hi - lo) if (lo is not None and hi is not None and hi > lo) else 1.0
            _yes_move = (spot - lo) if lo is not None else 0.0
            _yes_move_pct = _yes_move / _yes_range if _yes_range > 0 else 0.0
            _yes_move_pct = max(0.0, min(1.0, _yes_move_pct))

            # Evaluate all 4 conditions
            _yes_failed = []
            if _yes_prob < YES_ULTRA_MIN_PROB:
                _yes_failed.append("prob")
            if _yes_move_pct < YES_ULTRA_MIN_MOVE_PCT:
                _yes_failed.append("move_pct")
            if secs_to_close > YES_ULTRA_MAX_SECONDS:
                _yes_failed.append("time")
            if _yes_price < YES_ULTRA_MIN_PRICE:
                _yes_failed.append("price")

            # If ANY condition fails, skip and log
            if _yes_failed:
                log.warning(
                    f"[YES ULTRA SKIP] timestamp={datetime.now(timezone.utc).isoformat()}Z "
                    f"market_id={st.market} confidence={_yes_prob:.1%} "
                    f"price_move_pct={_yes_move_pct:.1%} "
                    f"minutes_remaining={_yes_mins_remaining:.1f} price={_yes_price}¢ "
                    f"which_condition_failed={','.join(_yes_failed)}"
                )
                yes_tracker.record_opportunity_skipped(_yes_failed)
                time.sleep(POLL_SECONDS)
                continue

            # --- All 4 ultra-strict conditions passed. Now run physical-lock gates. ---

            # Gate -1: Daytime block — YES disabled 8am-8pm EST (bleeds during daytime)
            if YES_DAYTIME_DISABLED:
                try:
                    est_now = datetime.now(ZoneInfo(YES_DAYTIME_TIMEZONE))
                    est_hour = est_now.hour
                    if YES_DAYTIME_START_HOUR <= est_hour < YES_DAYTIME_END_HOUR:
                        log.warning(
                            f"[YES ULTRA SKIP] timestamp={datetime.now(timezone.utc).isoformat()}Z "
                            f"market_id={st.market} confidence={_yes_prob:.1%} "
                            f"price_move_pct={_yes_move_pct:.1%} "
                            f"minutes_remaining={_yes_mins_remaining:.1f} price={_yes_price}¢ "
                            f"which_condition_failed=daytime_block"
                        )
                        yes_tracker.record_opportunity_skipped(["daytime_block"])
                        time.sleep(POLL_SECONDS)
                        continue
                except Exception as e:
                    log.warning(f"[YES DAYTIME] Timezone check failed: {e} — allowing trade")

            # Gate 0 (time lock) REMOVED — now identical to ultra-strict condition 3.
            # YES_MAX_SECONDS == YES_ULTRA_MAX_SECONDS == 600s; gate was always redundant.

            # Gate 1: Both BTC trends must align (60-min AND 30-min pointing up)
            if YES_REQUIRE_BOTH_TRENDS:
                alignment_60 = trend.trade_alignment("yes", lo, hi, spot)
                alignment_30 = trend_short.trade_alignment("yes", lo, hi, spot)
                if alignment_60 != "with" or alignment_30 != "with":
                    log.warning(
                        f"[YES ULTRA SKIP] timestamp={datetime.now(timezone.utc).isoformat()}Z "
                        f"market_id={st.market} confidence={_yes_prob:.1%} "
                        f"price_move_pct={_yes_move_pct:.1%} "
                        f"minutes_remaining={_yes_mins_remaining:.1f} price={_yes_price}¢ "
                        f"which_condition_failed=trend_alignment "
                        f"60m={alignment_60} 30m={alignment_30}"
                    )
                    yes_tracker.record_opportunity_skipped(["trend_alignment"])
                    time.sleep(POLL_SECONDS)
                    continue

            # Gate 2: BTC must be $200+ above the floor (physically impossible to reverse)
            btc_above_floor = 0.0
            if lo is not None:
                btc_above_floor = spot - lo
                if btc_above_floor < YES_MIN_BTC_DISTANCE:
                    log.warning(
                        f"[YES ULTRA SKIP] timestamp={datetime.now(timezone.utc).isoformat()}Z "
                        f"market_id={st.market} confidence={_yes_prob:.1%} "
                        f"price_move_pct={_yes_move_pct:.1%} "
                        f"minutes_remaining={_yes_mins_remaining:.1f} price={_yes_price}¢ "
                        f"which_condition_failed=btc_distance "
                        f"dist=${btc_above_floor:.0f} need=${YES_MIN_BTC_DISTANCE:.0f}"
                    )
                    yes_tracker.record_opportunity_skipped(["btc_distance"])
                    time.sleep(POLL_SECONDS)
                    continue

            # Gate 3: Volatility sanity — can BTC actually move that far in remaining time?
            sigma_now = float(get_sigma_cached(client))
            max_move = 3.0 * sigma_now * math.sqrt(float(secs_to_close))  # 3σ = 99.7% of moves
            if lo is not None and btc_above_floor < max_move:
                log.warning(
                    f"[YES ULTRA SKIP] timestamp={datetime.now(timezone.utc).isoformat()}Z "
                    f"market_id={st.market} confidence={_yes_prob:.1%} "
                    f"price_move_pct={_yes_move_pct:.1%} "
                    f"minutes_remaining={_yes_mins_remaining:.1f} price={_yes_price}¢ "
                    f"which_condition_failed=volatility "
                    f"dist=${btc_above_floor:.0f} 3σ√t=${max_move:.0f}"
                )
                yes_tracker.record_opportunity_skipped(["volatility"])
                time.sleep(POLL_SECONDS)
                continue

            # ALL GATES PASSED — record and log
            yes_tracker.record_opportunity_executed()
            log.warning(
                f"[YES ULTRA PASS] ALL GATES PASSED — YES entry approved | "
                f"timestamp={datetime.now(timezone.utc).isoformat()}Z "
                f"market_id={st.market} confidence={_yes_prob:.1%} "
                f"price_move_pct={_yes_move_pct:.1%} "
                f"minutes_remaining={_yes_mins_remaining:.1f} price={_yes_price}¢ "
                f"btc_dist=${btc_above_floor:.0f} 3σ√t=${max_move:.0f}"
            )

        # Check session limits before trading
        can_trade, pause_reason = session.check_can_trade()
        if not can_trade:
            st.sm = SM.COOLDOWN
            if (now - last_state_log) >= LOG_STATE_EVERY_SECONDS:
                log.warning(f"[SESSION] Trading paused: {pause_reason}")
                last_state_log = now
            time.sleep(POLL_SECONDS)
            continue

        # After cooldown: wipe stale trend data so the bot re-observes fresh
        if session._needs_trend_reset:
            trend.reset()
            prob_trend.reset(st.market)
            session._needs_trend_reset = False
            log.warning("[SESSION] Post-cooldown: trend data reset — re-evaluating from scratch")

        available_usd, total_usd = get_balance_usd(client)

        if chosen_side == "yes":
            edge_net = float(edge_yes)
            p_gate = float(p_yes_blend)
        else:
            edge_net = float(edge_no)
            p_gate = float(p_no_blend)

        raw_qty = compute_qty_from_bankroll(available_usd, int(chosen_px), edge_net=edge_net, p_gate=p_gate, session=session)
        qty = raw_qty
        adjustment_reason = "none"

        # BTC-SPECIFIC: NO side requires 85% minimum confidence.
        if chosen_side == "no" and p_gate < NO_MIN_CONFIDENCE:
            log.warning(
                f"[BTC ADJ] SKIP NO — confidence {p_gate:.1%} < {NO_MIN_CONFIDENCE:.0%} min | "
                f"side={chosen_side} price={chosen_px}¢ raw_size={raw_qty}"
            )
            st.traded_this_market = True
            time.sleep(POLL_SECONDS)
            continue

        # BTC-SPECIFIC: YES position sizing reduced by 50%.
        # Guard: if qty==0 (hard stop blocked), don't override with MIN_CONTRACTS.
        if chosen_side == "yes" and YES_POSITION_SIZE_MULT < 1.0 and qty > 0:
            qty = max(MIN_CONTRACTS, int(qty * YES_POSITION_SIZE_MULT))
            adjustment_reason = f"YES_SIZE_MULT={YES_POSITION_SIZE_MULT}"

        # NO-SIDE CONTRACT CEILING: 5 of 6 BTC blowups were NO side.
        # Hard cap regardless of Kelly output.
        if chosen_side == "no" and qty > MAX_NO_CONTRACTS:
            qty = MAX_NO_CONTRACTS
            adjustment_reason = f"NO_CAP={MAX_NO_CONTRACTS}"

        # UNIVERSAL VALIDATION: absolute last gate before order.
        # Makes blowups physically impossible even if all upstream sizing has bugs.
        pre_validate_qty = qty
        qty = validate_position_size(chosen_side, int(chosen_px), qty)
        if qty < pre_validate_qty:
            adjustment_reason = f"VALIDATE_CAP={qty}"

        # PRE-TRADE COMPREHENSIVE LOG: every trade must show worst-case analysis.
        # This is the canonical audit trail for verifying the $0.40 hard stop works.
        cost_per_contract = int(chosen_px) / 100.0  # Both YES and NO: you pay chosen_px cents per contract
        worst_case_loss = qty * cost_per_contract
        hard_stop_pass = worst_case_loss <= HARD_STOP_LOSS_USD or qty == 0
        log.warning(
            f"[PRE-TRADE] side={chosen_side.upper()} contracts={qty} "
            f"cost_per=${cost_per_contract:.2f} worst_case_loss=${worst_case_loss:.2f} "
            f"hard_stop_cap=${HARD_STOP_LOSS_USD:.2f} "
            f"PASS={'YES' if hard_stop_pass else '*** BLOCKED ***'} | "
            f"confidence={p_gate:.1%} edge={edge_net:.4f} "
            f"price={chosen_px}¢ raw_qty={raw_qty} adj_qty={qty} "
            f"reason={adjustment_reason} bankroll=${available_usd:.2f} "
            f"market={st.market}"
        )
        # SAFETY: If worst-case exceeds hard stop, block the trade.
        # This should never happen if upstream sizing is correct, but defense-in-depth.
        if not hard_stop_pass and qty > 0:
            log.error(
                f"[PRE-TRADE BLOCKED] worst_case=${worst_case_loss:.2f} > "
                f"hard_stop=${HARD_STOP_LOSS_USD:.2f} — REFUSING TRADE | "
                f"side={chosen_side} price={chosen_px}¢ qty={qty}"
            )
            qty = 0

        # Apply drawdown size multiplier if recovering from drawdown pause
        dd_mult = session.get_drawdown_size_multiplier()
        if dd_mult < 1.0 and qty > 0:
            old_qty = qty
            qty = max(MIN_CONTRACTS, int(qty * dd_mult))
            session.drawdown_reduced_trades += 1
            log.warning(
                f"[DRAWDOWN SIZE] Reduced {old_qty} -> {qty} contracts "
                f"(×{dd_mult:.0%}, trade {session.drawdown_reduced_trades}/{DRAWDOWN_RESUME_TRADES})"
            )

        if qty <= 0:
            log.warning(f"[SKIP] {st.market} qty=0")
            st.traded_this_market = True
            time.sleep(POLL_SECONDS)
            continue

        # MIN EXPECTED PAYOUT: don't enter trades where projected win is pennies
        expected_payout = qty * (100 - int(chosen_px)) / 100.0
        if expected_payout < MIN_EXPECTED_PAYOUT_USD:
            log.warning(
                f"[SKIP PAYOUT] {st.market} {chosen_side.upper()} @ {chosen_px}¢ × {qty} "
                f"→ expected win ${expected_payout:.2f} < ${MIN_EXPECTED_PAYOUT_USD:.2f} min — not worth the risk"
            )
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
            fill_tracker.record_posted(chosen_side, "main")
            log.warning(
                f"[ORDER] PLACED {st.market} order_id={oid} BUY {chosen_side.upper()} @ {chosen_px}¢ qty={qty} "
                f"edge={edge_net:.4f} p_gate={p_gate:.4f} bankroll=${available_usd:.2f} streak={session.consecutive_wins}W"
            )

            # Verify fill before committing state
            fill_status, filled_qty = wait_for_fill(client, oid, st.market)

            if fill_status in ("filled", "partial") and filled_qty > 0:
                fill_tracker.record_fill(chosen_side, "main", partial=(fill_status == "partial"))
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
                st.peak_unrealized_pnl = 0.0  # Reset trailing stop tracker
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
                st.peak_unrealized_pnl = 0.0  # Reset trailing stop tracker
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
                st.peak_unrealized_pnl = 0.0  # Reset trailing stop tracker
                resting_reason = "maker" if use_post_only else "locked_book"
                log.warning(
                    f"[FILL] Order {oid} resting ({resting_reason}) @ {chosen_px}¢ × {qty} — "
                    f"letting it sit (highest bid, zero cost if unfilled)"
                )
            else:
                # Taker order that should have filled but didn't — cancel and retry
                fill_tracker.record_unfilled(chosen_side, "main")
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
