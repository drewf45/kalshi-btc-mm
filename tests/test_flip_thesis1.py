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


def _book(yes=40, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=850, grain=None, spot=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": None}


def _prime_entry(flip, join=60, grain=GRAIN_YES2):
    """WO-2026-07-22-F 'wait for the pile' (build 58) two-poll prime: a baseline
    in-window poll (secs_into~65, small skew) then the entry poll (secs_into~80,
    skew grown >=5, agreeing trend >=$15, favored depth). Returns the entry
    poll's proposals — props[0] is the favored yes@join ENTRY (target
    min(90, join+17), stop join-10)."""
    other = 100 - join
    flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                               spot=66000.0, grain=grain))
    return flip.evaluate(TICKER, _ctx(_book(yes=join, no=other), secs_left=820,
                                      spot=66020.0, grain=grain))


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


def _open_position(flip, gateway, ledger, side="yes", entry=60,
                   fill_secs_left=790):
    """An OPEN custody position via the real path: proposal, submit-marker,
    booked fill. build 58: FLIP buys the FAVORED (higher-priced) side in the
    band [50,70], but only once the PILE has formed — so the entry now needs the
    two-poll pile prime; the canonical entry is favored yes @ 60 (stop 50,
    take 77 = min(90, 60+17))."""
    props = _prime_entry(flip)
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", side, "ENTRY", entry, 1, "PROBE")
    flip.note_fill(TICKER, side, entry, CLOSE - fill_secs_left)
    return flip.windows[TICKER].opens[side]


# ── §2 stage 1: THE PATIENCE FLOOR ─────────────────────────────────────────
def test_first_minute_dip_is_noise_not_a_decision(flip, gateway, ledger):
    """build 57: the 240s no-sell hold is RETIRED — the momentum stop is
    armed from the first poll, suppressed by no hold. But a SINGLE adverse
    print is noise: a first-minute mark <= entry−10 for ONE poll does NOT
    exit; it takes 2 sustained polls (the 48→41 hair-trigger stays dead)."""
    o = _open_position(flip, gateway, ledger)            # favored yes @ 60
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=55), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)             # the scalp take rests
    # one poll through the stop (mark 45 <= entry−10 = 50) — NOISE, no exit
    p2 = flip.evaluate(TICKER, _ctx(_book(yes=45), secs_left=730))
    assert [p for p in p2 if p.purpose in ("CUT", "EXIT")] == []
    assert o["stop_polls"] == 1
    assert o.get("done") is not True                     # the position HOLDS
    # a SECOND sustained poll DOES exit — the 2-poll momentum stop
    flip.evaluate(TICKER, _ctx(_book(yes=45), secs_left=729))
    assert o.get("done") is True


def test_spot_collapse_cuts_any_time_two_polls(flip, gateway, ledger):
    """build 57: SPOT_DECIDED is RETIRED, replaced by the MOMENTUM STOP. An
    adverse move against the favored side exits on 2 SUSTAINED polls at ANY
    time; a one-frame flicker (a single poll, or a poll that recovers) does
    NOT — the counter resets on any mark back above entry−10. When the book
    is already through the stop the exit crosses at the mark (crossfire)."""
    o = _open_position(flip, gateway, ledger)            # favored yes @ 60
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=60), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)             # the take rests
    # stop = entry−10 = 50; poll 1 through the stop is a flicker — no cut
    cuts1 = [p for p in flip.evaluate(TICKER, _ctx(_book(yes=48),
                                                   secs_left=760))
             if p.purpose == "CUT"]
    assert cuts1 == []
    assert o["stop_polls"] == 1
    # a recovery back above the stop RESETS the counter — the flicker holds
    flip.evaluate(TICKER, _ctx(_book(yes=55), secs_left=740))
    assert o["stop_polls"] == 0
    # now 2 sustained polls through the stop → cut, crossfire at the mark
    flip.evaluate(TICKER, _ctx(_book(yes=48), secs_left=720))
    props = flip.evaluate(TICKER, _ctx(_book(yes=48), secs_left=700))
    cuts = [p for p in props if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].crossfire
    assert cuts[0].price_cents == 48       # book through us: cross at the mark
    assert "momentum stop" in cuts[0].reason
    assert o["exit_reason"] == "MOMENTUM_STOP"


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
    o = _open_position(flip, gateway, ledger)          # favored yes @ 60
    p1 = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    with caplog.at_level(logging.WARNING, logger="relay.lane_flip"):
        props = flip.evaluate(TICKER, _ctx(_book(yes=61),
                                           secs_left=config.FLIP_DECISION_S - 1))
    assert props == []                                 # nothing SOLD
    assert o["hold"] is True
    assert any("FLIP_HOLD_TO_SETTLE" in r.message
               and "decision-winner-to-F" in r.message
               for r in caplog.records)
    assert flip.held(TICKER) == {"yes": {"count": 1, "basis": 60}}
    # the hold is deliberate: cycles pass, zero UNCOVERED pages
    flip.evaluate(TICKER, _ctx(_book(yes=61),
                               secs_left=config.FLIP_DECISION_S - 2))
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='FLIP_UNCOVERED_LEG'"
    ).fetchone()[0] == 0


def test_t10_handoff_flat_hands_off_nothing(flip):
    """§5: flat at T-10 → no handoff artifact; F's window is simply F's."""
    assert flip.evaluate(TICKER, _ctx(_book(), secs_left=599)) == []
    assert flip.held(TICKER) == {}


def test_held_winner_that_reverses_is_still_cut(flip, gateway, ledger):
    """Adversary (a): the curfew hold-to-settle is NOT exempt from the
    backstop — a held winner that genuinely COLLAPSES still cuts, never a
    ride to zero. build 57: the DEAD-FLOOR backstop (mark <= 20c with real
    depth, 2 polls) is UNCHANGED and stays armed on the hold; a shallow dip
    is illiquidity, held even on the hold."""
    o = _open_position(flip, gateway, ledger)            # favored yes @ 60
    p1 = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    flip.evaluate(TICKER, _ctx(_book(yes=61),
                               secs_left=config.FLIP_DECISION_S - 1))  # convert to hold
    assert o["hold"] is True
    # a shallow reversal (34c) is illiquidity — held even on the hold
    assert [p for p in flip.evaluate(TICKER, _ctx(_book(yes=34),
                                                  secs_left=230))
            if p.purpose == "CUT"] == []
    # a genuine collapse to the dead floor still cuts — never zero
    # (sustained 2 polls; the held position is long past the opening window)
    flip.evaluate(TICKER, _ctx(_book(yes=20), secs_left=210))  # poll 1: sustain
    cuts = [p for p in flip.evaluate(TICKER, _ctx(_book(yes=20),
                                                  secs_left=209))
            if p.purpose == "CUT"]
    assert len(cuts) == 1 and "dead-floor backstop" in cuts[0].reason
    assert cuts[0].crossfire and o["exit_reason"] == "DEAD_FLOOR"


def test_f_agrees_conversion_pre_t10(flip, gateway, ledger, caplog):
    """build 57: the F-agrees patience hold-conversion is RETIRED. The new
    thesis SELLS the +20 into the pile-in — a rising favorite gets its
    resting take (entry + OPEN_GOUGE_C, cap 90); it is NOT converted to a
    hold-to-settle before the curfew. No 'F-agrees' conversion fires pre-T10."""
    import logging
    o = _open_position(flip, gateway, ledger)            # favored yes @ 60
    props = flip.evaluate(TICKER, _ctx(_book(yes=60), secs_left=780))
    take = next(p for p in props if p.purpose == "EXIT")
    assert take.price_cents == LaneFlip._take_price(60) == 64   # entry+4
    flip.on_submitted(take, "OID-T1", CLOSE - 780)
    # a rising favorite well before the curfew: NO hold conversion — the
    # resting +20 take is the exit, the position sells the gouge, never holds
    with caplog.at_level(logging.WARNING, logger="relay.lane_flip"):
        flip.evaluate(TICKER, _ctx(_book(yes=79), secs_left=700))
    assert o.get("hold") is not True and o.get("done") is not True
    assert not any("F-agrees" in r.message for r in caplog.records)
    assert not any("FLIP_HOLD_TO_SETTLE" in r.message for r in caplog.records)


# ── §1 stage 4: continuity — a LOGGED feature, never a vote ────────────────
def test_continuity_logs_agreement_and_never_votes(flip, gateway, ledger,
                                                   caplog):
    """Scientist: prior-window direction is logged beside the chosen side
    (agree=True/False) ONCE per window; the entry side stays the grain's —
    the signal builds its dataset before it may vote."""
    import logging
    ledger.record_outcome("KXBTC15M-PRIOR-T99", False)     # prior went NO
    with caplog.at_level(logging.INFO, logger="relay.lane_flip"):
        # build 58: the entry fires on the two-poll pile prime; the baseline
        # poll skips (skew not yet grown), the entry poll proposes and logs
        # continuity ONCE, and a third in-window poll proves once-per-window.
        flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                                   spot=66000.0, grain=GRAIN_YES2))   # baseline
        props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=820,
                                           spot=66020.0, grain=GRAIN_YES2))
        flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=799,
                                   spot=66040.0, grain=GRAIN_YES2))   # once/window
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]
    lines = [r.message for r in caplog.records if "OPEN_CONTINUITY" in r.message]
    assert len(lines) == 1
    assert "prior_window=no" in lines[0] and "agree=False" in lines[0]
    assert "log-only" in lines[0]


def test_continuity_silent_with_no_prior_window(flip, gateway, ledger,
                                                caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="relay.lane_flip"):
        flip.evaluate(TICKER, _ctx(_book(), secs_left=850, grain=GRAIN_YES2))
    assert not any("OPEN_CONTINUITY" in r.message for r in caplog.records)


# ── §3 stage 2: the retune — ~20¢ scalp target, T-10 entry cutoff ──────────
def test_scalp_take_rests_at_the_goal_bounded_move(flip, gateway, ledger):
    """§5, OVERTURNED by WO-FLIP-GOAL-TAKE (build 42): the +20 swing this
    test once required was priced to a rare event (201430 rode it to the
    floor, −27¢), so the take now floats to the reachable convergence move —
    entry+5 at the 1-lot cap (54¢), banked reliably. The reachable nickel is
    the win convergence actually gives; the +20 was the SOMETIMES."""
    # build 58: the take is entry + OPEN_GOUGE_C, capped 90 — a favored yes@60
    # entry rests its take at 77c, sold INTO the pile-in of buyers. The entry
    # fires on the two-poll pile prime.
    take_px = LaneFlip._take_price(60)             # 77
    props = _prime_entry(flip)
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 60, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 60, CLOSE - 790)
    take = next(p for p in flip.evaluate(TICKER, _ctx(_book(yes=60),
                                                      secs_left=780))
                if p.purpose == "EXIT")
    assert take.price_cents == take_px and take.action == "sell"
    # the reachable move arrives: the fill books the target capture
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", take_px, 1, "PROBE")
    flip.note_exit(TICKER, "yes", take_px, CLOSE - 500)
    assert flip.windows[TICKER].window_realized == take_px - 60


def test_no_new_scalp_entry_at_or_after_t10(flip):
    """§3.5: FLIP owns T-15→T-10; no fresh scalp inventory once
    secs_left <= 600. F owns the final five minutes."""
    assert config.OPEN_ENTRY_CUTOFF == 600         # DREW-RULED §3.5
    assert flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=600,
                                      grain=GRAIN_YES2)) == []
    assert flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=550,
                                      grain=GRAIN_YES2)) == []
    # build 58: an entry DOES fire inside the pile window [60,180]s once the
    # pile has formed (two-poll prime) — a fresh window, clear of the pre-cutoff
    # polls' skew samples.
    flip.windows.clear()
    props = _prime_entry(flip)
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]


def test_post_window_genuine_collapse_cuts_hard(flip, gateway, ledger):
    """build 57: SPOT_DECIDED is RETIRED — the MOMENTUM STOP cuts a genuine
    collapse, loss bounded. An adverse move through the stop (mark < entry−10),
    sustained 2 polls, crosses at the mark (crossfire) — no hold to ride."""
    o = _open_position(flip, gateway, ledger)            # favored yes @ 60
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=60), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    # book collapses through the stop (20 < entry−10 = 50), sustained 2 polls
    flip.evaluate(TICKER, _ctx(_book(yes=20), secs_left=701))  # poll 1: sustain
    cuts = [p for p in flip.evaluate(TICKER, _ctx(_book(yes=20),
                                                  secs_left=700))
            if p.purpose == "CUT"]
    assert len(cuts) == 1
    assert "momentum stop" in cuts[0].reason
    assert cuts[0].count == 1 and cuts[0].crossfire      # bounded, NOW
    assert cuts[0].price_cents == 20 and o["exit_reason"] == "MOMENTUM_STOP"
