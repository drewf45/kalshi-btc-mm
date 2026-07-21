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


def _book(yes=48, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=800, grain=None, sl=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": None, "grain": grain, "spotlead": sl}


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


# ── §1/§2.1: the resting take is the exit — post entry+5 and wait ──────────
def test_the_take_rests_at_entry_plus_five(flip):
    o, now = _pos(flip, entry=44)
    o["take_proposed"] = False           # let the take propose
    o["take_oid"] = None
    b = _book(yes=44, no=55)
    props = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))
    exits = [p for p in props if p.purpose == "EXIT"]
    assert len(exits) == 1
    assert exits[0].price_cents == 44 + LaneFlip._take_cents(1)   # 49, held-side


# ── §2.2: a low mark is ILLIQUIDITY — the position HOLDS ───────────────────
def test_illiquidity_dips_are_held(flip):
    """The core change: no reactive stop fires during the pile-in. Every
    in-band low mark (above the catastrophe floor) is HELD — the position
    waits as the liquidity provider for the reversion to lift its take."""
    for mark in (40, 38, 36, 30, 25, 22, 21):
        o, now = _pos(flip, entry=44)
        b = _book(yes=mark, no=55)
        flip._open_custody(*w_side_ctx(flip, b, now, 700, None))     # poll 1
        p2 = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))  # poll 2
        assert [x for x in p2 if x.purpose == "CUT"] == [], f"mark {mark} cut"
        assert not o.get("done")


# ── build 48: a fresh thin-book sub-20c dip is ILLIQUIDITY — HELD ──────────
def test_fresh_catastrophe_low_is_illiquidity_held(flip):
    """WO-FLIP-CATASTROPHE-ILLIQUIDITY: a fresh cheap entry whose held-side
    bid dips to 19c in the opening window (no buyers yet) is illiquidity, not
    collapse — the price floor is gated off during the opening window, so it
    HOLDS (today it dumped at market within 2 minutes). Only spot decides
    here."""
    o, now = _pos(flip, entry=42, gap=10)          # fresh: held 10s
    b = _book(yes=19, no=55)                        # sub-catastrophe bid, real depth
    flip._open_custody(*w_side_ctx(flip, b, now, 700, None))     # poll 1
    p2 = flip._open_custody(*w_side_ctx(flip, b, now, 700, None))  # poll 2
    assert [x for x in p2 if x.purpose == "CUT"] == []
    assert not o.get("done")


# ── build 48: a thin-book low past the window is STILL illiquidity — held ──
def test_thin_book_low_is_held_even_past_the_opening_window(flip):
    """The depth guard: even past the opening window and sustained, a 1-lot
    thin quote at 19c is book emptiness, not a real low — it is HELD. Only a
    low with REAL depth on the held side counts as a catastrophe."""
    o, now = _pos(flip, entry=44, gap=config.OPEN_OPENING_WINDOW_S + 30)
    b = OrderBook(market=TICKER)
    b.apply_snapshot({19: 1}, {55: 10}, ts=1.0)    # held-side bid: 1-lot thin
    ctx = _ctx(b, secs_left=700)
    flip._open_custody(flip.windows[TICKER], TICKER, EVENT, b, ctx, 700, now)
    p2 = flip._open_custody(flip.windows[TICKER], TICKER, EVENT, b, ctx, 700, now)
    assert [x for x in p2 if x.purpose == "CUT"] == []
    assert not o.get("done")


# ── §2.3: a GENUINE collapse (real, deep, sustained, past opening) cuts ────
def test_catastrophe_backstop_still_cuts_when_real(flip):
    """The deep backstop remains: a REAL low — depth on the held side, held
    PAST the opening window, sustained 2 polls — still cuts. Only the
    thin-book / opening-window / single-poll dump is retired."""
    o, now = _pos(flip, entry=44, gap=config.OPEN_OPENING_WINDOW_S + 30)
    b = _book(yes=20, no=55)                        # depth 10 >= min
    flip._open_custody(*w_side_ctx(flip, b, now, 700, None))     # poll 1: sustain
    cuts = [p for p in flip._open_custody(*w_side_ctx(flip, b, now, 700, None))
            if p.purpose == "CUT"]
    assert len(cuts) == 1 and "CATASTROPHE" in cuts[0].reason
    assert cuts[0].price_cents == config.OPEN_CATASTROPHE_FLOOR == 20


def test_spot_collapse_still_cuts_sustained(flip):
    """Spot deciding against the held side (sustained 2 polls) cuts in the
    hold — the market ran away hard, distinguished from noise by the sustain."""
    o, now = _pos(flip, entry=44)
    sl = Needle(side="no", d_before=10.0, d_after=90.0,
                delta_p=config.OPEN_DETERMINED_K_POINTS + 5.0,
                fair_cents=0.0, t_remaining=700.0)
    b = _book(yes=40, no=55)
    flip._open_custody(*w_side_ctx(flip, b, now, 700, sl))           # poll 1
    cuts = [p for p in flip._open_custody(*w_side_ctx(flip, b, now, 700, sl))
            if p.purpose == "CUT"]
    assert len(cuts) == 1 and "SPOT decided" in cuts[0].reason


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
    T-10 endgame handoff, at the mark, never ridden to settlement."""
    o, now = _pos(flip, entry=44, secs=config.OPEN_FLAT_BY - 1)
    b = _book(yes=34, no=55)
    cuts = [p for p in flip._open_custody(*w_side_ctx(flip, b, now,
                                                     config.OPEN_FLAT_BY - 1, None))
            if p.purpose == "CUT"]
    assert len(cuts) == 1 and "T-10 handoff" in cuts[0].reason
    assert cuts[0].price_cents == 34


# ── the entry filter is the band — the lane is NOT closed ──────────────────
def test_thesis_entries_admitted_no_geometry_gate(flip):
    """The risk/reward geometry gate is gone — the entry filter is band
    membership (buy the cheap pile-in). The 44-49c thesis range is admitted;
    the lane trades so the reversion rate can be measured."""
    for join in (40, 44, 48, 49):
        no = min(55, 100 - join)
        props = flip.evaluate(TICKER, _ctx(_book(yes=join, no=no),
                                           grain=GRAIN_YES2))
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
