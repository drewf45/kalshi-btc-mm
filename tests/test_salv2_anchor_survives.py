"""WO-SALV-2 — THE ANCHOR SURVIVES RESTARTS: every adoption site computes
the salvage anchor from current spot + delta table (the entry path's own
derivation, reused); unavailable -> disabled LOUDLY via the SALV-1 tape;
ORPHANs price at the venue mark, not a fictional 50¢; and the
catastrophic backstop reads no anchor, so a crash-loop re-anchor can
never postpone the 5% floor."""

import json
import time

import pytest

from relay_engine import config, delta, failures
from relay_engine.custodian import OpenPosition, salvage_params
from relay_engine.reconcile import live_boot_reconcile
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"
STRIKE = 118_000.0


class _Client:
    def request(self, *a, **k):
        raise RuntimeError("no resting sweep in tests")


@pytest.fixture
def engine(tmp_path, monkeypatch):
    e = ShadowEngine(db_path=str(tmp_path / "salv2.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST",
                       boot_id=1)
    from relay_engine import venue
    monkeypatch.setattr(venue, "get_balance", lambda c: (100.0, 0.0))
    yield e
    failures._ledger = None


def _seed_market(engine, spot=None):
    engine.market_meta[TICKER] = {"close_ts": time.time() + 500,
                                  "boundary_lo": None,
                                  "boundary_hi": STRIKE,
                                  "rec_captured_ts": time.time()}
    if spot is not None:
        engine.record_spot(spot, time.time())


def test_restart_readopts_with_anchor_and_salvage_fires(engine, monkeypatch):
    """§1: simulated restart with an open position + live spot/table — the
    re-adopted position carries a non-None anchor, SALVAGE_ARMED lands,
    and a subsequent needle-collapse actually fires salvage."""
    from relay_engine import venue
    monkeypatch.setattr(venue, "get_positions",
                        lambda c: [{"ticker": TICKER, "position": 1}])
    monkeypatch.setattr(delta, "p_survive",
                        lambda d, t, session="ALL":
                        0.5 + 0.43 * min(1.0, d / (0.3 * max(1.0, t))))
    # our own fill explains the position -> RECOGNIZED re-adopt
    engine.ledger.record_fill(TICKER, "F", "yes", "ENTRY", 95, 1, "PROBE")
    _seed_market(engine, spot=STRIKE + 300)   # far side: p_entry high
    live_boot_reconcile(engine, _Client())
    pos = engine.custodian.positions[f"{TICKER}:F"]
    assert pos.p_entry is not None and pos.d_entry is not None
    assert pos.entry_p_win == pytest.approx(pos.p_entry)
    assert engine.ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE state='SALVAGE_ARMED'"
        " AND market=?", (TICKER,)).fetchone()[0] == 1
    # the needle collapses -> salvage fires from the ADOPTION anchor
    engine.custodian.set_lane_params("F", salvage_params())
    close = engine.market_meta[TICKER]["close_ts"]
    for i, now in enumerate((close - 400, close - 399)):
        engine.custodian.tick(
            books={TICKER: engine.feed.book(TICKER)},
            close_ts_of=lambda m: close, now=now, balance_usd=100.0,
            spot=STRIKE - 90, boundaries={TICKER: (None, STRIKE)})
    book = engine.feed.book(TICKER)
    book.apply_snapshot({62: 10}, {30: 10}, ts=1.0)
    for now in (close - 398, close - 397):
        engine.custodian.tick(
            books={TICKER: book}, close_ts_of=lambda m: close, now=now,
            balance_usd=100.0, spot=STRIKE - 90,
            boundaries={TICKER: (None, STRIKE)})
    assert pos.salvage_attempted is True and pos.salvage_fired is not None


def test_restart_without_table_disables_loudly_backstop_stands(engine,
                                                               monkeypatch):
    """§2 + §5: table unavailable at adoption -> SALVAGE_DISABLED_TAGGED
    (never silent); the catastrophic backstop still fires on the
    anchorless position below the 5% floor."""
    from relay_engine import venue
    monkeypatch.setattr(venue, "get_positions",
                        lambda c: [{"ticker": TICKER, "position": 1}])
    monkeypatch.setattr(delta, "p_survive", lambda d, t, session="ALL": None)
    engine.ledger.record_fill(TICKER, "F", "yes", "ENTRY", 95, 1, "PROBE")
    _seed_market(engine, spot=STRIKE + 300)
    live_boot_reconcile(engine, _Client())
    pos = engine.custodian.positions[f"{TICKER}:F"]
    assert pos.p_entry is None
    row = engine.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE"
        " state='SALVAGE_DISABLED_TAGGED' AND market=?",
        (TICKER,)).fetchone()[0]
    assert "table" in json.loads(row)["reason"]
    # §5 (Adversary, verified): the backstop reads NO anchor — it fires
    engine.custodian.set_lane_params("F", salvage_params())
    trigger = engine.custodian.should_cut(
        pos, now=time.time(), secs_remaining=400, p_win=0.04,
        exit_bid_cents=4, spot=STRIKE - 500, boundary_lo=None,
        boundary_hi=STRIKE, balance_usd=100.0)
    assert trigger == "CATASTROPHIC"


def test_orphan_uses_venue_mark_not_50(engine, monkeypatch):
    """§3: an ORPHAN with a readable book prices at the held side's mark."""
    from relay_engine import venue
    monkeypatch.setattr(venue, "get_positions",
                        lambda c: [{"ticker": TICKER, "position": 1}])
    monkeypatch.setattr(delta, "p_survive", lambda d, t, session="ALL": 0.9)
    _seed_market(engine, spot=STRIKE + 300)   # no fill of ours -> ORPHAN
    engine.feed.book(TICKER).apply_snapshot({83: 10}, {15: 10}, ts=1.0)
    live_boot_reconcile(engine, _Client())
    pos = engine.custodian.positions[f"{TICKER}:ORPHAN"]
    assert pos.entry_price_cents == 83        # the book's yes bid, not 50
    assert pos.p_entry is not None            # anchored like everyone else


def test_orphan_falls_back_to_50_only_logged(engine, monkeypatch, caplog):
    import logging

    from relay_engine import venue
    monkeypatch.setattr(venue, "get_positions",
                        lambda c: [{"ticker": TICKER, "position": 1}])
    _seed_market(engine)                      # no spot, no book, dead client
    with caplog.at_level(logging.WARNING, logger="relay.reconcile"):
        live_boot_reconcile(engine, _Client())
    pos = engine.custodian.positions[f"{TICKER}:ORPHAN"]
    assert pos.entry_price_cents == 50
    assert any("ORPHAN_MARK_FALLBACK" in r.message for r in caplog.records)
