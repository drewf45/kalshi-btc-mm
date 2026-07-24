"""WO-BLEED-DIAGNOSIS — the two live bleeds (build 35).

Bleed 1: HUNT chased a collapsing needle 20→13→8, each level "converging"
— averaging down into a loser, the one move the doctrine forbids. The
law: one direction per window; re-entry at OR BELOW the prior entry
(Adversary: <=) is refused (HUNT_REFUSE_LOWER); one HUNT loss sits the
window out. OPEN untouched.

Bleed 3: Instrument 2 measured losers cutting −25c past a −15c floor —
the patience gate held the cut through the collapse and fired at the
bottom. The law: a book SUSTAINED through the band floor (2 polls) is a
DECISION and cuts NOW, any minute; a one-frame flicker still holds; the
ΔP-collapse leg stays patience-gated.

RESERVED: §3 F passthrough A-vs-B is Drew's ruling — no F code here;
the pack gains the F TAIL data line the ruling needs.

HARD RAIL: OPEN swing gate, Kelly, cash-integrity, rate-halt untouched."""

import json

import pytest

from relay_engine import config, delta, failures
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


def _needle(side="no", fair=90.0):
    return Needle(side=side, d_before=200.0, d_after=20.0,
                  delta_p=config.HUNT_NEEDLE_POINTS + 5.0,
                  fair_cents=fair, t_remaining=700.0)


def _ctx(book, secs_left=850, sl=None, grain=None, spot=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": sl}


def _prime_open(flip, entry=60, grain=None):
    """WO-2026-07-22-F "wait for the pile": an OPEN entry needs two in-window
    polls that build the pile — a baseline (secs_into~65, small skew, spot low)
    then the entry poll (secs_into~80, skew grown >=5, |trend|>=15 agreeing with
    the favored yes side, favored depth >= other). Returns the entry proposals."""
    flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                               spot=66000.0))                    # baseline skew 6
    return flip.evaluate(TICKER, _ctx(_book(yes=entry, no=40), secs_left=820,
                                      spot=66020.0, grain=grain))


@pytest.fixture(autouse=True)
def funnel(ledger):
    alerts = []
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=alerts.append, run_mode="TEST",
                       boot_id=1)
    yield alerts
    failures._ledger = None


@pytest.fixture(autouse=True)
def _settle_surface(monkeypatch):
    """WO-2026-07-24-J: HUNT's forward gate reads the SETTLE surface. These
    bleed tests exercise the re-entry guards (which sit BEFORE gate B), so a
    high, permissive settle LB lets the first hunt fire and the guards do their
    job — the point of these tests is direction/averaging discipline, not the
    edge floor (that is graded in test_p18_detective / the WO-J suite)."""
    monkeypatch.setattr(delta, "p_end", lambda d, t, session="ALL": 0.92)
    monkeypatch.setattr(delta, "p_end_wilson_lb",
                        lambda d, t, session="ALL": 0.90)


@pytest.fixture
def flip(gateway, ledger, surface):
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def _hunt_at(flip, join, secs, side="no"):
    """Two confirm frames at a stable join -> the hunt entry proposal (or
    none, if the guards refuse)."""
    sl = _needle(side=side)
    p1 = flip.evaluate(TICKER, _ctx(_book(no=join), secs_left=secs, sl=sl))
    p2 = flip.evaluate(TICKER, _ctx(_book(no=join), secs_left=secs - 1,
                                    sl=sl))
    return [p for p in p1 + p2 if p.purpose == "ENTRY"]


# ── BLEED 1: HUNT never averages down ──────────────────────────────────────
def test_hunt_chase_20_13_8_is_dead(flip, caplog):
    """The exact tape: needle collapses 20→13→8, each level 'converging'.
    The first hunt fires; every LOWER re-entry is refused — the knife is
    not chased."""
    import logging
    props = _hunt_at(flip, 20, 800)
    assert len(props) == 1 and props[0].price_cents == 20   # the first hunt
    flip.on_submitted(props[0], "OID-H1", CLOSE - 799)
    flip.windows[TICKER].posted.clear()      # order gone (filled/expired)
    with caplog.at_level(logging.INFO, logger="relay.lane_flip"):
        assert _hunt_at(flip, 13, 700) == []                # refused: lower
        assert _hunt_at(flip, 8, 650) == []                 # refused: lower
    lines = [r.message for r in caplog.records
             if "HUNT_REFUSE_LOWER" in r.message]
    assert lines and "13c <= prior 20c" in lines[0]
    assert len(lines) == 1                    # logged once per window


def test_hunt_equal_price_reentry_refused(flip):
    """Adversary (a): <= not < — a dip-then-redisplace at the SAME price
    is equally blocked."""
    props = _hunt_at(flip, 20, 800)
    flip.on_submitted(props[0], "OID-H1", CLOSE - 799)
    flip.windows[TICKER].posted.clear()
    assert _hunt_at(flip, 20, 700) == []


def test_hunt_higher_reentry_allowed_recovering_needle(flip):
    """A re-displacement ABOVE the prior entry is a recovering needle —
    a legit fresh opportunity, not a chase (Engineer: compare prior
    ENTRY, never the current mark)."""
    props = _hunt_at(flip, 20, 800)
    flip.on_submitted(props[0], "OID-H1", CLOSE - 799)
    flip.windows[TICKER].posted.clear()
    props2 = _hunt_at(flip, 26, 700)
    assert len(props2) == 1 and props2[0].price_cents == 26


def test_hunt_direction_locked_per_window(flip):
    """The tape's no→yes flip-flop: the window's direction locks at the
    first hunt; the opposite side is refused."""
    props = _hunt_at(flip, 20, 800, side="no")
    flip.on_submitted(props[0], "OID-H1", CLOSE - 799)
    flip.windows[TICKER].posted.clear()
    sl = _needle(side="yes", fair=90.0)
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=25), secs_left=700, sl=sl))
    p2 = flip.evaluate(TICKER, _ctx(_book(yes=25), secs_left=699, sl=sl))
    assert [p for p in p1 + p2 if p.purpose == "ENTRY"] == []


def test_one_hunt_loss_sits_the_window_out_open_unaffected(flip, gateway,
                                                           ledger):
    """§2.2: after ONE HUNT loss the window hunts no more — but OPEN (the
    working trade) is untouched and still posts."""
    props = _hunt_at(flip, 30, 800)
    flip.on_submitted(props[0], "OID-H1", CLOSE - 799)
    ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 30, 1, "PROBE")
    flip.note_fill(TICKER, "no", 30, CLOSE - 790)
    ledger.record_fill(TICKER, "FLIP", "no", "EXIT", 28, 1, "PROBE")
    flip.note_exit(TICKER, "no", 28, CLOSE - 780, count=1)   # -2c: a loss
    assert flip.windows[TICKER].hunt_lost is True
    assert _hunt_at(flip, 45, 700) == []      # higher, same side — still out
    # WO-2026-07-22-F/-G: OPEN enters when the PILE forms in [60,180]s; hunt_lost
    # never gates it. OPEN buys the FAVORED side (yes@60, in the band). The
    # earlier HUNT polls seeded spot-less ticks; clear the tick history so the
    # pile reads a clean baseline (the hunt_lost state, the point here, stands).
    w = flip.windows[TICKER]
    w.skew_ticks.clear()
    w.spot_ticks.clear()
    open_props = _prime_open(flip, grain=GRAIN_YES2)
    assert [(p.side, p.purpose) for p in open_props] == [("yes", "ENTRY")]


# ── BLEED 3: the floor stops slipping ──────────────────────────────────────
def _open_pos(flip, gateway, ledger, entry=60):
    # WO-2026-07-22-F: a favored-side (yes@entry over no@40) OPEN position built
    # through the pile prime; the take rests at entry+17 and the momentum stop
    # sits at entry−10.
    props = _prime_open(flip, entry=entry, grain=GRAIN_YES2)
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", entry, 1, "PROBE")
    flip.note_fill(TICKER, "yes", entry, CLOSE - 790)
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=entry, no=40), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    return flip.windows[TICKER].opens["yes"]


def test_bleed3_a_loser_cuts_at_the_momentum_stop_not_a_ride(flip, gateway,
                                                             ledger):
    """WO-2026-07-22-E: a loser never rides — the momentum stop (entry−10)
    cuts it. A mark WITHIN the stop (55 > stop 50 for a 60c entry) holds; a
    mark sustained AT the stop for 2 polls exits MAKER-FIRST at the stop (the
    WO-BLEED-3 regression stays cut, now by the stop not a spot-decided ride)."""
    o = _open_pos(flip, gateway, ledger)                    # entry 60, stop 50
    # within the stop: holds (does NOT ride or cut)
    for secs in (770, 769, 768):
        p = flip.evaluate(TICKER, _ctx(_book(yes=55, no=40), secs_left=secs))
        assert [x for x in p if x.purpose in ("CUT", "EXIT")] == []
    assert not o.get("done")
    # sustained AT the stop → 2 polls → momentum stop, maker-first at 50
    flip.evaluate(TICKER, _ctx(_book(yes=50, no=40), secs_left=767))   # poll 1
    props = flip.evaluate(TICKER, _ctx(_book(yes=50, no=40), secs_left=766))
    exits = [x for x in props if x.purpose in ("EXIT", "CUT")]
    assert len(exits) == 1 and exits[0].price_cents == 50 \
        and "momentum stop" in exits[0].reason and not exits[0].crossfire


def test_favored_dip_through_the_stop_cuts_no_illiquidity_hold(flip, gateway,
                                                               ledger):
    """INVERTED from WO-FLIP-LIQUIDITY-HOLD: on the favored side a low mark is
    NOT illiquidity to hold through — an adverse move is the thesis being
    WRONG. A shallow dip within the stop holds; a deep dip THROUGH the stop
    (34 < 50), sustained, cuts. WO-2026-07-24-D Part 2: through the floor (47)
    it rests one poll at the floor before the cross (the flatten's floor)."""
    o = _open_pos(flip, gateway, ledger)                    # entry 60, stop 50
    for secs in (780, 779):                 # shallow dip within the stop: holds
        p = flip.evaluate(TICKER, _ctx(_book(yes=55, no=40), secs_left=secs))
        assert [x for x in p if x.purpose in ("CUT", "EXIT")] == []
    assert not o.get("done")
    floor = 60 - config.OPEN_MOMENTUM_STOP_C - config.SLIP_TOLERANCE_C   # 47
    flip.evaluate(TICKER, _ctx(_book(yes=34, no=40), secs_left=778))   # poll 1
    p2 = flip.evaluate(TICKER, _ctx(_book(yes=34, no=40), secs_left=777))  # rest
    assert [x for x in p2 if x.purpose == "CUT"] == [] and any(
        x.price_cents == floor and not x.crossfire
        for x in p2 if x.purpose == "EXIT")
    props = flip.evaluate(TICKER, _ctx(_book(yes=34, no=40), secs_left=776))  # cross
    cuts = [x for x in props if x.purpose in ("CUT", "EXIT")]
    assert len(cuts) == 1 and "momentum stop" in cuts[0].reason
    assert cuts[0].crossfire and o.get("done")


def test_momentum_stop_needs_no_spot_and_two_polls_to_fire(flip, gateway,
                                                           ledger):
    """The momentum stop needs NO ΔP/spot signal (the spot-decided leg is
    retired) — an adverse held-side mark alone fires it, and only after 2
    sustained polls (a one-frame flicker still holds)."""
    o = _open_pos(flip, gateway, ledger)                    # entry 60, stop 50
    # one poll through the stop then a recovery: the count resets, no cut
    flip.evaluate(TICKER, _ctx(_book(yes=48, no=40), secs_left=770))   # poll 1
    p = flip.evaluate(TICKER, _ctx(_book(yes=55, no=40), secs_left=769))  # recover
    assert [x for x in p if x.purpose in ("CUT", "EXIT")] == []
    assert not o.get("done")
    # two SUSTAINED polls through the stop, no spotlead at all → cut
    flip.evaluate(TICKER, _ctx(_book(yes=48, no=40), secs_left=768))   # poll 1
    props = flip.evaluate(TICKER, _ctx(_book(yes=48, no=40), secs_left=767))
    exits = [x for x in props if x.purpose in ("EXIT", "CUT")]
    assert len(exits) == 1 and "momentum stop" in exits[0].reason


def test_the_momentum_stop_is_the_price_backstop(flip, gateway, ledger):
    """WO-2026-07-22-E: the fixed 20c CATASTROPHE floor is retired for live
    positions (the dead-floor now only guards a curfew-HELD winner). The
    momentum stop IS the price backstop — a book crashed to 20c is far through
    the stop (50) and crosses out at the mark. WO-2026-07-24-D Part 2: below the
    floor (47) it rests one poll at the floor first, then crosses on the 3rd."""
    o = _open_pos(flip, gateway, ledger)                    # entry 60, stop 50
    floor = 60 - config.OPEN_MOMENTUM_STOP_C - config.SLIP_TOLERANCE_C   # 47
    flip.evaluate(TICKER, _ctx(_book(yes=20, no=40), secs_left=771))  # poll 1
    p2 = flip.evaluate(TICKER, _ctx(_book(yes=20, no=40), secs_left=770))  # rest
    assert [x for x in p2 if x.purpose == "CUT"] == [] and any(
        x.price_cents == floor for x in p2 if x.purpose == "EXIT")
    cuts = [x for x in flip.evaluate(TICKER, _ctx(_book(yes=20, no=40),
                                                  secs_left=769))
            if x.purpose in ("CUT", "EXIT")]
    assert len(cuts) == 1 and "momentum stop" in cuts[0].reason
    assert cuts[0].price_cents == 20 and cuts[0].crossfire


# ── §3: the ruling's data line (no F behavior change) ──────────────────────
def test_pack_carries_the_f_tail_line(ledger, surface):
    ledger.record_settlement("M1", "F", 4, "won")
    ledger.record_settlement("M2", "F", 3, "won")
    ledger.record_settlement("M3", "F", -93, "the tail")
    from relay_engine.ledger import CashProtocol
    from relay_engine.ops import daily_pack
    pack = daily_pack(ledger, surface, CashProtocol(ledger,
                                                    alert_fn=lambda m: None))
    assert "F TAIL (§3 ruling data): markets 3 · wins 2 avg +3.5c · " \
           "tails 1 avg -93.0c · decided-against rate 33.3%" in pack


def test_f_passthrough_unchanged_until_drew_rules():
    """RESERVED: no F code ships on §3 — passthrough stands as ruled."""
    from relay_engine.custodian import salvage_params
    assert salvage_params().salvage_enabled is True   # F's cut-disabled-
    # except-catastrophic-AND-salvage passthrough params, unchanged
    from relay_engine.lane_flip import flip_cut_params
    assert flip_cut_params().salvage_enabled is False