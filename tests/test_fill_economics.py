"""WO-DAILY-PACK-FILL-ECON (build 47) — the fill-economics pack section.

Read-only reporting off the honest fills table + the FLIP_SWING instrument:
what we paid, sold, netted after fees, the maker/taker split (maker = 0-fee,
taker > 0¢ — the fee drag), FLIP's resting-take fill (reversion) rate, and its
distribution by realized take distance. No trading change."""

import json

from relay_engine import config
from relay_engine.ops import fill_economics, flip_fill_rate_hourly


def _swing(surface, market, took, gross):
    surface.write_row("FLIP", market, f"w-{market}", "FLIP_SWING",
                      detail=json.dumps({"took_swing": took,
                                         "gross_cents": gross,
                                         "entry_price": 44,
                                         "exit_price": 44 + gross}))


def _flip_line(ledger):
    return next(l for l in fill_economics(ledger) if l.strip().startswith("[FLIP]"))


# ── the maker nickel clears (0-fee both legs) ──────────────────────────────
def test_maker_nickel_clears_fees(ledger):
    ledger.record_fill("M1", "FLIP", "yes", "ENTRY", 44, 1, config.TIER_PROBE, fee_cents=0)
    ledger.record_fill("M1", "FLIP", "yes", "EXIT", 49, 1, config.TIER_PROBE, fee_cents=0)
    line = _flip_line(ledger)
    assert "entry~44.0c" in line and "exit~49.0c" in line
    assert "spread~+5.0c" in line
    assert "maker 100%" in line and "net~+5.0c" in line


# ── the taker fee eats into the nickel (the suspected FLIP drag) ────────────
def test_taker_fee_eats_the_nickel(ledger):
    ledger.record_fill("M1", "FLIP", "yes", "ENTRY", 44, 1, config.TIER_PROBE, fee_cents=2)
    ledger.record_fill("M1", "FLIP", "yes", "EXIT", 49, 1, config.TIER_PROBE, fee_cents=2)
    line = _flip_line(ledger)
    assert "maker 0%" in line            # both legs paid a taker fee
    assert "net~+3.0c" in line           # +5 gross − 2¢/ct fee drag


# ── FLIP's fill rate + the by-take-distance curve ──────────────────────────
def test_flip_fill_rate_and_distance_curve(ledger, surface):
    _swing(surface, "S1", True, 5)
    _swing(surface, "S2", True, 5)
    _swing(surface, "S3", True, 6)
    _swing(surface, "S4", False, 0)      # camped, unfilled
    lines = fill_economics(ledger)
    fr = next(l for l in lines if "fill-rate" in l)
    assert "3/4 = 75%" in fr
    td = next(l for l in lines if "take-distance" in l)
    assert "+5c:2" in td and "+6c:1" in td


# ── the compact hourly line ────────────────────────────────────────────────
def test_hourly_compact_line(ledger, surface):
    _swing(surface, "S1", True, 5)
    _swing(surface, "S2", False, 0)
    ledger.record_fill("M1", "FLIP", "yes", "ENTRY", 44, 1, config.TIER_PROBE, fee_cents=0)
    line = flip_fill_rate_hourly(ledger)
    assert "flip_fill=50%(1/2)" in line and "flip_maker=100%" in line


def test_no_fills_is_graceful(ledger):
    lines = fill_economics(ledger)
    assert any("no fills yet" in l for l in lines)   # never crashes on empty


# ── HARD RAIL — read-only instrumentation, no trading change ───────────────
def test_read_only_no_booking_change(ledger):
    before = ledger.book_cents()
    ledger.record_fill("M1", "FLIP", "yes", "ENTRY", 44, 1, config.TIER_PROBE, fee_cents=0)
    fill_economics(ledger)               # the report reads; it never writes
    flip_fill_rate_hourly(ledger)
    # book is unchanged by the report itself (only the record_fill moved it)
    assert ledger.book_cents() == before   # ENTRY books no settlement/cash
