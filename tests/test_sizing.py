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


def test_capital_raises_budgets_never_tiers():
    # same evidence, 100x the book: tier identical, contracts may grow within tier
    small = size_order(config.TIER_PROBE, book_cents=1_000, price_cents=60, visible_depth=100)
    large = size_order(config.TIER_PROBE, book_cents=100_000, price_cents=60, visible_depth=100)
    assert small.tier == large.tier == config.TIER_PROBE
    assert large.contracts <= config.TIER_MAX_CONTRACTS[config.TIER_PROBE]  # tier still caps


def test_kelly_ceiling():
    # book 1200c, ceiling 1/12 -> 100c budget; at 60c that's 1 contract
    d = size_order(config.TIER_CLEAR, book_cents=1_200, price_cents=60, visible_depth=1_000)
    assert d.contracts == 1


def test_depth_fraction_cap():
    # DREW-DEFAULT 25% of visible depth: depth 8 -> max 2
    d = size_order(config.TIER_CLEAR, book_cents=1_000_000, price_cents=60, visible_depth=8)
    assert d.contracts == 2


def test_thin_book_backoff():
    thin = size_order(config.TIER_CLEAR, book_cents=1_000_000, price_cents=60,
                      visible_depth=config.THIN_BOOK_MIN_DEPTH - 1)
    assert thin.tier == config.TIER_LEAN  # one tier down
    d = size_order(config.TIER_PROBE, book_cents=1_000_000, price_cents=60, visible_depth=1)
    assert d.contracts == 0  # PROBE backs off to SUPPRESS on a thin book
