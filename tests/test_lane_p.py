"""P3.5 — Lane P: THE NEGATIVE SPEC FIRST (the whipsaw shape): sequences P
must NOT trigger on. Then the one positive control."""

import pytest

from relay_engine.book import OrderBook
from relay_engine.lane_p import (
    P_CONFIRM_FRAMES, P_DISPLACEMENT, P_DISPLACEMENT_MAX, LaneP,
)

TICKER = "KXBTC15M-02JAN251000-T99"
CLOSE = 1_000_000.0


def frame(yes_bid, no_bid, ts):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes_bid: 20}, {no_bid: 20}, ts=ts)
    return b


def ctx(book, now, prior=0.50, spot_ticks=None):
    return {"book": book, "close_ts": CLOSE, "now": now, "prior_p": prior,
            "spot_ticks": spot_ticks}


@pytest.fixture
def lane():
    return LaneP()


# Frames: prior 0.50. Implied mid of (yes_bid, 100-no_bid)/2:
# calm 48/50 -> (48+50)/2 = 0.49 (displacement 0.01)
# spike 55/38 -> (55+62)/2 = 0.585 (displacement +0.085, in band)
# Frames are stamped AT their evaluation time (fresh) unless a test says otherwise.
T0 = CLOSE - 600
CALM = lambda dt: frame(48, 50, T0 + dt)
SPIKE = lambda dt: frame(55, 38, T0 + dt)


# ── THE NEGATIVE SPEC ──────────────────────────────────────────────────────
def test_single_frame_flicker_never_triggers(lane):
    """The whipsaw shape: one spiked frame, back to calm — no trade."""
    assert lane.evaluate(TICKER, ctx(CALM(0.0), now=T0))[0] is None
    p, r = lane.evaluate(TICKER, ctx(SPIKE(1.0), now=T0 + 1))
    assert p is None and r.startswith("CONFIRMING")     # 1/2, not a trigger
    p, r = lane.evaluate(TICKER, ctx(CALM(2.0), now=T0 + 2))
    assert p is None and r == "DISPLACEMENT_UNDER_TRIGGER"
    # and the flicker did NOT leave a primed counter behind
    p, r = lane.evaluate(TICKER, ctx(SPIKE(3.0), now=T0 + 3))
    assert p is None and r.startswith("CONFIRMING")


def test_stale_frames_never_count(lane):
    lane.evaluate(TICKER, ctx(SPIKE(0.0), now=T0))
    # same spike, but the frame is STALE (book ts far behind now) -> reset
    p, r = lane.evaluate(TICKER, ctx(SPIKE(1.0), now=T0 + 100))
    assert p is None and r == "STALE_FRAME"
    st = lane.states[TICKER]
    assert st.confirm_count == 0


def test_same_frame_never_double_counts(lane):
    b = SPIKE(0.0)
    lane.evaluate(TICKER, ctx(b, now=T0))
    p, r = lane.evaluate(TICKER, ctx(b, now=T0 + 1))  # same book ts re-read
    assert p is None and r == "NO_NEW_FRAME"
    assert lane.states[TICKER].confirm_count == 1


def test_oscillation_resets(lane):
    """Spike up, spike down, spike up — direction flips reset the count."""
    up = frame(55, 38, T0)       # implied 0.585 (+)
    down = frame(38, 55, T0 + 1)  # implied 0.415 (-)
    up2 = frame(55, 38, T0 + 2)
    lane.evaluate(TICKER, ctx(up, now=T0))
    p, r = lane.evaluate(TICKER, ctx(down, now=T0 + 1))
    assert p is None and r == "WHIPSAW_RESET"
    p, r = lane.evaluate(TICKER, ctx(up2, now=T0 + 2))
    assert p is None and r == "WHIPSAW_RESET"


def test_sub_band_and_over_band_displacement(lane):
    small = frame(49, 49, T0)    # implied 0.50 vs prior 0.50 -> 0.00
    p, r = lane.evaluate(TICKER, ctx(small, now=T0))
    assert p is None and r == "DISPLACEMENT_UNDER_TRIGGER"
    huge = frame(70, 20, T0 + 1)  # implied 0.75 -> displacement 0.25 > 0.10
    p, r = lane.evaluate(TICKER, ctx(huge, now=T0 + 1))
    assert p is None and r == "MOVE_NOT_SPIKE"


def test_tape_confirmed_move_not_faded(lane):
    """If the spot tape agrees with the spike, it is a MOVE — stand down."""
    lane.evaluate(TICKER, ctx(SPIKE(0.0), now=T0))
    rising = [1, 2, 3, 4, 5]  # all-up ticks agree with a yes-ward spike
    p, r = lane.evaluate(TICKER, ctx(SPIKE(1.0), now=T0 + 1,
                                     spot_ticks=rising))
    assert p is None and r == "TAPE_CONFIRMS_MOVE"


def test_no_prior_no_trade(lane):
    p, r = lane.evaluate(TICKER, ctx(SPIKE(0.0), now=T0, prior=None))
    assert p is None and r == "NO_PRIOR"


def test_too_late_never_triggers(lane):
    p, r = lane.evaluate(TICKER, ctx(frame(55, 38, CLOSE - 30), now=CLOSE - 30))
    assert p is None and r == "TOO_LATE"


# ── the positive control ───────────────────────────────────────────────────
def test_sustained_fresh_spike_fades(lane):
    """Two fresh consecutive spike frames, tape silent -> fade the cheap side."""
    p1, r1 = lane.evaluate(TICKER, ctx(SPIKE(0.0), now=T0))
    assert p1 is None
    p2, r2 = lane.evaluate(TICKER, ctx(SPIKE(1.0), now=T0 + 1))
    assert p2 is not None and r2 == "FADE"
    # yes-ward spike cheapened NO: fade buys NO at its bid (38 <= 49)
    assert (p2.lane, p2.side, p2.price_cents, p2.count) == ("P", "no", 38, 1)
    # one entry per market
    p3, r3 = lane.evaluate(TICKER, ctx(SPIKE(2.0), now=T0 + 2))
    assert p3 is None and r3 == "ALREADY_ENTERED"