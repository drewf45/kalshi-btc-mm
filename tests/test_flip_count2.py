"""WO-FLIP-COUNT-2 — THE LAST LOSS-TERM LEAK. The 191030 tape: OPEN intent
yes×4 → venue filled yes@48¢ as two 1-lot records straddling a
determined-against exit → two single-lot exits and a FLIP_UNCOVERED_LEG.
Not a missing merge — a fill-vs-exit RACE.

The law under test: (§3.1) exits fire only on FULLY-BOOKED positions — a
partial in flight defers, bounded, then the remainder cancels loudly
(Adversary b); (§3.2) a fill landing on a CLOSING bucket never
re-increments it — it buffers and re-opens position-aware when the old leg
concludes (FLIP_LATE_FILL_REOPEN); (§3.3) FLIP_UNCOVERED_LEG pages AND
covers the leg — healed once, then a still-uncovered leg is FATAL
(Adversary c); (§3.4) every exit clamps to booked-net (phantom-sell
guard). HARD RAIL: no constant, band, gate, or take-cent moves here."""

import json

import pytest

from relay_engine import config, failures, lane_flip
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.errors import FatalIntegrityError
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import FLIP_STUCK_PARTIAL_POLLS, LaneFlip

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=60, no=40):
    # WO-2026-07-22-E: FLIP buys the FAVORED (higher-priced) side in [50,70];
    # the default book is favored-yes@60 so the entry path enters yes.
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=850, grain=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": None, "grain": grain, "spotlead": None}


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


def _rows(ledger, tag):
    return ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag=?", (tag,)).fetchone()[0]


# ── §4.1 THE 191030 REPLAY — the exact failing tape must pass ──────────────
def test_191030_replay_partial_fills_straddle_determined_exit(flip, gateway,
                                                              ledger):
    """2-lot OPEN entry; the venue fills it as two 1-lot records with the
    adverse (momentum-stop) condition arriving between them. Pre-fix: two
    single-lot exits + UNCOVERED page. Now: the exit DEFERS while the
    entry is partially filled (§3.1), the second half merges, and the
    position leaves as ONE covered 2-lot action. ZERO pages."""
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2))
    entry = props[0]
    entry.count = 2                                    # the 2-lot intent
    r = gateway.submit(entry, _book())                 # REAL resting order
    flip.on_submitted(entry, r.order_id, CLOSE - 800)
    # venue fill record 1 of 2: order stays resting, partially filled
    gateway.on_fill(r.order_id, count=1)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 48, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 48, CLOSE - 790)
    assert r.order_id in gateway.resting               # the partial, live
    # the book moves ADVERSELY to momentum-stop territory (mark 20 <= entry−10)
    # BETWEEN the two halves; §3.1 still DEFERS every exit while the entry is
    # partially filled (the exit acts only on a fully-booked position).
    p_mid = flip.evaluate(TICKER, _ctx(_book(yes=20), secs_left=780))
    assert [p for p in p_mid if p.purpose in ("EXIT", "CUT")] == []  # §3.1 defers
    # venue fill record 2 of 2: entry fully booked, merge lands
    gateway.on_fill(r.order_id, count=1)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 48, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 48, CLOSE - 779)
    assert r.order_id not in gateway.resting
    w = flip.windows[TICKER]
    assert w.opens["yes"]["count"] == 2 and "yes" not in w.fills
    # WO-2026-07-22-E: the MOMENTUM STOP (entry−10, 2 sustained polls,
    # maker-first) is this race-test's exit trigger — armed from the first
    # poll, no opening-window gate.
    # the take posts at FULL size (custody order: take first), then the
    # momentum-stop evacuation fires — also at full size
    p_take = flip.evaluate(TICKER, _ctx(_book(yes=20), secs_left=778))
    take = next(p for p in p_take if p.purpose == "EXIT")
    assert take.count == 2                             # never a 1-lot split
    flip.on_submitted(take, "OID-T", CLOSE - 778)
    flip.evaluate(TICKER, _ctx(_book(yes=20), secs_left=777))   # poll 1: sustain
    p_exit = flip.evaluate(TICKER, _ctx(_book(yes=20), secs_left=776))
    cuts = [p for p in p_exit if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].count == 2       # ONE covered action
    assert "momentum stop" in cuts[0].reason
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 34, 2, "PROBE")
    flip.note_exit(TICKER, "yes", 34, CLOSE - 776, count=2)
    assert w.window_realized == -28                    # bound loss, together
    flip.evaluate(TICKER, _ctx(_book(yes=34), secs_left=775))
    assert _rows(ledger, "FLIP_UNCOVERED_LEG") == 0    # ZERO pages, ever


# ── §3.2 the late fill: buffered, never re-incremented, reopened ───────────
def test_late_fill_on_closing_bucket_buffers_then_reopens(flip, gateway,
                                                          ledger, caplog):
    """Fill 2 arrives while the bucket is done (exit fired, CUT not yet
    booked): the closed record is NOT re-incremented; the leg buffers and
    FLIP_LATE_FILL_REOPEN opens a fresh position-aware record the moment
    the old leg's accounting concludes. Zero orphan, zero UNCOVERED."""
    import logging
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2))
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)  # fake oid: gate off
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 48, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 48, CLOSE - 790)
    # WO-2026-07-22-E: the MOMENTUM STOP (entry−10, 2 sustained polls) is this
    # race-test's exit trigger; the mark 20 <= entry−10 sustains it.
    flip.windows[TICKER].opens["yes"]["fill_ts"] = CLOSE - 1100
    # take posts on the 1-lot leg, then the momentum-stop cut fires -> done=
    # True, CUT x1 in flight (the pre-merge exit — the race's first half).
    p_take = flip.evaluate(TICKER, _ctx(_book(yes=20), secs_left=781))
    flip.on_submitted(next(p for p in p_take if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 781)
    flip.evaluate(TICKER, _ctx(_book(yes=20), secs_left=780))    # poll 1: sustain
    p_cut = flip.evaluate(TICKER, _ctx(_book(yes=20), secs_left=779))
    cut = next(p for p in p_cut if p.purpose == "CUT")
    assert cut.count == 1
    w = flip.windows[TICKER]
    assert w.opens["yes"]["done"] is True
    # the SECOND half lands while the bucket is closing (the race)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 48, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 48, CLOSE - 779)
    assert w.opens["yes"]["count"] == 1                # NOT re-incremented
    assert w.late_fills["yes"] == {"entry": 48, "count": 1,
                                   "bucket": "opens"}
    # the old leg's CUT books -> conclude -> REOPEN fires
    with caplog.at_level(logging.WARNING, logger="relay.lane_flip"):
        ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 20, 1, "PROBE")
        flip.note_exit(TICKER, "yes", 20, CLOSE - 778, count=1)
    assert any("FLIP_LATE_FILL_REOPEN" in r.message for r in caplog.records)
    assert w.opens["yes"]["count"] == 1                # fresh, position-aware
    assert not w.opens["yes"].get("done")
    assert "yes" not in w.late_fills
    # the reopened leg gets a normal exit next cycle; nothing orphans
    p2 = flip.evaluate(TICKER, _ctx(_book(yes=34), secs_left=777))
    assert any(p.purpose in ("EXIT", "CUT") and p.count == 1 for p in p2)
    assert _rows(ledger, "FLIP_UNCOVERED_LEG") == 0


def test_late_fill_promote_clamps_to_booked(flip, gateway, ledger):
    """Phantom-sell guard on the reopen path (Adversary a): if the venue
    says nothing is held, the buffered leg opens NO record."""
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {"entry": 48, "fill_ts": 0.0, "count": 1,
                      "take_oid": None, "take_proposed": True,
                      "collapse_polls": 0, "det_ts": None,
                      "entry_oid": None, "defer_polls": 0, "done": True}
    w.late_fills["yes"] = {"entry": 48, "count": 1, "bucket": "opens"}
    # booked: one ENTRY, one full EXIT -> held 0
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 48, 1, "PROBE")
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 41, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 41, CLOSE - 700, count=1)
    assert "yes" not in w.opens and "yes" not in w.late_fills  # no phantom


# ── regressions: the simple cases stay simple ──────────────────────────────
def test_single_two_lot_fill_one_record_one_exit(flip, gateway, ledger):
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2))
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 48, 2, "PROBE")
    flip.note_fill(TICKER, "yes", 48, CLOSE - 790, count=2)
    w = flip.windows[TICKER]
    assert w.opens["yes"]["count"] == 2
    takes = [p for p in flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
             if p.purpose == "EXIT"]
    assert len(takes) == 1 and takes[0].count == 2


def test_two_fills_no_exit_between_merges_clean(flip, gateway, ledger):
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2))
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    for _ in range(2):
        ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 48, 1, "PROBE")
        flip.note_fill(TICKER, "yes", 48, CLOSE - 790)
    w = flip.windows[TICKER]
    assert w.opens["yes"]["count"] == 2 and "yes" not in w.fills
    takes = [p for p in flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
             if p.purpose == "EXIT"]
    assert len(takes) == 1 and takes[0].count == 2
    assert _rows(ledger, "FLIP_UNCOVERED_LEG") == 0


# ── §3.3 UNCOVERED self-heals ──────────────────────────────────────────────
def test_uncovered_pages_and_heals_covered_next_cycle(flip, gateway, ledger,
                                                      funnel):
    """WO-UNCOVERED-FLATTEN AMENDED FLIP-COUNT-2 §3.3: the first detection
    RECONCILES against broker truth — the stale ×1 take (the self-net
    artifact) is cancelled, so the heal covers the WHOLE booked leg (2),
    not just the gap it measured against an order that never confirmed."""
    w = flip._window(TICKER, CLOSE)
    w.fills["no"] = 48
    w.first_fill_ts = CLOSE - 790
    w.trips = 1
    w.takes_posted["no"] = "OID-T1"                    # stale ×1 take rests
    w.take_counts["no"] = 1
    gateway.positions[(EVENT, TICKER, "FLIP")] = -2
    ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 48, 2, "PROBE")
    flip.evaluate(TICKER, _ctx(_book(), secs_left=700))
    assert _rows(ledger, "FLIP_UNCOVERED_LEG") == 1    # the page stays loud
    assert w.opens["no"]["count"] == 2                 # WHOLE leg, reconciled
    assert w.opens["no"]["entry"] == 48                # ...at booked entry
    assert "no" not in w.takes_posted                  # stale take cancelled
    p2 = flip.evaluate(TICKER, _ctx(_book(), secs_left=699))
    heals = [p for p in p2 if p.purpose == "EXIT" and p.side == "no"]
    assert len(heals) == 1 and heals[0].count == 2     # covers the whole leg
    assert _rows(ledger, "FLIP_UNCOVERED_LEG") == 1    # once, not a loop


def test_uncovered_flattens_before_it_fatals(flip, gateway, ledger, funnel):
    """WO-UNCOVERED-FLATTEN §2.4 OVERTURNED the old heal-once-then-FATAL:
    a leg that can neither cover nor confirm escalates heal → reconcile+
    retry → FLATTEN at market (never bare, never FATAL with an open naked
    leg), and only a flatten that itself fails to register is the FATAL."""
    w = flip._window(TICKER, CLOSE)
    w.fills["no"] = 48
    w.first_fill_ts = CLOSE - 790
    w.trips = 1
    gateway.positions[(EVENT, TICKER, "FLIP")] = -2
    ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 48, 2, "PROBE")

    def _sabotage(w):
        # every cycle: the heal's cover never confirms (drop the revived
        # record before it can register a resting take) — the escalation
        # must still reach FLATTEN, never ride bare
        w.opens.pop("no", None)
        w.hunts.pop("no", None)

    # the escalation crosses grace → reconcile → retry → FLATTEN, one
    # stage per cycle; drive it until the flatten order appears
    flats = []
    for i, s in enumerate((700, 699, 698, 697, 696)):
        p = flip.evaluate(TICKER, _ctx(_book(no=45), secs_left=s))
        flats = [x for x in p if x.purpose == "CUT"
                 and "FLIP_UNCOVERED_FLATTENED" in (x.reason or "")]
        if flats:
            break
        _sabotage(w)
    assert len(flats) == 1 and flats[0].crossfire       # market close NOW
    assert flats[0].count == 2 and flats[0].side == "no"
    assert _rows(ledger, "FLIP_UNCOVERED_FLATTENED") == 1
    assert _rows(ledger, "FLIP_UNCOVERED_LEG") == 1     # paged once, not a loop
    # the flatten never registered a close (sabotaged) → NOW it may FATAL,
    # with the close already attempted (§2.4: never FATAL on an untried leg)
    _sabotage(w)
    with pytest.raises(FatalIntegrityError,
                       match="FLIP_UNCOVERED_UNHEALABLE"):
        flip.evaluate(TICKER, _ctx(_book(no=45), secs_left=695))


# ── §3.1 Adversary (b): the stuck partial fails toward a known state ───────
def test_stuck_partial_defers_then_cancels_loudly(flip, gateway, ledger,
                                                  funnel):
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2))
    entry = props[0]
    entry.count = 2
    r = gateway.submit(entry, _book())
    flip.on_submitted(entry, r.order_id, CLOSE - 800)
    gateway.on_fill(r.order_id, count=1)               # half fills, forever
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 48, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 48, CLOSE - 790)
    # determined condition present the whole time
    for i in range(FLIP_STUCK_PARTIAL_POLLS - 1):
        p = flip.evaluate(TICKER, _ctx(_book(yes=41), secs_left=780 - i))
        assert [x for x in p if x.purpose in ("EXIT", "CUT")] == []  # defer
    p = flip.evaluate(TICKER, _ctx(_book(yes=41), secs_left=770))
    assert r.order_id not in gateway.resting           # remainder CANCELLED
    exits = [x for x in p if x.purpose in ("EXIT", "CUT")]
    assert len(exits) == 1 and exits[0].count == 1     # booked size exits
    assert _rows(ledger, "FLIP_STUCK_PARTIAL") == 1
    assert any("FLIP_STUCK_PARTIAL" in a for a in funnel)


# ── §3.4 the phantom-sell guard stands everywhere ──────────────────────────
def test_exit_never_exceeds_booked_held(flip, gateway, ledger):
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {"entry": 48, "fill_ts": CLOSE - 790, "count": 3,
                      "take_oid": None, "take_proposed": False,
                      "collapse_polls": 0, "det_ts": None,
                      "entry_oid": None, "defer_polls": 0}
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 48, 2, "PROBE")
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 50, 1, "PROBE")
    takes = [p for p in flip.evaluate(TICKER, _ctx(_book(), secs_left=700))
             if p.purpose == "EXIT"]
    assert len(takes) == 1 and takes[0].count == 1     # min(memory 3, booked 1)
