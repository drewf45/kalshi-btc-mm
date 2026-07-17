"""CHUNK 4 verify: synthetic cut with a live resting exit -> atomic
cancel-then-cut on tape; kill semantics; F passthrough; size-tier discipline."""

import pytest

from relay_engine import config
from relay_engine.book import OrderBook
from relay_engine.custodian import CutParams, Custodian, OpenPosition
from relay_engine.errors import FatalIntegrityError
from relay_engine.feed import DegradeLadder
from relay_engine.gateway import Order


BASE = CutParams(prob_flip_below=0.45, prob_drop_fraction=0.20,
                 market_flip_below=0.30, price_danger_sigmas=1.5,
                 min_time_remaining_s=30)


def make_book():
    b = OrderBook(market="M1")
    b.apply_snapshot({45: 100}, {52: 80}, ts=1.0)
    return b


def pos(lane="D", tier=config.TIER_PROBE, exit_id=None):
    return OpenPosition(event="EV1", market="M1", lane=lane, side="yes", count=1,
                        entry_price_cents=61, entry_p_win=0.80, size_tier=tier,
                        resting_exit_id=exit_id)


@pytest.fixture
def custodian(gateway, ledger, surface):
    c = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    c.set_lane_params("D", BASE)
    c.set_lane_params("F", CutParams(prob_flip_below=0.45, prob_drop_fraction=0.20,
                                     market_flip_below=0.30, price_danger_sigmas=1.5,
                                     min_time_remaining_s=30, passthrough=True))
    return c


def test_dump_triggers_generalized(custodian):
    p = pos()
    ok = dict(p_win=0.80, market_p=0.75, spot_sigma_distance=5.0, seconds_remaining=300)
    assert custodian.should_cut(p, **ok) is None
    assert custodian.should_cut(p, **{**ok, "p_win": 0.40}) == "PROB_FLIP"
    assert custodian.should_cut(p, **{**ok, "p_win": 0.60}) == "PROB_DROP"  # 25% drop from 0.80
    assert custodian.should_cut(p, **{**ok, "market_p": 0.25}) == "MARKET_FLIP"
    assert custodian.should_cut(p, **{**ok, "spot_sigma_distance": 1.0}) == "PRICE_DANGER"
    # legacy DUMP mechanic: never cut inside the endgame window
    assert custodian.should_cut(p, **{**ok, "p_win": 0.10, "seconds_remaining": 10}) is None


def test_size_buys_discipline(custodian):
    """Bigger tier -> tighter triggers, never looser. At p_win=0.66 from entry 0.80,
    PROBE (base params) holds while CLEAR (scaled) already cuts on the drop."""
    ok = dict(p_win=0.66, market_p=0.75, spot_sigma_distance=5.0, seconds_remaining=300)
    assert custodian.should_cut(pos(tier=config.TIER_PROBE), **ok) is None
    assert custodian.should_cut(pos(tier=config.TIER_CLEAR), **ok) == "PROB_DROP"


def test_lane_f_passthrough_catastrophic_only(custodian):
    p = pos(lane="F")
    bad = dict(p_win=0.20, market_p=0.10, spot_sigma_distance=0.5, seconds_remaining=300)
    assert custodian.should_cut(p, **bad) is None  # passthrough holds
    assert custodian.should_cut(p, **{**bad, "p_win": 0.01}) == "CATASTROPHIC"


def test_baton_lifecycle_cancel_then_cut(custodian, gateway):
    b = make_book()
    # open the position through the gateway so the cut is classified risk-reducing
    r = gateway.submit(Order(lane="D", event="EV1", market="M1", side="yes", action="buy",
                             price_cents=61, count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY"), b)
    gateway.on_fill(r.order_id)
    exit_r = gateway.submit(Order(lane="D", event="EV1", market="M1", side="yes",
                                  action="sell", price_cents=80, count=1,
                                  size_tier=config.TIER_PROBE, purpose="EXIT"), b)
    p = pos(exit_id=exit_r.order_id)
    custodian.adopt(p)
    cut_id = custodian.execute_cut(p, cut_price_cents=44, book=b, trigger="PROB_FLIP")
    assert exit_r.order_id not in gateway.resting  # exit canceled FIRST
    assert cut_id in gateway.resting               # then the cut went out
    assert p.resting_exit_id is None
    assert "M1:D" not in custodian.positions


def test_baton_fail_loud_on_uncancelable_exit(custodian, gateway):
    p = pos(exit_id="SHADOW-DOES-NOT-EXIST")
    custodian.adopt(p)
    with pytest.raises(FatalIntegrityError):
        custodian.execute_cut(p, cut_price_cents=44, book=make_book(), trigger="PROB_FLIP")
    # position NOT silently dropped
    assert "M1:D" in custodian.positions


def test_cut_attributes_to_opening_lane(custodian, gateway, ledger):
    b = make_book()
    r = gateway.submit(Order(lane="P", event="EV1", market="M1", side="yes", action="buy",
                             price_cents=50, count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY"), b)
    gateway.on_fill(r.order_id)
    p = OpenPosition(event="EV1", market="M1", lane="P", side="yes", count=1,
                     entry_price_cents=50, entry_p_win=0.8, size_tier=config.TIER_PROBE)
    custodian.adopt(p)
    custodian.execute_cut(p, cut_price_cents=42, book=b, trigger="MARKET_FLIP")
    lane, action = ledger.db.execute(
        "SELECT lane, action FROM fills WHERE action='CUSTODIAN_EXIT'").fetchone()
    assert lane == "P"  # the OPENING lane's cells, not a custodian bucket


def test_kill_semantics_entries_only(custodian, gateway):
    b = make_book()
    r = gateway.submit(Order(lane="D", event="EV1", market="M1", side="yes", action="buy",
                             price_cents=61, count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY"), b)
    gateway.on_fill(r.order_id)
    custodian.adopt(pos())
    custodian.kill_lane("D")
    # entries halted...
    from relay_engine.errors import WallRejection
    with pytest.raises(WallRejection) as e:
        gateway.submit(Order(lane="D", event="EV2", market="M2", side="yes", action="buy",
                             price_cents=61, count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY"), b)
    assert e.value.wall == "ENTRIES_HALTED"
    # ...but the open position is still custodied and can still be cut
    assert "M1:D" in custodian.positions
    p = custodian.positions["M1:D"]
    custodian.execute_cut(p, cut_price_cents=44, book=b, trigger="PROB_FLIP")
    assert "M1:D" not in custodian.positions
