"""Sizing born-in: Wilson tiers, ~1/12 Kelly ceiling, depth_fraction cap,
thin-book backoff; capital raises budgets, never tiers."""

from relay_engine import config
from relay_engine.sizing import size_order, tier_for, wilson_lower_bound


def test_wilson_lower_bound_basics():
    assert wilson_lower_bound(0, 0) == 0.0
    assert wilson_lower_bound(10, 10) < 1.0  # small n never proves certainty
    assert wilson_lower_bound(90, 100) > wilson_lower_bound(9, 10)  # evidence tightens


def test_tiers_move_on_wilson_lower_bound_only():
    assert tier_for(0, 0) == config.TIER_SUPPRESS
    assert tier_for(3, 4) == config.TIER_SUPPRESS      # hot streak, no evidence
    assert tier_for(30, 40) in (config.TIER_PROBE, config.TIER_LEAN)
    assert tier_for(950, 1000) == config.TIER_CLEAR


def test_full_kelly_no_tier_term():
    """P27 §1 OVERTURNED 'capital raises budgets, never tiers': sizing is
    min(kelly, depth, net-risk cap) — the tier ladder REPORTS (tier_for
    above stays tested) and never votes. Drew's ruling, twice."""
    small = size_order(book_cents=1_000, price_cents=60, visible_depth=100)
    large = size_order(book_cents=100_000, price_cents=60, visible_depth=100)
    assert small.contracts == 1          # kelly caps: 83c budget // 60c
    assert large.contracts == config.NET_RISK_CROSS_LANE_CAP  # the kept wall caps
    # THE §5 case: 2 lots at 49c at the current book — depth-bounded
    d = size_order(book_cents=10_000, price_cents=49, visible_depth=10)
    assert d.contracts == 2


def test_kelly_ceiling():
    # book 1200c, ceiling 1/12 -> 100c budget; at 60c that's 1 contract
    d = size_order(book_cents=1_200, price_cents=60, visible_depth=1_000)
    assert d.contracts == 1


def test_depth_fraction_cap():
    # DREW-DEFAULT 25% of visible depth: depth 8 -> max 2
    d = size_order(book_cents=1_000_000, price_cents=60, visible_depth=8)
    assert d.contracts == 2


def test_depth_floor_stands():
    """RULING 3 (P15) survives P27 — it is depth doctrine, not a governor:
    >=1 visible lot admits ONE lot; an empty book admits nothing. (The
    tier backoff died with the tier term.)"""
    d = size_order(book_cents=1_000_000, price_cents=60, visible_depth=1)
    assert d.contracts == 1
    empty = size_order(book_cents=1_000_000, price_cents=60, visible_depth=0)
    assert empty.contracts == 0
