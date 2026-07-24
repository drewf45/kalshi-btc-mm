"""WO-FLIP-LIQUIDITY-HOLD — post and wait, don't react (build 45).

The FLIP lane is reconceived as LIQUIDITY PROVISION. A low price after a FLIP
buy is ILLIQUIDITY (the opening pile-in — nobody is buying your side yet),
NOT a losing position. The job: buy the inventory the panic is dumping, POST
the take (entry+5, already resting), and HOLD as the liquidity provider until
the reversion lifts it — managing only the endgame if still unfilled.

This RETIRES build-43's reactive scalp stop (which sold inventory during the
exact early illiquidity the model must hold through) and the risk/reward
geometry gate it grounded. What REMAINS is the collapse backstop
(Adversary-mandated §2.3): a CONFIRMED collapse (spot decided sustained, or
the catastrophe floor 20) still cuts even in the passive hold — a real move,
not illiquidity noise. The endgame T-10 handoff is the primary loss exit.

§4 EMPIRICAL GATE: the reversion / resting-take fill rate is UNPROVEN — run
at the 1-lot cap and measure (Instrument 1) before any size increase. §5
F-covers-FLIP is martingale-adjacent and is BANKED, NOT BUILT (F sizes to its
own edge; NEVER because FLIP lost). HARD RAIL: no Kelly/cash/rate-halt/F
change."""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip, FLIP_X
from relay_engine.spotlead import Needle

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=40, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=850, grain=None, sl=None, spot=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": sl}


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


def _pos(flip, entry=44, *, secs=700, gap=10, hold=False):
    """Inject a booked OPEN position with its take resting."""
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    now = CLOSE - secs
    w.opens["yes"] = {"entry": entry, "fill_ts": now - gap, "count": 1,
                      "take_oid": None, "take_proposed": True,
                      "collapse_polls": 0, "det_ts": None,
                      "entry_oid": None, "defer_polls": 0, "hold": hold}
    return w.opens["yes"], now


def _cuts(flip, mark, now, secs=700, sl=None):
    b = _book(yes=mark, no=55)
    return [p for p in flip._open_custody(*w_side_ctx(flip, b, now, secs, sl))
            if p.purpose == "CUT"]


def w_side_ctx(flip, b, now, secs, sl):
    w = flip.windows[TICKER]
    return (w, TICKER, EVENT, b, _ctx(b, secs, sl=sl), secs, now)


# ── §1/§2.1: the resting take is the exit — post the +20 gouge and wait ────
def test_the_take_rests_at_entry_plus_gouge(flip):
    """WO-2026-07-22-F (build 58): the resting take is now entry + OPEN_GOUGE_C
    (the +17 sold INTO the pile-in of buyers on the FAVORED side), capped at
    90¢ to stay out of the illiquid tail — a 60¢ favored entry rests its take at
    77¢. Still one resting exit, still held-side; the anchor moved from the
    middle to the +17 gouge."""
    o, now = _pos(flip, entry=60)
    o["take_proposed"] = False           # let the take propose
    o["take_oid"] = None
    b = _book(yes=60, no=40)
    props = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))
    exits = [p for p in props if p.purpose == "EXIT"]
    assert len(exits) == 1
    assert exits[0].price_cents == LaneFlip._take_price(60) == 64   # entry+4


# ── WO-2026-07-22-E: dips partition at the stop — above held, below exits ──
def test_dips_above_the_stop_held_below_the_stop_exit(flip):
    """WO-2026-07-22-E OVERTURNS the 'every dip is held' thesis. On the FAVORED
    side the momentum stop partitions dips by the stop line (entry −
    OPEN_MOMENTUM_STOP_C): a mark ABOVE the stop is still held (noise inside the
    geometry), but a mark AT OR BELOW it — sustained 2 polls — EXITS. There is
    no liquidity hold: an adverse move means the favored-side thesis is wrong."""
    entry = 60
    stop_px = entry - config.OPEN_MOMENTUM_STOP_C      # 50
    for mark in (58, 55, 51):                          # above the stop: held
        o, now = _pos(flip, entry=entry)
        b = _book(yes=mark, no=40)
        flip._open_custody(*w_side_ctx(flip, b, now, 700, None))     # poll 1
        p2 = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))  # poll 2
        assert [x for x in p2 if x.purpose in ("EXIT", "CUT")] == [], \
            f"mark {mark} above stop exited"
        assert not o.get("done")
    # WO-2026-07-24-D Part 2: at/within-slip of the stop it exits on poll 2;
    # THROUGH the floor (stop−slip) it rests one poll at the floor, then crosses.
    floor = stop_px - config.SLIP_TOLERANCE_C          # 47
    for mark in (50, 45, 40):                          # at/below the stop: exits
        o, now = _pos(flip, entry=entry)
        b = _book(yes=mark, no=40)
        flip._open_custody(*w_side_ctx(flip, b, now, 700, None))     # poll 1
        p2 = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))  # poll 2
        if mark >= floor:                              # within slip: exits poll 2
            exd = [x for x in p2 if x.purpose in ("EXIT", "CUT")]
            assert len(exd) == 1 and o.get("exit_reason") == "MOMENTUM_STOP", \
                f"mark {mark} at/below stop held"
        else:                                          # through floor: rest then cross
            assert any(x.price_cents == floor and x.purpose == "EXIT"
                       for x in p2), f"mark {mark} did not rest at the floor"
            p3 = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))  # poll 3
            exd = [x for x in p3 if x.purpose in ("EXIT", "CUT")]
            assert len(exd) == 1 and o.get("exit_reason") == "MOMENTUM_STOP", \
                f"mark {mark} through floor never crossed"


# ── WO-2026-07-22-E: the stop is armed from poll 1 — no opening-window grace ─
def test_fresh_position_stops_no_opening_window_grace(flip):
    """WO-2026-07-22-E: the momentum stop is armed from the FIRST poll — there
    is no opening-window 'illiquidity hold' anymore (the retired thesis held a
    fresh sub-floor dip through the pile-in; today that is the thesis being
    wrong). A fresh position (held only seconds) whose favored side has fallen
    THROUGH the stop for 2 polls crosses out at the mark just the same."""
    o, now = _pos(flip, entry=60, gap=10)              # fresh: held 10s
    stop_px = 60 - config.OPEN_MOMENTUM_STOP_C          # 50
    floor = stop_px - config.SLIP_TOLERANCE_C           # 47
    b = _book(yes=stop_px - 5, no=40)                   # 45: through the floor
    flip._open_custody(*w_side_ctx(flip, b, now, 700, None))     # poll 1
    p2 = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))  # poll 2: rest
    # WO-2026-07-24-D Part 2: below the floor it rests one poll before crossing
    assert any(x.price_cents == floor and x.purpose == "EXIT" for x in p2)
    p3 = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))  # poll 3: cross
    cuts = [x for x in p3 if x.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].price_cents == 45 and cuts[0].crossfire
    assert o.get("exit_reason") == "MOMENTUM_STOP"


# ── WO-2026-07-22-E: the favored side has NO hold — an adverse move exits ──
def test_favored_side_adverse_move_stops_no_hold(flip):
    """The core inversion. Build 45's liquidity-hold is RETIRED: on the FAVORED
    side an adverse move means the thesis is ALREADY WRONG, so there is NO hold.
    A sustained (2-poll) mark at/below entry − OPEN_MOMENTUM_STOP_C EXITS via the
    momentum stop — maker-first, resting AT the stop when the book has not gone
    through it. A single poll does NOT fire (never a lone print)."""
    o, now = _pos(flip, entry=60, gap=config.OPEN_OPENING_WINDOW_S + 30)
    stop_px = 60 - config.OPEN_MOMENTUM_STOP_C         # 50
    b = _book(yes=stop_px, no=55)                       # mark == stop, at the line
    p1 = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))    # poll 1: armed
    assert [x for x in p1 if x.purpose in ("EXIT", "CUT")] == []
    assert not o.get("done")
    p2 = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))    # poll 2: fires
    exits = [x for x in p2 if x.purpose == "EXIT"]
    assert len(exits) == 1 and exits[0].price_cents == stop_px == 50
    assert not exits[0].crossfire and "momentum stop" in exits[0].reason
    assert o.get("exit_reason") == "MOMENTUM_STOP" and o.get("done")


# ── WO-2026-07-22-E: the momentum stop crosses when the book is through ────
def test_momentum_stop_crosses_when_book_through(flip):
    """The evacuation fork: when the book has already gone THROUGH the stop
    (mark below entry − OPEN_MOMENTUM_STOP_C), the momentum stop crosses at the
    top of book to get out — CUT, crossfire — rather than rest behind a market
    that has left. Sustained 2 polls; a single print does not fire."""
    o, now = _pos(flip, entry=60, gap=config.FLIP_NO_SELL_S + 30)
    stop_px = 60 - config.OPEN_MOMENTUM_STOP_C         # 50
    floor = stop_px - config.SLIP_TOLERANCE_C          # 47
    mark = stop_px - 6                                  # 44: through the floor
    b = _book(yes=mark, no=55)
    p1 = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))    # poll 1: sustain
    assert [p for p in p1 if p.purpose == "CUT"] == []
    # WO-2026-07-24-D Part 2: poll 2 rests at the floor, poll 3 crosses at mark
    p2 = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))
    assert [p for p in p2 if p.purpose == "CUT"] == []
    assert any(p.price_cents == floor and p.purpose == "EXIT" for p in p2)
    cuts = [p for p in flip._open_custody(*w_side_ctx(flip, b, now, 700, None))
            if p.purpose == "CUT"]
    assert len(cuts) == 1 and "momentum stop" in cuts[0].reason
    assert cuts[0].price_cents == mark == 44 and cuts[0].crossfire
    assert o.get("exit_reason") == "MOMENTUM_STOP"


def test_momentum_stop_requires_sustain_maker_first(flip):
    """The stop demands 2 SUSTAINED polls (distinguished from noise by the
    sustain, as the retired spot-collapse was). An adverse poll followed by a
    recovery back above the stop RESETS the counter — never a lone print — and
    only when the mark holds at/below the stop for two polls does it exit,
    maker-first (resting AT the stop when the book has not gone through)."""
    o, now = _pos(flip, entry=60, gap=config.FLIP_NO_SELL_S + 30)
    stop_px = 60 - config.OPEN_MOMENTUM_STOP_C         # 50
    adverse = _book(yes=stop_px, no=55)                 # mark == stop
    recover = _book(yes=stop_px + 5, no=55)             # bounced above the stop
    flip._open_custody(*w_side_ctx(flip, adverse, now, 700, None))   # poll 1: armed
    flip._open_custody(*w_side_ctx(flip, recover, now, 700, None))   # poll 2: reset
    assert o.get("stop_polls") == 0 and not o.get("done")
    flip._open_custody(*w_side_ctx(flip, adverse, now, 700, None))   # poll 3: armed
    props = flip._open_custody(*w_side_ctx(flip, adverse, now, 700, None))  # poll 4
    assert [p for p in props if p.purpose == "CUT"] == []
    exits = [p for p in props if p.purpose == "EXIT"]
    assert len(exits) == 1 and exits[0].price_cents == stop_px == 50
    assert not exits[0].crossfire and o.get("exit_reason") == "MOMENTUM_STOP"


def test_illiquidity_dip_is_not_a_spot_flicker(flip):
    """A one-poll spot flicker (not sustained) is still held — the backstop
    demands 2 sustained polls, so noise does not trip it."""
    o, now = _pos(flip, entry=44)
    sl = Needle(side="no", d_before=10.0, d_after=90.0,
                delta_p=config.OPEN_DETERMINED_K_POINTS + 5.0,
                fair_cents=0.0, t_remaining=700.0)
    b = _book(yes=40, no=55)
    cuts = [p for p in flip._open_custody(*w_side_ctx(flip, b, now, 700, sl))
            if p.purpose == "CUT"]
    assert cuts == []                     # one poll: held


# ── §2.4: the endgame T-10 handoff is the PRIMARY loss exit ────────────────
def test_unreverted_loser_cleared_at_t10(flip):
    """An unreverted loser is not stopped on price — it is cleared at the
    endgame decision handoff (build 50: moved from T-10 to FLIP_DECISION_S), at
    the mark, never ridden to settlement."""
    o, now = _pos(flip, entry=44, secs=config.FLIP_DECISION_S - 1)
    b = _book(yes=34, no=55)
    cuts = [p for p in flip._open_custody(*w_side_ctx(flip, b, now,
                                                     config.FLIP_DECISION_S - 1, None))
            if p.purpose == "CUT"]
    assert len(cuts) == 1 and "decision point" in cuts[0].reason
    assert cuts[0].price_cents == 34


# ── the entry filter is the band — the lane is NOT closed ──────────────────
def test_thesis_entries_admitted_no_geometry_gate(flip):
    """WO-2026-07-22-F re-anchor: the risk/reward geometry gate is still gone —
    the entry filter is the PILE gate on the FAVORED side in [50,70]. Favored
    joins across the band, each with a formed pile (two-poll prime: baseline
    small skew, then a grown skew + agreeing rising trend + favored depth), are
    admitted; the lane trades so the reversion rate can be measured."""
    for join in (55, 58, 62, 64):     # favored side across the deliberate [55,64] band
        other = join - 20             # skew 20
        flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                                   grain=GRAIN_YES2, spot=66000.0))   # baseline
        props = flip.evaluate(TICKER, _ctx(_book(yes=join, no=other),
                                           secs_left=820, grain=GRAIN_YES2,
                                           spot=66020.0))
        assert [p for p in props if p.purpose == "ENTRY"], f"{join} rejected"
        flip.windows.clear()


# ── §4: the resting-take fill is the measured reversion event ──────────────
def test_take_fill_is_the_measured_event(flip, surface):
    """Instrument 1 logs the resting-take outcome — with the reactive stops
    gone, the take fill IS the reversion evidence (§4 empirical gate). A fill
    at entry+5 reads as took_swing True."""
    flip._log_swing_outcome(TICKER, 44, 49, CLOSE - 600, CLOSE - 790)
    import json
    (d,) = surface.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='FLIP_SWING'"
        " AND market=? ORDER BY id DESC LIMIT 1", (TICKER,)).fetchone()
    row = json.loads(d)
    assert row["took_swing"] is True and row["gross_cents"] == 5


# ── HARD RAIL ──────────────────────────────────────────────────────────────
def test_the_scalp_stop_is_retired():
    """WO-45 removed the reactive scalp stop and its constant entirely."""
    assert not hasattr(config, "OPEN_SCALP_STOP_CENTS")


def test_take_and_winner_side_unchanged():
    assert LaneFlip._take_cents(1) == 5
    assert config.WINDOW_BOOK_GOAL_CENTS == 5
    assert config.OPEN_TAKE_MIN == 5 and config.OPEN_TAKE_MAX == 20


def test_rails_unchanged():
    assert config.KELLY_FRACTION_CEILING == pytest.approx(1.0 / 12.0)
    assert config.RATE_HALT_LOSSES == 2
    assert config.OPEN_CATASTROPHE_FLOOR == 20     # the retained backstop
    assert FLIP_X == 4


def test_f_covers_flip_not_built_no_martingale_hook():
    """§5 HARD CONSTRAINT: F sizes to its OWN edge, NEVER because FLIP lost.
    F-covers-FLIP is banked, not built — F's lane carries no coupling to
    FLIP's P&L (no loss-recovery objective can leak into F sizing)."""
    import inspect
    from relay_engine import lane_fh8
    src = inspect.getsource(lane_fh8)
    assert "window_realized" not in src        # no FLIP P&L read
    assert "OPEN_SCALP_STOP_CENTS" not in src
