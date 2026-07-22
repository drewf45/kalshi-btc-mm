"""WO-FLIP-EXIT-DOCTRINE — the cut is about the DECISION, not the price
(build 40).

Drew watched FLIP self-exit almost immediately on nearly every entry
(201215: bought yes@44¢, cut at 35¢ same window, −11¢). Not a bug — a
DOCTRINE GAP: the exit was a scalper's stop (price touches a level → out)
bolted onto a position trade whose thesis is buy-cheap-hold-sell-the-swing.
The band floor (35¢) was doing two incompatible jobs — marking where
swings happen AND where the cut fires — and for a cheap entry those were
9¢ apart, inside the swing, so the contract could never breathe toward its
take without tripping its own cut.

The three changes (ATOMIC — all pass together or the commit is
incomplete):
  1. SEPARATE the swing floor (35¢, oscillation is held) from the price
     cut (a FIXED catastrophe floor at 20¢, well below the swing).
  2. determined-against is PRIMARILY spot+time (the market's decision),
     the price floor demoted to the catastrophe backstop.
  3. patience means what it says — the ~2s floor-poll bypass is gone; only
     the catastrophe floor and a sustained spot collapse act inside it.

HARD RAIL: no Kelly/cash/rate-halt change; HUNT's scalper cut unchanged;
the swing ENTRY gate untouched — EXIT doctrine only.

SUPERSEDED on the loss side by WO-FLIP-LIQUIDITY-HOLD (build 45): the FLIP
lane is reconceived as LIQUIDITY PROVISION — a low mark after a buy is
ILLIQUIDITY (the pile-in), not a loss. Build-43's brief SCALP stop is
RETIRED; the position HOLDS through the dip as the resting liquidity
provider, and only a CONFIRMED collapse (SPOT-decided sustained, or the
catastrophe floor 20) cuts. The band-floor price cut is retired too; an
unreverted loser is cleared by the T-10 endgame handoff. The loss-side tests
below are rewritten to the liquidity-hold reality; the take, SPOT, and
catastrophe tests stand."""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip
from relay_engine.spotlead import Needle

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=40, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=850, grain=None, sl=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": None, "grain": grain, "spotlead": sl}


@pytest.fixture(autouse=True)
def funnel(ledger):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    yield
    failures._ledger = None


@pytest.fixture
def flip(gateway, ledger, surface):
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def _entry(flip, gateway, ledger, entry=60):
    """A booked FAVORED-yes OPEN leg (yes@entry favored over no@40) with its
    take resting — WO-2026-07-22-E re-anchors the old cheap-yes shape."""
    b = OrderBook(market=TICKER)
    b.apply_snapshot({entry: 10}, {40: 10}, ts=1.0)
    ctx = {"book": b, "now": CLOSE - 850, "close_ts": CLOSE, "spot": None,
           "grain": GRAIN_YES2, "spotlead": None}
    props = flip.evaluate(TICKER, ctx)
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", entry, 1, "PROBE")
    flip.note_fill(TICKER, "yes", entry, CLOSE - 790)
    o = flip.windows[TICKER].opens["yes"]
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=entry, no=40), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    return o


def _cuts(props):
    return [p for p in props if p.purpose == "CUT"]


# ── Part D, test 1: an adverse move EXITS — there is no hold (INVERTED) ────
def test_favored_dip_to_stop_exits_no_hold(flip, gateway, ledger):
    """INVERTED by WO-2026-07-22-E: on the FAVORED side an adverse move means the
    thesis is ALREADY WRONG — there is NO hold. A 60¢ entry dipping through its
    momentum stop (entry−10=50) does NOT sit as a liquidity provider; sustained
    2 polls, it EXITS. (The old illiquidity-hold that rode the dip is retired.)"""
    o = _entry(flip, gateway, ledger, entry=60)
    # the first adverse poll ARMS the stop but does not fire (never a single print)
    assert _cuts(flip.evaluate(TICKER, _ctx(_book(yes=48, no=55),
                                            secs_left=770))) == []
    assert o["stop_polls"] == 1 and not o.get("done")
    # the second sustained poll fires — the book is through the stop, so it
    # crosses at the mark
    cuts = _cuts(flip.evaluate(TICKER, _ctx(_book(yes=48, no=55),
                                            secs_left=769)))
    assert len(cuts) == 1 and cuts[0].price_cents == 48 and cuts[0].crossfire
    assert "momentum stop" in cuts[0].reason
    assert o.get("exit_reason") == "MOMENTUM_STOP" and o.get("done")


# ── Part D, test 2: the momentum stop is MAKER-FIRST, not a market dump ────
def test_momentum_stop_rests_maker_first_not_a_market_dump(flip, gateway, ledger):
    """REPLACED by WO-2026-07-22-E: the spot-decided walk-to-scratch is retired.
    The momentum stop is MAKER-FIRST — when the mark sits AT the stop (book not
    yet through us), it RESTS a maker sell at the stop, never a crossfire
    market-dump. Sustained 2 polls."""
    o = _entry(flip, gateway, ledger, entry=60)
    # mark sits exactly at the stop (entry−10=50): the book is not through us
    flip.evaluate(TICKER, _ctx(_book(yes=50, no=55), secs_left=770))       # poll 1
    props = flip.evaluate(TICKER, _ctx(_book(yes=50, no=55), secs_left=769))  # poll 2
    assert _cuts(props) == []                          # NO market-dump
    exits = [p for p in props if p.purpose == "EXIT"]
    assert len(exits) == 1
    assert exits[0].price_cents == 50 and not exits[0].crossfire   # rest at the stop, maker
    assert "momentum stop" in exits[0].reason
    assert o.get("exit_reason") == "MOMENTUM_STOP" and not o.get("hold")


# ── Part D, test 3: a gap THROUGH the stop → crossed out (bounded) ─────────
def test_ride_through_the_stop_crosses_out(flip, gateway, ledger):
    """REPLACED by WO-2026-07-22-E: the fixed catastrophe floor for a live scalp
    is retired (it now only backstops a curfew HOLD). A non-hold favored position
    that gaps THROUGH its momentum stop (book already below entry−10) is crossed
    out at the mark on 2 sustained polls — a bounded cut, never a ride to the
    bell."""
    o = _entry(flip, gateway, ledger, entry=60)
    flip.evaluate(TICKER, _ctx(_book(yes=20, no=55), secs_left=771))  # poll 1
    cuts = _cuts(flip.evaluate(TICKER, _ctx(_book(yes=20, no=55),
                                            secs_left=770)))
    assert len(cuts) == 1 and cuts[0].crossfire
    assert cuts[0].price_cents == 20 and "momentum stop" in cuts[0].reason


# ── Part D, test 5: the swing to the take → TAKE, exit changes don't touch it ─
def test_swing_to_take_still_fires(flip, gateway, ledger):
    """The take is unaffected by the exit-doctrine changes — a 60¢ favored
    entry's take now rests at entry + OPEN_GOUGE_C (=80, cap 90) and a fill there
    realizes +20."""
    o = _entry(flip, gateway, ledger, entry=60)
    assert o["take_oid"] == "OID-T1"       # the take rested
    assert o["take_px"] == 80              # entry + OPEN_GOUGE_C, cap 90
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 80, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 80, CLOSE - 700, count=1)
    assert flip.windows[TICKER].window_realized == 20   # 80 − 60


# ── Part D, test 6: the cut REASON names the momentum stop ─────────────────
def test_cut_reason_names_the_momentum_stop(flip, gateway, ledger):
    """WO-2026-07-22-E: the only adverse-move cut names the MOMENTUM STOP — never
    a band-floor touch, the retired scalp stop, or the catastrophe floor. A lone
    adverse poll does not fire (never a single print); the sustained 2-poll cut
    names the stop."""
    o = _entry(flip, gateway, ledger, entry=60)
    # a single adverse poll arms but does not cut
    assert _cuts(flip.evaluate(TICKER, _ctx(_book(yes=48, no=55),
                                            secs_left=770))) == []
    c = _cuts(flip.evaluate(TICKER, _ctx(_book(yes=48, no=55),
                                         secs_left=769)))
    assert len(c) == 1 and "momentum stop" in c[0].reason
    assert "band floor" not in c[0].reason and "SCALP" not in c[0].reason
    assert "CATASTROPHE" not in c[0].reason
    assert o.get("exit_reason") == "MOMENTUM_STOP"


# ── the unreverted loser clears at the decision handoff, not a price floor ─
def test_unreverted_loser_clears_at_decision_not_a_price_floor(flip, gateway, ledger):
    """CURFEW (unchanged), re-anchored to the favored side: a lone poll of a
    loser mark ABOVE the stop does not exit (the momentum stop needs the mark at
    or below entry−10, sustained 2 polls); an unreverted loser that survives to
    the decision point is cleared THERE at the mark by the endgame handoff,
    never ridden to settlement."""
    o = _entry(flip, gateway, ledger, entry=60)
    # a loser mark above the stop (54 > entry−10=50) HOLDS — no price-floor cut
    assert _cuts(flip.evaluate(TICKER, _ctx(_book(yes=54, no=45),
                                            secs_left=760))) == []
    # at the decision point the unreverted loser is cleared by the handoff
    cuts = _cuts(flip.evaluate(TICKER, _ctx(_book(yes=55, no=45),
                                            secs_left=config.FLIP_DECISION_S - 1)))
    assert len(cuts) == 1 and "decision point" in cuts[0].reason


# ── HARD RAIL ──────────────────────────────────────────────────────────────
def test_rails_unchanged():
    assert config.KELLY_FRACTION_CEILING == pytest.approx(1.0 / 12.0)
    assert config.RATE_HALT_LOSSES == 2
    assert config.OPEN_TAKE_CENTS == 20          # the take is untouched
    assert config.OPEN_CATASTROPHE_FLOOR == 20   # the new price backstop
    # HUNT's scalper cut is unchanged — Job-B fast bails still fire
    from relay_engine.lane_flip import FLIP_X
    assert FLIP_X == 4
