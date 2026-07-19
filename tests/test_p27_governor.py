"""WO-P27 "THE GOVERNOR IS THE HALT" — §1 full Kelly + governors die,
§2 whys report / doctrine gates / process gates die, §3 the one governor
confirmed (mixed-lane, account-level, lane-blind, restart-proof), §4 the
era stamp. Walls stop bugs, custody stops losses, the halt stops bad
days, NOTHING stops trading."""

import inspect

import pytest

from relay_engine import config, failures, lane_fh8, lane_flip
from relay_engine.shadow_runner import ShadowEngine
from relay_engine.sizing import size_order

TICKER = "KXBTC15M-02JAN251000-T99"
TICKER2 = "KXBTC15M-02JAN251015-T99"


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "p27.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST",
                       boot_id=1)
    yield e
    failures._ledger = None


# ── §1: sizing = full Kelly; the governors are dead in source ──────────────
def test_two_lots_at_49_at_current_book():
    """THE §5 case: at the current book, 49¢ with 10 visible lots sizes to
    2 lots — min(kelly=17, depth=2); no tier term anywhere."""
    d = size_order(book_cents=10_000, price_cents=49, visible_depth=10)
    assert d.contracts == 2
    assert "kelly" in d.reason and "tier" not in d.reason


def test_per_lane_governors_absent_from_source():
    """§1(b)/(c): Wall 3c, HOURLY_CAP, the H8 probe-kill and daily budget,
    and FLIP's stop-streak kill are all GONE as governors — grep-proof."""
    fh8 = inspect.getsource(lane_fh8)
    assert 'base.reject_code = "LANE_KILLED"' not in fh8
    assert 'base.reject_code = "HOURLY_CAP"' not in fh8
    assert 'base.reject_code = "H8_PROBE_KILLED"' not in fh8
    assert 'base.reject_code = "H8_BUDGET"' not in fh8
    flip_src = inspect.getsource(lane_flip.LaneFlip.note_window_result)
    assert "kill_lane" not in flip_src and "self.killed = True" not in flip_src
    from relay_engine import gateway as _g
    assert "_wall_sizing_tier(order)" not in inspect.getsource(_g.Gateway.submit)


def test_hourly_exposure_still_accumulates_as_reporting(engine):
    """The stats the dead walls read stay banked for the packs."""
    st = engine.fh8_shared.state
    st.add_exposure(3.20)
    st.add_exposure(2.00)
    assert st.hourly_exposure_usd() == pytest.approx(5.20)


# ── §3: THE ONE GOVERNOR — mixed-lane, account-level, lane-blind ──────────
def _mixed_lane_window(engine, market, f_pnl, open_pnl, now):
    """Two lanes trade ONE window: F wins, OPEN loses (or vice versa) —
    the streak reads the WINDOW NET, never a lane."""
    open_value = engine.ledger.book_cents()
    engine.econ.open_bracket(market, open_value, now=now)
    engine.ledger.record_fill(market, "F", "yes", "ENTRY", 61, 1, "PROBE")
    engine.ledger.record_settlement(market, "F", f_pnl, detail="synthetic")
    engine.ledger.record_fill(market, "FLIP", "no", "ENTRY", 48, 1, "PROBE")
    engine.ledger.record_settlement(market, "FLIP", open_pnl,
                                    detail="synthetic")
    net = f_pnl + open_pnl
    engine.econ.close_bracket(market, open_value + net, net,
                              lanes_active="F,FLIP", fills_count=2,
                              now=now + 900)
    return net


def test_mixed_lane_window_nets_one_strike_two_halt(engine, tmp_path):
    """§3 (the Adversary's one requirement): one lane wins, the other
    loses, window net red → EXACTLY one strike; two consecutive → halt;
    the halt persists across a restart; /reset_halt is the only key."""
    net1 = _mixed_lane_window(engine, TICKER, f_pnl=+4, open_pnl=-10,
                              now=1000.0)
    assert net1 == -6
    assert engine.econ.streak == 1 and not engine.econ.halted()
    net2 = _mixed_lane_window(engine, TICKER2, f_pnl=-3, open_pnl=-5,
                              now=3000.0)
    assert net2 == -8
    assert engine.econ.streak == 2 and engine.econ.halted()
    assert "RATE_HALT" in engine.gateway.entries_halted_reasons
    # restart: a NEW engine on the SAME database still halted
    e2 = ShadowEngine(db_path=str(tmp_path / "p27.db"))
    e2.boot()
    assert e2.econ.halted()
    # Drew's key, entries only
    e2.econ.reset_halt()
    assert not e2.econ.halted()


def test_mixed_lane_green_window_no_strike(engine):
    """Lane-blind the other way too: OPEN loses but F wins MORE — the
    window nets green, zero strikes (a lane's red day is not a strike)."""
    net = _mixed_lane_window(engine, TICKER, f_pnl=+12, open_pnl=-5,
                             now=1000.0)
    assert net == 7
    assert engine.econ.streak == 0 and not engine.econ.halted()


# ── §4: the era stamp ──────────────────────────────────────────────────────
def test_cell_rows_stamp_the_governor_era(ledger):
    ledger.record_cell_outcome("F", 95, won=True, pnl_cents=5, fees_cents=0,
                               market="M1", kind="settle")
    gov = ledger.db.execute(
        "SELECT governor FROM cell_outcomes").fetchone()[0]
    assert gov == "halt-only"


def test_boot_speaks_the_constitution():
    from relay_engine.boot import boot_tape
    tape = "\n".join(boot_tape())
    assert "GOVERNOR: halt-only" in tape
    assert "FULL KELLY" in tape
    assert "NOTHING else stops trading" in tape
