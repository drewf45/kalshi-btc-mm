# scoring_v10.py — Live signal-driven scoring for Kalshi 15M bots
# V10 (Mar 10, 2026)
#
# REPLACES V9's hardcoded HISTORICAL_ACCURACY tables with 4 live inputs:
#
# entry_score = signal_score × price_score × vol_multiplier × time_score
#
# signal_score  — trend confirmation (own coin + BTC master signal)
# price_score   — EV quality of entry price (from actual fill analysis)
# vol_multiplier — current volatility regime (live 5-min candle range)
# time_score    — time remaining to close (V9 formula, unchanged)
#
# TIER → SIZING:
#   HIGH   (≥ 0.70) → 18% of bankroll
#   MEDIUM (≥ 0.45) → 10% of bankroll
#   LOW    (≥ 0.25) →  4% of bankroll
#   < 0.25          → NO TRADE (natural kill — score math decides, not a rule)

import logging
from typing import Optional, Tuple

log = logging.getLogger("scoring_v10")

# ─────────────────────────────────────────────────────────────
# TIER THRESHOLDS + SIZING
# ─────────────────────────────────────────────────────────────
SCORE_HIGH_THRESHOLD = 0.70
SCORE_MED_THRESHOLD  = 0.45
MIN_SCORE_TO_TRADE   = 0.25

TIER_PCT = {
    'HIGH':   0.18,
    'MEDIUM': 0.10,
    'LOW':    0.04,
}
MAX_RISK_PCT       = 0.20   # Hard cap: never risk more than 20% on one trade
MAX_CONTRACTS_ABS  = 50     # Absolute ceiling (prevents absurd counts at low prices)


# ─────────────────────────────────────────────────────────────
# SIGNAL SCORE — trend confirmation
# ─────────────────────────────────────────────────────────────
# own_trend and btc_trend: "bullish" | "bearish" | "neutral"
# side: "yes" (betting price goes UP) | "no" (betting price stays flat/goes DOWN)
#
# A YES entry is confirmed by a BULLISH trend.
# A NO entry is confirmed by a BEARISH trend.

def signal_score(own_trend: str, btc_trend: str, side: str) -> float:
    """
    Score 0.0–1.0 based on trend alignment between own coin, BTC, and trade direction.
    Conflicting signals → 0.3 (won't clear MIN_SCORE when combined with other factors).
    """
    confirming_trend = "bullish" if side == "yes" else "bearish"
    opposing_trend   = "bearish" if side == "yes" else "bullish"

    own_confirms = own_trend == confirming_trend
    own_opposes  = own_trend == opposing_trend
    btc_confirms = btc_trend == confirming_trend
    btc_opposes  = btc_trend == opposing_trend

    # Both confirm → strong signal
    if own_confirms and btc_confirms:
        return 1.0

    # Own confirms, BTC neutral → decent signal
    if own_confirms and btc_trend == "neutral":
        return 0.75

    # BTC confirms, own neutral → BTC is master signal, still tradeable
    if btc_confirms and own_trend == "neutral":
        return 0.70

    # Both neutral → no directional information
    if own_trend == "neutral" and btc_trend == "neutral":
        return 0.55   # Score will likely miss threshold when combined with other factors

    # Either opposes → conflicting, dangerous
    if own_opposes or btc_opposes:
        return 0.30   # Natural kill — score math handles this

    return 0.55   # Catch-all


# ─────────────────────────────────────────────────────────────
# PRICE SCORE — EV quality of entry price
# ─────────────────────────────────────────────────────────────
# Based on actual fill data analysis (Mar 5–10, 2026):
#   90-95¢ entries: EV = -$0.002/fill (slightly NEGATIVE)
#   95-100¢ entries: EV = +$0.055/fill (positive)
#
# Mid-price (70-89¢) entries are UNTESTED but represent larger upside —
# require stronger overall signal to justify the uncertainty.

def price_score(price_cents: int, side: str) -> float:
    """
    Score 0.0–1.0 based on EV quality of entry price.
    Higher price = higher market confidence but lower upside.
    Lower price = higher upside but requires stronger signal confirmation.
    """
    p = price_cents

    # NO contracts (our primary edge — bot bets price stays flat/goes down)
    if side == "no":
        if p >= 97: return 1.00   # Near-certain, EV-positive, take it
        if p >= 95: return 0.90   # EV-positive from data
        if p >= 90: return 0.55   # EV-negative from data — needs very strong signal
        if p >= 85: return 0.70   # Mid-price: more upside, untested, score-gated
        if p >= 80: return 0.65   # Mid-price: good upside, higher signal required
        if p >= 75: return 0.60   # Mid-price: strong upside, strong signal required
        if p >= 70: return 0.50   # Speculative — needs near-perfect signal
        return 0.30               # Below 70¢ — very speculative, almost always kills

    # YES contracts (less common, higher price = betting on upward move confirmed)
    else:
        if p >= 97: return 1.00
        if p >= 95: return 0.90
        if p >= 90: return 0.55   # Same EV profile as NO
        if p >= 85: return 0.65
        if p >= 80: return 0.60
        return 0.35


# ─────────────────────────────────────────────────────────────
# VOL MULTIPLIER — current volatility regime
# ─────────────────────────────────────────────────────────────
# avg_range: average 5-min candle high-low range from live Coinbase data
# Asset-specific bands (calibrated to typical ranges):
#   BTC: low<50, med=50-150, high=150-300, extreme>300
#   ETH: low<2,  med=2-8,    high=8-20,   extreme>20
#   SOL: low<0.1, med=0.1-0.5, high=0.5-1.5, extreme>1.5
#   XRP: low<0.005, med=0.005-0.02, high=0.02-0.05, extreme>0.05
#
# High vol = orderbook price less trustworthy = lower multiplier

VOL_BANDS = {
    'BTC': [(50, 1.00), (150, 0.85), (300, 0.70), (float('inf'), 0.50)],
    'ETH': [(2,  1.00), (8,   0.85), (20,  0.70), (float('inf'), 0.50)],
    'SOL': [(0.10, 1.00), (0.50, 0.85), (1.50, 0.70), (float('inf'), 0.50)],
    'XRP': [(0.005, 1.00), (0.02, 0.85), (0.05, 0.70), (float('inf'), 0.50)],
}

def vol_multiplier(asset: str, avg_range: float) -> float:
    """
    Returns 0.50–1.00 based on current volatility regime.
    Low vol = trust the orderbook price. High vol = discount it.
    """
    bands = VOL_BANDS.get(asset, VOL_BANDS['BTC'])
    for threshold, mult in bands:
        if avg_range < threshold:
            return mult
    return 0.50


# ─────────────────────────────────────────────────────────────
# TIME SCORE — time remaining to close (from V9, unchanged)
# ─────────────────────────────────────────────────────────────
# Earlier entry = more uncertainty but potentially better price.
# V9's time_weight was validated — keeping it.

BUY_START_SECONDS = 600   # Bot starts watching at T-600s (10 min)

def time_score(secs_to_close: float) -> float:
    """
    Score 0.5–1.0 based on time remaining.
    T-30s = 1.0 (last chance, high confidence from market movement)
    T-600s = 0.5 (earliest entry, maximum uncertainty)
    Linear interpolation between.
    """
    if secs_to_close <= 0:
        return 1.0
    if secs_to_close >= BUY_START_SECONDS:
        return 0.50
    # Linear: 600s → 0.50, 0s → 1.0
    return 1.0 - (secs_to_close / BUY_START_SECONDS) * 0.50


# ─────────────────────────────────────────────────────────────
# TIER CLASSIFICATION
# ─────────────────────────────────────────────────────────────

def score_to_tier(entry_score: float) -> Optional[str]:
    """Returns 'HIGH', 'MEDIUM', 'LOW', or None (no trade)."""
    if entry_score >= SCORE_HIGH_THRESHOLD:
        return 'HIGH'
    elif entry_score >= SCORE_MED_THRESHOLD:
        return 'MEDIUM'
    elif entry_score >= MIN_SCORE_TO_TRADE:
        return 'LOW'
    return None


# ─────────────────────────────────────────────────────────────
# CONTRACT SIZING
# ─────────────────────────────────────────────────────────────

def compute_contracts(entry_score: float, price_cents: int, live_balance: float) -> int:
    """
    Tier → bankroll % → contract count.
    Hard cap: MAX_RISK_PCT × balance. Absolute cap: MAX_CONTRACTS_ABS.
    """
    tier = score_to_tier(entry_score)
    if tier is None or price_cents <= 0 or live_balance <= 0:
        return 0

    tier_pct      = TIER_PCT[tier]
    price_dollars = price_cents / 100.0

    raw      = int(tier_pct * live_balance / price_dollars)
    hard_cap = int(MAX_RISK_PCT * live_balance / price_dollars)
    result   = max(1, min(raw, hard_cap, MAX_CONTRACTS_ABS))

    log.debug(f"[SIZE] tier={tier} pct={tier_pct:.0%} bal=${live_balance:.2f} price={price_cents}¢ → {result} contracts")
    return result


# ─────────────────────────────────────────────────────────────
# UNIFIED ENTRY EVALUATION
# ─────────────────────────────────────────────────────────────

def evaluate_entry(
    asset: str,
    side: str,
    price_cents: int,
    own_trend: str,
    btc_trend: str,
    avg_range: float,
    secs_to_close: float,
    live_balance: float,
) -> Optional[Tuple[int, int, str, float]]:
    """
    Full V10 entry evaluation.
    Returns (price_cents, contracts, tier, entry_score) or None if no trade.

    entry_score = signal_score × price_score × vol_multiplier × time_score
    """
    sig  = signal_score(own_trend, btc_trend, side)
    ps   = price_score(price_cents, side)
    vm   = vol_multiplier(asset, avg_range)
    ts   = time_score(secs_to_close)

    score = sig * ps * vm * ts

    tier = score_to_tier(score)

    log.warning(
        f"[V10] {asset} {side}@{price_cents}¢ | "
        f"signal={sig:.2f} price={ps:.2f} vol={vm:.2f} time={ts:.2f} → "
        f"score={score:.3f} tier={tier or 'NO TRADE'}"
    )

    if tier is None:
        return None

    contracts = compute_contracts(score, price_cents, live_balance)
    if contracts == 0:
        return None

    return price_cents, contracts, tier, score
