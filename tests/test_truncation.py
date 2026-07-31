"""WO-2026-07-23-F Part 1 — FIX THE TRUNCATION. The venue ticks in 0.1c and
fills carry exact fractions (42% of them). `to_yes_terms` used int(), which
truncated toward zero — understating the cost basis and OVERSTATING profit,
always in the same direction, on every fractional fill. The settlement path now
carries the fraction and book_cents rounds ONCE, the rule it already followed.

HARD RAIL: instrument/accounting only — no trading behaviour change; F byte-identical."""

import pytest

from relay_engine import config
from relay_engine.book import to_yes_terms

MKT = "KXBTC15M-02JAN251000-T99"
WIN = "w-" + MKT


def test_to_yes_terms_preserves_the_fraction():
    assert to_yes_terms("yes", 98.4) == 98.4          # not truncated to 98
    assert to_yes_terms("no", 97.3) == pytest.approx(2.7)   # 100−97.3, not 100−97=3
    assert to_yes_terms("yes", 60) == 60.0            # whole numbers unchanged
    assert isinstance(to_yes_terms("no", 55), float)


def test_settlement_pnl_books_half_even_integer_cents(ledger, surface):
    """WO-2026-07-28-X X6 rerules WO-23-F Part 1: `to_yes_terms` still preserves
    the 0.1c fraction (no truncation bias), but the settlement ACCRUES in integer
    deci-cents and books HALF-EVEN whole cents — no float touches the money math.
    A no@97.3 win pays +2.7c/contract → books 3c (half-even), an INTEGER, never
    2.699999…c of float dust."""
    ledger.record_fill(MKT, "F", "no", "ENTRY", 97.3, 1, config.TIER_PROBE)
    surface.settle_market(MKT, WIN, settled_yes=False)   # NO wins
    pnl = ledger.db.execute(
        "SELECT pnl_cents FROM settlements WHERE market=? AND lane='F'",
        (MKT,)).fetchone()[0]
    assert pnl == 3 and isinstance(pnl, int)          # half-even integer, no dust


def test_settlement_accrual_is_integer_and_dust_free(ledger, surface):
    """X6: the float spray is unrepresentable. Two no@97.3 wins each book an
    INTEGER 3c (half-even) — the settlements table holds integers, book_cents sums
    integers, and no value carries `…999986` dust anywhere."""
    for i, mkt in enumerate((MKT, MKT + "-B")):
        ledger.record_fill(mkt, "F", "no", "ENTRY", 97.3, 1, config.TIER_PROBE)
        surface.settle_market(mkt, "w-" + mkt, settled_yes=False)
    rows = ledger.db.execute(
        "SELECT pnl_cents FROM settlements").fetchall()
    assert all(isinstance(r[0], int) for r in rows)   # integer cents, no float
    total = sum(r[0] for r in rows)
    assert total == 6                                 # 3 + 3, half-even at booking
    # the fixture book starts at $100.00; +6c integer settlements → 10006 exactly
    assert ledger.book_cents() == 10006
