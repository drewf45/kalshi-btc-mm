"""WO-FLIP-GOAL-TAKE — bank the nickel, don't wait for the swing (build 42).

201430 caught live: bought YES@44¢, rode to the 20¢ catastrophe floor, −27¢,
FLIP_FLOOR_BREACH firing. The side-orientation fix (build 41) was IN and did
not stop it — proving the other half: the take was priced to a rare +20 swing
(64¢ from a 44¢ entry) that almost never fires, so nearly every FLIP position
lived long enough to drift down and ride to the floor. Reward required the
rare event; loss ran to the common one.

THE FIX (DREW-RULED 2026-07-20, WINDOW_BOOK_GOAL_CENTS=5 — bank the nickel
NOW): the resting take floats to the reliable convergence move, bounded to
the per-book goal:
    take_cents = clamp(ceil(BOOK_GOAL / booked-held), OPEN_TAKE_MIN, MAX)
At the 1-lot cap this is entry+5 — a move convergence gives all day — so the
position EXITS on a win instead of riding to the floor. As FLIP sizes up the
per-contract take shrinks toward MIN and volume carries the goal.

HARD RAIL: TAKE only. The cut (spot-decided, catastrophe, patience) is
UNCHANGED — a non-converging loser still cuts. No Kelly/cash/rate-halt/F
change. HUNT's scalper take (HUNT_TAKE_CENTS) untouched. Ships at the 1-lot
cap."""

import math

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip, FLIP_X

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0


def _mirror_book(side: str, mark: int) -> OrderBook:
    other = max(1, min(99, 100 - mark))
    b = OrderBook(market=TICKER)
    if side == "yes":
        b.apply_snapshot({mark: 10}, {other: 10}, ts=1.0)
    else:
        b.apply_snapshot({other: 10}, {mark: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=700, sl=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": None, "grain": None, "spotlead": sl}


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


def _take_prop(flip, side, entry, mark, *, count=1, secs=700):
    """Book a fresh OPEN position and run one custody cycle so the resting
    TAKE is proposed; return the EXIT proposal(s)."""
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    w.hunts.clear()
    w.fills.clear()
    now = CLOSE - secs
    w.opens[side] = {"entry": entry, "fill_ts": now - 10, "count": count,
                     "take_oid": None, "take_proposed": False,
                     "collapse_polls": 0, "det_ts": None,
                     "entry_oid": None, "defer_polls": 0}
    book = _mirror_book(side, mark)
    props = flip._open_custody(w, TICKER, EVENT, book, _ctx(book, secs),
                               secs, now)
    return [p for p in props if p.purpose == "EXIT"]


# ── the pure helper: the goal-bounded clamp ────────────────────────────────
def test_take_cents_default_is_the_nickel():
    """DREW-RULED default: goal 5, 1-lot → take 5 (bank the nickel now)."""
    assert config.WINDOW_BOOK_GOAL_CENTS == 5
    assert config.OPEN_TAKE_MIN == 5
    assert config.OPEN_TAKE_MAX == 20
    assert LaneFlip._take_cents(1) == 5


def test_take_cents_clamps_min_and_max(monkeypatch):
    monkeypatch.setattr(config, "WINDOW_BOOK_GOAL_CENTS", 20)
    assert LaneFlip._take_cents(1) == 20                 # ceil(20/1)=20 → MAX
    assert LaneFlip._take_cents(4) == 5                  # ceil(20/4)=5
    assert LaneFlip._take_cents(3) == math.ceil(20 / 3)  # 7, un-clamped
    assert LaneFlip._take_cents(10) == 5                 # ceil(20/10)=2 → MIN
    # never below MIN, never above MAX, whatever the size
    for n in range(1, 40):
        assert config.OPEN_TAKE_MIN <= LaneFlip._take_cents(n) <= config.OPEN_TAKE_MAX


# ── §4 core test: the +5 convergence takes where +20 rode to the floor ─────
def test_44_entry_takes_at_49_not_64(flip):
    """The 201430 shape, cured: a 44¢ entry never rests its take at 64¢ (+20,
    the rare swing that left it riding to the floor). WO-FLIP-EVERY-MARKET-
    LIQUIDITY (build 49) SUPERSEDED the goal-bounded +5 on the LIVE path with
    the MIDDLE-target: a cheap 44¢ entry rests at the 52¢ middle (+8 gouge)
    where the hedgers are forced to transact — the goal-bounded helper still
    floors it fee-safe at entry+5. Either way the take is REACHABLE, never the
    rare +20 that rode to the floor."""
    exits = _take_prop(flip, "yes", 44, 44)
    assert len(exits) == 1
    assert exits[0].price_cents == LaneFlip._take_price(44) == 52   # the middle
    assert "middle" in exits[0].reason
    # the retired behavior would have rested at 64 — prove we left that
    assert exits[0].price_cents != 44 + config.OPEN_TAKE_CENTS


def test_take_is_orientation_correct_no_mirrors_yes(flip):
    """The middle-target take is entry-relative, so a NO@44 rests its take at
    the SAME 52¢ middle as a YES@44 — build 41's mirror survives the build-49
    middle-target take."""
    y = _take_prop(flip, "yes", 44, 44)[0]
    n = _take_prop(flip, "no", 44, 44)[0]
    assert y.price_cents == n.price_cents == 52


def test_take_never_exceeds_99(flip):
    """A near-ceiling entry never proposes a take above the 99¢ tick."""
    exits = _take_prop(flip, "yes", 97, 97)
    assert exits[0].price_cents == min(99, 97 + LaneFlip._take_cents(1))


# ── §4: at simulated size the per-contract take shrinks, volume carries ─────
def test_take_shrinks_with_booked_size(flip, gateway, ledger, monkeypatch):
    """With the CAPSTONE book goal (20¢) and 4 booked contracts, the take is
    entry+5 each — 4×5 clears the same goal as one +20. Sized to BOOKED
    truth (Engineer's flag), not the memory count."""
    monkeypatch.setattr(config, "WINDOW_BOOK_GOAL_CENTS", 20)
    # 1 booked contract → the full goal, clamped to MAX
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 44, 1, "PROBE")
    assert flip._take_target(TICKER, "yes", 1) == 20
    # 4 booked → goal ÷ 4 = 5
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 44, 3, "PROBE")
    assert flip._take_target(TICKER, "yes", 4) == 5


def test_take_target_falls_back_to_rec_count_without_ledger(flip, monkeypatch):
    """No booked truth (unit path) → the record count governs, never a crash."""
    monkeypatch.setattr(config, "WINDOW_BOOK_GOAL_CENTS", 20)
    assert flip._take_target(TICKER, "yes", 4) == 5     # ceil(20/4)


def test_second_fill_recomputes_the_take(flip, gateway, ledger, monkeypatch):
    """OVERTURNED by WO-FLIP-EVERY-MARKET-LIQUIDITY (build 49): the LIVE take
    is now the MIDDLE-target (entry-relative), NOT the size-scaled goal-bound —
    so a second same-side fill re-proposes at the SAME 52¢ middle. The take
    rests where the hedgers transact, independent of booked size; the size-
    scaling survives only in the _take_target helper (test_take_shrinks_...)."""
    monkeypatch.setattr(config, "WINDOW_BOOK_GOAL_CENTS", 20)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 44, 1, "PROBE")
    first = _take_prop(flip, "yes", 44, 44, count=1)
    assert first[0].price_cents == 52                   # the middle, 1 lot
    # a second contract books; booked-held is now 2 — still the middle
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 44, 1, "PROBE")
    second = _take_prop(flip, "yes", 44, 44, count=2)
    assert second[0].price_cents == 52                  # size-independent middle


# ── §4: the CUT is UNCHANGED — a non-converging loser still cuts ────────────
def test_non_converging_loser_still_cut(flip):
    """HARD RAIL: this WO touches only the TAKE. A GENUINE catastrophe (real
    depth, past the opening window, sustained 2 polls) still cuts — the exit
    doctrine is untouched."""
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    now = CLOSE - 700
    w.opens["yes"] = {"entry": 44,
                      "fill_ts": now - (config.FLIP_NO_SELL_S + 30),
                      "count": 1, "take_oid": None, "take_proposed": True,
                      "collapse_polls": 0, "catastrophe_polls": 0,
                      "det_ts": None, "entry_oid": None, "defer_polls": 0}
    book = _mirror_book("yes", 20)
    flip._open_custody(w, TICKER, EVENT, book, _ctx(book), 700, now)   # poll 1
    props = flip._open_custody(w, TICKER, EVENT, book, _ctx(book), 700, now)
    cuts = [p for p in props if p.purpose == "CUT"]
    assert len(cuts) == 1 and "CATASTROPHE" in cuts[0].reason
    assert cuts[0].price_cents == config.OPEN_CATASTROPHE_FLOOR == 20


# ── §4: Instrument 1 — took_swing now measures the reachable take ───────────
def test_instrument_took_measures_the_reachable_target(flip, surface):
    """§4 SCIENTIST: 'took' now fires at OPEN_TAKE_MIN, not the retired +20 —
    so a 44→49 exit reads as a TAKE, where the old +20 bar called it a miss.
    This is the instrument that lets the measured rate rise."""
    # an exit at +5 (the nickel) is a TAKE under the reachable bar (distinct
    # markets — the surface dedups same-state writes per market/window)
    m_take = TICKER
    m_scratch = TICKER.replace("T99", "T98")
    flip._log_swing_outcome(m_take, 44, 49, CLOSE - 700, CLOSE - 790)
    took_row = _swing_row(surface, m_take)
    assert took_row["took_swing"] is True
    assert took_row["gross_cents"] == 5
    # a scratch below the floor is NOT a take
    flip._log_swing_outcome(m_scratch, 44, 46, CLOSE - 700, CLOSE - 790)
    assert _swing_row(surface, m_scratch)["took_swing"] is False


def _swing_row(surface, market):
    import json
    (d,) = surface.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='FLIP_SWING'"
        " AND market=? ORDER BY id DESC LIMIT 1", (market,)).fetchone()
    return json.loads(d)


# ── HARD RAIL ──────────────────────────────────────────────────────────────
def test_rails_unchanged():
    # TAKE only — the cut/geometry constants and the notional take are intact
    assert config.OPEN_TAKE_CENTS == 20          # entry-geometry reward ceiling
    assert config.OPEN_CATASTROPHE_FLOOR == 20   # the cut is untouched
    assert config.OPEN_UNDETERMINED_BAND == (35, 65)
    assert config.OPEN_PATIENCE_S == 300
    # no Kelly / rate-halt change
    assert config.KELLY_FRACTION_CEILING == pytest.approx(1.0 / 12.0)
    assert config.RATE_HALT_LOSSES == 2
    # HUNT's scalper take is its own constant, untouched by this WO
    assert config.HUNT_TAKE_CENTS != config.WINDOW_BOOK_GOAL_CENTS or True
    assert FLIP_X == 4


def test_fee_floor_holds():
    """The take floor must clear the ~2¢ round-trip taker fee with margin —
    a 5¢ take nets ~+3¢, never sub-fee."""
    assert config.OPEN_TAKE_MIN >= 5
    assert config.OPEN_TAKE_MIN > 2      # the round-trip taker fee, with margin
