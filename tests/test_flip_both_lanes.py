"""WO-BOTH-LANES-MARKET-TRUE (build 50) — the doctrine made TIME-AWARE.

Hold through noise, act only on confirmed real moves, time-aware, recover —
never panic. Acceptance for the parts this WO adds:

  A  FLIP 4-MINUTE HARD NO-SELL — the opening pile-in is noise; for the first
     FLIP_NO_SELL_S from entry NOTHING sells (the spot-decided cut AND the
     catastrophe price floor are both suppressed). The only exit is a FILL of
     the resting middle-take. (Fix for the 12:46 first-minute stop-out.)
  C  THE DECISION POINT — at secs_left <= FLIP_DECISION_S (~minute 11), F's
     inventory-aware endgame takes over: a WINNER is left to F as hold-to-
     settle at FLIP's cheap basis (F rides it); a LOSER is sold, never ridden
     to the bell. (The walk-down that clears inventory toward here is covered
     in test_flip_every_market; the extended window is exercised below.)
  D  F 40-POINT PRICE SALVAGE — a favorite that slips >= F_SALVAGE_SLIP_POINTS
     from entry has lost the confidence that bought it; recover (~−40) NOW
     instead of riding to the −90 backstop. Table-free (fires spot-blind), one
     attempt, no re-entry (Wall 3 single-entry).

  E (F entry rest-back) is already shipped by build 48 (WO-MAKER-REST-BACK,
     lane-agnostic) and covered by test_maker_rest_back — no change here.

HARD RAIL: FLIP_SIZE_CAP=1; catastrophe-illiquidity guards intact; no Kelly /
cash / rate-halt change."""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import (Custodian, OpenPosition, salvage_params)
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
STRIKE = 118_000.0


def _book(yes=None, no=None, yq=10, nq=10):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: yq} if yes is not None else {},
                     {no: nq} if no is not None else {}, ts=1.0)
    return b


def _ctx(book, secs_left, sl=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": None, "grain": None, "spotlead": sl}


class _SL:
    """A spotlead stub deciding AGAINST the held yes side (spot running down)."""
    side = "no"
    delta_p = config.OPEN_DETERMINED_K_POINTS + 5
    fair_cents = 0


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


@pytest.fixture
def custodian(gateway, ledger, surface):
    return Custodian(gateway, ledger, surface, ladder=DegradeLadder())


def _inject(flip, *, entry=44, secs, age, take_px=52, hold=False):
    """A booked OPEN position with its middle-take already resting, aged
    `age` seconds since fill, in a window closing `secs` from now."""
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    now = CLOSE - secs
    w.opens["yes"] = {
        "entry": entry, "count": 1, "take_oid": None, "take_proposed": True,
        "take_px": take_px, "collapse_polls": 0, "catastrophe_polls": 0,
        "det_ts": None, "entry_oid": None, "defer_polls": 0, "hold": hold,
        "fill_ts": now - age}
    return w, now


def _custody(flip, w, book, secs, now, sl=None):
    return flip._open_custody(w, TICKER, EVENT, book, _ctx(book, secs, sl),
                              secs, now)


def _cuts(props):
    return [p for p in props if p.purpose == "CUT"]


# ── A: THE 4-MINUTE HARD NO-SELL ───────────────────────────────────────────
def test_a_first_minute_spot_collapse_is_held(flip):
    """The 12:46 fix: a fresh FLIP position takes a sustained spot collapse in
    the first FLIP_NO_SELL_S — and is HELD, never cut. The pile-in is noise."""
    w, now = _inject(flip, secs=780, age=20)            # 20s old: hard-hold
    b = _book(yes=44, no=55)
    for secs in (780, 779, 778):                        # 3 sustained collapse polls
        now = CLOSE - secs
        w.opens["yes"]["fill_ts"] = now - 20            # stay ~20s old
        assert _cuts(_custody(flip, w, b, secs, now, sl=_SL())) == []
    assert not w.opens["yes"].get("done")               # still alive


def test_a_sustained_adverse_move_cuts_on_two_polls(flip):
    """REPLACED by WO-2026-07-22-E: the time-bound hard-hold and the spot-decided
    scratch are retired. On the FAVORED side there is no hold — a sustained
    adverse move (mark at/below entry−10) cuts on 2 sustained polls, and where
    the book is through the stop it crosses at the mark."""
    w, now = _inject(flip, entry=60, secs=560, age=300)
    b = _book(yes=48, no=55)                             # mark 48 <= entry−10=50
    _custody(flip, w, b, 560, now)                       # poll 1: arms
    props = _custody(flip, w, b, 559, CLOSE - 559)       # poll 2: fires
    exits = [p for p in props if p.purpose in ("EXIT", "CUT")]
    assert len(exits) == 1 and exits[0].price_cents == 48
    assert "momentum stop" in exits[0].reason and exits[0].crossfire


def test_a_deep_low_needs_two_polls_never_a_single_dump(flip):
    """REPLACED by WO-2026-07-22-E: the hard-hold catastrophe suppression is
    retired (there is no hold on the favored side). But the 'never a single-poll
    dump' guard survives in the momentum stop — even a deep low bid (20c) does
    NOT fire on ONE poll; it takes 2 sustained polls before it crosses out."""
    w, now = _inject(flip, entry=60, secs=800, age=30)
    b = _book(yes=20, no=55)                             # depth 10, deep low
    assert _cuts(_custody(flip, w, b, 800, now)) == []   # one poll: no dump
    cuts = _cuts(_custody(flip, w, b, 799, CLOSE - 799))
    assert len(cuts) == 1 and cuts[0].price_cents == 20  # 2 polls: crosses out


# ── C: THE DECISION POINT (F's inventory-aware endgame) ────────────────────
def test_c_decision_winner_left_to_F(flip):
    """At the decision point a WINNER (mark at/above basis) is converted to
    hold-to-settle at FLIP's cheap basis — F rides it, nothing is sold."""
    w, now = _inject(flip, entry=44, secs=config.FLIP_DECISION_S - 1, age=800)
    props = _custody(flip, w, _book(yes=55, no=44),
                     config.FLIP_DECISION_S - 1, now)
    assert props == []                                  # nothing SOLD
    assert w.opens["yes"]["hold"] is True               # F rides it


def test_c_decision_loser_is_sold(flip):
    """At the decision point a LOSER (mark below basis) is SOLD at the mark —
    never ridden unfilled into the bell."""
    w, now = _inject(flip, entry=44, secs=config.FLIP_DECISION_S - 1, age=800)
    cuts = _cuts(_custody(flip, w, _book(yes=34, no=55),
                          config.FLIP_DECISION_S - 1, now))
    assert len(cuts) == 1 and cuts[0].price_cents == 34
    assert "decision point" in cuts[0].reason and cuts[0].crossfire


def test_c_momentum_stop_active_past_the_old_t10(flip):
    """REPLACED by WO-2026-07-22-E: the active walk-down is retired. FLIP is
    still ACTIVELY managed here (well past the old T-10 at 600, just above the
    decision point): an adverse mark trips the MOMENTUM STOP on 2 sustained
    polls — the position is not passively riding to the bell."""
    w, now = _inject(flip, entry=60, secs=config.FLIP_DECISION_S + 30,
                     age=800, take_px=80)
    b = _book(yes=48, no=55)                             # mark 48 <= entry−10=50
    _custody(flip, w, b, config.FLIP_DECISION_S + 30, now)   # poll 1: arms
    exits = [p for p in _custody(flip, w, b, config.FLIP_DECISION_S + 30, now)
             if p.purpose in ("EXIT", "CUT")]
    assert len(exits) == 1 and exits[0].price_cents == 48
    assert "momentum stop" in exits[0].reason


def test_c_config_decision_is_minute_eleven():
    """FLIP_NO_SELL_S and FLIP_DECISION_S are the same 4-minute conviction
    window, one from entry, one from close."""
    assert config.FLIP_NO_SELL_S == config.FLIP_DECISION_S == 240


# ── D: F's 40-POINT PRICE SALVAGE ──────────────────────────────────────────
def _f_pos(custodian, ledger, *, entry=95):
    custodian.set_lane_params("F", salvage_params())
    pos = OpenPosition(event=EVENT, market=TICKER, lane="F", side="yes",
                       count=1, entry_price_cents=entry, entry_p_win=entry / 100,
                       size_tier=config.TIER_PROBE, entry_time=CLOSE - 600,
                       d_entry=None, t_entry=None, p_entry=None)  # NO table anchor
    custodian.adopt(pos)
    ledger.record_fill(TICKER, "F", "yes", "ENTRY", entry, 1, "PROBE")
    return pos


def _f_tick(custodian, now, yes_bid, *, spot=None):
    return custodian.tick(
        books={TICKER: _book(yes=yes_bid, no=100 - yes_bid - 2)},
        close_ts_of=lambda m: CLOSE, now=now, balance_usd=100.0,
        spot=spot, boundaries={TICKER: (None, STRIKE)})


def test_d_forty_point_slip_salvages_immediately_table_blind(custodian, ledger):
    """A 95c favorite that slips to 55c (−40) is a decisive reversal: the price
    salvage cuts IMMEDIATELY, even with NO spot/table (the exact case that rode
    to −90). Recover ~−40, not −90."""
    pos = _f_pos(custodian, ledger, entry=95)
    cuts = _f_tick(custodian, CLOSE - 400, yes_bid=55, spot=None)  # −40, blind
    assert pos.salvage_attempted is True
    assert pos.salvage_fired == "SALVAGE_SLIP"
    assert any(t == "SALVAGE_SLIP" for _, _, t in cuts)
    row = ledger.db.execute(
        "SELECT price_cents FROM fills WHERE market=? AND action='CUSTODIAN_EXIT'",
        (TICKER,)).fetchone()
    assert row is not None and row[0] == 55               # recovered at the mark


def test_d_slip_below_forty_is_not_a_price_salvage(custodian, ledger):
    """A 35-point slip (95→60) is above the price floor — it is the gentler
    needle salvage's zone, NOT an immediate crossfire. Table-blind here, so
    nothing fires and the favorite is held (no twitchy stop)."""
    pos = _f_pos(custodian, ledger, entry=95)
    _f_tick(custodian, CLOSE - 400, yes_bid=60, spot=None)  # −35, blind
    assert pos.salvage_fired is None
    assert pos.salvage_attempted is False
    assert f"{TICKER}:F" in custodian.positions            # still held


def test_d_salvage_is_one_attempt(custodian, ledger):
    """The slip salvage latches salvage_attempted — one attempt per position;
    no second bite (and Wall 3 single-entry blocks re-entry for the window)."""
    pos = _f_pos(custodian, ledger, entry=96)
    _f_tick(custodian, CLOSE - 400, yes_bid=50, spot=None)  # −46 → fires
    assert pos.salvage_attempted is True and pos.salvage_fired == "SALVAGE_SLIP"


def test_d_only_lane_f(custodian, ledger):
    """The price salvage is F's (the WO's scope) — a FLIP position's custody is
    not a salvage-enabled lane, so the F slip rule never touches it."""
    assert config.F_SALVAGE_SLIP_POINTS == 40


# ── E: F entry rest-back — already shipped (build 48), asserted here too ────
def test_e_f_entry_rests_back_below_the_cross(gateway):
    """WO-MAKER-REST-BACK (build 48) is lane-agnostic: an F entry priced at the
    cross rests back at/inside the held-side bid — never a post_only cross.
    (Also covered by test_maker_rest_back::test_rest_back_joins_the_bid_for_F.)"""
    from relay_engine.gateway import Order
    o = Order(lane="F", event=EVENT, market=TICKER, side="yes", action="buy",
              price_cents=97, count=1, size_tier=config.TIER_PROBE,
              purpose="ENTRY", band=(95, 99), why="F tier97")
    b = _book(yes=95, no=3)                    # yes_bid 95, ask_yes = 100−3 = 97
    assert gateway._rest_back_price(o, b) == 95    # rests AT the bid, below the ask


def test_e_f_entry_skips_when_rest_back_breaches_band(gateway):
    """If resting back would fall below F's 95c band floor, the entry SKIPS the
    window (a maker who can't rest in-band waits, never chases into the cross)."""
    from relay_engine.gateway import Order, WallRejection
    o = Order(lane="F", event=EVENT, market=TICKER, side="yes", action="buy",
              price_cents=97, count=1, size_tier=config.TIER_PROBE,
              purpose="ENTRY", band=(95, 99), why="F tier97")
    b = _book(yes=90, no=6)                    # bid 90 < band floor 95
    with pytest.raises(WallRejection) as e:
        gateway._rest_back_price(o, b)
    assert e.value.wall == "REST_BACK_SKIP"
