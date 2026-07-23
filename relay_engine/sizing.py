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
               visible_depth: int, lane: str = None) -> SizeDecision:
    """P27 §1 — SIZING = FULL KELLY; GOVERNORS DIE (Drew's ruling, twice):
    contracts = min(kelly_lots, depth_lots). The tier term is REMOVED from
    the entry path — the Wilson ladder remains as reporting (scoreboard,
    pages, custody scaling), but it no longer votes. The account halt is
    THE stop.

    RULING 3 (P15, ratified) stands — it is depth doctrine, not a
    governor: a real book with >=1 visible lot admits ONE lot even when
    the fraction rounds to zero (the 7:58 depth-starvation storms).

    WO-2026-07-23-B Part 1 — F ALONE self-scales: `lane="F"` sizes to a
    percentage of book (F_NOTIONAL_PCT), bounded only by REAL depth — NOT by
    Kelly and NOT by the count cap (the constant that converted F's compound
    growth into linear growth). Guard (d): kelly_max, depth_max, and
    notional_max all ride the reason so "is depth ever real" is answered
    permanently. Every other lane is unchanged."""
    if price_cents <= 0:
        return SizeDecision("-", 0, "no price")
    kelly_budget_cents = book_cents * config.KELLY_FRACTION_CEILING
    kelly_max = int(kelly_budget_cents // price_cents)
    depth_max = int(visible_depth * config.DEPTH_FRACTION)
    if visible_depth >= 1:
        depth_max = max(1, depth_max)
    if lane == "F":
        # F's dial: notional = pct of book, bounded by depth only. Kelly and
        # the count cap do NOT bind F (the whole point of the WO). count=1
        # floor is applied by the caller (_score_and_size), as before.
        notional_max = int(book_cents * config.F_NOTIONAL_PCT // price_cents)
        contracts = min(notional_max, depth_max)
        bound = "notional" if notional_max <= depth_max else "depth"
        return SizeDecision(
            "-", contracts,
            f"F: notional={notional_max} depth={depth_max} "
            f"(kelly={kelly_max}, cap n/a) → {bound} bound")
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
