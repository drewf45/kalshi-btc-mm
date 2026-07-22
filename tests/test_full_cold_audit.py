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
    """A genuine decision (>= K) routes through the walk-down: a MAKER exit at
    scratch (entry), never a crossfire market-dump at the depressed bid."""
    w, now = _held(flip, entry=40, age=config.FLIP_NO_SELL_S + 40)
    # mark 35 is ABOVE the A1 salvage floor (entry−8=32), so this isolates the
    # SPOT_DECIDED path (a deeper mark would hit the relative floor first)
    b = _book(yes=35, no=55)                          # mark 35, decided against
    sl = _SL(config.OPEN_DETERMINED_K_POINTS + 5)     # 45pts: a real decision
    _custody(flip, w, b, now, sl=sl)                  # poll 1
    props = _custody(flip, w, b, now, sl=sl)          # poll 2 -> confirmed
    assert [p for p in props if p.purpose == "CUT"] == []   # NO market-dump
    exits = [p for p in props if p.purpose == "EXIT"]
    assert len(exits) == 1
    assert exits[0].price_cents == 40 and not exits[0].crossfire  # scratch, maker
    assert "spot-decided" in exits[0].reason and "scratch" in exits[0].reason


def test_f1_catastrophe_still_crossfires(flip):
    """The DEEP backstop is unchanged — a genuinely-gone position (mark<=20,
    real depth, past the opening, sustained) still crossfires out before zero."""
    w, now = _held(flip, entry=40, age=config.FLIP_NO_SELL_S + 40)
    b = _book(yes=20, no=55)                          # at the catastrophe floor
    _custody(flip, w, b, now)                         # poll 1
    cuts = [p for p in _custody(flip, w, b, now) if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].crossfire
    assert "CATASTROPHE" in cuts[0].reason


# ── Finding 3: the GATE — a hard-trending open skips OPEN's reversion entry ──
def test_f3_hard_trending_open_skips(flip):
    """The opening BTC-spot already run >= OPEN_TREND_SKIP_USD one-directionally
    → OPEN skips (no reversion edge)."""
    w = flip._window(TICKER, CLOSE)
    w.spot_ticks = [66_000.0, 66_000.0 + config.OPEN_TREND_SKIP_USD + 20]
    props = flip.evaluate(TICKER, _ctx(_book(yes=40, no=49), secs_left=850,
                                       spot=66_000.0 + config.OPEN_TREND_SKIP_USD + 20,
                                       grain=GRAIN))
    assert props == []


def test_f3_calm_open_enters(flip):
    """A calm open (small spot move) still enters the cheap side."""
    w = flip._window(TICKER, CLOSE)
    w.spot_ticks = [66_000.0, 66_010.0]              # a $10 wiggle
    props = flip.evaluate(TICKER, _ctx(_book(yes=40, no=49), secs_left=850,
                                       spot=66_010.0, grain=GRAIN))
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]


# ── Finding 4: the ENTRY — real-gouge only ──────────────────────────────────
def test_f4_entry_ceiling_is_42(flip):
    assert config.OPEN_MAX_ENTRY_CENTS == 42


def test_f4_real_gouge_enters_coinflip_skips(flip):
    # 42c cheap side (a real +10 gouge to the 52 middle): enters
    assert flip.evaluate(TICKER, _ctx(_book(yes=42, no=55), secs_left=850,
                                      grain=GRAIN))
    # 45c cheap side (a ~coinflip, no real gouge): skips
    flip.windows.clear()
    assert flip.evaluate(TICKER, _ctx(_book(yes=45, no=54), secs_left=850,
                                      grain=GRAIN)) == []


# ── Finding 5: MEASURE — the swing-gate telemetry line, every window ─────────
def test_f5_swing_telemetry_line_logged(flip, caplog):
    """The gate is live-but-permissive; the telemetry line makes its state
    legible so the sample-floor decision is data, not argument."""
    with caplog.at_level(logging.INFO, logger="relay.lane_flip"):
        flip.evaluate(TICKER, _ctx(_book(yes=40, no=49), secs_left=850,
                                   spot=66_000.0, grain=GRAIN))
    line = next((r.message for r in caplog.records
                 if r.message.startswith("swing ")), None)
    assert line is not None
    assert "n=" in line and "p_up=" in line and "p_down=" in line


# ── build 53 (WO-2026-07-21-FLIP-SELECTION) — the tape-derived Part A ────────
def test_a1_salvage_floor_is_relative_and_a_maker(flip):
    """A1: the price floor is RELATIVE to entry (entry−budget), and the
    relative-floor exit is a MAKER (fee-saved), never a crossfire dump —
    bounding every loss at OPEN_SALVAGE_BUDGET_C instead of riding to 20c."""
    assert config.OPEN_SALVAGE_BUDGET_C == 8
    w, now = _held(flip, entry=40, age=config.FLIP_NO_SELL_S + 40)
    b = _book(yes=32, no=55)                     # at the relative floor (40−8), depth 10
    _custody(flip, w, b, now)                    # poll 1
    props = _custody(flip, w, b, now)            # poll 2 -> confirmed
    assert [p for p in props if p.purpose == "CUT"] == []      # NO crossfire
    exits = [p for p in props if p.purpose == "EXIT"]
    assert len(exits) == 1
    assert exits[0].price_cents == 32 and not exits[0].crossfire  # bounded 8c, maker
    assert "salvage floor" in exits[0].reason


def test_a1_absolute_floor_still_crossfires(flip):
    """A1: only the ABSOLUTE 20c floor (genuinely gone) keeps its crossfire."""
    w, now = _held(flip, entry=26, age=config.FLIP_NO_SELL_S + 40)
    b = _book(yes=20, no=55)                      # mark <= 20 absolute
    _custody(flip, w, b, now)
    cuts = [p for p in _custody(flip, w, b, now) if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].crossfire and "CATASTROPHE" in cuts[0].reason


def test_a2_trend_usd_printed_on_enter(flip):
    """A2: trend_usd on EVERY OPEN entry line (enter, not only skip) — so the
    threshold is set from the observed distribution, not guessed."""
    w = flip._window(TICKER, CLOSE)
    w.spot_ticks = [66_000.0, 66_040.0]          # a $40 opening drift
    props = flip.evaluate(TICKER, _ctx(_book(yes=40, no=49), secs_left=850,
                                       spot=66_040.0, grain=GRAIN))
    assert "trend $+40" in props[0].why


def test_a3_depth_ratio_recorded_not_gated(flip):
    """A3: depth_ratio (held ÷ other) on the entry row — RECORD-ONLY, gating on
    nothing (n=3; a hypothesis, not a finding)."""
    b = _book(yes=40, no=49, yq=14, nq=10)       # cheap yes depth 14 / other 10 = 1.4
    props = flip.evaluate(TICKER, _ctx(b, secs_left=850, grain=GRAIN))
    assert "ratio 1.40x" in props[0].why
    # a thin abandoned side (the tape's losers) reads < 1.0, and still ENTERS
    flip.windows.clear()
    b2 = _book(yes=40, no=49, yq=6, nq=10)        # cheap yes depth 6 / other 10 = 0.6
    props2 = flip.evaluate(TICKER, _ctx(b2, secs_left=850, grain=GRAIN))
    assert "ratio 0.60x" in props2[0].why and props2[0].purpose == "ENTRY"
