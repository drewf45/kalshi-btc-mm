"""WO-2026-07-28-X X7 — the scrape hwm reads VENUE TRUTH, not the derived book.

The P0: high_water_cents → trading_equity_cents → book_cents (the DERIVED book,
the number X1/X2 drift), so a phantom could mint owed. X7 points trading_equity
at the venue's cash (WO-V stamp) in LIVE — ratchet-safe (a mid-window low cash
read never advances the max() high-water) — and a one-time restatement prints
venue-truth vs the old derived book with the delta explained.
"""

import pytest

from relay_engine import config, ops
from relay_engine.ledger import Ledger


def test_shadow_trading_equity_is_the_paper_book(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: False)
    led = Ledger(str(tmp_path / "s.db"))
    led.baseline(5000, confirmed_by="boot")
    led.set_state("last_venue_cash_cents", "9999")     # ignored in SHADOW
    assert led.trading_equity_cents() == 5000          # book − ext, byte-identical


def test_live_trading_equity_is_venue_cash_minus_externals(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    led = Ledger(str(tmp_path / "l.db"))
    led.baseline(9700, confirmed_by="boot")            # phantom book $97
    led.set_state("last_venue_cash_cents", "3291")     # the venue's real cash
    # trading equity reads the VENUE cash − externals (0 here), not the 9700 book
    assert led.trading_equity_cents() == 3291


def test_hwm_no_longer_drifts_with_the_derived_book(tmp_path, monkeypatch):
    """The P0 fixed: a phantom that inflates the DERIVED book no longer mints
    owed, because the high-water reads the venue's cash, not the book."""
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    led = Ledger(str(tmp_path / "p.db"))
    led.baseline(3000, confirmed_by="boot")
    led.set_state("last_venue_cash_cents", "3000")     # venue agrees at seed
    led.seed_scrape()
    base_owed = led.owed_cents()
    # a phantom inflates the DERIVED book by $50 (a divergent settlement booked)…
    led.record_settlement("KXBTC15M-PHANTOM", "F", 5000, detail="phantom")
    assert led.book_cents() > 3000                     # the book drifted up
    # …but the venue cash is unchanged, so trading equity / hwm / owed do NOT move
    assert led.trading_equity_cents() == 3000          # venue truth, not the book
    assert led.owed_cents() == base_owed               # no phantom-minted owe


def test_hwm_advances_only_at_a_flat_full_cash_read(tmp_path, monkeypatch):
    """Ratchet-safe: a mid-window low cash read never advances the high-water; a
    later flat read at a genuine new high does."""
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    led = Ledger(str(tmp_path / "r.db"))
    led.baseline(3000, confirmed_by="boot")
    led.set_state("last_venue_cash_cents", "3000")
    led.seed_scrape()
    hwm0 = led.high_water_cents()
    led.set_state("last_venue_cash_cents", "1200")     # mid-window: cash deployed
    assert led.high_water_cents() == hwm0              # low read never lowers hwm
    led.set_state("last_venue_cash_cents", "4000")     # flat, a real new high
    assert led.high_water_cents() == 4000              # advances on venue truth


def test_restatement_prints_venue_vs_derived_delta_owed_unchanged(tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    led = Ledger(str(tmp_path / "rs.db"))
    led.baseline(9700, confirmed_by="boot")            # derived book $97
    led.set_state("last_venue_cash_cents", "3291")     # venue truth $32.91
    led.seed_scrape()
    owed_before = led.owed_cents()
    lines = ops.scrape_restatement_lines(led)
    body = "\n".join(lines)
    assert "RESTATED" in body and "venue-truth $32.91" in body
    assert "derived-book $97.00" in body and "Δ" in body    # the delta line-item
    assert "owed" in body and "UNCHANGED" in body
    assert led.owed_cents() == owed_before                  # the print mutates nothing


def test_restatement_before_any_venue_read_holds_the_book(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    led = Ledger(str(tmp_path / "nv.db"))
    led.baseline(4000, confirmed_by="boot")            # no venue read yet
    led.seed_scrape()
    body = "\n".join(ops.scrape_restatement_lines(led))
    assert "no venue read yet" in body and "$40.00" in body
