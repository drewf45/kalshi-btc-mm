"""P3.3 — Lane D port tests: classify verdicts with evidence, the inside
body's absolute veto (append-only), watchdog re-verify -> abandon, the
recovery baton, and the multi-series seam staying a seam."""

import pytest

from relay_engine import config, delta
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_d import (
    D_BAND_HI, D_FLOOR_CENTS, D_RECOVERY_X, DBudget, LaneD, classify,
)
from relay_engine.lanes import DLaneWrapper

TICKER = "KXBTC15M-02JAN251000-T99"
CLOSE = 1_000_000.0


def d_book(yes=65, no=None, market=TICKER):
    b = OrderBook(market=market)
    b.apply_snapshot({yes: 20} if yes is not None else {},
                     {no: 20} if no is not None else {}, ts=1.0)
    return b


def load_table(wub=0.001):
    cells = {}
    for d in range(50, 2001, 5):
        for t in [10, 30, 60, 120, 180, 300, 600, 900]:
            cells[(d, t, "ALL")] = {"p_cross": wub / 2, "n": 500,
                                    "effective_n": 250, "wilson_ub": wub}
    delta._TABLE, delta._LOADED = cells, True


@pytest.fixture(autouse=True)
def clean_table():
    yield
    delta._TABLE, delta._LOADED = {}, False


GOOD = dict(spot=65_000.0, boundary_lo=64_000.0, boundary_hi=None)


def test_classify_verdicts():
    load_table(wub=0.001)
    # the CHEAP side is the lower bid: yes 30 is under the 60 floor -> that side skips
    v = classify(TICKER, d_book(yes=30, no=65), 600, **GOOD)
    assert v.verdict == "SKIP_UNDER_FLOOR" and v.side == "yes"
    # 65c cheap side seeds
    v = classify(TICKER, d_book(yes=65, no=None), 600, **GOOD)
    assert (v.verdict, v.side, v.cost_cents) == ("SEED", "yes", 65)
    assert v.evidence["table"]["qualified"] is True
    # over band
    v = classify(TICKER, d_book(yes=85), 600, **GOOD)
    assert v.verdict == "SKIP_OVER_BAND"
    # evidence-born: no table -> no trade (unlike F, no static substitute)
    delta._TABLE, delta._LOADED = {}, False
    v = classify(TICKER, d_book(yes=65), 600, **GOOD)
    assert v.verdict == "SKIP_CANT_VERIFY"
    # risky table cell -> SKIP_TABLE_RISK
    load_table(wub=0.9)
    v = classify(TICKER, d_book(yes=65), 600, **GOOD)
    assert v.verdict == "SKIP_TABLE_RISK"
    # no spot evidence
    load_table(wub=0.001)
    v = classify(TICKER, d_book(yes=65), 600, spot=None, boundary_lo=None, boundary_hi=None)
    assert v.verdict == "SKIP_CANT_VERIFY"


def test_multi_series_stays_a_seam():
    load_table(wub=0.001)
    v = classify("KXETH15M-X", d_book(yes=65, market="KXETH15M-X"), 600, **GOOD)
    assert v.verdict == "SKIP_WRONG_SERIES"
    assert "seam" in v.evidence["reason"]


def test_inside_body_veto_append_only(ledger):
    b = DBudget(ledger)
    assert b.reserve("M1", 65) is True
    # per-market cap: second reservation on the same market denied
    assert b.reserve("M1", 65) is False
    # book cap $3: 65+65... M2 65c ok (1.30 total), M3..M4 to exceed 300c
    assert b.reserve("M2", 65) is True
    assert b.reserve("M3", 65) is True
    assert b.reserve("M4", 65) is True   # 260c
    assert b.reserve("M5", 65) is False  # 325c > $3 cap
    # lane killed -> denied
    assert b.reserve("M6", 65, lane_killed=True) is False
    # evidence broken pending blocks everything
    b.evidence_broken_pending = 1
    assert b.reserve("M7", 61) is False
    # every decision is an append-only row, grants and denials alike
    rows = ledger.db.execute(
        "SELECT decision, COUNT(*) FROM d_budget_decisions GROUP BY decision").fetchall()
    counts = dict(rows)
    assert counts["GRANTED"] == 4 and counts["DENIED"] == 4


def test_reserve_before_seed_and_convert(gateway, ledger, surface):
    load_table(wub=0.001)
    lane = LaneD(gateway=gateway,
                 custodian=Custodian(gateway, ledger, surface, ladder=DegradeLadder()),
                 ledger=ledger)
    ctx = {"book": d_book(yes=65), "close_ts": CLOSE, "now": CLOSE - 600, **GOOD}
    order = lane.evaluate(TICKER, ctx)
    assert order is not None
    assert (order.lane, order.side, order.price_cents, order.count) == ("D", "yes", 65, 1)
    assert TICKER in lane.budget.reserved
    lane.budget.convert(TICKER)
    assert TICKER in lane.budget.at_risk and TICKER not in lane.budget.reserved
    # second sweep on the same market: already seeded
    assert lane.evaluate(TICKER, ctx) is None
    assert lane.last_verdict.verdict == "SKIP_ALREADY_SEEDED"


def test_watchdog_breaks_on_lost_evidence(gateway, ledger, surface):
    load_table(wub=0.001)
    lane = LaneD(gateway=gateway, ledger=ledger)
    ctx = {"book": d_book(yes=65), "close_ts": CLOSE, "now": CLOSE - 600, **GOOD}
    assert lane.evaluate(TICKER, ctx) is not None
    # table turns risky -> watchdog re-verify breaks the evidence
    load_table(wub=0.9)
    assert lane.watchdog_tick(TICKER, ctx) == "EVIDENCE_BROKEN"
    assert lane.budget.evidence_broken_pending == 1
    # broken pending blocks new seeds until the abandon clears
    assert lane.budget.reserve("OTHER", 65) is False
    lane.clear_broken()
    assert lane.budget.reserve("OTHER", 65) is True


def test_recovery_baton(gateway, ledger, surface):
    load_table(wub=0.001)
    lane = LaneD(gateway=gateway, ledger=ledger)
    ctx = {"book": d_book(yes=65), "close_ts": CLOSE, "now": CLOSE - 600, **GOOD}
    entry = lane.evaluate(TICKER, ctx)
    assert entry is not None
    exit_order = lane.recovery_exit(TICKER)
    assert (exit_order.action, exit_order.price_cents, exit_order.purpose) == \
        ("sell", 65 + D_RECOVERY_X, "EXIT")


def test_wrapper_posts_baton_after_fill(gateway, ledger, surface):
    load_table(wub=0.001)
    custodian = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    lane = LaneD(gateway=gateway, custodian=custodian, ledger=ledger)
    wrapper = DLaneWrapper(lane)
    ctx = {"book": d_book(yes=65), "close_ts": CLOSE, "now": CLOSE - 600, **GOOD}
    d1 = wrapper.evaluate(TICKER, ctx)
    assert d1.proposal is not None and d1.proposal.purpose == "ENTRY"
    r = gateway.submit(d1.proposal, ctx["book"])
    gateway.on_fill(r.order_id)  # seed fills
    d2 = wrapper.evaluate(TICKER, {**ctx, "now": CLOSE - 595})
    assert d2.proposal is not None and d2.proposal.purpose == "EXIT"
    assert d2.proposal.price_cents == 70