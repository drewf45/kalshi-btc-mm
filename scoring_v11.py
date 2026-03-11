# scoring_v11.py — Evidence-First Participation
# V11 (Mar 11, 2026)
#
# PHILOSOPHY: Three filters must align to enter a trade:
#   1. HISTORY — our data says this price bucket actually wins
#   2. TREND   — BTC direction agrees (or is at least neutral)
#   3. TIME    — late entries score higher (confirmed signal)
#
# FORMULA:
#   score = (wr^1.5 × kelly × sample_weight) × trend_mult × time_weight
#
# TREND MULTIPLIER:
#   Agrees with market direction:  1.30x  (tailwind)
#   Neutral:                       1.00x
#   Disagrees:                     0.50x  (headwind — trades small, doesn't stop)
#
# DRAWDOWN PROTECTION:
#   Session down 0-15%:  1.00x size
#   Session down 15-25%: 0.50x size
#   Session down 25%+:   0.25x size  (still in the game, just tiny)
#
# TIERS:
#   HIGH   >= 0.55  → 15% of balance
#   MEDIUM >= 0.28  → 8%  of balance
#   LOW    >= 0.10  → 3%  of balance
#   SKIP   <  0.10  → no trade (historical data says don't bother)

import math
import logging
from typing import Optional, Tuple

from config import HISTORICAL_ACCURACY, SAMPLE_CONFIDENCE_TARGET

log = logging.getLogger("v11")

# ── THRESHOLDS ─────────────────────────────────────────────────
MIN_SCORE    = 0.10
SCORE_HIGH   = 0.55
SCORE_MED    = 0.28

TIER_PCT = {
    'HIGH':   0.15,
    'MEDIUM': 0.08,
    'LOW':    0.03,
}

HARD_CAP_PCT = 0.20   # never exceed 20% of balance regardless of score
MIN_TRADE_USD = 1.00  # minimum $1 cost

# Trend thresholds (BTC 1h % change)
TREND_BULL_THRESHOLD = 0.30   # +0.3% = bullish
TREND_BEAR_THRESHOLD = -0.30  # -0.3% = bearish

# Trend multipliers
TREND_AGREES    = 1.30
TREND_NEUTRAL   = 1.00
TREND_DISAGREES = 0.50

# Drawdown multipliers
DRAWDOWN_MILD     = 0.50   # 15-25% down
DRAWDOWN_SEVERE   = 0.25   # 25%+ down


# ── BUCKET LOOKUP ──────────────────────────────────────────────

def _bucket_key(side: str, price_cents: int) -> Optional[str]:
    s = side.lower()
    p = price_cents
    if not (1 <= p <= 99):
        return None
    if s == 'no':
        if   p <=  5: return 'NO_1_5'
        elif p <= 10: return 'NO_6_10'
        elif p <= 15: return 'NO_11_15'
        elif p <= 20: return 'NO_16_20'
        elif p <= 30: return 'NO_21_30'
        elif p <= 40: return 'NO_31_40'
        elif p <= 50: return 'NO_41_50'
        elif p <= 65: return 'NO_51_65'
        else:         return 'NO_66_80'
    elif s == 'yes':
        if   p <= 50: return 'YES_1_50'
        elif p <= 80: return 'YES_51_80'
        elif p <= 90: return 'YES_81_90'
        else:         return 'YES_91_99'
    return None


def _historical(asset: str, side: str, price_cents: int) -> Tuple[float, int, float]:
    """Returns (win_rate, n_samples, kelly). Falls back to weak defaults."""
    key = _bucket_key(side, price_cents)
    if key is None:
        return (0.50, 1, 0.05)

    data = HISTORICAL_ACCURACY.get(asset.upper(), {})
    if key in data:
        return data[key]

    # Merged-bucket fallbacks (SOL/XRP have broader keys)
    fallbacks = {
        'NO_21_30': ['NO_21_50'],
        'NO_31_40': ['NO_21_50', 'NO_31_50'],
        'NO_41_50': ['NO_21_50', 'NO_31_50'],
        'YES_81_90': ['YES_81_99'],
        'YES_91_99': ['YES_81_99'],
    }
    for alt in fallbacks.get(key, []):
        if alt in data:
            return data[alt]

    return (0.50, 1, 0.05)   # Unknown bucket → weak score, will likely skip


# ── SCORE COMPONENTS ───────────────────────────────────────────

def _sample_weight(n: int) -> float:
    return min(1.0, n / SAMPLE_CONFIDENCE_TARGET)


def _trend_multiplier(side: str, btc_1h_pct: float) -> float:
    """
    YES trade: BULLISH agrees, BEARISH disagrees.
    NO  trade: BEARISH agrees, BULLISH disagrees.
    """
    s = side.lower()
    if btc_1h_pct >= TREND_BULL_THRESHOLD:
        return TREND_AGREES if s == 'yes' else TREND_DISAGREES
    elif btc_1h_pct <= TREND_BEAR_THRESHOLD:
        return TREND_AGREES if s == 'no'  else TREND_DISAGREES
    else:
        return TREND_NEUTRAL


def _time_weight(secs_to_close: float, watch_window: float = 180.0) -> float:
    """0.0 at window open → 1.0 at close. Late entries score higher."""
    if watch_window <= 0:
        return 1.0
    return min(1.0, max(0.0, 1.0 - (secs_to_close / watch_window)))


# ── MAIN SCORING ENTRY POINT ───────────────────────────────────

def model_fair_cents(asset: str, side: str, price_cents: int) -> float:
    """Historical win rate converted to fair price in cents."""
    wr, _, _ = _historical(asset, side, price_cents)
    return wr * 100.0


def v11_score(
    asset: str,
    side: str,
    price_cents: int,
    secs_to_close: float,
    btc_1h_pct: float,
    watch_window: float = 180.0,
) -> float:
    """
    Returns V11 score in [0, ~2.0].
    Use MIN_SCORE as gate: < 0.10 → skip.

    CRITICAL: returns 0.0 if price > model_fair (negative EV — no edge).
    This blocks the V5 safety-net problem of entering at 98-99¢ with 96.4% WR.
    """
    wr, n, kelly = _historical(asset, side, price_cents)

    # Hard EV gate: don't pay more than model fair value
    fair = wr * 100.0
    if price_cents > fair:
        log.debug(f"[V11] {asset} {side}@{price_cents}¢ SKIP — price {price_cents}¢ > fair {fair:.1f}¢")
        return 0.0

    sw   = _sample_weight(n)
    tm   = _trend_multiplier(side, btc_1h_pct)
    tw   = _time_weight(secs_to_close, watch_window)

    # Gap bonus: reward entries further below fair value
    gap_pct = (fair - price_cents) / fair   # 0.0 to 1.0
    base  = (wr ** 1.5) * kelly * sw
    score = base * tm * (0.5 + 0.5 * tw) * (0.7 + 0.3 * gap_pct)

    log.debug(
        f"[V11] {asset} {side}@{price_cents}¢ fair={fair:.1f}¢ gap={fair-price_cents:.1f}¢ "
        f"wr={wr:.3f} kelly={kelly:.2f} sw={sw:.2f} tm={tm:.2f} tw={tw:.2f} "
        f"base={base:.3f} score={score:.3f}"
    )
    return score


def score_to_tier(score: float) -> Optional[str]:
    if score >= SCORE_HIGH: return 'HIGH'
    if score >= SCORE_MED:  return 'MEDIUM'
    if score >= MIN_SCORE:  return 'LOW'
    return None


# ── DRAWDOWN PROTECTION ────────────────────────────────────────

def drawdown_multiplier(session_start: float, current: float) -> float:
    """Returns size multiplier based on session drawdown."""
    if session_start <= 0 or current >= session_start:
        return 1.0
    pct_down = (session_start - current) / session_start
    if pct_down >= 0.25:
        return DRAWDOWN_SEVERE
    elif pct_down >= 0.15:
        return DRAWDOWN_MILD
    return 1.0


# ── SIZING ─────────────────────────────────────────────────────

def compute_usdc_risk(
    score: float,
    price_cents: int,
    balance: float,
    session_start: float,
) -> float:
    """
    Returns USDC to risk on this trade.
    Applies tier → drawdown multiplier → hard cap → min cost gate.
    Returns 0.0 if trade should be skipped.
    """
    tier = score_to_tier(score)
    if tier is None:
        return 0.0

    dd_mult = drawdown_multiplier(session_start, balance)
    pct     = TIER_PCT[tier] * dd_mult
    usdc    = balance * pct

    # Hard cap
    usdc = min(usdc, balance * HARD_CAP_PCT)

    # Minimum trade gate
    if usdc < MIN_TRADE_USD:
        return 0.0

    return usdc


def compute_contracts_kalshi(
    score: float,
    price_cents: int,
    balance: float,
    session_start: float,
) -> int:
    """Returns contract count for Kalshi (integer contracts, min 1)."""
    usdc = compute_usdc_risk(score, price_cents, balance, session_start)
    if usdc <= 0 or price_cents <= 0:
        return 0
    contracts = int(usdc / (price_cents / 100.0))
    return max(1, min(contracts, 500))


def compute_shares_polymarket(
    score: float,
    price: float,          # decimal (0.0–1.0)
    balance: float,
    session_start: float,
    min_shares: int = 5,
) -> float:
    """Returns share count for Polymarket (float, min 5)."""
    usdc = compute_usdc_risk(score, int(price * 100), balance, session_start)
    if usdc <= 0 or price <= 0:
        return 0.0
    shares = round(usdc / price, 2)
    return max(float(min_shares), shares)
