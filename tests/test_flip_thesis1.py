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
    p2 = flip.evaluate(TICKER, _ctx(_book(yes=34), secs_left=730))
    assert [p for p in p2 if p.purpose == "CUT"] == []
    assert o.get("done") is not True                     # the position HOLDS


def test_spot_collapse_cuts_any_time_two_polls(flip, gateway, ledger):
    """WO-FLIP-EXIT-DOCTRINE Change 2 OVERTURNED the patience-gate on the
    ΔP leg: SPOT deciding against is the market's decision — the position
    trader's real exit — so it cuts on 2 sustained polls at ANY time
    (inside patience too). A one-frame flicker still holds (2 polls)."""
    o = _open_position(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)             # the take rests

    class SL:
        side = "no"
        delta_p = config.OPEN_DETERMINED_K_POINTS + 5
        fair_cents = 0          # hunt-entry gate B fails: no hunt fires
    # WELL inside patience (fill ~790, now 700 = 90s << 300s)
    ctx_collapse = _ctx(_book(yes=44), secs_left=700)
    ctx_collapse["spotlead"] = SL()
    assert flip.evaluate(TICKER, ctx_collapse) == []     # poll 1: one flicker holds
    assert o["collapse_polls"] == 1
    ctx_collapse2 = _ctx(_book(yes=44), secs_left=699)
    ctx_collapse2["spotlead"] = SL()
    cuts = [p for p in flip.evaluate(TICKER, ctx_collapse2)
            if p.purpose == "CUT"]
    assert len(cuts) == 1 and "SPOT decided" in cuts[0].reason  # 2 polls, cut inside patience


# ── §3.5/§4 stage 3: the T-10 handoff + F coordination ─────────────────────
def _stub_shared(inv, side="yes", cost=96):
    """A minimal FH8Shared-shaped stub: F decides to buy `side` at `cost`;
    the shared inventory answers `inv`."""
    from types import SimpleNamespace

    class _S:
        def __init__(self):
            self.flip_inventory = lambda market: inv
            self.stands_down_logged = set()

        def decide(self, market, ctx):
            return ("PROPOSE", SimpleNamespace(
                lane="F", side=side, cost_cents=cost, rest_fp=None,
                breakeven_pct=None, spot_price=None, distance_pct=None))

        def to_order(self, market, res):
            from relay_engine.gateway import Order
            return Order(lane="F", event=EVENT, market=market,
                         side=res.side, action="buy",
                         price_cents=res.cost_cents, count=1,
                         size_tier=config.TIER_PROBE, purpose="ENTRY",
                         why=f"F tier{res.cost_cents}")
    return _S()


def test_f_stands_down_when_flip_holds_the_side(caplog):
    """§5: FLIP holds X, market decides X, F would buy X → F_STANDS_DOWN,
    F does NOT buy — one position, held at FLIP's cheaper basis."""
    import logging

    from relay_engine.lanes import LaneF
    lane = LaneF(_stub_shared({"yes": {"count": 1, "basis": 49}}))
    with caplog.at_level(logging.WARNING, logger="relay.lanes"):
        d = lane.evaluate(TICKER, {"now": 0.0})
    assert d.proposal is None and d.pass_reason == "F_STANDS_DOWN"
    line = next(r.message for r in caplog.records
                if "F_STANDS_DOWN" in r.message)
    assert "flip_basis=49c" in line and "f_would_pay=96c" in line
    # logged once per (market, side); the stand-down itself repeats
    with caplog.at_level(logging.WARNING, logger="relay.lanes"):
        d2 = lane.evaluate(TICKER, {"now": 1.0})
    assert d2.pass_reason == "F_STANDS_DOWN"


def test_f_buys_freely_when_flip_does_not_hold_the_side():
    """§5: market decides Y against FLIP's X — F is free to act on Y (and
    coordination never double-buys: the gate keys on the SIDE)."""
    from relay_engine.lanes import LaneF
    lane = LaneF(_stub_shared({"yes": {"count": 1, "basis": 49}},
                              side="no", cost=96))
    d = lane.evaluate(TICKER, {"now": 0.0, "spotlead": None})
    assert d.proposal is not None and d.proposal.side == "no"


def test_runner_wires_the_shared_inventory(tmp_path):
    """§4 atomicity (Adversary b): ONE inventory object — the F evaluator's
    read IS LaneFlip.held, wired at engine construction."""
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "wire.db"))
    assert e.fh8_shared.flip_inventory == e.flip.held
    failures._ledger = None


def test_t10_handoff_winner_left_to_f_not_sold(flip, gateway, ledger,
                                               caplog):
    """§5: winner at T-10 → NOT sold; converted to hold-to-settle at
    FLIP's basis; the shared inventory still shows the side so F stands
    down; no UNCOVERED page for the deliberate hold."""
    import logging
    o = _open_position(flip, gateway, ledger)          # yes @ 48
    p1 = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    with caplog.at_level(logging.WARNING, logger="relay.lane_flip"):
        props = flip.evaluate(TICKER, _ctx(_book(yes=61), secs_left=599))
    assert props == []                                 # nothing SOLD
    assert o["hold"] is True
    assert any("FLIP_HOLD_TO_SETTLE" in r.message
               and "T-10-handoff-winner" in r.message
               for r in caplog.records)
    assert flip.held(TICKER) == {"yes": {"count": 1, "basis": 48}}
    # the hold is deliberate: cycles pass, zero UNCOVERED pages
    flip.evaluate(TICKER, _ctx(_book(yes=61), secs_left=598))
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='FLIP_UNCOVERED_LEG'"
    ).fetchone()[0] == 0


def test_t10_handoff_flat_hands_off_nothing(flip):
    """§5: flat at T-10 → no handoff artifact; F's window is simply F's."""
    assert flip.evaluate(TICKER, _ctx(_book(), secs_left=599)) == []
    assert flip.held(TICKER) == {}


def test_held_winner_that_reverses_is_still_cut(flip, gateway, ledger):
    """Adversary (a): the hold-to-settle is NOT exempt from the
    determined floor — a deep reversal still cuts; never a ride to zero."""
    o = _open_position(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    flip.evaluate(TICKER, _ctx(_book(yes=61), secs_left=599))  # convert
    assert o["hold"] is True
    # inside the patience floor the reversal is still noise (custodian's
    # catastrophic backstop guards the gap); past it, the cut fires
    assert [p for p in flip.evaluate(TICKER, _ctx(_book(yes=34),
                                                  secs_left=580))
            if p.purpose == "CUT"] == []
    cuts = [p for p in flip.evaluate(TICKER, _ctx(_book(yes=34),
                                                  secs_left=480))
            if p.purpose == "CUT"]
    assert len(cuts) == 1 and "determined-against" in cuts[0].reason


def test_f_agrees_conversion_pre_t10(flip, gateway, ledger, caplog):
    """§4: post-patience, the mark runs through F's band floor (95¢) with
    the take unfilled → convert to hold-to-settle, reason F-agrees."""
    import logging
    o = _open_position(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    o["fill_ts"] = CLOSE - 1100                        # patience elapsed
    with caplog.at_level(logging.WARNING, logger="relay.lane_flip"):
        props = flip.evaluate(TICKER, _ctx(_book(yes=95), secs_left=700))
    assert props == [] and o["hold"] is True
    assert any("reason=F-agrees" in r.message for r in caplog.records)


# ── §1 stage 4: continuity — a LOGGED feature, never a vote ────────────────
def test_continuity_logs_agreement_and_never_votes(flip, gateway, ledger,
                                                   caplog):
    """Scientist: prior-window direction is logged beside the chosen side
    (agree=True/False) ONCE per window; the entry side stays the grain's —
    the signal builds its dataset before it may vote."""
    import logging
    ledger.record_outcome("KXBTC15M-PRIOR-T99", False)     # prior went NO
    with caplog.at_level(logging.INFO, logger="relay.lane_flip"):
        props = flip.evaluate(TICKER, _ctx(_book(), secs_left=800,
                                           grain=GRAIN_YES2))
        flip.evaluate(TICKER, _ctx(_book(), secs_left=799,
                                   grain=GRAIN_YES2))      # once per window
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]
    lines = [r.message for r in caplog.records if "OPEN_CONTINUITY" in r.message]
    assert len(lines) == 1
    assert "prior_window=no" in lines[0] and "agree=False" in lines[0]
    assert "log-only" in lines[0]


def test_continuity_silent_with_no_prior_window(flip, gateway, ledger,
                                                caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="relay.lane_flip"):
        flip.evaluate(TICKER, _ctx(_book(), secs_left=800, grain=GRAIN_YES2))
    assert not any("OPEN_CONTINUITY" in r.message for r in caplog.records)


# ── §3 stage 2: the retune — ~20¢ scalp target, T-10 entry cutoff ──────────
def test_scalp_take_rests_at_entry_plus_20(flip, gateway, ledger):
    """§5: entry 49¢, the market swings — the take rests at ~entry+20
    (69¢), not the retired +5 nibble that fees ate."""
    assert config.OPEN_TAKE_CENTS == 20            # DREW-RULED §3
    props = flip.evaluate(TICKER, _ctx(_book(yes=49), secs_left=800,
                                       grain=GRAIN_YES2))
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 49, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 49, CLOSE - 790)
    take = next(p for p in flip.evaluate(TICKER, _ctx(_book(yes=49),
                                                      secs_left=780))
                if p.purpose == "EXIT")
    assert take.price_cents == 69 and take.action == "sell"
    # the swing arrives: the fill books the 20c capture
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 69, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 69, CLOSE - 500)
    assert flip.windows[TICKER].window_realized == 20


def test_no_new_scalp_entry_at_or_after_t10(flip):
    """§3.5: FLIP owns T-15→T-10; no fresh scalp inventory once
    secs_left <= 600. F owns the final five minutes."""
    assert config.OPEN_ENTRY_CUTOFF == 600         # DREW-RULED §3.5
    assert flip.evaluate(TICKER, _ctx(_book(), secs_left=600,
                                      grain=GRAIN_YES2)) == []
    assert flip.evaluate(TICKER, _ctx(_book(), secs_left=550,
                                      grain=GRAIN_YES2)) == []
    props = flip.evaluate(TICKER, _ctx(_book(), secs_left=601,
                                       grain=GRAIN_YES2))
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]


def test_post_window_genuine_decision_cuts_hard(flip, gateway, ledger):
    """§5: still against after the window, mark genuinely through trigger →
    the cut fires, loss bounded. Patience is upside-only."""
    o = _open_position(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    o["fill_ts"] = CLOSE - 1100                          # window long over
    cuts = [p for p in flip.evaluate(TICKER, _ctx(_book(yes=34),
                                                  secs_left=700))
            if p.purpose == "CUT"]
    assert len(cuts) == 1
    assert "determined-against" in cuts[0].reason
    assert cuts[0].count == 1 and cuts[0].crossfire      # bounded, NOW
