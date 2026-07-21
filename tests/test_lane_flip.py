"""P3.2 — Lane FLIP tests. P21 A4/A5 OVERTURNED the PAIR-era laws here
(the venue nets one account's sides — a pair-bundle was a fiction; the
10:45/11:30 WINDOW_ECON_DIVERGENCE pages were its ghost): entries are now
Lane OPEN (band + grain, one lot, grain side) and exits are intent-based
(the patient hold). Gateway routing, one exit owner, stop-streak kill, and
the Engineer's acceptance test survive unchanged in spirit."""

import pytest

from relay_engine import config, lane_flip
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.fills import FillBooker
from relay_engine.lane_flip import (
    LaneFlip, book_lean, flip_cut_params, ofi_side, scratch_reason,
    spot_adverse, tick_direction,
)

TICKER = "KXBTC15M-02JAN251000-T99"
CLOSE = 1_000_000.0

# P21 A3: a grain the OPEN setup accepts (streak >= OPEN_MIN_GRAIN)
GRAIN_NO3 = {"direction": "no", "length": 3, "k": 4}
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def flip_book(yes=40, no=45, yq=10, nq=10):
    b = OrderBook(market=TICKER)
    yes_levels = {yes: yq} if yes is not None else {}
    no_levels = {no: nq} if no is not None else {}
    b.apply_snapshot(yes_levels, no_levels, ts=1.0)
    return b


def ctx(book, secs_left=850, spot=None, grain=None, spotlead=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": spotlead}


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


# ── P21 A4: Lane OPEN entry mechanics (PAIR retired) ───────────────────────
def test_open_posts_cheap_side_of_the_imbalance(flip):
    """WO-FLIP-IMMEDIATE-ENTRY (build 47): an open-band book posts ONE lot on
    the CHEAP side (the lower bid — the pile-in-abandoned side), NOT the grain
    side. yes 48 < no 49 -> buy YES@48; grain only informs the why."""
    props = flip.evaluate(TICKER, ctx(flip_book(yes=40, no=49),
                                      grain=GRAIN_NO3))
    assert [(p.side, p.price_cents, p.purpose) for p in props] == \
        [("yes", 40, "ENTRY")]
    assert props[0].lane == "FLIP" and props[0].count == 1
    assert "OPEN liquidity yes@40c" in props[0].why
    assert "book y40/n49" in props[0].why


def test_open_no_grain_still_enters(flip):
    """WO-FLIP-IMMEDIATE-ENTRY OVERTURNED 'no grain -> no trade': the grain
    wait is retired (the liquidity capstone). A band book with weak grain OR
    no grain at all ENTERS the cheap side immediately — the imbalance IS the
    setup, not a grain streak."""
    # weak grain (length < OPEN_MIN_GRAIN) no longer blocks
    w_ctx = ctx(flip_book(yes=40, no=49),
                grain={"direction": "no", "length": 1, "k": 4})
    assert [(p.side, p.purpose) for p in flip.evaluate(TICKER, w_ctx)] == \
        [("yes", "ENTRY")]
    # and with NO grain at all (empty screen): still enters the cheap side
    flip.windows.clear()
    assert [(p.side, p.purpose) for p in
            flip.evaluate(TICKER, ctx(flip_book(yes=40, no=49)))] == \
        [("yes", "ENTRY")]


def test_open_band_required(flip):
    """OVERTURNED by WO-FLIP-EVERY-MARKET-LIQUIDITY (build 49): the 'both sides
    inside the open band' gate is RETIRED — it rejected biased opens and made
    FLIP wait for a balanced book. FLIP is the liquidity provider now: a biased
    open (yes 30 / no 65) ENTERS the cheap side (yes@30, inside the buyable
    range [25,50]) — the imbalance IS the setup."""
    props = flip.evaluate(TICKER, ctx(flip_book(yes=30, no=65),
                                      grain=GRAIN_NO3))
    assert [(p.side, p.price_cents, p.purpose) for p in props] == \
        [("yes", 30, "ENTRY")]


def test_open_cheap_side_paid_up_passes(flip):
    """OVERTURNED by WO-FLIP-EVERY-MARKET-LIQUIDITY (build 49): with
    OPEN_MAX_ENTRY_CENTS relaxed to 50 and book coherence (yes_bid+no_bid<=101)
    guaranteeing the cheap side <= 50, the 'paid up' CEILING skip is now
    unreachable — every coherent biased open is buyable. The remaining range
    gate is the FLOOR: a near-worthless cheap side (yes 20, below
    OPEN_ENTRY_FLOOR 25) is skipped."""
    assert config.OPEN_MAX_ENTRY_CENTS == 42       # build 52 Finding 4: real-gouge only
    assert config.OPEN_ENTRY_FLOOR == 25
    # below the floor (near-worthless) skips
    assert flip.evaluate(TICKER, ctx(flip_book(yes=20, no=79),
                                     grain=GRAIN_NO3)) == []
    # build 52: above the tightened ceiling (a ~coinflip, no real gouge) skips
    flip.windows.clear()
    assert flip.evaluate(TICKER, ctx(flip_book(yes=45, no=54),
                                     grain=GRAIN_NO3)) == []


def test_open_curfew_and_no_entry_phase(flip):
    """OVERTURNED by WO-INSTRUMENTATION-AND-FLIP-TIMING (build 51): entry is
    the opening 90s only — an open-band book posts at the OPEN (secs_into 50),
    never mid-window. The late curfew stands."""
    # build 51: the entry window is T-15 → the first 90s (was mid-window)
    props = flip.evaluate(TICKER, ctx(flip_book(yes=40, no=49),
                                      secs_left=850, grain=GRAIN_NO3))
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]
    flip.windows.clear()
    # past the T-10 cutoff nothing posts; window over posts nothing
    assert flip.evaluate(TICKER, ctx(flip_book(yes=40, no=49),
                                     secs_left=200, grain=GRAIN_NO3)) == []
    assert flip.evaluate(TICKER, ctx(flip_book(yes=40, no=49),
                                     secs_left=50, grain=GRAIN_NO3)) == []


def test_open_take_posted_after_fill(flip, gateway):
    """A5 exit one of three: the TAKE rests at entry + the goal-bounded take
    the cycle after the fill books (WO-FLIP-GOAL-TAKE overturned the fixed
    entry+OPEN_TAKE_CENTS — at the 1-lot cap this is entry+5, the reachable
    nickel; pre-P21 it was the pair take entry+4)."""
    from relay_engine.lane_flip import LaneFlip
    props = flip.evaluate(TICKER, ctx(flip_book(yes=40, no=49),
                                      grain=GRAIN_YES2))
    flip.on_submitted(props[0], "OID-Y", CLOSE - 800)
    event = TICKER.rsplit("-", 1)[0]
    gateway.positions[(event, TICKER, "FLIP")] = 1
    flip.note_fill(TICKER, "yes", 40, CLOSE - 790)
    assert "yes" in flip.windows[TICKER].opens
    props2 = flip.evaluate(TICKER, ctx(flip_book(yes=40, no=49),
                                       secs_left=780))
    takes = [p for p in props2 if p.purpose == "EXIT"]
    assert len(takes) == 1
    # build 49+: the take rests at the middle-target (_take_price), entry 40 -> 52
    assert (takes[0].side, takes[0].price_cents, takes[0].action) == \
        ("yes", LaneFlip._take_price(40), "sell")
    assert "open take" in takes[0].reason


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


def test_reentry_is_open_gated(flip, gateway):
    """P21 A4 OVERTURNED the OFI re-entry gate: a new trip after a completed
    one opens the same way the first did — band + grain (the herd's screen,
    not the tape's last four ticks)."""
    w = flip._window(TICKER, CLOSE)
    w.trips = 1
    # WO-FLIP-IMMEDIATE-ENTRY: re-entry opens on band + imbalance, no grain
    # needed. Too early is gated by the entry cutoff, not a grain wait.
    assert flip.evaluate(TICKER, ctx(flip_book(yes=40, no=49),
                                     secs_left=200)) == []      # past T-10 cutoff
    # inside the opening 90s (build 51): re-entry buys the cheap side (yes 48 < no 49)
    props = flip.evaluate(TICKER, ctx(flip_book(yes=40, no=49),
                                      secs_left=850))
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]


def test_stop_streak_reports_never_kills(flip, gateway):
    """P27 §1b OVERTURNED the stop-streak lane kill (R2's per-lane kill
    class, same as fh8 Wall 3c): the streak still COUNTS for the packs,
    but the lane keeps trading — the account halt is THE stop."""
    flip.note_window_result(TICKER, -30)   # stopped window 1
    flip.note_window_result(TICKER, -26)   # stopped window 2 — NO kill
    assert not flip.killed
    assert flip.stop_streak == 2
    assert "LANE_KILL:FLIP" not in gateway.entries_halted_reasons
    # a green window still resets the streak (reporting stays honest)
    flip.note_window_result(TICKER, +4)
    assert flip.stop_streak == 0


def test_cut_params_catastrophic_only():
    """P21 A5 OVERTURNED the P3-era scratch mapping (was: entry-3 per
    contract, markout reversal, flip dollar stop): FLIP exits are
    INTENT-BASED and lane-owned; the custodian keeps ONLY the catastrophic
    backstop. SOURCE: the −11¢ (10:30) and −12¢ (11:17) round-trips were
    OPEN-intent positions killed by fast-intent stops — the last of their
    kind."""
    p = flip_cut_params()
    assert p.max_loss_cents_per_contract == 100    # scratch (a) retired
    assert p.reversal_threshold == 1.0             # markout (c) retired
    assert p.hard_stop_usd == 999.0                # window stop off custody
    assert p.catastrophic_loss_cents == 90         # the one backstop
    assert p.salvage_enabled is False  # P19 renamed passthrough


def test_custodian_holds_flip_through_wiggles(flip, gateway, ledger, surface):
    """P21 A5: mark entry-3 with adverse spot is a WIGGLE, not an exit —
    pre-P21 this exact shape cut HARD_STOP_PER_CONTRACT (the −11¢/−12¢
    class). The custodian holds; only the catastrophic backstop cuts."""
    from relay_engine.custodian import OpenPosition
    c = flip.custodian
    pos = OpenPosition(event="EV", market=TICKER, lane="FLIP", side="yes",
                       count=1, entry_price_cents=45, entry_p_win=0.45,
                       size_tier=config.TIER_PROBE, entry_time=100.0)
    assert c.should_cut(pos, now=200.0, secs_remaining=400, p_win=0.42,
                        exit_bid_cents=42, spot=63_000.0,
                        boundary_lo=64_000.0, boundary_hi=None,
                        balance_usd=100.0) is None
    # the backstop remains REACHABLE: a 90c/contract collapse still cuts
    pos2 = OpenPosition(event="EV", market=TICKER, lane="FLIP", side="yes",
                        count=1, entry_price_cents=95, entry_p_win=0.95,
                        size_tier=config.TIER_PROBE, entry_time=100.0)
    assert c.should_cut(pos2, now=200.0, secs_remaining=400, p_win=0.04,
                        exit_bid_cents=4, spot=63_000.0,
                        boundary_lo=64_000.0, boundary_hi=None,
                        balance_usd=100.0) == "CATASTROPHIC"


def test_flip_round_trip_reconstructible_from_surface_rows(gateway, ledger, surface):
    """THE ACCEPTANCE TEST (Engineer flag), P21 A1 edition: the venue nets
    one account's sides, so the old two-leg pair bundle is impossible by
    design (the A2 wall refuses the second buy — graded in
    test_p21_doctrine). The property stands on a round trip: entry + take
    through the same gateway/fills path as lane FLIP, and the settled trip
    reconstructs per-lane from surface rows alone."""
    booker = FillBooker(gateway, ledger, surface)
    book = flip_book(yes=40, no=49)
    entry = lane_flip.Order(lane="FLIP", event="EV", market=TICKER, side="yes",
                            action="buy", price_cents=48, count=1,
                            size_tier=config.TIER_PROBE, purpose="ENTRY",
                            why="OPEN grain yesx2 · join 48c · PROBE n=0 · geometry=v2")
    r = gateway.submit(entry, book)
    booker.sweep([{"fill_id": "fb-e", "order_id": r.order_id,
                   "yes_price_dollars": "0.4800", "count": 1}], now=1000.0)
    take = lane_flip.Order(lane="FLIP", event="EV", market=TICKER, side="yes",
                           action="sell", price_cents=53, count=1,
                           size_tier=config.TIER_PROBE, purpose="EXIT")
    r2 = gateway.submit(take, book)
    booker.sweep([{"fill_id": "fb-x", "order_id": r2.order_id,
                   "yes_price_dollars": "0.5300", "count": 1}], now=1010.0)

    per_lane = surface.settle_market(TICKER, "w1", settled_yes=True)
    # round trip: entry 48, take 53 -> +5, the OPEN_TAKE_CENTS capture
    assert per_lane == {"FLIP": 5}
    recon = surface.reconstruct_per_lane(TICKER, "w1")
    assert recon["FLIP"] == 5
    assert ledger.lifetime_pnl_cents() == 5