# scoring.py — Universal weighted scoring system for Kalshi bots
# V9 (Feb 21, 2026)
#
# TWO INDEPENDENT SCORES:
# entry_score  → gates whether to trade + determines posting aggressiveness
# sizing_score → determines contract count as % of live bankroll
#
# SCORE FORMULA:
# entry_score  = win_rate^1.5 × kelly × sample_weight × gap_weight × time_weight
# sizing_score = win_rate^2.0 × kelly × sample_weight × gap_weight × time_weight
#
# TIER MAPPING (entry_score):
# ≥ 0.70 → HIGH   → 18% of bankroll
# ≥ 0.40 → MEDIUM → 10% of bankroll
# ≥ 0.15 → LOW    →  4% of bankroll
# < 0.15 → NO TRADE (natural kill — score math did it, not a hardcoded rule)
#
# CONTRACT COUNT:
# contracts = floor((tier_pct × live_balance) / (price_cents / 100))
# hard_cap  = floor((0.20 × live_balance) / (price_cents / 100))
# final     = min(contracts, hard_cap), minimum 1 if tier clears threshold

import math
import logging
from typing import Optional, Tuple

from config import (
    HISTORICAL_ACCURACY,
    BUY_START_SECONDS,
    GAP_SCALE_CENTS,
    SAMPLE_CONFIDENCE_TARGET,
    ENTRY_WR_EXP,
    SIZING_WR_EXP,
    MIN_SCORE_TO_TRADE,
    SCORE_HIGH_THRESHOLD,
    SCORE_MED_THRESHOLD,
    TIER_PCT,
    MAX_RISK_PCT,
)

log = logging.getLogger("scoring")

# ─────────────────────────────────────────────────────────────
# BUCKET LOOKUP
# ─────────────────────────────────────────────────────────────

def get_bucket_key(side: str, price_cents: int) -> Optional[str]:
    """
    Map (side, price) → HISTORICAL_ACCURACY bucket key.
    Returns None only if price is literally out of 1-99 range.
    Every valid price has a bucket — even weak ones.
    """
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


def get_historical_stats(asset: str, side: str, price_cents: int) -> Optional[Tuple[float, int, float]]:
    """
    Returns (win_rate, sample_size, kelly) for this asset/side/price.
    Returns None only if bucket key lookup fails.
    """
    key = get_bucket_key(side, price_cents)
    if key is None:
        return None

    asset_data = HISTORICAL_ACCURACY.get(asset, {})

    # Try exact key first
    if key in asset_data:
        return asset_data[key]

    # Fallback: try broader bucket mappings for assets with merged buckets
    # e.g. SOL uses 'NO_21_50' as a merged key
    fallbacks = {
        'NO_21_30': ['NO_21_50'],
        'NO_31_40': ['NO_21_50', 'NO_31_50'],
        'NO_41_50': ['NO_21_50', 'NO_31_50'],
        'YES_81_90': ['YES_81_99'],
        'YES_91_99': ['YES_81_99'],
    }
    for alt in fallbacks.get(key, []):
        if alt in asset_data:
            return asset_data[alt]

    # No data at all — return a placeholder that will score below MIN_SCORE_TO_TRADE
    return (0.50, 1, 0.05)


# ─────────────────────────────────────────────────────────────
# WEIGHT COMPONENTS
# ─────────────────────────────────────────────────────────────

def sample_weight(sample_size: int) -> float:
    """
    Confidence in the historical statistic itself.
    100 markets = full weight (1.0), 10 markets = 0.10, 50 markets = 0.50.
    Prevents small-sample buckets from scoring high even with 100% WR.
    """
    return min(1.0, sample_size / SAMPLE_CONFIDENCE_TARGET)


def gap_weight(gap_cents: float) -> float:
    """
    How much the model leads the current book price.
    0¢ gap = 0.0, 10¢+ gap = 1.0, linear.
    At 0 gap the score is 0 — model agrees with market, no edge.
    """
    return min(1.0, max(0.0, gap_cents / GAP_SCALE_CENTS))


def time_weight(secs_to_close: float) -> float:
    """
    Late entries are higher confidence (signal vs noise).
    At entry window open (600s): weight=0.0
    At 60s remaining: weight=0.90
    At 10s remaining: weight=0.98
    Formula: 1 - (secs_to_close / BUY_START_SECONDS), clamped 0-1
    """
    if BUY_START_SECONDS <= 0:
        return 1.0
    raw = 1.0 - (secs_to_close / BUY_START_SECONDS)
    return min(1.0, max(0.0, raw))


# ─────────────────────────────────────────────────────────────
# CORE SCORING
# ─────────────────────────────────────────────────────────────

def compute_scores(
    asset: str,
    side: str,
    price_cents: int,
    gap_cents: float,
    secs_to_close: float,
) -> Tuple[float, float]:
    """
    Returns (entry_score, sizing_score).

    entry_score  = win_rate^1.5 × kelly × sample_wt × gap_wt × time_wt
    sizing_score = win_rate^2.0 × kelly × sample_wt × gap_wt × time_wt

    Both range 0.0–1.0.
    entry_score  < MIN_SCORE_TO_TRADE (0.15) → no trade
    sizing_score drives tier selection for contract count.
    """
    stats = get_historical_stats(asset, side, price_cents)
    if stats is None:
        return 0.0, 0.0

    win_rate, n_samples, kelly = stats

    sw = sample_weight(n_samples)
    gw = gap_weight(gap_cents)
    tw = time_weight(secs_to_close)

    # Additive bonus formula:
    #   base = win_rate^exp × kelly × sample_weight
    #   bonus = 0.3 × gap_weight + 0.2 × time_weight  (max 0.5 additive)
    #   final = base × (1 + bonus)
    #
    # Gap and time ENHANCE a good score but can't rescue a bad one.
    # A 50% WR bucket with perfect gap/timing still won't clear MIN_SCORE.
    # A 100% WR bucket with no gap scores lower but still enters (correctly).
    bonus = 0.3 * gw + 0.2 * tw

    entry_score  = (win_rate ** ENTRY_WR_EXP)  * kelly * sw * (1.0 + bonus)
    sizing_score = (win_rate ** SIZING_WR_EXP) * kelly * sw * (1.0 + bonus)

    log.debug(
        f"[SCORE] {asset} {side}@{price_cents}¢ | "
        f"wr={win_rate:.3f} kelly={kelly:.2f} sw={sw:.2f} gw={gw:.2f} tw={tw:.2f} | "
        f"entry={entry_score:.3f} sizing={sizing_score:.3f}"
    )

    return entry_score, sizing_score


# ─────────────────────────────────────────────────────────────
# TIER CLASSIFICATION
# ─────────────────────────────────────────────────────────────

def score_to_tier(entry_score: float) -> Optional[str]:
    """
    Returns 'HIGH', 'MEDIUM', 'LOW', or None (no trade).
    Thresholds from config — derived from what data says is profitable.
    """
    if entry_score >= SCORE_HIGH_THRESHOLD:
        return 'HIGH'
    elif entry_score >= SCORE_MED_THRESHOLD:
        return 'MEDIUM'
    elif entry_score >= MIN_SCORE_TO_TRADE:
        return 'LOW'
    else:
        return None   # Natural kill — score math decided, not a rule


# ─────────────────────────────────────────────────────────────
# CONTRACT SIZING
# ─────────────────────────────────────────────────────────────

def compute_contracts(
    sizing_score: float,
    price_cents: int,
    live_balance: float,
) -> int:
    """
    Sizing score → tier → bankroll % → contract count.

    contracts = floor((tier_pct × live_balance) / (price_cents / 100))
    hard_cap  = floor((MAX_RISK_PCT × live_balance) / (price_cents / 100))
    final     = max(1, min(contracts, hard_cap))

    At $120 balance, 8¢ price:
      HIGH:   floor(0.18 × 120 / 0.08) = 270 → capped at floor(0.20 × 120 / 0.08) = 300
      MEDIUM: floor(0.10 × 120 / 0.08) = 150
      LOW:    floor(0.04 × 120 / 0.08) =  60

    Wait — that's too many contracts at low prices.
    At 8¢, 30 contracts = $2.40. The % approach works better at mid prices.
    Solution: also cap by MAX_CONTRACTS_ABS = 50.
    """
    if price_cents <= 0 or live_balance <= 0:
        return 0

    # Determine tier from sizing_score (uses same thresholds as entry)
    tier = score_to_tier(sizing_score)
    if tier is None:
        return 0

    tier_pct = TIER_PCT[tier]
    price_dollars = price_cents / 100.0

    # Raw from bankroll %
    raw = int(tier_pct * live_balance / price_dollars)

    # Hard cap: 20% of bankroll
    hard_cap = int(MAX_RISK_PCT * live_balance / price_dollars)

    # Absolute contract ceiling (prevents absurd counts at 1¢ prices)
    MAX_CONTRACTS_ABS = 50

    result = max(1, min(raw, hard_cap, MAX_CONTRACTS_ABS))

    log.debug(
        f"[SIZE] tier={tier} tier_pct={tier_pct:.0%} "
        f"balance=${live_balance:.2f} price={price_cents}¢ | "
        f"raw={raw} hard_cap={hard_cap} final={result}"
    )

    return result


# ─────────────────────────────────────────────────────────────
# ENTRY POSTING PRICE
# ─────────────────────────────────────────────────────────────

def compute_post_price(
    entry_score: float,
    book_ask: int,
    model_fair: int,
    side: str,
) -> int:
    """
    Entry score drives posting aggressiveness.

    HIGH score   → post at book_ask (take liquidity, want the fill)
    MEDIUM score → post 1¢ inside book_ask (patient, slightly better price)
    LOW score    → post 2¢ inside book_ask (very patient, only fills if book moves to us)

    For YES (expensive contracts): always post at ask — don't give up edge on payout.
    For NO (cheap contracts): can afford to be patient.
    """
    tier = score_to_tier(entry_score)
    if tier is None:
        return book_ask  # Won't be used — entry blocked

    if side.lower() == 'yes':
        # YES contracts: post at ask always (payout asymmetry means price matters)
        return book_ask

    # NO contracts: aggressiveness based on tier
    if tier == 'HIGH':
        offset = 0    # Post at ask — fill immediately
    elif tier == 'MEDIUM':
        offset = 1    # 1¢ inside ask — patient
    else:
        offset = 2    # 2¢ inside ask — very patient

    # Post price must be ≥ 1¢ and ≤ 99¢
    post = max(1, min(99, book_ask - offset))
    return post


# ─────────────────────────────────────────────────────────────
# UNIFIED ENTRY DECISION
# ─────────────────────────────────────────────────────────────

def evaluate_entry(
    asset: str,
    side: str,
    book_ask: int,
    model_fair_cents: int,
    secs_to_close: float,
    live_balance: float,
) -> Optional[Tuple[int, int, str, float, float]]:
    """
    Full entry evaluation. Returns (post_price, contracts, tier, entry_score, sizing_score)
    or None if no trade.

    gap_cents = model_fair_cents - book_ask
    Positive gap = model thinks it's worth more than the book is charging.
    That's the edge.
    """
    gap_cents = float(model_fair_cents - book_ask)

    # Negative gap = model thinks contract is overpriced relative to book
    # Still allow entry if gap > -2 (model nearly agrees), but score will be very low
    entry_score, sizing_score = compute_scores(
        asset, side, book_ask, max(0.0, gap_cents), secs_to_close
    )

    tier = score_to_tier(entry_score)
    if tier is None:
        log.info(
            f"[EVAL] {asset} {side}@{book_ask}¢ gap={gap_cents:.1f}¢ "
            f"t={secs_to_close:.0f}s | entry_score={entry_score:.3f} → NO TRADE"
        )
        return None

    post_price = compute_post_price(entry_score, book_ask, model_fair_cents, side)
    contracts  = compute_contracts(sizing_score, post_price, live_balance)

    if contracts == 0:
        return None

    log.warning(
        f"[ENTRY] {asset} {side}@{post_price}¢ (ask={book_ask}¢) "
        f"gap={gap_cents:.1f}¢ t={secs_to_close:.0f}s | "
        f"tier={tier} entry={entry_score:.3f} sizing={sizing_score:.3f} | "
        f"contracts={contracts} cost=${contracts * post_price / 100:.2f}"
    )

    return post_price, contracts, tier, entry_score, sizing_score
