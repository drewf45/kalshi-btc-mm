"""WO-FLIP-EVERY-MARKET-LIQUIDITY (build 49) — BE THE LIQUIDITY, EVERY WINDOW.

FLIP is the market's LIQUIDITY PROVIDER: it buys the cheap side of EVERY biased
open, holds, and sells back to the forced hedgers at the MIDDLE. Three atomic
parts, shipped together:

  B1  ENTER EVERY MARKET — the both-sides-in-OPEN_BAND gate is retired (it
      rejected biased opens). The only entry filter is the cheap side being
      BUYABLE ([OPEN_ENTRY_FLOOR, OPEN_MAX_ENTRY]), the true-50/50 skip (no
      cheap side), and the needle trend-guard (never buy a running market).

  B2  REST TOWARD THE MIDDLE, SCALED BY ENTRY DEPTH — take = clamp(
      MIDDLE_TARGET, entry+MIN, 99). The cheaper the entry, the bigger the
      gouge (buy 39 -> rest 52 = +13; buy 49 -> rest 54 = +5, fee-floored).

  B3  ACTIVE LATE-WINDOW WALK-DOWN (load-bearing) — a position not filled at
      the middle does NOT ride unfilled into a catastrophic bell dump. As the
      clock runs from WALK_START to FLAT_BY, the resting MAKER take steps DOWN
      from the middle toward scratch (linear), re-posting lower — a graceful
      near-breakeven exit late. LATE-window only (past the opening-illiquidity
      window); the early hold is protected by the catastrophe-illiquidity
      guards. Never below scratch — the T-10 handoff owns a genuine loser.

HARD RAIL: no Kelly/cash/rate-halt/F change; the catastrophe-illiquidity guards
(build 48) intact; FLIP_SIZE_CAP == 1."""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0


def _book(yes=None, no=None, yq=10, nq=10):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: yq} if yes is not None else {},
                     {no: nq} if no is not None else {}, ts=1.0)
    return b


def _ctx(book, secs_left=850, grain=None, sl=None):
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


# ── B1: ENTER EVERY MARKET — the both-in-band gate is retired ───────────────
def test_b1_biased_open_enters_the_cheap_side(flip):
    """A biased open (yes 30 / no 65) — the expensive side far out of the old
    band — ENTERS the cheap side (yes@30). The imbalance IS the setup."""
    props = flip.evaluate(TICKER, _ctx(_book(yes=30, no=65)))
    assert [(p.side, p.price_cents, p.purpose) for p in props] == \
        [("yes", 30, "ENTRY")]


def test_b1_enters_the_cheap_no_side_too(flip):
    """Orientation-symmetric: yes 66 / no 34 enters the cheap NO side @34."""
    props = flip.evaluate(TICKER, _ctx(_book(yes=66, no=34)))
    assert [(p.side, p.price_cents, p.purpose) for p in props] == \
        [("no", 34, "ENTRY")]


def test_b1_true_50_50_skips(flip):
    """A true 50/50 (yes == no) has no cheap side to provide against: SKIP."""
    assert flip.evaluate(TICKER, _ctx(_book(yes=47, no=47))) == []


def test_b1_cheap_side_below_floor_skips(flip):
    """A near-worthless cheap side (yes 20, below OPEN_ENTRY_FLOOR 25) is not
    buyable liquidity — providing there is catching a falling knife: SKIP."""
    assert config.OPEN_ENTRY_FLOOR == 25
    assert flip.evaluate(TICKER, _ctx(_book(yes=20, no=79))) == []


def test_b1_two_sided_book_required(flip):
    """No two-sided book (a lone bid, the other side empty) => no cheap side to
    find => nothing posts."""
    assert flip.evaluate(TICKER, _ctx(_book(yes=None, no=34))) == []


def test_b1_size_cap_is_one_lot(flip):
    """HARD RAIL: the liquidity is provided one lot at a time."""
    assert config.FLIP_SIZE_CAP == 1
    props = flip.evaluate(TICKER, _ctx(_book(yes=30, no=65)))
    assert props and all(p.count == 1 for p in props)


# ── B2: REST TOWARD THE MIDDLE, SCALED BY ENTRY DEPTH ───────────────────────
def _take_prop(flip, side, entry, *, secs=700, gap=10, count=1):
    """Inject a booked OPEN position and run one custody cycle so the resting
    TAKE proposes; return the EXIT proposal(s). gap<opening-window keeps the
    walk-down dormant so only the fresh middle take shows."""
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    now = CLOSE - secs
    w.opens[side] = {"entry": entry, "fill_ts": now - gap, "count": count,
                     "take_oid": None, "take_proposed": False,
                     "collapse_polls": 0, "catastrophe_polls": 0,
                     "det_ts": None, "entry_oid": None, "defer_polls": 0}
    other = max(1, min(99, 100 - entry))
    book = _book(yes=entry, no=other) if side == "yes" else _book(yes=other,
                                                                  no=entry)
    props = flip._open_custody(w, TICKER, EVENT, book, _ctx(book, secs), secs,
                               now)
    return [p for p in props if p.purpose == "EXIT"]


def test_b2_cheap_entry_rests_at_the_middle_bigger_gouge(flip):
    """Buy 39 -> rest at the 52c middle: a +13 gouge. The cheaper the entry,
    the bigger the reach to the forced-hedger middle."""
    exits = _take_prop(flip, "yes", 39)
    assert len(exits) == 1
    assert exits[0].price_cents == config.OPEN_MIDDLE_TARGET == 52
    assert exits[0].price_cents - 39 == 13
    assert "middle" in exits[0].reason and "gouge +13" in exits[0].reason


def test_b2_near_middle_entry_is_fee_floored(flip):
    """Buy 49 -> rest at 54 (entry+MIN 5), NOT the 52 middle: a near-middle
    entry is floored fee-safe so it still clears the round-trip fee."""
    exits = _take_prop(flip, "yes", 49)
    assert exits[0].price_cents == 49 + config.OPEN_TAKE_MIN == 54


def test_b2_take_price_is_the_clamp(flip):
    """The pure geometry: clamp(MIDDLE_TARGET, entry+MIN, 99)."""
    assert LaneFlip._take_price(39) == 52          # middle governs
    assert LaneFlip._take_price(49) == 54          # entry+5 floor governs
    assert LaneFlip._take_price(96) == 99          # 99 ceiling governs
    assert LaneFlip._take_price(44) == 52


# ── B3: ACTIVE LATE-WINDOW WALK-DOWN (load-bearing) ─────────────────────────
def _walk_pos(flip, entry=44, *, take_px=52):
    """A position whose middle take is resting UNFILLED, aged well past the
    opening-illiquidity window (the reversion is clearly not coming)."""
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    w.opens["yes"] = {
        "entry": entry, "count": 1, "take_oid": None, "take_proposed": True,
        "take_px": take_px, "collapse_polls": 0, "catastrophe_polls": 0,
        "det_ts": None, "entry_oid": None, "defer_polls": 0, "hold": False,
        "fill_ts": None}
    return w


def _walk_step(flip, w, secs):
    now = CLOSE - secs
    # aged far past the opening window: this is a late, non-reverting hold
    w.opens["yes"]["fill_ts"] = now - (config.OPEN_OPENING_WINDOW_S + 600)
    book = _book(yes=44, no=55)          # held mark 44c: no catastrophe (>20)
    props = flip._open_custody(w, TICKER, EVENT, book, _ctx(book, secs), secs,
                               now)
    return [p for p in props if p.purpose == "EXIT"]


def test_b3_walk_down_marches_the_take_toward_scratch(flip):
    """THE load-bearing proof: an unfilled middle take is NOT left to ride into
    the bell. As the clock runs WALK_START -> FLAT_BY, the resting maker take
    steps DOWN from the middle (52) toward scratch (entry 44), re-posting lower
    each step — a graceful near-breakeven exit, never a catastrophic dump."""
    w = _walk_pos(flip, entry=44, take_px=52)
    seen = []
    for secs in (760, 700, 650, 620, 610):
        exits = _walk_step(flip, w, secs)
        if exits:
            seen.append(exits[0].price_cents)
            assert "walk-down" in exits[0].reason
    # strictly decreasing, from below the middle down toward entry — never up
    assert seen == sorted(seen, reverse=True) and len(seen) >= 3
    assert max(seen) < 52 and min(seen) >= 44          # toward scratch, bounded


def test_b3_never_walks_below_scratch(flip):
    """The walk floors at scratch (entry): a loser below breakeven is the
    T-10 handoff's to clear, never sold below cost by the walk."""
    w = _walk_pos(flip, entry=44, take_px=45)
    # deep in the walk window, near FLAT_BY — the step bottoms at entry
    exits = _walk_step(flip, w, 605)
    for e in exits:
        assert e.price_cents >= 44                      # never below scratch
    # and once the take is already at scratch, the walk re-posts nothing lower
    w.opens["yes"]["take_px"] = 44
    assert _walk_step(flip, w, 605) == []


def test_b3_dormant_for_a_fresh_position(flip):
    """The walk-down is LATE-window management, NOT a reactive early cut: a
    fresh position (inside the opening-illiquidity window) is HELD — the walk
    does not fire, the catastrophe-illiquidity guards protect the hold."""
    w = _walk_pos(flip, entry=44, take_px=52)
    now = CLOSE - 700
    w.opens["yes"]["fill_ts"] = now - 10               # 10s old: fresh
    book = _book(yes=44, no=55)
    props = flip._open_custody(w, TICKER, EVENT, book, _ctx(book, 700), 700,
                               now)
    assert [p for p in props if p.purpose == "EXIT"] == []


def test_b3_dormant_outside_the_walk_window(flip):
    """Before WALK_START (early in the window) the walk is dormant — the
    middle take rests untouched; only late does it step down."""
    w = _walk_pos(flip, entry=44, take_px=52)
    # secs above WALK_START: outside the walk window
    exits = _walk_step(flip, w, config.OPEN_WALK_START_S + 20)
    assert exits == []


def test_b3_config_schedule():
    """The walk schedule runs from WALK_START down to the T-10 handoff floor."""
    assert config.OPEN_WALK_START_S > config.OPEN_FLAT_BY
    assert config.OPEN_MIDDLE_TARGET == 52
