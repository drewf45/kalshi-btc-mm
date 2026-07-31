"""WO-FLIP-COUNT-1 — THE ORPHANED SECOND CONTRACT. Today's tape: FLIP/OPEN
bought no@48¢ ×2, the take sold ×1 (+5¢), and the surviving contract rode
to $0 (−$0.43) because the SECOND same-side fill fell through the popped
mode marker into the legacy rung-A dict whose only exit hardcoded count=1.

The law under test: (§2.1) same-side fills MERGE into the existing custody
bucket at a count-weighted blended entry; (§2.2) every exit sells the
BOOKED ledger size, never a literal 1, clamped >= 0 (Adversary); (§2.3)
a held leg the resting exits don't cover pages FLIP_UNCOVERED_LEG once
per (market, close_ts, side) with bucket provenance (Scientist)."""

import pytest

from relay_engine import config, failures, lane_flip
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def flip_book(yes=60, no=40, yq=10, nq=10):
    # WO-2026-07-22-E: FLIP buys the FAVORED (higher-priced) side in [50,70];
    # the default book is favored-yes@60 so the entry path enters yes.
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: yq}, {no: nq}, ts=1.0)
    return b


def ctx(book, secs_left=850, spot=None, grain=None, spotlead=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": spotlead}


def _prime(flip, entry=60, grain=None):
    """WO-2026-07-22-F "wait for the pile": an OPEN entry now needs two in-window
    polls that build the pile — a baseline poll (secs_into~65, small skew, spot
    low) then the entry poll (secs_into~80, skew grown >=5, |trend|>=15 agreeing
    with the favored side, favored depth >= other). Returns the entry proposals."""
    flip.evaluate(TICKER, ctx(flip_book(yes=54, no=48), secs_left=835,
                              spot=66000.0))                     # baseline skew 6
    return flip.evaluate(TICKER, ctx(flip_book(yes=entry, no=40, yq=14, nq=10),
                                     secs_left=820, spot=66020.0, grain=grain))


@pytest.fixture(autouse=True)
def funnel(ledger):
    alerts = []
    failures._warn_last.clear()   # per-tag WARN throttle is cross-test state
    failures.configure(ledger, alert_fn=alerts.append, run_mode="TEST",
                       boot_id=1)
    yield alerts
    failures._ledger = None


@pytest.fixture
def flip(gateway, ledger, surface):
    custodian = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    return LaneFlip(gateway, custodian=custodian)


def _uncovered_rows(ledger):
    return ledger.db.execute(
        "SELECT what, how_json FROM failures"
        " WHERE why_tag='FLIP_UNCOVERED_LEG'").fetchall()


# ── §2.1: the merge — a second same-side fill never orphans ────────────────
def test_second_open_fill_merges_and_take_sells_two(flip, gateway, ledger):
    """The tape's shape, fixed: fill ×1, take rests ×1, a SECOND same-side
    fill books → ONE custody bucket count=2 @ blended entry, the stale ×1
    take is cancelled, and the re-proposed take sells 2. Never w.fills."""
    props = _prime(flip, grain=GRAIN_YES2)
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 40, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 40, CLOSE - 790)
    props2 = flip.evaluate(TICKER, ctx(flip_book(), secs_left=780))
    take1 = next(p for p in props2 if p.purpose == "EXIT")
    assert take1.count == 1                      # one-lot path unchanged
    flip.on_submitted(take1, "OID-T1", CLOSE - 780)
    # the orphan-maker: a SECOND same-side fill (mode marker long popped)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 50, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 50, CLOSE - 770)
    w = flip.windows[TICKER]
    assert "yes" not in w.fills                  # never the legacy rung-A dict
    o = w.opens["yes"]
    assert o["count"] == 2
    assert o["entry"] == 45                      # (40+50)/2 count-weighted
    assert o["take_oid"] is None and o["take_proposed"] is False
    props3 = flip.evaluate(TICKER, ctx(flip_book(), secs_left=760))
    take2 = next(p for p in props3 if p.purpose == "EXIT")
    assert take2.count == 2                      # the whole bucket flips
    # WO-2026-07-22-F: the re-proposed take is entry + OPEN_GOUGE_C (cap 90)
    # on the blended merged entry (45 -> 62), size-independent
    assert take2.price_cents == LaneFlip._take_price(45)   # entry+17 on the merge
    assert _uncovered_rows(ledger) == []         # covered every cycle


def test_second_hunt_fill_merges(flip):
    """The merge covers BOTH custody dicts — a hunt side blends the same."""
    w = flip._window(TICKER, CLOSE)
    w.hunts["no"] = {"entry": 30, "fill_ts": 0.0, "count": 1,
                     "take_oid": None, "take_proposed": True,
                     "be_ts": None, "be_repriced": False}
    flip.note_fill(TICKER, "no", 34, 1.0)
    h = w.hunts["no"]
    assert h["count"] == 2 and h["entry"] == 32
    assert h["take_proposed"] is False           # re-propose at merged size


def test_lean_two_lot_single_fill_exits_full_size(flip, gateway, ledger):
    """A single ×2 fill (the earned-tier shape) opens custody at count=2
    and the take sells 2 — count rides the fills wiring end to end."""
    props = _prime(flip, grain=GRAIN_YES2)
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 40, 2, "PROBE")
    flip.note_fill(TICKER, "yes", 40, CLOSE - 790, count=2)
    assert flip.windows[TICKER].opens["yes"]["count"] == 2
    props2 = flip.evaluate(TICKER, ctx(flip_book(), secs_left=780))
    take = next(p for p in props2 if p.purpose == "EXIT")
    assert take.count == 2


# ── §2.2: exits sell the booked size, clamped to ledger truth ──────────────
def test_rung_a_take_sells_booked_size_never_one(flip, gateway, ledger):
    """Today's tape replayed on the legacy path: no@48 ×2 booked in
    w.fills — the take must sell 2 (was: literal count=1; the survivor
    rode to $0)."""
    w = flip._window(TICKER, CLOSE)
    w.fills["no"] = 48
    w.first_fill_ts = CLOSE - 790
    w.trips = 1
    gateway.positions[(EVENT, TICKER, "FLIP")] = -2
    ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 48, 2, "PROBE")
    props = flip.evaluate(TICKER, ctx(flip_book(), secs_left=700))
    takes = [p for p in props if p.purpose == "EXIT" and p.side == "no"]
    assert len(takes) == 1
    assert takes[0].count == 2                   # booked truth, not 1
    assert takes[0].price_cents == 48 + lane_flip.FLIP_X
    assert _uncovered_rows(ledger) == []         # proposed = covered


def test_exit_clamped_to_booked_held(flip, gateway, ledger):
    """Adversary: memory says 2 but the ledger booked one exit already —
    the take offers min(memory, booked)=1, never oversells."""
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {"entry": 48, "fill_ts": CLOSE - 790, "count": 2,
                      "take_oid": None, "take_proposed": False,
                      "collapse_polls": 0, "det_ts": None}
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 40, 2, "PROBE")
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 50, 1, "PROBE")
    props = flip.evaluate(TICKER, ctx(flip_book(), secs_left=700))
    take = next(p for p in props if p.purpose == "EXIT")
    assert take.count == 1


def test_exit_count_zero_marks_done_no_order(flip, gateway, ledger):
    """Adversary: booked flat (both contracts already sold) → no sell
    order at all; the bucket marks done instead of offering air."""
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {"entry": 48, "fill_ts": CLOSE - 790, "count": 2,
                      "take_oid": None, "take_proposed": False,
                      "collapse_polls": 0, "det_ts": None}
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 40, 2, "PROBE")
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 50, 2, "PROBE")
    props = flip.evaluate(TICKER, ctx(flip_book(), secs_left=700))
    assert [p for p in props if p.purpose == "EXIT"] == []
    assert w.opens["yes"]["done"] is True


# ── note_exit: ×count realization, decrement until depleted ────────────────
def test_note_exit_realizes_merged_count_fully(flip):
    """Engineer: a merged 2-lot bucket exiting ×2 realizes the FULL round
    trip — (53−49)×2 = +8 — and the bucket pops depleted."""
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {"entry": 49, "fill_ts": 0.0, "count": 2,
                      "take_oid": None, "take_proposed": True,
                      "collapse_polls": 0, "det_ts": None}
    flip.note_exit(TICKER, "yes", 53, CLOSE - 700, count=2)
    assert w.window_realized == 8
    assert "yes" not in w.opens
    assert w.open_consumed is True               # P26 §3.1 stands


def test_note_exit_partial_decrements_and_survives(flip):
    """A partial exit fill decrements the bucket and the remainder keeps
    its accounting — no orphan, no double-realize."""
    w = flip._window(TICKER, CLOSE)
    w.hunts["no"] = {"entry": 40, "fill_ts": 0.0, "count": 2,
                     "take_oid": None, "take_proposed": True,
                     "be_ts": None, "be_repriced": False}
    flip.note_exit(TICKER, "no", 45, 1.0, count=1)
    assert w.window_realized == 5
    assert w.hunts["no"]["count"] == 1           # the remainder survives
    flip.note_exit(TICKER, "no", 45, 2.0, count=1)
    assert w.window_realized == 10
    assert "no" not in w.hunts                   # depleted → popped


# ── §2.3: the FAIL-LOUD invariant ──────────────────────────────────────────
def test_uncovered_leg_pages_once_with_provenance(flip, gateway, ledger,
                                                  funnel):
    """Held 2 booked, resting exit covers 1, nothing proposed this cycle →
    FLIP_UNCOVERED_LEG pages ONCE per (market, close_ts, side), row
    carrying held/covered and bucket provenance."""
    w = flip._window(TICKER, CLOSE)
    w.fills["no"] = 48
    w.first_fill_ts = CLOSE - 790
    w.trips = 1
    w.takes_posted["no"] = "OID-T1"              # the stale ×1 take rests
    w.take_counts["no"] = 1
    gateway.positions[(EVENT, TICKER, "FLIP")] = -2
    ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 48, 2, "PROBE")
    flip.evaluate(TICKER, ctx(flip_book(), secs_left=700))
    rows = _uncovered_rows(ledger)
    assert len(rows) == 1
    assert "held 2 > covered 1" in rows[0][0]
    assert "fills" in rows[0][1]                 # bucket provenance
    assert any("FLIP_UNCOVERED_LEG" in a for a in funnel)
    # once per (market, close_ts, side): the second cycle stays quiet
    flip.evaluate(TICKER, ctx(flip_book(), secs_left=699))
    assert len(_uncovered_rows(ledger)) == 1
