"""WO-FLIP-THESIS-1 — SCALP-OR-HOLD + F COORDINATION. The lane's true
thesis in code: buy cheap into the ~50/50 open, hold patiently (§2 the
5-min patience floor — no reflexive cut on noise), scalp ~20¢ into the
swing (§3) or hold-to-settle when F agrees (§4 — shared inventory, F
stands down); at T-10 a book-aware handoff (§3.5): winners LEFT to F at
basis, losers cleared, F owns the final window. Determined-against still
cuts a genuine loser post-window — patience is upside-only, never a ride
to zero.

RAIL: only the §3-named constants moved (OPEN_TAKE_CENTS, entry cutoff,
OPEN_FLAT_BY semantics, patience gate) + the coordination build. Kelly,
depth, net-risk, and the integrity paths untouched."""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=48, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=800, grain=None):
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


def _open_position(flip, gateway, ledger, side="yes", entry=48,
                   fill_secs_left=790):
    """An OPEN custody position via the real path: proposal, submit-marker,
    booked fill."""
    props = flip.evaluate(TICKER, _ctx(_book(), secs_left=800,
                                       grain=GRAIN_YES2))
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", side, "ENTRY", entry, 1, "PROBE")
    flip.note_fill(TICKER, side, entry, CLOSE - fill_secs_left)
    return flip.windows[TICKER].opens[side]


# ── §2 stage 1: THE PATIENCE FLOOR ─────────────────────────────────────────
def test_first_minute_dip_is_noise_not_a_decision(flip, gateway, ledger):
    """§5: a fresh entry dips through the determined trigger in minute 1 →
    NO cut. The 48→41-nine-seconds-later −9¢ evacuate is dead."""
    o = _open_position(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=41), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)             # the scalp take rests
    # 60 seconds after the fill, mark 41 < trigger 42 — still NOISE
    p2 = flip.evaluate(TICKER, _ctx(_book(yes=41), secs_left=730))
    assert [p for p in p2 if p.purpose == "CUT"] == []
    assert o.get("done") is not True                     # the position HOLDS


def test_collapse_counts_pre_window_but_fires_only_after(flip, gateway,
                                                         ledger):
    """The sustained ΔP-collapse keeps counting through the patience window
    and fires the moment the window ends — patience never blinds the
    loss-term, it only un-hair-triggers it."""
    o = _open_position(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)             # the take rests
    class SL:
        side = "no"
        delta_p = config.OPEN_DETERMINED_K_POINTS + 5
        fair_cents = 0          # hunt-entry gate B fails: no hunt fires
    ctx_collapse = _ctx(_book(yes=44), secs_left=700)
    ctx_collapse["spotlead"] = SL()
    assert flip.evaluate(TICKER, ctx_collapse) == []     # poll 1: counted
    ctx_collapse2 = _ctx(_book(yes=44), secs_left=699)
    ctx_collapse2["spotlead"] = SL()
    assert [p for p in flip.evaluate(TICKER, ctx_collapse2)
            if p.purpose == "CUT"] == []                 # pre-window: HOLDS
    assert o["collapse_polls"] >= 2
    # the window ends; the very next collapse poll cuts
    o["fill_ts"] = CLOSE - 1100                          # patience elapsed
    ctx_collapse3 = _ctx(_book(yes=44), secs_left=698)
    ctx_collapse3["spotlead"] = SL()
    cuts = [p for p in flip.evaluate(TICKER, ctx_collapse3)
            if p.purpose == "CUT"]
    assert len(cuts) == 1 and "collapse" in cuts[0].reason


def test_post_window_genuine_decision_cuts_hard(flip, gateway, ledger):
    """§5: still against after the window, mark genuinely through trigger →
    the cut fires, loss bounded. Patience is upside-only."""
    o = _open_position(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    o["fill_ts"] = CLOSE - 1100                          # window long over
    cuts = [p for p in flip.evaluate(TICKER, _ctx(_book(yes=41),
                                                  secs_left=700))
            if p.purpose == "CUT"]
    assert len(cuts) == 1
    assert "determined-against" in cuts[0].reason
    assert cuts[0].count == 1 and cuts[0].crossfire      # bounded, NOW
