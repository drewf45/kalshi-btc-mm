"""P3.4 — custodian live-cut tick, D abandon wiring, concurrent-lane stamp,
worst-day bound in the pack."""

from relay_engine import config
from relay_engine.custodian import CutParams, Custodian, OpenPosition
from relay_engine.feed import DegradeLadder
from relay_engine.ops import daily_pack, worst_day_bound_line
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"


def tight_params():
    return CutParams(
        hard_stop_usd=0.10, soft_stop_usd=0.08, min_time_remaining_s=5,
        hold_to_settle_s=0, spot_danger_buffer_usd=0, grace_period_s=0,
        early_exit_window_s=0, early_exit_loss_fraction=1.0,
        max_loss_fraction_of_balance=1.0, max_loss_fraction_of_cost=1.0,
        catastrophic_loss_cents=99, spot_safe_buffer_early_usd=1e9,
        spot_safe_buffer_late_usd=1e9, spot_safe_cutoff_s=60,
        rapid_drop_threshold=1.0, rapid_drop_window_s=10,
        max_loss_cents_per_contract=99, reversal_threshold=1.0,
        reversal_threshold_settling=1.0, reversal_threshold_profit=1.0,
        profit_tighten_above_entry=1.0, peak_window_s=30, proactive_after_s=0,
        prob_floor=0.0)


def test_custodian_tick_cuts_with_crossfire():
    engine = ShadowEngine(db_path=":memory:")
    engine.boot()
    from relay_engine.lanes import infer_close_ts_from_ticker
    close = infer_close_ts_from_ticker(TICKER)
    book = engine.feed.book(TICKER)
    book.apply_snapshot({40: 20}, {1: 20}, ts=0.0)

    engine.custodian.set_lane_params("D", tight_params())
    engine.custodian.adopt(OpenPosition(
        event="EV", market=TICKER, lane="D", side="yes", count=1,
        entry_price_cents=61, entry_p_win=0.61, size_tier=config.TIER_PROBE,
        entry_time=close - 700))
    engine.gateway.positions[("EV", TICKER, "D")] = 1

    # mark 40 vs entry 61: $0.21 loss >= hard stop $0.10 -> the tick cuts
    engine.cycle([TICKER], now=close - 600)
    cut_orders = [o for o in engine.gateway.shadow_orders if o.purpose == "CUT"]
    assert len(cut_orders) == 1
    assert cut_orders[0].crossfire is True   # the one deliberate cross
    assert cut_orders[0].price_cents == 40   # at the mark, priced to fill
    assert f"{TICKER}:D" not in engine.custodian.positions
    rows = engine.ledger.db.execute(
        "SELECT state, detail FROM surface_rows WHERE state='EXITED'").fetchall()
    assert any("CUSTODIAN_CUT:STOP_LOSS_HARD" in d for _, d in rows)


def test_concurrent_lane_stamp():
    engine = ShadowEngine(db_path=":memory:")
    engine.boot()
    from relay_engine.lanes import infer_close_ts_from_ticker
    close = infer_close_ts_from_ticker(TICKER)
    book = engine.feed.book(TICKER)
    book.apply_snapshot({99: 50}, {1: 40}, ts=0.0)
    # drive F to a proposal (9 tier-0 confirms) with FLIP also on the market
    start = close - 850
    for i in range(10):
        engine.cycle([TICKER], now=start + i * 5)
    stamps = [s for (s,) in engine.ledger.db.execute(
        "SELECT DISTINCT concurrent_lanes FROM surface_rows WHERE concurrent_lanes != ''")]
    # once F and FLIP both rest on the market, rows carry both lanes
    assert any("F" in s and "FLIP" in s for s in stamps)


def test_worst_day_bound_is_a_number(ledger):
    line = worst_day_bound_line(ledger)
    # book $100, floor $25 -> rail $75 binds (cap x events = $285.12, kill = $71.28)
    assert "WORST-DAY BOUND: $71.28" in line or "WORST-DAY BOUND: $75.00" in line
    # all three components printed
    assert "cap x events" in line and "kill clamp" in line and "drawdown rail" in line


def test_pack_states_live_and_pending_lanes(ledger, surface, cash):
    pack = daily_pack(ledger, surface, cash, foreign_fills=3)
    assert "LANES LIVE: F, H8, FLIP, D" in pack
    assert "NOT YET BUILT: P (P3.5)" in pack
    assert "FOREIGN FILLS seen: 3" in pack
    assert "WORST-DAY BOUND" in pack