"""WO-2026-07-23 COLD READ (build 67) — THE PHANTOM BOOK BUG.

`account_value()` sums venue `cash + position_value`, sampled at ENTRY (cash
debited, position not yet reflected → LOW by the notional) and at SETTLEMENT
(cash credited, position not yet cleared → HIGH by the notional). Differencing
an OPEN read against a CLOSE read put that notional into `window_pnl` twice,
same sign — the +819c-vs-true-+24c phantom, and the venue read was also printed
under the name "book". The fix: window-econ brackets and the reported book come
from `ledger.book_cents()` (internally consistent — cash_movements +
settlements); the venue read is kept for `standing_reconcile` ONLY.

The ledger was never wrong; the phantom lived entirely in the venue read."""

import pytest

from relay_engine import config, window_econ
from relay_engine.ledger import Ledger
from relay_engine.shadow_runner import ShadowEngine


def _engine(ledger):
    eng = object.__new__(ShadowEngine)
    eng.ledger = ledger
    return eng


def test_bracket_book_is_the_ledger_book_not_the_venue_read(tmp_path, monkeypatch):
    """The bracket value is decoupled from the venue account read: even when
    account_value() returns a wildly inconsistent (phantom) number, the bracket
    tracks the internally-consistent ledger book."""
    led = Ledger(str(tmp_path / "b.db"))
    led.baseline(4306, confirmed_by="test")            # the real book: $43.06
    eng = _engine(led)
    # the venue read is off by the entry notional in BOTH directions...
    monkeypatch.setattr(eng, "account_value",
                        lambda now=None: (4306 - 776, "venue"))   # entry: LOW
    assert eng.bracket_book() == (4306, "ledger")      # the bracket ignores it
    monkeypatch.setattr(eng, "account_value",
                        lambda now=None: (4306 + 800, "venue"))   # settle: HIGH
    assert eng.bracket_book() == (4306, "ledger")      # still the ledger truth


def test_window_pnl_from_ledger_delta_is_phantom_free(tmp_path):
    """The gap that equalled the entry notional every time is gone: differencing
    two LEDGER reads gives exactly the settlement delta (here +24c), never the
    +819c phantom, because the ledger book is consistent at both instants."""
    led = Ledger(str(tmp_path / "w.db"))
    led.baseline(4306, confirmed_by="test")
    eng = _engine(led)
    open_val, _ = eng.bracket_book()                   # at entry: 4306, no settle yet
    # the window settles: no@97 × 8, NO wins → (100−97)×8 = +24c into the ledger
    led.record_settlement("MKT", "F", 24, detail="window=w")
    close_val, _ = eng.bracket_book()                  # at close: 4330
    window_pnl = close_val - open_val - 0              # minus cash_moves (none)
    assert window_pnl == 24                            # == the fills truth, not +819


def test_ledger_is_a_valid_live_bracket_source_but_paper_is_not(monkeypatch):
    """The live invariant is amended: 'ledger' is a REAL reconciled number and
    passes in live; 'paper' (a shadow fabrication) still FATALs."""
    from relay_engine import failures
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    econ = object.__new__(window_econ.WindowEcon)
    # "ledger" and "venue" are accepted (no raise)
    econ._reject_paper_in_live("MKT", "ledger")
    econ._reject_paper_in_live("MKT", "venue")
    # "paper" is the lie the invariant forbids in live
    calls = []
    monkeypatch.setattr(failures, "fail",
                        lambda *a, **k: calls.append((a, k)))
    econ._reject_paper_in_live("MKT", "paper")
    assert calls and calls[0][1].get("fatal") is True
