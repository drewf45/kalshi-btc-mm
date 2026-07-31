"""WO-ORIENT-1 — TWO-WITNESS ORIENTATION SENTINEL: a stale discovery
record (up to 60s old) can forge a mirror on any symmetric cross of 50¢;
the sentinel now convicts only on a FRESH record, on both touches, with
the record's age printed on every verdict line. Tape: 12:22:59Z FATAL
rec=37/ours=66 — 7s later the fresh record read 66."""

import logging

import pytest

from relay_engine import failures, venue
from relay_engine.errors import FatalIntegrityError
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "orient.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST",
                       boot_id=1)
    yield e
    failures._ledger = None


def _arm(engine, stale_rec_bid, ours, captured_age_s=53.0):
    """Seed the stale discovery record + our live book."""
    import time
    engine.market_meta[TICKER] = {
        "close_ts": time.time() + 500, "boundary_lo": None,
        "boundary_hi": 118_000.0, "rec_yes_bid": stale_rec_bid,
        "rec_yes_ask": stale_rec_bid + 2,
        "rec_captured_ts": time.time() - captured_age_s}
    book = engine.feed.book(TICKER)
    book.apply_snapshot({ours: 10}, {100 - ours - 2: 10}, ts=1.0)
    return book


def test_todays_tape_stale_mirror_cleared_by_fresh_record(engine,
                                                          monkeypatch,
                                                          caplog):
    """THE REPLAY: stale rec=37, live=66 — the old sentinel FATALed here.
    The fresh record reads 66 (the prior live price: staleness, not
    inversion) → PASS, with the cleared mirror and record age logged."""
    book = _arm(engine, stale_rec_bid=37, ours=66)
    monkeypatch.setattr(venue, "get_market",
                        lambda c, t: {"yes_bid_dollars": "0.66",
                                      "yes_ask_dollars": "0.68"})
    engine.gateway.venue_client = object()
    with caplog.at_level(logging.WARNING, logger="relay.shadow"):
        engine.orientation_selftest(TICKER, book)   # must NOT raise
    line = next(r.message for r in caplog.records if "CLEARED" in r.message)
    assert "age 53s" in line and "stale y37" in line and "fresh y66" in line
    assert engine._orientation_checked
    # the fresh pull also refreshed the meta record + captured_ts
    assert engine.market_meta[TICKER]["rec_yes_bid"] == 66


def test_true_inversion_confirmed_on_both_touches_fatals(engine,
                                                         monkeypatch):
    book = _arm(engine, stale_rec_bid=37, ours=66)
    monkeypatch.setattr(venue, "get_market",
                        lambda c, t: {"yes_bid_dollars": "0.34",
                                      "yes_ask_dollars": "0.36"})
    engine.gateway.venue_client = object()
    with pytest.raises(FatalIntegrityError):
        engine.orientation_selftest(TICKER, book)
    row = engine.ledger.db.execute(
        "SELECT what FROM failures WHERE why_tag='ORIENTATION_MIRROR'"
    ).fetchone()[0]
    assert "FRESH record" in row and "BOTH touches" in row


def test_missing_ask_single_touch_fallback_still_fatals(engine, monkeypatch):
    book = _arm(engine, stale_rec_bid=37, ours=66)
    monkeypatch.setattr(venue, "get_market",
                        lambda c, t: {"yes_bid_dollars": "0.34"})
    engine.gateway.venue_client = object()
    with pytest.raises(FatalIntegrityError):
        engine.orientation_selftest(TICKER, book)
    row = engine.ledger.db.execute(
        "SELECT what FROM failures WHERE why_tag='ORIENTATION_MIRROR'"
    ).fetchone()[0]
    assert "single-touch fallback" in row


def test_unpullable_fresh_record_is_unverifiable_fatal(engine, monkeypatch):
    """§1.3: cannot prove innocence → refuse (fail-loud unchanged)."""
    book = _arm(engine, stale_rec_bid=37, ours=66)
    calls = []

    def boom(c, t):
        calls.append(t)
        raise RuntimeError("venue down")
    monkeypatch.setattr(venue, "get_market", boom)
    engine.gateway.venue_client = object()
    with pytest.raises(FatalIntegrityError):
        engine.orientation_selftest(TICKER, book)
    assert len(calls) == 2                      # one retry, then refuse
    row = engine.ledger.db.execute(
        "SELECT what FROM failures WHERE why_tag='ORIENTATION_UNVERIFIABLE'"
    ).fetchone()[0]
    assert "cannot prove innocence" in row and "record age" in row


def test_clean_pass_prints_record_age(engine, caplog):
    """§1.4: every verdict line — pass or fail — carries the record age."""
    book = _arm(engine, stale_rec_bid=65, ours=66, captured_age_s=7.0)
    with caplog.at_level(logging.INFO, logger="relay.shadow"):
        engine.orientation_selftest(TICKER, book)
    line = next(r.message for r in caplog.records
                if "SELF-TEST OK" in r.message)
    assert "record age 7s" in line
