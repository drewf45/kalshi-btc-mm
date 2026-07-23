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


def test_settlement_pnl_carries_the_fraction_not_truncated(ledger, surface):
    """A no@97.3 entry that WINS pays +2.7c/contract. The old int() booked +3c —
    0.3c of phantom profit. The settlement now records the true 2.7c."""
    ledger.record_fill(MKT, "F", "no", "ENTRY", 97.3, 1, config.TIER_PROBE)
    surface.settle_market(MKT, WIN, settled_yes=False)   # NO wins
    pnl = ledger.db.execute(
        "SELECT pnl_cents FROM settlements WHERE market=? AND lane='F'",
        (MKT,)).fetchone()[0]
    assert pnl == pytest.approx(2.7)                  # not 3 — the fraction survived


def test_book_cents_rounds_once_over_fractional_settlements(ledger, surface):
    """Two +2.7c settlements sum to 5.4 → book rounds ONCE to 5, not 3+3=6. The
    sub-cent precision accumulates instead of being lost per row."""
    for i, mkt in enumerate((MKT, MKT + "-B")):
        ledger.record_fill(mkt, "F", "no", "ENTRY", 97.3, 1, config.TIER_PROBE)
        surface.settle_market(mkt, "w-" + mkt, settled_yes=False)
    # settlements hold the fractions; book_cents sums at full precision, rounds once
    total = ledger.db.execute(
        "SELECT COALESCE(SUM(pnl_cents),0) FROM settlements").fetchone()[0]
    assert total == pytest.approx(5.4)
    # the fixture book starts at $100.00; +5.4c settlements → round(10005.4) =
    # 10005 (round ONCE), not 10000 + round(2.7)·2 = 10006 (round per row)
    assert ledger.book_cents() == 10005
