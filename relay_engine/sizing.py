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
               visible_depth: int, lane: str = None,
               notional_pct: float = None) -> SizeDecision:
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
        # fallback-audited: RULING-3 (WO-P §B3) — a real book with >=1 visible
        # lot admits 1 lot even when the DEPTH_FRACTION rounds to zero. This is
        # depth-DRIVEN and HONEST: the guard fires only when the book actually
        # SHOWS >=1 resting lot. A blind/empty book never reaches here — it
        # deferred as DEPTH_BLIND / SIZE_ZERO_DEFER upstream (shadow_runner).
        depth_max = max(1, depth_max)  # fallback-audited: RULING-3 depth-driven floor
    if lane == "F":
        # F's dial: notional = pct of book, bounded by depth only. Kelly and
        # the count cap do NOT bind F (the whole point of the WO). count=1
        # floor is applied by the caller (_score_and_size), as before.
        # WO-2026-07-26-S: the caller passes the ROOM's dial via notional_pct
        # (f_notional_pct_of series). Absent → the earned BTC F_NOTIONAL_PCT, so
        # the BTC F path is byte-identical.
        f_dial = notional_pct if notional_pct is not None else config.F_NOTIONAL_PCT
        notional_max = int(book_cents * f_dial // price_cents)
        contracts = min(notional_max, depth_max)
        bound = "notional" if notional_max <= depth_max else "depth"
        return SizeDecision(
            "-", contracts,
            f"F: notional={notional_max} depth={depth_max} "
            f"(kelly={kelly_max}, cap n/a) → {bound} bound")
    if lane == "FLIP":
        # WO-2026-07-24-D Part 1: FLIP self-scales by NOTIONAL (pct of book), not
        # Kelly (which capped it at ~5-6 on a $43 book).
        # WO-2026-07-24-G Part 2 "FLOODGATES": the fixed FLIP_SIZE_CAP is RETIRED
        # as a binder — at a ~$90 book notional says ~21 lots and a 10-cap would
        # freeze FLIP exactly when Drew ruled it should SCALE (the same
        # count-vs-compound disease P27 killed for Kelly). FLIP = min(notional,
        # depth), like F: depth is the term the size test measures, the per-lane
        # at-risk WALL is the (book-proportional) backstop above. Guard (d): all
        # terms + kelly (n/a) + the binding one logged.
        # WO-2026-07-25-K §P3: the notional is the DESK LADDER's active dial —
        # tuition (FLIP_NOTIONAL_PCT) unless the caller passes the promoted pct
        # (full, gated on conversion + the entry cell's margin). Default = the
        # tuition floor, so a bare size_order (tests, boot preview) reads tuition.
        pct = config.FLIP_NOTIONAL_PCT if notional_pct is None else notional_pct
        notional_max = int(book_cents * pct // price_cents)
        contracts = min(notional_max, depth_max)
        term = {"notional": notional_max, "depth": depth_max}
        bound = min(term, key=term.get)
        return SizeDecision(
            "-", contracts,
            f"FLIP: min(notional={notional_max}@{pct:.0%}, depth={depth_max}) → "
            f"{bound} bound (no cap — scales with book; kelly n/a={kelly_max})")
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
