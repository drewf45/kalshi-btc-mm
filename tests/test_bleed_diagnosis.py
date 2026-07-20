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


def _book(yes=48, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _needle(side="no", fair=90.0):
    return Needle(side=side, d_before=200.0, d_after=20.0,
                  delta_p=config.HUNT_NEEDLE_POINTS + 5.0,
                  fair_cents=fair, t_remaining=700.0)


def _ctx(book, secs_left=800, sl=None, grain=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": None, "grain": grain, "spotlead": sl}


@pytest.fixture(autouse=True)
def funnel(ledger):
    alerts = []
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=alerts.append, run_mode="TEST",
                       boot_id=1)
    yield alerts
    failures._ledger = None


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
    open_props = flip.evaluate(TICKER, _ctx(_book(yes=48, no=49),
                                            secs_left=650,
                                            grain=GRAIN_YES2))
    assert [(p.side, p.purpose) for p in open_props] == [("yes", "ENTRY")]


# ── BLEED 3: the floor stops slipping ──────────────────────────────────────
def _open_pos(flip, gateway, ledger, entry=48):
    props = flip.evaluate(TICKER, _ctx(_book(yes=entry), secs_left=800,
                                       grain=GRAIN_YES2))
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", entry, 1, "PROBE")
    flip.note_fill(TICKER, "yes", entry, CLOSE - 790)
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=entry), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    return flip.windows[TICKER].opens["yes"]


def test_sustained_through_floor_cuts_in_minute_one(flip, gateway, ledger):
    """Instrument 2's −25c finding, fixed: a book SUSTAINED through the
    band floor (2 polls) cuts NOW — inside the patience window — at ~the
    floor, instead of riding the collapse out and cutting at the bottom."""
    _open_pos(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=34), secs_left=770))
    assert [p for p in p1 if p.purpose == "CUT"] == []      # poll 1: flicker
    p2 = flip.evaluate(TICKER, _ctx(_book(yes=34), secs_left=769))
    cuts = [p for p in p2 if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].price_cents == 34     # AT the floor
    assert "sustained — WO-BLEED-3" in cuts[0].reason
    # the loss lands inside the floor expectation: Instrument 2 says ok
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 34, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 34, CLOSE - 768, count=1)
    audit = [json.loads(d) for (d,) in ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='FLIP_LOSER_CUT'"
    ).fetchall()][0]
    assert audit["ok"] is True and audit["loss_cents"] == 14


def test_one_frame_flicker_still_holds(flip, gateway, ledger):
    """The anti-churn law stands: ONE poll through the floor then back
    in-band is noise — no cut, the floor-poll counter resets."""
    o = _open_pos(flip, gateway, ledger)
    flip.evaluate(TICKER, _ctx(_book(yes=34), secs_left=770))   # flicker
    p2 = flip.evaluate(TICKER, _ctx(_book(yes=44), secs_left=769))
    assert [p for p in p2 if p.purpose == "CUT"] == []
    assert o["floor_polls"] == 0                                # reset
    p3 = flip.evaluate(TICKER, _ctx(_book(yes=34), secs_left=768))
    assert [p for p in p3 if p.purpose == "CUT"] == []          # count anew


def test_dp_collapse_leg_still_patience_gated(flip, gateway, ledger):
    """The ΔP leg (a spot signal that CAN flicker early) keeps the
    patience gate — only the BOOK through the floor is exempt."""
    o = _open_pos(flip, gateway, ledger)
    sl = Needle(side="no", d_before=10.0, d_after=80.0,
                delta_p=config.OPEN_DETERMINED_K_POINTS + 3.0,
                fair_cents=0.0, t_remaining=700.0)
    for secs in (770, 769, 768):
        props = flip.evaluate(TICKER, _ctx(_book(yes=44), secs_left=secs,
                                           sl=sl))
        assert [p for p in props if p.purpose == "CUT"] == []
    assert o["collapse_polls"] >= 2         # counted, gated — not fired


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