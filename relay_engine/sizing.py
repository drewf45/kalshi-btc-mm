"""Sizing — born-in (C.3 / Charter §8). P16-B ladder on Wilson lower bounds.

Tiers: SUPPRESS / PROBE / LEAN / CLEAR, admitted strictly by the Wilson lower
bound of the lane's cell record. Size is then capped by ALL of:
  - the tier's max contracts,
  - ~1/12 Kelly ceiling on the book,
  - per-level size <= depth_fraction of visible depth (thin-book backoff below).
CAPITAL RAISES BUDGETS, NEVER TIERS: more book means bigger budgets inside a
tier; only Wilson evidence moves the tier.

Win/loss path symmetry: the Wilson cell counts wins and losses in the same
record; a loss lowers the bound exactly as a win raises it — sizing reads one
number either way.
"""

import math
from dataclasses import dataclass

from . import config


def wilson_lower_bound(wins: int, n: int, z: float = config.WILSON_Z) -> float:
    """Lower bound of the Wilson score interval for a win-rate observation."""
    if n == 0:
        return 0.0
    phat = wins / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def tier_for(wins: int, n: int) -> str:
    lb = wilson_lower_bound(wins, n)
    if lb >= config.TIER_LOWER_BOUNDS[config.TIER_CLEAR]:
        return config.TIER_CLEAR
    if lb >= config.TIER_LOWER_BOUNDS[config.TIER_LEAN]:
        return config.TIER_LEAN
    if lb >= config.TIER_LOWER_BOUNDS[config.TIER_PROBE]:
        return config.TIER_PROBE
    return config.TIER_SUPPRESS


_TIER_ORDER = [config.TIER_SUPPRESS, config.TIER_PROBE, config.TIER_LEAN, config.TIER_CLEAR]


@dataclass
class SizeDecision:
    tier: str
    contracts: int
    reason: str


def size_order(tier: str, book_cents: int, price_cents: int, visible_depth: int) -> SizeDecision:
    """Contracts for one entry at one level. Tier is an input — this function can
    only shrink within it (capital raises budgets, never tiers)."""
    effective_tier = tier
    if visible_depth < config.THIN_BOOK_MIN_DEPTH and tier != config.TIER_SUPPRESS:
        effective_tier = _TIER_ORDER[_TIER_ORDER.index(tier) - 1]  # thin-book backoff: one tier down
    tier_max = config.TIER_MAX_CONTRACTS[effective_tier]
    if tier_max == 0 or price_cents <= 0:
        return SizeDecision(effective_tier, 0, "suppressed")
    kelly_budget_cents = book_cents * config.KELLY_FRACTION_CEILING
    kelly_max = int(kelly_budget_cents // price_cents)
    depth_max = int(visible_depth * config.DEPTH_FRACTION)
    contracts = max(0, min(tier_max, kelly_max, depth_max))
    return SizeDecision(
        effective_tier, contracts,
        f"min(tier={tier_max}, kelly={kelly_max}, depth={depth_max})",
    )
