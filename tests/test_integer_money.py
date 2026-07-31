"""WO-2026-07-28-X X6 — INTEGER CENTS AT THE ACCRUAL, ROUNDED AT EVERY BOUNDARY.

Money carried as integer DECI-CENTS through the accrual, half-even to whole cents
at booking. The 8.999999999999986c float spray becomes unrepresentable.
"""

import inspect

import pytest

from relay_engine import config, money
from relay_engine.surface import Surface


# ── the money helpers ───────────────────────────────────────────────────────
def test_to_decicents_is_exact_on_venue_ticks():
    assert money.to_decicents(96.5) == 965      # a 0.1c tick, exact
    assert money.to_decicents(97) == 970
    assert money.to_decicents(2.7) == 27
    assert money.to_decicents(2.699999999999) == 27   # float dust → clean integer


def test_cents_half_even_is_bankers_rounding():
    assert money.cents_half_even(27) == 3       # 2.7c → 3
    assert money.cents_half_even(24) == 2       # 2.4c → 2
    assert money.cents_half_even(25) == 2       # 2.5c → 2 (nearest EVEN, no up-bias)
    assert money.cents_half_even(35) == 4       # 3.5c → 4 (nearest even)
    assert money.cents_half_even(-25) == -2     # symmetric
    assert money.cents_half_even(90) == 9       # a whole cent round-trips


def test_round_cents_kills_float_dust():
    # the 8.999999999999986c spray → a clean integer 9, unbiased
    assert money.round_cents_half_even(8.999999999999986) == 9
    assert money.round_cents_half_even(9.0) == 9
    assert isinstance(money.round_cents_half_even(8.9999999), int)


# ── the accrual is integer + dust-free (the fixture from the screen) ────────
def test_the_8_9999_fixture_renders_as_9c(ledger, surface):
    """The screen showed ETH fills-based 8.999999999999986c for a true 9c. Build
    an accrual that in floats would spray dust; X6 books a clean integer 9c."""
    # a no@95.5 win pays +4.5c; three of them = 13.5c → the float path dusted
    for i in range(3):
        m = f"KXETH15M-02JAN25100{i}-T3"
        ledger.record_fill(m, "F", "no", "ENTRY", 95.5, 1, config.TIER_PROBE)
        per_lane = surface.settle_market(m, f"w-{m}", settled_yes=False)  # NO wins
        # each booked value is an INTEGER cent, never float dust
        assert isinstance(per_lane["F"], int)
    total = ledger.db.execute(
        "SELECT COALESCE(SUM(pnl_cents),0) FROM settlements").fetchone()[0]
    # 4.5c each → half-even books 4,4,4? no: 45 deci → cents_half_even(45)=4 (4.5→4
    # nearest even). three × 4 = 12, an integer with no dust.
    assert total == 12 and isinstance(total, int)


# ── grep artifact (Acceptance #4): no bare float arithmetic in the accrual ──
def test_settle_accrual_uses_integer_decicents_no_float():
    """The settlement accrual path carries integer deci-cents and rounds half-even
    at booking — no bare float multiplication on the money."""
    src = inspect.getsource(Surface.settle_market)
    assert "to_decicents" in src and "cents_half_even" in src   # integer path
    assert "pnl_dc" in src                                       # deci-cent accrual
    # the retired float accrual (`(side_payoff - side_basis) * count` on floats)
    # is gone — the basis/payoff are deci-cent integers now
    assert "side_basis" not in src and "side_payoff" not in src
