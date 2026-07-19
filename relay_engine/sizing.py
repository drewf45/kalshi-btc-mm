"""Sizing — P27 "THE GOVERNOR IS THE HALT": full Kelly, bounded by depth.

Size = min(~1/12-Kelly on the book, depth_fraction of visible depth).
The Wilson tier ladder (SUPPRESS/PROBE/LEAN/CLEAR, tier_for below) REMAINS
as REPORTING — the scoreboard, the tier pages, and custody scaling read
it — but nothing on the entry path consumes it. Walls stop bugs, custody
stops losses, the account halt stops bad days; nothing stops trading.

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
    tier: str        # P27: the REPORTING stamp only — it never caps contracts
    contracts: int
    reason: str


def size_order(book_cents: int, price_cents: int,
               visible_depth: int) -> SizeDecision:
    """P27 §1 — SIZING = FULL KELLY; GOVERNORS DIE (Drew's ruling, twice):
    contracts = min(kelly_lots, depth_lots). The tier term is REMOVED from
    the entry path — the Wilson ladder remains as reporting (scoreboard,
    pages, custody scaling), but it no longer votes. The account halt is
    THE stop.

    RULING 3 (P15, ratified) stands — it is depth doctrine, not a
    governor: a real book with >=1 visible lot admits ONE lot even when
    the fraction rounds to zero (the 7:58 depth-starvation storms)."""
    if price_cents <= 0:
        return SizeDecision("-", 0, "no price")
    kelly_budget_cents = book_cents * config.KELLY_FRACTION_CEILING
    kelly_max = int(kelly_budget_cents // price_cents)
    depth_max = int(visible_depth * config.DEPTH_FRACTION)
    if visible_depth >= 1:
        depth_max = max(1, depth_max)
    # The kept walls are LAW (P27 §2d): net-risk <=3/event stands, so
    # sizing proposes at most the cap — full Kelly lives UNDER the wall,
    # it does not fight it (a 7-lot proposal dying whole at the wall would
    # be a governor by accident).
    contracts = max(0, min(kelly_max, depth_max,
                           config.NET_RISK_CROSS_LANE_CAP))
    return SizeDecision(
        "-", contracts,
        f"min(kelly={kelly_max}, depth={depth_max}, "
        f"risk_cap={config.NET_RISK_CROSS_LANE_CAP})",
    )
