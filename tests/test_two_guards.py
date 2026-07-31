"""WO-2026-07-26-T — THE TWO GUARDS (open the XRP room clean).

Guard 1: the counterparty-liquidity gate into F's entry path — never rest into a
one-sided book (no fill, or worse no EXIT). Guard 2: the table speaks its series
or says BLIND — a card never prints BTC physics on an XRP row.
"""

import json
import logging

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.gateway import Order
from relay_engine.shadow_runner import ShadowEngine

EVENT = "KXBTC15M-02JAN251000"
TICKER = "KXBTC15M-02JAN251000-T99"


def _f(price=97, side="yes"):
    return Order(lane="F", event=EVENT, market=TICKER, side=side, action="buy",
                 price_cents=price, count=1, size_tier=config.TIER_PROBE,
                 purpose="ENTRY", why="F tier97")


# ── GUARD 1 — the counterparty gate ──────────────────────────────────────────
def test_g1_empty_opposite_side_refuses_no_counterparty(tmp_path, caplog):
    e = ShadowEngine(db_path=str(tmp_path / "g1.db"))
    e.ledger.baseline(50_000, confirmed_by="boot")
    failures.configure(e.ledger, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    try:
        book = OrderBook(market=TICKER)
        book.apply_snapshot({97: 500}, {}, ts=1.0)   # buying yes, NO no-side bid
        p = _f()
        with caplog.at_level(logging.INFO, logger="relay.shadow"):
            e._score_and_size(p, book)
        assert p.count == 0                            # refused, not a 1-lot bet
        assert any("NO_COUNTERPARTY" in r.message for r in caplog.records)
        # counted for the room's liquidity map (by series/hour)
        n = e.ledger.db.execute(
            "SELECT COUNT(*) FROM failures WHERE why_tag='NO_COUNTERPARTY'"
        ).fetchone()[0]
        assert n == 1
    finally:
        failures._ledger = None


def test_g1_re_eligible_next_poll_when_a_bid_appears(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "g1b.db"))
    e.ledger.baseline(50_000, confirmed_by="boot")
    failures.configure(e.ledger, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    try:
        # poll 1: empty opposite → refuse
        empty = OrderBook(market=TICKER)
        empty.apply_snapshot({97: 500}, {}, ts=1.0)
        p1 = _f()
        e._score_and_size(p1, empty)
        assert p1.count == 0
        # poll 2: a counterparty bid has appeared → F sizes normally (re-eligible)
        full = OrderBook(market=TICKER)
        full.apply_snapshot({97: 500}, {1: 500}, ts=2.0)
        p2 = _f()
        e._score_and_size(p2, full)
        assert p2.count >= 1                           # the trap cleared, F proposes
    finally:
        failures._ledger = None


def test_g1_btc_deep_book_never_triggers(tmp_path):
    """The gate is free: BTC's two-sided book always has a counterparty, so F
    sizes exactly as before (byte-identical behavior on a real book)."""
    e = ShadowEngine(db_path=str(tmp_path / "g1c.db"))
    e.ledger.baseline(50_000, confirmed_by="boot")
    failures.configure(e.ledger, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    try:
        book = OrderBook(market=TICKER)
        book.apply_snapshot({97: 500}, {1: 500, 2: 500}, ts=1.0)
        p = _f()
        e._score_and_size(p, book)
        assert p.count >= 1
        assert "NO_COUNTERPARTY" not in (p.why or "")
        assert e.ledger.db.execute(
            "SELECT COUNT(*) FROM failures WHERE why_tag='NO_COUNTERPARTY'"
        ).fetchone()[0] == 0
    finally:
        failures._ledger = None


def test_g1_counts_once_per_window_not_every_poll(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "g1d.db"))
    e.ledger.baseline(50_000, confirmed_by="boot")
    failures.configure(e.ledger, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    try:
        book = OrderBook(market=TICKER)
        book.apply_snapshot({97: 500}, {}, ts=1.0)
        for _ in range(5):                             # five polls, same trap
            e._score_and_size(_f(), book)
        assert e.ledger.db.execute(
            "SELECT COUNT(*) FROM failures WHERE why_tag='NO_COUNTERPARTY'"
        ).fetchone()[0] == 1                           # counted ONCE, not five times
    finally:
        failures._ledger = None


def test_g1_registered_as_a_data_question():
    from relay_engine import registry
    q = registry.get("NO_COUNTERPARTY")
    assert q is not None and q.is_complete()
    assert "NO_COUNTERPARTY" in registry.SEED_SURFACES


# ── GUARD 2 — the table speaks its series or says BLIND ───────────────────────
class _AnyTable(dict):
    """A table that answers every (d, t, session) — isolates the series guard
    from grid rounding so the test exercises the REAL _lookup scoping."""
    _CELL = {"p_cross": 0.30, "p_end": 0.20, "p_end_wilson_lb": 0.15,
             "n": 1000, "effective_n": 1000, "wilson_ub": 0.34}

    def get(self, key, default=None):
        return dict(self._CELL)


@pytest.fixture
def loaded_table(monkeypatch):
    """Force the delta table 'loaded' as a BTC corpus for the test."""
    from relay_engine import delta
    monkeypatch.setattr(delta, "_LOADED", True)
    monkeypatch.setattr(delta, "_TABLE_SERIES", "KXBTC15M")
    monkeypatch.setattr(delta, "_TABLE", _AnyTable())
    return delta


def test_g2_btc_lookup_answers_foreign_lookup_is_blind(loaded_table):
    d = loaded_table
    # BTC (the table's own series, or unscoped) answers
    assert d.p_survive(500, 300) is not None                 # unscoped = legacy BTC
    assert d.p_survive(500, 300, series="KXBTC15M") is not None
    assert d.p_cross(500, 300, series="KXBTC15M") == 0.30
    # a FOREIGN series → None (BLIND), never a borrowed BTC number
    assert d.p_survive(500, 300, series="KXXRP15M") is None
    assert d.p_cross(500, 300, series="KXXRP15M") is None
    assert d.p_end(500, 300, series="KXXRP15M") is None
    assert d.p_end_wilson_lb(500, 300, series="KXXRP15M") is None
    assert d.distance_for_p(0.25, 300, series="KXXRP15M") is None
    assert d.table_series() == "KXBTC15M"


def test_g2_xrp_card_prints_surv_na_not_borrowed_physics(loaded_table):
    """The F card for an XRP market prints 'surv n/a (no KXXRP15M table)' — the
    machine admitting what it doesn't know, never BTC physics on an XRP row."""
    from relay_engine.lanes import _PortedLane

    class _Ord:
        def __init__(self, market, side):
            self.market, self.side, self.why = market, side, ""
    ctx = {"spot": 60000.0, "close_ts": 1000.0, "now": 700.0,
           "boundary_lo": 59500.0, "boundary_hi": 60500.0}
    # XRP market → surv n/a (BLIND), the BTC corpus refuses to speak
    order = _Ord("KXXRP15M-x-T3", "yes")
    anchor = _PortedLane._table_survival(order.side, ctx,
                                     series="KXXRP15M")
    assert anchor is None                                    # no borrowed number
    # BTC market → the same cell answers (byte-identical path)
    btc = _PortedLane._table_survival("yes", ctx, series="KXBTC15M")
    assert btc is not None


def test_g2_spotlead_needle_and_settle_blind_for_foreign_series(loaded_table):
    from relay_engine import spotlead as sl
    # needle: a real BTC move produces a needle; an XRP move is BLIND
    n_btc = sl.needle(59000.0, 59200.0, 60000.0, 300.0, series="KXBTC15M")
    n_xrp = sl.needle(59000.0, 59200.0, 60000.0, 300.0, series="KXXRP15M")
    assert n_xrp is None                                     # no fabricated needle
    assert n_btc is not None
    # settle-fair: BTC answers, XRP is BLIND (no fabricated confidence)
    assert sl.settle_fair_favored(60000.0, 60500.0, "yes", 300.0,
                                  series="KXXRP15M") is None


def test_g2_planted_none_fabricates_nothing_downstream(loaded_table):
    """Adversary (ii): a planted foreign-series None must not become a number
    anywhere — every consumer degrades to None/BLIND, never a guess."""
    from relay_engine import spotlead as sl, delta
    for fn in (lambda: delta.p_survive(500, 300, series="KXSOL15M"),
               lambda: delta.p_end(500, 300, series="KXSOL15M"),
               lambda: sl.needle(1.0, 1.2, 2.0, 300.0, series="KXSOL15M"),
               lambda: sl.settle_fair_favored(1.0, 2.0, "yes", 300.0, series="KXSOL15M")):
        assert fn() is None
