"""WO-FULL-COLD-AUDIT (build 52) — the FLIP exit state machine, fixed as a
cluster. The catastrophic loss was two exits fighting (the violent SPOT_DECIDED
market-dump pre-empting the gentle walk-down) at 15pt of drift, plus an
un-winnable trending market never skipped and an entry band admitting coinflips.

  Finding 1 (ROOT): K raised 15→40 (a real decision, not drift); the
    SPOT_DECIDED exit routes through the WALK-DOWN — a MAKER order at scratch,
    never a crossfire market-dump. The CATASTROPHE deep backstop (genuinely
    gone) keeps its crossfire.
  Finding 3 (GATE): a hard-trending open skips OPEN's reversion entry.
  Finding 4 (ENTRY): OPEN_MAX_ENTRY_CENTS tightened 50→42 (real-gouge only).
  Finding 5 (MEASURE): the swing-gate telemetry line, every window.
  Finding 6: F unchanged (byte-identical — no test here touches F sizing/salvage).
"""

import logging
import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip, FLIP_WINDOW_SEC

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
GRAIN = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=40, no=49, yq=10, nq=10):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: yq} if yes is not None else {},
                     {no: nq} if no is not None else {}, ts=1.0)
    return b


def _ctx(book, secs_left=850, spot=None, grain=None, sl=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": sl}


class _SL:
    def __init__(self, dp):
        self.side = "no"
        self.delta_p = dp
        self.fair_cents = 0


@pytest.fixture(autouse=True)
def _funnel(ledger):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    yield
    failures._ledger = None


@pytest.fixture
def flip(gateway, ledger, surface):
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def _held(flip, *, entry=40, age, take_px=52):
    """A held OPEN position, aged `age`s past fill (past the hard-hold)."""
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    now = CLOSE - 700
    w.opens["yes"] = {
        "entry": entry, "count": 1, "take_oid": None, "take_proposed": True,
        "take_px": take_px, "collapse_polls": 0, "catastrophe_polls": 0,
        "det_ts": None, "entry_oid": None, "defer_polls": 0, "hold": False,
        "fill_ts": now - age}
    return w, now


def _custody(flip, w, book, now, sl=None, secs=700):
    return flip._open_custody(w, TICKER, EVENT, book, _ctx(book, secs, sl=sl),
                              secs, now)


# ── Finding 1: the ROOT — no market-dump on a decision; K is a real decision ──
def test_f1_k_is_a_real_decision_not_drift():
    assert config.OPEN_DETERMINED_K_POINTS == 40   # was 15 (drift)


def test_f1_sub_k_drift_does_not_cut(flip):
    """A drift below K (the old 15pt trigger) no longer fires the exit — the
    position holds/walks, never dumped on noise."""
    w, now = _held(flip, entry=40, age=config.FLIP_NO_SELL_S + 40)
    b = _book(yes=38, no=55)
    sl = _SL(config.OPEN_DETERMINED_K_POINTS - 5)   # 35pts: below the decision bar
    _custody(flip, w, b, now, sl=sl)                 # poll 1
    props = _custody(flip, w, b, now, sl=sl)         # poll 2
    assert [p for p in props if p.purpose == "CUT"] == []
    # no SPOT_DECIDED exit either — the collapse never confirmed
    assert not any("spot-decided" in (p.reason or "") for p in props)


def test_f1_decision_walks_to_scratch_never_crossfire(flip):
    """WO-2026-07-22-E re-anchor: the walk-to-scratch is RETIRED — an adverse
    move on the favored side now trips the MOMENTUM STOP. It exits MAKER-first
    (rest at entry−OPEN_MOMENTUM_STOP_C), never a crossfire market-dump — and
    only after 2 sustained polls (a first-poll blip does not fire)."""
    w, now = _held(flip, entry=40, age=100)
    b = _book(yes=30, no=55)                          # mark 30 == entry−10, AT the stop
    props1 = _custody(flip, w, b, now)                # poll 1 -> armed, not fired
    assert props1 == []                               # a single poll does not exit
    props = _custody(flip, w, b, now)                 # poll 2 -> sustained, fires
    assert [p for p in props if p.purpose == "CUT"] == []   # NO market-dump
    exits = [p for p in props if p.purpose == "EXIT"]
    assert len(exits) == 1
    assert exits[0].price_cents == 30 and not exits[0].crossfire  # at the stop, maker
    assert "momentum stop" in exits[0].reason and "maker" in exits[0].reason


def test_f1_catastrophe_still_crossfires(flip):
    """WO-2026-07-22-E re-anchor: a book already THROUGH the momentum stop
    (mark below entry−10, sustained) crossfires out at top of book rather than
    resting a maker nobody will hit — the deep-adverse path still crossfires."""
    w, now = _held(flip, entry=40, age=100)
    b = _book(yes=20, no=55)                          # mark 20 < stop 30: book through us
    _custody(flip, w, b, now)                         # poll 1
    cuts = [p for p in _custody(flip, w, b, now) if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].crossfire
    assert cuts[0].price_cents == 20 and "momentum stop" in cuts[0].reason


# ── Finding 3: the GATE — a hard-trending open skips OPEN's reversion entry ──
def test_f3_hard_trending_open_enters_trend_logged_not_a_skip(flip):
    """WO-2026-07-22-E: the volatility trend-SKIP is RETIRED. A hard one-
    directional open no longer skips — a trend AGREES with the favored side
    (that IS the thesis); trend_usd is LOGGED on the why and gates nothing."""
    w = flip._window(TICKER, CLOSE)
    w.spot_ticks = [66_000.0, 66_000.0 + config.OPEN_TREND_SKIP_USD + 20]
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=850,
                                       spot=66_000.0 + config.OPEN_TREND_SKIP_USD + 20,
                                       grain=GRAIN))
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]
    assert "trend $" in props[0].why and "logged" in props[0].why


def test_f3_calm_open_enters(flip):
    """WO-2026-07-22-E re-anchor: FLIP buys the FAVORED (higher-priced) side.
    A calm open with a favored side in-band [50,70] enters that side."""
    w = flip._window(TICKER, CLOSE)
    w.spot_ticks = [66_000.0, 66_010.0]              # a $10 wiggle
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=850,
                                       spot=66_010.0, grain=GRAIN))
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]


# ── Finding 4: the ENTRY — real-gouge only ──────────────────────────────────
def test_f4_entry_band_is_favored_50_to_70(flip):
    # WO-2026-07-22-E: the cheap-side ceiling (42) is retired from the entry
    # path; the favored-side band is [OPEN_ENTRY_MIN_C, OPEN_ENTRY_MAX_C].
    assert config.OPEN_ENTRY_MIN_C == 50 and config.OPEN_ENTRY_MAX_C == 70


def test_f4_real_gouge_enters_coinflip_skips(flip):
    # WO-2026-07-22-E re-anchor: the entry filter is the FAVORED side being
    # in-band [OPEN_ENTRY_MIN_C, OPEN_ENTRY_MAX_C].
    # favored no@60 (in [50,70], real demand to sell the +20 into): enters
    assert flip.evaluate(TICKER, _ctx(_book(yes=40, no=60), secs_left=850,
                                      grain=GRAIN))
    # favored no@72 (>70 — the move already fully priced, no gouge left): skips
    flip.windows.clear()
    assert flip.evaluate(TICKER, _ctx(_book(yes=28, no=72), secs_left=850,
                                      grain=GRAIN)) == []


# ── Finding 5: MEASURE — the swing-gate telemetry line, every window ─────────
def test_f5_swing_telemetry_line_logged(flip):
    """WO-2026-07-22-E re-anchor: the swing gate is RETIRED as an entry gate,
    so its telemetry line is gone. Its spirit — make the confirms legible so
    the sample-floor decision is data, not argument — survives as the
    LOGGED-NOT-GATED confirms on every entry `why` (depth ratio + trend
    agreement, gating on nothing until the tape earns them a gate)."""
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=850,
                                       spot=66_000.0, grain=GRAIN))
    why = props[0].why
    assert "confirms logged-not-gated" in why
    assert "ratio" in why and "trend $" in why and "depth" in why


# ── build 53 (WO-2026-07-21-FLIP-SELECTION) — the tape-derived Part A ────────
def test_a1_salvage_floor_is_relative_and_a_maker(flip):
    """WO-2026-07-22-E re-anchor: the relative SALVAGE floor is RETIRED and
    REPLACED by the MOMENTUM STOP. The loss floor is still RELATIVE to entry
    (entry − OPEN_MOMENTUM_STOP_C — a 60c entry stops at 50, a 40c entry at 30),
    bounding every loss at 10c, and it still exits MAKER-first (rest at the
    stop), never a crossfire dump."""
    assert config.OPEN_MOMENTUM_STOP_C == 10
    w, now = _held(flip, entry=60, age=100, take_px=80)
    b = _book(yes=50, no=45)                     # mark 50 == entry−10, the relative floor
    _custody(flip, w, b, now)                    # poll 1
    props = _custody(flip, w, b, now)            # poll 2 -> confirmed
    assert [p for p in props if p.purpose == "CUT"] == []      # NO crossfire
    exits = [p for p in props if p.purpose == "EXIT"]
    assert len(exits) == 1
    assert exits[0].price_cents == 50 and not exits[0].crossfire  # bounded 10c, maker
    assert "momentum stop" in exits[0].reason


def test_a1_absolute_floor_still_crossfires(flip):
    """WO-2026-07-22-E re-anchor: the absolute floor survives ONLY as the
    DEAD-FLOOR backstop for a curfew winner LEFT TO F (o['hold']). A held
    winner gone worthless (mark<=OPEN_CATASTROPHE_FLOOR, real depth, 2 polls)
    is evacuated by crossfire, never ridden to zero on the theory F has it."""
    w, now = _held(flip, entry=40, age=100)
    w.opens["yes"]["hold"] = True                 # a curfew winner handed to F
    b = _book(yes=20, no=55, yq=10)               # mark 20 <= dead floor, real depth
    _custody(flip, w, b, now)                     # poll 1
    cuts = [p for p in _custody(flip, w, b, now) if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].crossfire and "dead-floor" in cuts[0].reason


def test_a2_trend_usd_printed_on_enter(flip):
    """A2: trend_usd on EVERY OPEN entry line (enter, not only skip) — so the
    threshold is set from the observed distribution, not guessed."""
    w = flip._window(TICKER, CLOSE)
    w.spot_ticks = [66_000.0, 66_040.0]          # a $40 opening drift
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=850,
                                       spot=66_040.0, grain=GRAIN))
    assert "trend $+40" in props[0].why


def test_a3_depth_ratio_recorded_not_gated(flip):
    """A3: depth_ratio (held ÷ other) on the entry row — RECORD-ONLY, gating on
    nothing (n=3; a hypothesis, not a finding)."""
    # WO-2026-07-22-E re-anchor: depth_ratio = FAVORED-side depth ÷ other side.
    b = _book(yes=60, no=40, yq=14, nq=10)       # favored yes depth 14 / other 10 = 1.4
    props = flip.evaluate(TICKER, _ctx(b, secs_left=850, grain=GRAIN))
    assert "ratio 1.40x" in props[0].why
    # a thin favored side (little depth behind us) reads < 1.0, and still ENTERS
    flip.windows.clear()
    b2 = _book(yes=60, no=40, yq=6, nq=10)        # favored yes depth 6 / other 10 = 0.6
    props2 = flip.evaluate(TICKER, _ctx(b2, secs_left=850, grain=GRAIN))
    assert "ratio 0.60x" in props2[0].why and props2[0].purpose == "ENTRY"
