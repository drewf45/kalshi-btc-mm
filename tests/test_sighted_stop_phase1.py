"""WO-2026-07-24-E "THE SIGHTED STOP" — Phase 1 (weekday: instrumentation only,
ZERO behavior change). The momentum stop is a pure LEVEL test (mark <= stop_px)
with no trajectory term — on 26JUL0845 it sold at 48 INTO a book that had
recovered ~28pts off its low. Phase 1 computes the sighted verdict as a SHADOW
and stamps it onto every FLIP_SWING record; the live stop is byte-identical.
Phase 2 (Saturday, DREW's go) flips the deferral live.

HARD RAIL: behavior byte-identical (the 137-test momentum-stop corpus still
passes untouched); F byte-identical (this is FLIP custody instrumentation)."""

import json

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0


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


def _book(yes, no=40):
    """held-side (yes) bid at `yes`; yes=None → NO yes bid (held_price None)."""
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10} if yes is not None else {}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs=700):
    return {"book": book, "now": CLOSE - secs, "close_ts": CLOSE,
            "spot": None, "grain": None, "spotlead": None}


def _pos(flip, entry=60):
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    now = CLOSE - 700
    w.opens["yes"] = {"entry": entry, "fill_ts": now - 200, "count": 1,
                      "take_oid": None, "take_proposed": True,
                      "collapse_polls": 0, "catastrophe_polls": 0,
                      "det_ts": None, "entry_oid": None, "defer_polls": 0}
    return w.opens["yes"], now


def _poll(flip, o_ignored, mark, now):
    b = _book(yes=mark)
    return flip._open_custody(flip._window(TICKER, CLOSE), TICKER, EVENT, b,
                              _ctx(b), 700, now)


def _swing_row(ledger):
    (d,) = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='FLIP_SWING'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(d)


# ── the flight recorder: low_mark / mark_prev tracked every poll ────────────
def test_low_mark_and_mark_prev_track_the_path(flip):
    """low_mark = min(mark) since entry; mark_prev = the prior poll's mark. The
    recorder the level-only stop stood next to and never read."""
    o, now = _pos(flip, entry=60)
    for mark in (57, 40, 20, 46):          # fall then climb
        _poll(flip, o, mark, now)
    assert o["low_mark"] == 20              # the trough, remembered
    assert o["mark_prev"] == 46             # last poll's mark


def test_none_mark_poll_never_fabricates(flip):
    """Acceptance #4 + the BLIND/MUTE law: a None-mark poll (book-fetch failure)
    updates NEITHER low_mark NOR mark_prev (carry forward) and never counts as
    recovery — and the live stop counter still resets exactly as today."""
    o, now = _pos(flip, entry=60)
    _poll(flip, o, 45, now)                 # a real poll: low 45, prev 45
    assert o["low_mark"] == 45 and o["mark_prev"] == 45 and o["stop_polls"] == 1
    _poll(flip, o, None, now)               # book-fetch failure: no yes bid
    # carried forward, not fabricated; and the live counter reset (unchanged)
    assert o["low_mark"] == 45 and o["mark_prev"] == 45
    assert o["stop_polls"] == 0             # None-mark resets the level counter


# ── the 0845 counterfactual: a cut INTO a recovery would DEFER ──────────────
def test_stop_into_a_recovery_shadows_would_defer(flip, ledger):
    """The finding, reproduced: entry 60, book collapses to 20 then climbs to 48
    (still <= stop 50) — the level-only stop FIRES on the climb. The shadow
    records would_defer=true, off_low≈+28 (the 26JUL0845 signature), and the
    FLIP_SWING row carries the full counterfactual. Behavior byte-identical:
    the cut still fires this poll."""
    o, now = _pos(flip, entry=60)           # stop 50, floor 47, hard floor 39
    _poll(flip, o, 20, now)                 # poll 1: the trough (stop_polls=1)
    props = _poll(flip, o, 48, now)         # poll 2: climbing, still <= stop → FIRES
    assert o["stop_polls"] == 2             # the live stop fired (unchanged)
    assert any(p.purpose == "CUT" for p in props)   # it still cut this poll
    assert o["shadow_would_defer"] is True
    assert o["shadow_off_low"] == 28 and o["low_mark"] == 20
    assert o["grace_polls_shadow"] == 1
    # and the counterfactual lands on the FLIP_SWING forensic record
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 48, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 48, now, count=1)
    r = _swing_row(ledger)
    assert r["would_defer"] is True and r["off_low_c"] == 28
    assert r["low_mark"] == 20 and r["mark_at_cut"] == 48
    assert r["grace_polls_shadow"] == 1


def test_genuine_falling_cut_shadows_no_defer(flip, ledger):
    """A book falling monotonically to the stop is the thesis being WRONG — the
    shadow says would_defer=false (not recovering), so Phase 2 would still cut.
    The FLIP_SWING row records the honest negative."""
    o, now = _pos(flip, entry=60)           # stop 50
    _poll(flip, o, 49, now)                 # poll 1: just below the stop, falling
    _poll(flip, o, 48, now)                 # poll 2: still falling → FIRES
    assert o["stop_polls"] == 2 and o["shadow_would_defer"] is False
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 48, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 48, now, count=1)
    assert _swing_row(ledger)["would_defer"] is False


# ── the guardrails, as SHADOW verdicts (G1 hard, G3 sawtooth) ───────────────
def test_g1_hard_floor_a_deep_bounce_would_still_cut(flip):
    """G1: a 'recovery' from 12→18 is a dead position twitching, not a repair.
    Below the hard floor (stop − SLIP − OPEN_GRACE_HARD_C = 39) the shadow says
    would_defer=FALSE even though it is technically off the low — bounded worst
    case stays bounded."""
    o, now = _pos(flip, entry=60)           # hard floor 50−3−8 = 39
    _poll(flip, o, 12, now)                 # poll 1: deep collapse (low 12)
    _poll(flip, o, 18, now)                 # poll 2: a twitch, off the low +6 but < 39
    assert o["stop_polls"] == 2
    assert o["shadow_would_defer"] is False   # G1 hard overrides the recovery flag


def test_g3_sawtooth_bleeder_cannot_fake_recovery(flip):
    """G3: recovery is measured off low_mark, never the prior poll alone. A
    decaying sawtooth (48→44→45→41→42…) keeps making NEW lows, so mark never
    clears low + OPEN_RECOVERY_MIN_C — the shadow never defers it."""
    o, now = _pos(flip, entry=60)
    deferred_any = False
    for mark in (48, 44, 45, 41, 42, 38, 39):   # each bounce below the last low+6
        _poll(flip, o, mark, now)
        deferred_any = deferred_any or bool(o.get("shadow_would_defer"))
    assert o.get("grace_polls_shadow", 0) == 0   # never once counted as recovering
    assert o.get("shadow_would_defer") in (False, None)


# ── HARD RAIL: F byte-identical (this is FLIP custody instrumentation) ──────
def test_f_sizing_untouched():
    from relay_engine import scoring
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason
