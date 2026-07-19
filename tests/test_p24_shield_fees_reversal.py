"""WO-P24 COMBINED FINAL "SHIELD, FEES, AND THE ZERO IN THE REVERSAL" —
§1 the anchor never goes missing (price-implied shield + named miss cause +
the 1715 replay counterfactual), §2 fees are read, never imagined (the
(key, per_contract) table, one parser both entrances), §3 the zero in the
reversal (entry_p_win real at the writer, belted at the consumer)."""

import json

import pytest

from relay_engine import config, delta, failures, venue
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian, CutParams, OpenPosition
from relay_engine.feed import DegradeLadder
from relay_engine.fills import FillBooker
from relay_engine.gateway import Order, SubmitResult

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
STRIKE = 118_000.0
CLOSE = 1_000_000.0


def _book(yes=48, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


@pytest.fixture(autouse=True)
def _funnel(ledger):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    yield
    failures._ledger = None


@pytest.fixture
def custodian(gateway, ledger, surface):
    return Custodian(gateway, ledger, surface, ladder=DegradeLadder())


def _book_entry(gateway, ledger, surface, custodian, anchor,
                lane="F", price=95):
    booker = FillBooker(gateway, ledger, surface, custodian=custodian)
    booker.anchor_fn = lambda market, side: anchor
    r = gateway.submit(Order(lane=lane, event=EVENT, market=TICKER,
                             side="yes", action="buy", price_cents=price,
                             count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY",
                             why=f"{lane} tier{price} · surv~price"),
                       _book(yes=price, no=100 - price - 1))
    booker.sweep([{"fill_id": "f-e", "order_id": r.order_id,
                   "yes_price_dollars": f"{price / 100:.4f}", "count": 1}],
                 now=1000.0)
    return custodian.positions[f"{TICKER}:{lane}"]


# ── §1.1/§1.2: the shield and the named miss ───────────────────────────────
def test_anchor_from_price_with_named_cause(gateway, ledger, surface,
                                            custodian):
    """A table miss adopts with the PRICE-implied anchor, banks a WARN row
    naming WHICH organ missed, and tags the fill row anchor=price."""
    pos = _book_entry(gateway, ledger, surface, custodian, anchor="table")
    assert pos.p_entry == pytest.approx(0.95)
    assert pos.entry_p_win == pytest.approx(0.95)   # §3 writer fix rides §1
    assert pos.d_entry is None and pos.t_entry is None
    what, how = ledger.db.execute(
        "SELECT what, how_json FROM failures WHERE"
        " why_tag='ANCHOR_FROM_PRICE'").fetchone()
    assert "anchor miss: table" in what
    assert json.loads(how)["cause"] == "table"
    detail = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='ENTERED'").fetchone()[0]
    assert "anchor=price" in detail


def test_table_anchor_tags_and_fills_entry_p_win(gateway, ledger, surface,
                                                 custodian):
    pos = _book_entry(gateway, ledger, surface, custodian,
                      anchor=(200.0, 500.0, 0.93))
    assert pos.entry_p_win == pytest.approx(0.93)   # §3: the zero is dead
    assert pos.p_entry == pytest.approx(0.93)
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='ANCHOR_FROM_PRICE'"
    ).fetchone()[0] == 0
    detail = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='ENTERED'").fetchone()[0]
    assert "anchor=table" in detail


def test_salvage_anchor_names_all_four_misses(tmp_path, monkeypatch):
    """§1.2: spot | strike | close | table — four organs, four names."""
    from relay_engine.shadow_runner import ShadowEngine
    eng = object.__new__(ShadowEngine)
    eng.market_meta = {}
    eng._meta = lambda m: eng.market_meta.get(m, {})
    eng.fresh_spot = lambda now: None
    assert eng._salvage_anchor(TICKER, "yes") == "spot"
    eng.fresh_spot = lambda now: STRIKE - 50
    eng.market_meta[TICKER] = {}   # no boundaries -> no strike
    assert eng._salvage_anchor(TICKER, "yes") == "strike"
    eng.market_meta[TICKER] = {"boundary_hi": STRIKE}
    monkeypatch.setattr("relay_engine.lanes.infer_close_ts_from_ticker",
                        lambda m: None)
    assert eng._salvage_anchor(TICKER, "yes") == "close"
    import time as _t
    eng.market_meta[TICKER] = {"boundary_hi": STRIKE,
                               "close_ts": _t.time() + 500}
    monkeypatch.setattr(delta, "p_survive", lambda d, t, session="ALL": None)
    assert eng._salvage_anchor(TICKER, "yes") == "table"
    monkeypatch.setattr(delta, "p_survive", lambda d, t, session="ALL": 0.9)
    anchor = eng._salvage_anchor(TICKER, "yes")
    assert isinstance(anchor, tuple) and anchor[2] == pytest.approx(0.1)


def test_1715_replay_price_anchor_salvages_the_slide(gateway, ledger,
                                                     surface, custodian,
                                                     monkeypatch):
    """§1.3 the counterfactual: the 1715-15 shape with the price-implied
    anchor (None, None, 0.95) — the collapse trigger fires on the 95→16
    slide and the salvage maker rests at ≥40¢ instead of riding to 16."""
    from relay_engine.custodian import salvage_params
    monkeypatch.setattr(delta, "p_survive",
                        lambda d, t, session="ALL":
                        0.5 + 0.43 * min(1.0, d / (0.3 * max(1.0, t))))
    custodian.set_lane_params("F", salvage_params())
    pos = OpenPosition(event=EVENT, market=TICKER, lane="F", side="yes",
                       count=1, entry_price_cents=95, entry_p_win=0.95,
                       size_tier=config.TIER_PROBE, entry_time=CLOSE - 600,
                       d_entry=None, t_entry=None, p_entry=0.95)  # §1 shield
    custodian.adopt(pos)
    ledger.record_fill(TICKER, "F", "yes", "ENTRY", 95, 1, "PROBE")

    def tick(now, yes_bid):
        return custodian.tick(
            books={TICKER: _book(yes=yes_bid, no=100 - yes_bid - 2)},
            close_ts_of=lambda m: CLOSE, now=now, balance_usd=100.0,
            spot=STRIKE - 90, boundaries={TICKER: (None, STRIKE)})

    tick(CLOSE - 400, yes_bid=40)     # strike 1 (sustained, not a knife)
    assert pos.salvage_oid is None
    tick(CLOSE - 399, yes_bid=40)     # strike 2 -> TRIGGER, mid-slide
    assert pos.salvage_attempted is True and pos.salvage_oid is not None
    order = gateway.order_index[pos.salvage_oid]
    assert order.purpose == "EXIT" and order.price_cents >= 40


# ── §2: fees are read, never imagined ──────────────────────────────────────
def test_average_fee_paid_is_per_contract_ceiled():
    """§2.1: average_fee_paid is PER-CONTRACT dollars — ceil(avg×count×100);
    the total-of-record keys keep precedence, no form-guessing."""
    cost, fee, count = venue.parse_fill(
        {"yes_price_dollars": "0.6200", "average_fee_paid": "0.0034",
         "count": 1}, "yes")
    assert (cost, fee, count) == (62.0, 1, 1)      # ceil(0.34) = 1¢
    _, fee3, _ = venue.parse_fill(
        {"yes_price_dollars": "0.6200", "average_fee_paid": "0.0034",
         "count": 3}, "yes")
    assert fee3 == 2                               # ceil(1.02) = 2¢
    # a total-of-record key outranks the average when both appear
    _, fee_t, _ = venue.parse_fill(
        {"yes_price_dollars": "0.6200", "fee_cost": "0.050000",
         "average_fee_paid": "0.0034", "count": 3}, "yes")
    assert fee_t == 5


def test_order_response_fee_same_parser_both_shapes():
    """§2.2: the response entrance reads through the SAME table — nested
    'order' shape and flat shape both; garbage reads zero."""
    assert venue.parse_response_fee(
        {"order": {"order_id": "x", "average_fee_paid": "0.0034"}}, 1) == 1
    assert venue.parse_response_fee({"average_fee_paid": "0.0100"}, 2) == 2
    assert venue.parse_response_fee({"order_id": "x"}, 1) == 0
    assert venue.parse_response_fee(None, 1) == 0


def test_cut_books_the_response_fee(gateway, ledger, surface, custodian,
                                    monkeypatch):
    """§2.2/§2.3: the 21:13:44 entrance — a crossfire cut books the venue's
    OWN fee (mocked resp 0.0034×1 → 1¢), the receipt shows it, and the cell
    row counts it (scoreboard margins include booked fees)."""
    pos = OpenPosition(event=EVENT, market=TICKER, lane="F", side="yes",
                       count=1, entry_price_cents=95, entry_p_win=0.95,
                       size_tier=config.TIER_PROBE, entry_time=100.0)
    custodian.adopt(pos)
    ledger.record_fill(TICKER, "F", "yes", "ENTRY", 95, 1, "PROBE")
    real_submit = gateway.submit

    def submit_with_resp(order, book, **kw):
        r = real_submit(order, book, **kw)
        return SubmitResult(order_id=r.order_id, shadow=r.shadow,
                            payload=r.payload,
                            resp={"order": {"order_id": r.order_id,
                                            "average_fee_paid": "0.0034"}})
    monkeypatch.setattr(gateway, "submit", submit_with_resp)
    custodian.execute_cut(pos, 62, _book(yes=62, no=30), "CATASTROPHIC")
    side, fee = ledger.db.execute(
        "SELECT side, fee_cents FROM fills WHERE action='CUSTODIAN_EXIT'"
    ).fetchone()
    assert (side, fee) == ("yes", 1)
    fees = ledger.db.execute(
        "SELECT fees_cents, pnl_cents FROM cell_outcomes WHERE kind='trip'"
    ).fetchone()
    assert fees == (1, (62 - 95) - 1)


# ── §3: the zero in the reversal ───────────────────────────────────────────
def _reversal_params():
    return CutParams(
        hard_stop_usd=999.0, soft_stop_usd=999.0,
        min_time_remaining_s=2, hold_to_settle_s=0,
        spot_danger_buffer_usd=0, grace_period_s=0,
        early_exit_window_s=0, early_exit_loss_fraction=1.0,
        max_loss_fraction_of_balance=1.0, max_loss_fraction_of_cost=1.0,
        catastrophic_loss_cents=99,
        spot_safe_buffer_early_usd=0.01, spot_safe_buffer_late_usd=0.01,
        spot_safe_cutoff_s=60,
        rapid_drop_threshold=1.0, rapid_drop_window_s=10,
        max_loss_cents_per_contract=100,
        reversal_threshold=0.10, reversal_threshold_settling=0.10,
        reversal_threshold_profit=0.04, profit_tighten_above_entry=0.05,
        peak_window_s=30, proactive_after_s=0, prob_floor=0.0)


def _drive(custodian, pos, seq):
    trig = None
    for now, p_win in seq:
        trig = custodian.should_cut(
            pos, now=now, secs_remaining=400, p_win=p_win,
            exit_bid_cents=int(p_win * 100), spot=STRIKE - 1000,
            boundary_lo=STRIKE, boundary_hi=None, balance_usd=100.0)
    return trig


def test_high_entry_uses_standard_reversal_threshold(custodian):
    """A position entered at 95 with peak 96 computes gain_above_entry =
    +0.01 (NOT +0.96) — the STANDARD threshold applies; a 6-point wiggle
    does not cut. Pre-P24 this exact shape cut REVERSAL on the tightened
    branch every time."""
    custodian.set_lane_params("D", _reversal_params())
    pos = OpenPosition(event=EVENT, market=TICKER, lane="D", side="yes",
                       count=1, entry_price_cents=95, entry_p_win=0.95,
                       size_tier=config.TIER_PROBE, entry_time=100.0)
    assert _drive(custodian, pos, [(200, 0.96), (201, 0.90)]) is None


def test_legacy_zero_entry_p_win_belted(custodian):
    """The belt at the consumer: a legacy 0.0 falls back to the entry price
    — a zero can never mean 'infinite profit' again."""
    custodian.set_lane_params("D", _reversal_params())
    pos = OpenPosition(event=EVENT, market=TICKER, lane="D", side="yes",
                       count=1, entry_price_cents=95, entry_p_win=0.0,
                       size_tier=config.TIER_PROBE, entry_time=100.0)
    assert _drive(custodian, pos, [(200, 0.96), (201, 0.90)]) is None


def test_profitable_position_uses_tightened_threshold(custodian):
    """The doctrine as ported: entry 49, peak 80 — genuinely profitable →
    the TIGHTENED threshold applies and the same 6-point drop cuts."""
    custodian.set_lane_params("D", _reversal_params())
    pos = OpenPosition(event=EVENT, market=TICKER, lane="D", side="yes",
                       count=1, entry_price_cents=49, entry_p_win=0.49,
                       size_tier=config.TIER_PROBE, entry_time=100.0)
    assert _drive(custodian, pos, [(300, 0.80), (301, 0.74)]) == "REVERSAL"
