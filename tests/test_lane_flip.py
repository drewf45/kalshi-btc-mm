"""P3.2 — Lane FLIP port tests: internal walls whole, gateway routing, one
exit owner, cut-param mapping, stop-streak kill, and the Engineer's acceptance
test (a flip bundle reconstructible per-lane from surface rows alone)."""

import pytest

from relay_engine import config, lane_flip
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.fills import FillBooker
from relay_engine.lane_flip import (
    FLIP_SCRATCH_S, FLIP_SIDE_MAX, FLIP_X, LaneFlip, book_lean, flip_cut_params,
    ofi_side, scratch_reason, spot_adverse, tick_direction,
)

TICKER = "KXBTC15M-02JAN251000-T99"
CLOSE = 1_000_000.0


def flip_book(yes=40, no=45, yq=10, nq=10):
    b = OrderBook(market=TICKER)
    yes_levels = {yes: yq} if yes is not None else {}
    no_levels = {no: nq} if no is not None else {}
    b.apply_snapshot(yes_levels, no_levels, ts=1.0)
    return b


def ctx(book, secs_left=800, spot=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE, "spot": spot}


@pytest.fixture
def flip(gateway, ledger, surface):
    custodian = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    return LaneFlip(gateway, custodian=custodian)


# ── pure signal helpers (byte-identical semantics) ─────────────────────────
def test_tick_direction():
    assert tick_direction([1, 2, 3, 4, 5], 4) == "yes"
    assert tick_direction([5, 4, 3, 2, 1], 4) == "no"
    assert tick_direction([1, 2, 1, 2, 3], 4) is None  # mixed
    assert tick_direction([1, 2], 4) is None           # insufficient


def test_book_lean_and_ofi_fail_closed():
    assert book_lean(10, 5) == "yes"
    assert book_lean(5, 10) == "no"
    assert book_lean(5, 5) is None
    assert book_lean(None, 5) is None
    # OFI: ticks and lean must AGREE; anything else waits
    up = [1, 2, 3, 4, 5]
    assert ofi_side(up, "yes") == "yes"
    assert ofi_side(up, "no") is None      # disagreement -> wait, never fade
    assert ofi_side(up, None) is None
    assert ofi_side([1, 2, 1, 2, 3], "yes") is None


def test_spot_adverse_conservative():
    assert spot_adverse("yes", 64_000, 64_100, None) is True   # below strike, holding yes
    assert spot_adverse("yes", 64_200, 64_100, None) is False
    assert spot_adverse("no", 64_200, None, 64_100) is True
    assert spot_adverse("yes", None, 64_100, None) is False    # missing data -> not adverse
    assert spot_adverse("yes", 64_000, 64_100, 64_500) is False  # two-sided range -> no


def test_scratch_reason_triggers():
    assert "mark" in scratch_reason("yes", 45, 42, None, None, 0)          # (a) 42<=45-3
    assert scratch_reason("yes", 45, 44, None, None, 0) is None
    assert "spot" in scratch_reason("yes", 45, None, None, None, 2)        # (b)
    assert "markout" in scratch_reason("yes", 45, None, -2, -1, 0)         # (c) worsening
    assert scratch_reason("yes", 45, None, -2, -3, 0) is None              # improving


# ── entry mechanics through the lane interface ─────────────────────────────
def test_both_sides_proposed_under_side_max(flip):
    props = flip.evaluate(TICKER, ctx(flip_book(yes=40, no=45)))
    assert {(p.side, p.price_cents, p.purpose) for p in props} == \
        {("yes", 40, "ENTRY"), ("no", 45, "ENTRY")}
    assert all(p.lane == "FLIP" and p.count == 1 for p in props)


def test_side_over_max_not_posted(flip):
    props = flip.evaluate(TICKER, ctx(flip_book(yes=55, no=45)))
    assert [(p.side, p.price_cents) for p in props] == [("no", 45)]  # 55 > 49 skipped


def test_combined_line_wall(flip, monkeypatch):
    """At the traded knobs (side_max 49) two <=49 joins can't exceed the 99
    line — the wall binds when side_max is raised, so raise it to prove the
    wall (the wall itself ships verbatim)."""
    monkeypatch.setattr(lane_flip, "FLIP_SIDE_MAX", 60)
    props = flip.evaluate(TICKER, ctx(flip_book(yes=49, no=None), secs_left=800))
    assert len(props) == 1
    flip.on_submitted(props[0], "OID-1", CLOSE - 800)
    # second side at 51 would make 100 > 99 -> refused by the lane's own wall
    props2 = flip.evaluate(TICKER, ctx(flip_book(yes=49, no=51), secs_left=795))
    assert props2 == []
    # at 50 the bundle is 99 == line -> allowed
    props3 = flip.evaluate(TICKER, ctx(flip_book(yes=49, no=50), secs_left=790))
    assert [(p.side, p.price_cents) for p in props3] == [("no", 50)]


def test_entry_phase_and_curfew(flip):
    # outside the entry phase (secs <= 600), first trip does not post
    assert flip.evaluate(TICKER, ctx(flip_book(), secs_left=500)) == []
    # past curfew nothing posts
    assert flip.evaluate(TICKER, ctx(flip_book(), secs_left=200)) == []
    # window over
    assert flip.evaluate(TICKER, ctx(flip_book(), secs_left=50)) == []


def test_take_quote_after_fill(flip, gateway):
    props = flip.evaluate(TICKER, ctx(flip_book(yes=40, no=None)))
    flip.on_submitted(props[0], "OID-Y", CLOSE - 800)
    # the yes entry fills at 40 -> lane learns via note_fill; position is long 1
    event = TICKER.rsplit("-", 1)[0]
    gateway.positions[(event, TICKER, "FLIP")] = 1
    flip.note_fill(TICKER, "yes", 40, CLOSE - 790)
    props2 = flip.evaluate(TICKER, ctx(flip_book(yes=40, no=None), secs_left=780))
    takes = [p for p in props2 if p.purpose == "EXIT"]
    assert len(takes) == 1
    assert (takes[0].side, takes[0].price_cents, takes[0].action) == ("yes", 40 + FLIP_X, "sell")


def test_take_registered_with_custodian(flip, gateway):
    from relay_engine.custodian import OpenPosition
    flip.custodian.adopt(OpenPosition(event="EV", market=TICKER, lane="FLIP",
                                      side="yes", count=1, entry_price_cents=40,
                                      entry_p_win=0.0, size_tier=config.TIER_PROBE))
    take = lane_flip.Order(lane="FLIP", event="EV", market=TICKER, side="yes",
                           action="sell", price_cents=44, count=1,
                           size_tier=config.TIER_PROBE, purpose="EXIT")
    flip.windows[TICKER] = lane_flip.FlipWindow(market=TICKER, close_ts=CLOSE)
    flip.on_submitted(take, "TAKE-1", 0.0)
    # one exit owner: the custodian holds the resting exit's baton
    assert flip.custodian.positions[f"{TICKER}:FLIP"].resting_exit_id == "TAKE-1"


def test_r1_wall_blocks_reentry_when_not_flat(flip, gateway):
    w = flip._window(TICKER, CLOSE)
    w.trips = 1  # a completed trip
    event = TICKER.rsplit("-", 1)[0]
    gateway.positions[(event, TICKER, "FLIP")] = 1  # NOT flat
    assert flip.evaluate(TICKER, ctx(flip_book(), secs_left=500)) == []


def test_ofi_gated_reentry(flip, gateway):
    w = flip._window(TICKER, CLOSE)
    w.trips = 1
    # flat, but ticks/lean disagree -> wait
    w.spot_ticks = [1, 2, 1, 2, 3]
    assert flip.evaluate(TICKER, ctx(flip_book(yq=10, nq=5), secs_left=500)) == []
    # agreeing tape + lean -> re-enter that side only
    w.spot_ticks = [1, 2, 3, 4, 5]
    props = flip.evaluate(TICKER, ctx(flip_book(yes=40, no=45, yq=10, nq=5),
                                      secs_left=495))
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]


def test_stop_streak_kills_lane(flip, gateway):
    flip.note_window_result(TICKER, -30)   # stopped window 1
    assert not flip.killed
    flip.note_window_result(TICKER, -26)   # stopped window 2 -> kill
    assert flip.killed
    assert "LANE_KILL:FLIP" in gateway.entries_halted_reasons
    assert flip.evaluate(TICKER, ctx(flip_book())) == []
    # a green window would have reset the streak
    flip2 = LaneFlip(gateway)
    flip2.note_window_result(TICKER, -30)
    flip2.note_window_result(TICKER, +4)
    flip2.note_window_result(TICKER, -30)
    assert not flip2.killed and flip2.stop_streak == 1


def test_cut_params_encode_scratch_reasons():
    p = flip_cut_params()
    assert p.max_loss_cents_per_contract == FLIP_SCRATCH_S           # (a)
    assert p.spot_safe_buffer_early_usd == 0.01                      # (b)
    assert p.reversal_threshold == lane_flip.FLIP_MARKOUT_STOP / 100  # (c)
    assert p.hard_stop_usd == lane_flip.FLIP_STOP_CENTS / 100
    assert p.passthrough is False


def test_custodian_scratches_on_flip_mark_drop(flip, gateway, ledger, surface):
    """Scratch (a) executes through the custodian: mark 3c under entry cuts."""
    from relay_engine.custodian import OpenPosition
    c = flip.custodian
    pos = OpenPosition(event="EV", market=TICKER, lane="FLIP", side="yes",
                       count=1, entry_price_cents=45, entry_p_win=0.45,
                       size_tier=config.TIER_PROBE, entry_time=100.0)
    # mark 42 = entry-3; spot adverse (below strike) so the master override
    # does not hold the leg
    trigger = c.should_cut(pos, now=200.0, secs_remaining=400, p_win=0.42,
                           exit_bid_cents=42, spot=63_000.0,
                           boundary_lo=64_000.0, boundary_hi=None,
                           balance_usd=100.0)
    assert trigger == "HARD_STOP_PER_CONTRACT"
    # mark 43 = entry-2: holds
    pos2 = OpenPosition(event="EV", market=TICKER, lane="FLIP", side="yes",
                        count=1, entry_price_cents=45, entry_p_win=0.45,
                        size_tier=config.TIER_PROBE, entry_time=100.0)
    assert c.should_cut(pos2, now=200.0, secs_remaining=400, p_win=0.43,
                        exit_bid_cents=43, spot=63_000.0,
                        boundary_lo=64_000.0, boundary_hi=None,
                        balance_usd=100.0) is None


def test_flip_bundle_reconstructible_from_surface_rows(gateway, ledger, surface):
    """THE ACCEPTANCE TEST (Engineer flag): both flip legs route through the
    gateway, book through the fills path as lane FLIP, and the settled bundle
    reconstructs per-lane from surface rows alone."""
    booker = FillBooker(gateway, ledger, surface)
    book = flip_book(yes=40, no=45)
    for side, px in (("yes", 40), ("no", 45)):
        order = lane_flip.Order(lane="FLIP", event="EV", market=TICKER, side=side,
                                action="buy", price_cents=px, count=1,
                                size_tier=config.TIER_PROBE, purpose="ENTRY")
        r = gateway.submit(order, book)
        booker.sweep([{"fill_id": f"fb-{side}", "order_id": r.order_id,
                       "yes_price_dollars": f"{px / 100:.4f}" if side == "yes"
                       else f"{(100 - px) / 100:.4f}",
                       "count": 1}], now=1000.0)

    per_lane = surface.settle_market(TICKER, "w1", settled_yes=True)
    # bundle: yes leg +60, no leg -45 -> net +15 = 100 - (40+45), the rung-A capture
    assert per_lane == {"FLIP": 15}
    recon = surface.reconstruct_per_lane(TICKER, "w1")
    assert recon["FLIP"] == 15
    assert ledger.lifetime_pnl_cents() == 15