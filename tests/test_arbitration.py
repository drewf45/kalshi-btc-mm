"""P3.6 — the cap arbitration is ENCODED, not discovered live (Broker part):
custodian exits run before any lane; FLIP's takes lead its own list; entries
submit in F -> H8 -> FLIP -> D -> P registry order."""

from relay_engine import config
from relay_engine.custodian import CutParams, OpenPosition
from relay_engine.lanes import build_registry
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"


def test_registry_is_arbitration_order():
    assert [l.name for l in build_registry()] == ["F", "H8", "FLIP", "D", "P"]


def test_custodian_exit_outranks_entries_for_cap():
    """A cut frees net-risk cap in the SAME cycle the lanes then draw on:
    the custodian tick runs before any lane submits."""
    engine = ShadowEngine(db_path=":memory:")
    engine.boot()
    from relay_engine.lanes import infer_close_ts_from_ticker
    close = infer_close_ts_from_ticker(TICKER)
    book = engine.feed.book(TICKER)
    book.apply_snapshot({99: 50}, {1: 40}, ts=0.0)

    # a D position that will be cut immediately (tight params, deep loss)
    tight = CutParams(
        hard_stop_usd=0.01, soft_stop_usd=0.01, min_time_remaining_s=5,
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
    engine.custodian.set_lane_params("D", tight)
    event = TICKER.rsplit("-", 1)[0]
    # a NO position marked at 1c against a 95c entry: deep loss, cuts at once
    engine.custodian.adopt(OpenPosition(
        event=event, market=TICKER, lane="D", side="no", count=1,
        entry_price_cents=95, entry_p_win=0.95, size_tier=config.TIER_PROBE,
        entry_time=close - 900))
    engine.gateway.positions[(event, TICKER, "D")] = -1

    order_log = []
    original = engine.gateway.submit

    def logging_submit(order, book):
        order_log.append((order.lane, order.purpose))
        return original(order, book)

    engine.gateway.submit = logging_submit
    engine.cycle([TICKER], now=close - 850)

    # the CUT came first, before any lane's proposal in the same cycle
    assert order_log[0] == ("D", "CUT")
    lane_seq = [l for l, p in order_log[1:] if p == "ENTRY"]
    # entries that did submit this cycle came in registry order
    assert lane_seq == sorted(lane_seq, key=["F", "H8", "FLIP", "D", "P"].index)


def test_flip_takes_lead_its_proposal_list(gateway, ledger, surface):
    from relay_engine.custodian import Custodian
    from relay_engine.feed import DegradeLadder
    from relay_engine.lane_flip import LaneFlip
    from relay_engine.book import OrderBook

    flip = LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))
    close = 1_000_000.0
    b = OrderBook(market=TICKER)
    b.apply_snapshot({40: 20}, {45: 20}, ts=1.0)
    ctx = {"book": b, "close_ts": close, "now": close - 800}
    props = flip.evaluate(TICKER, ctx)
    flip.on_submitted(props[0], "E1", close - 800)
    event = TICKER.rsplit("-", 1)[0]
    gateway.positions[(event, TICKER, "FLIP")] = 1
    flip.note_fill(TICKER, props[0].side, props[0].price_cents, close - 790)
    props2 = flip.evaluate(TICKER, {**ctx, "now": close - 780})
    purposes = [p.purpose for p in props2]
    assert purposes and purposes[0] == "EXIT"  # the take leads the list