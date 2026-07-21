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


def _entry(flip, gateway, ledger, entry=40):
    """A booked OPEN leg with its take resting — the 201215 shape."""
    b = OrderBook(market=TICKER)
    b.apply_snapshot({entry: 10}, {55: 10}, ts=1.0)
    ctx = {"book": b, "now": CLOSE - 850, "close_ts": CLOSE, "spot": None,
           "grain": GRAIN_YES2, "spotlead": None}
    props = flip.evaluate(TICKER, ctx)
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", entry, 1, "PROBE")
    flip.note_fill(TICKER, "yes", entry, CLOSE - 790)
    o = flip.windows[TICKER].opens["yes"]
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=entry, no=55), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    return o


def _cuts(props):
    return [p for p in props if p.purpose == "CUT"]


# ── Part D, test 1: the dip is ILLIQUIDITY — the position HOLDS ────────────
def test_44_entry_dip_is_illiquidity_holds(flip, gateway, ledger):
    """OVERTURNED by WO-FLIP-LIQUIDITY-HOLD (build 45): a low mark after a
    FLIP buy is ILLIQUIDITY (the opening pile-in), not a loss. The build-43
    scalp stop that sold into it is GONE — a 44¢ entry dipping to 34¢ HOLDS
    as the resting liquidity provider, waiting for the reversion to lift its
    take. Only a confirmed collapse (spot/catastrophe) cuts."""
    o = _entry(flip, gateway, ledger, entry=40)
    for secs in (770, 769, 768, 760, 740):
        assert _cuts(flip.evaluate(TICKER, _ctx(_book(yes=34, no=55),
                                                secs_left=secs))) == []
    assert not o.get("done")


# ── Part D, test 2: spot decides → CUT on spot, any time ───────────────────
def test_spot_decision_walks_to_scratch_not_a_market_dump(flip, gateway, ledger):
    """OVERTURNED by WO-FULL-COLD-AUDIT Finding 1 (build 52): a SPOT-decided
    collapse NO LONGER crossfire-DUMPS at the depressed bid (the catastrophic-
    loss generator). It routes through the walk-down — a MAKER exit at scratch
    (entry, never below cost, no crossfire). And K is raised to 40, so only a
    real decision triggers it (not 15pt drift). Past the hard-hold, 2 sustained
    polls."""
    o = _entry(flip, gateway, ledger, entry=40)
    o["fill_ts"] = CLOSE - 1050        # past FLIP_NO_SELL_S (age ~280), inside patience
    sl = Needle(side="no", d_before=10.0, d_after=90.0,
                delta_p=config.OPEN_DETERMINED_K_POINTS + 5.0,
                fair_cents=0.0, t_remaining=700.0)
    flip.evaluate(TICKER, _ctx(_book(yes=38, no=55), secs_left=770, sl=sl))
    props = flip.evaluate(TICKER, _ctx(_book(yes=38, no=55),
                                       secs_left=769, sl=sl))
    assert _cuts(props) == []                          # NO market-dump
    exits = [p for p in props if p.purpose == "EXIT"]
    assert len(exits) == 1
    assert exits[0].price_cents == 40 and not exits[0].crossfire   # scratch, maker
    assert "spot-decided" in exits[0].reason and "scratch" in exits[0].reason
    assert not o.get("hold")


# ── Part D, test 3: rides to the catastrophe floor → CUT (bounded) ─────────
def test_ride_to_catastrophe_floor_cuts(flip, gateway, ledger):
    o = _entry(flip, gateway, ledger, entry=40)
    o["fill_ts"] = CLOSE - 1100                        # past the opening window
    flip.evaluate(TICKER, _ctx(_book(yes=20, no=55), secs_left=771))  # poll 1
    cuts = _cuts(flip.evaluate(TICKER, _ctx(_book(yes=20, no=55),
                                            secs_left=770)))
    assert len(cuts) == 1 and "CATASTROPHE floor" in cuts[0].reason
    assert cuts[0].price_cents == config.OPEN_CATASTROPHE_FLOOR == 20


# ── Part D, test 5: the swing to 64 → TAKE, exit changes don't touch it ────
def test_swing_to_take_still_fires(flip, gateway, ledger):
    """The middle take is unaffected by the exit-doctrine changes — a 40¢
    entry's take rests at the 52¢ middle and the fix leaves it intact."""
    o = _entry(flip, gateway, ledger, entry=40)
    assert o["take_oid"] == "OID-T1"       # the take rested at the 52 middle
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 52, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 52, CLOSE - 700, count=1)
    assert flip.windows[TICKER].window_realized == 12   # 52 − 40


# ── Part D, test 6: the cut REASON names a real decision, not illiquidity ──
def test_cut_reason_names_the_decision(flip, gateway, ledger):
    """The only cuts in the passive hold name a real DECISION — SPOT decided
    or the CATASTROPHE backstop — never a band-floor touch and never the
    retired scalp stop. An illiquidity dip (34¢) is HELD."""
    o = _entry(flip, gateway, ledger, entry=40)
    # a dip to the old band floor is illiquidity now → HOLD
    assert _cuts(flip.evaluate(TICKER, _ctx(_book(yes=34, no=55),
                                            secs_left=770))) == []
    # a GENUINE collapse (past the opening window, sustained) names the
    # catastrophe — not a band-floor touch, not the retired scalp stop
    o["fill_ts"] = CLOSE - 1100                        # past the opening window
    flip.evaluate(TICKER, _ctx(_book(yes=20, no=55), secs_left=769))  # poll 1
    c = _cuts(flip.evaluate(TICKER, _ctx(_book(yes=20, no=55),
                                         secs_left=768)))
    assert len(c) == 1 and "CATASTROPHE" in c[0].reason
    assert "band floor" not in c[0].reason and "SCALP" not in c[0].reason


# ── the unreverted loser clears at the T-10 handoff, not a price floor ─────
def test_unreverted_loser_clears_at_t10_not_a_price_floor(flip, gateway, ledger):
    """The band-floor 'the swing did not come' price cut is RETIRED (WO-45): a
    low mark is illiquidity, held through the window. An unreverted loser is
    not stopped on price — it is cleared by the T-10 endgame handoff (the
    primary loss exit now), never ridden to settlement."""
    o = _entry(flip, gateway, ledger, entry=40)
    # even past patience, a low in-band mark HOLDS (no band-floor cut)
    o["fill_ts"] = CLOSE - 1100
    assert _cuts(flip.evaluate(TICKER, _ctx(_book(yes=34, no=55),
                                            secs_left=760))) == []
    # at the decision point (build 50: moved from T-10 to FLIP_DECISION_S), the
    # unreverted loser is cleared by the endgame handoff
    cuts = _cuts(flip.evaluate(TICKER, _ctx(_book(yes=34, no=55),
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
