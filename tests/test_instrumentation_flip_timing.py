"""WO-INSTRUMENTATION-AND-FLIP-TIMING (build 51) — every place a data point.

  A  RICH PER-TRADE TAGGING — every FLIP conclusion writes the COMPLETE data
     point: entry/exit spot PRICES, captured spread, precise window timing
     (secs-into at entry AND exit), the exact exit reason tag, and book state
     both ends — so a 25-hour run is reconstructable and losses diagnosable.
  B  THE MONEY CURVE — fill-rate-by-posted-price: of the takes posted at each
     gouge level, what % filled. (Pack trigger robustified to fire every day.)
  C  FLIP 90-SECOND ENTRY CUTOFF — the proven timing fix: FLIP wins buying the
     opening pile-in (≤90s into the window) and loses wandering in mid-market;
     entry is hard-cut at OPEN_OPENING_WINDOW_S into the window.
"""

import json
import pytest

from relay_engine import config, failures, ops
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip, FLIP_WINDOW_SEC

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
GRAIN = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=48, no=49, yq=10, nq=10):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: yq} if yes is not None else {},
                     {no: nq} if no is not None else {}, ts=1.0)
    return b


def _ctx(book, secs_left, spot=None, grain=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": None}


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


def _swing_row(ledger):
    (d,) = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='FLIP_SWING'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(d)


# ── C: THE 90-SECOND ENTRY CUTOFF ──────────────────────────────────────────
def test_c_enters_in_the_opening_window(flip):
    """FLIP enters buying the opening pile-in — secs_into 50 (<= 90)."""
    props = flip.evaluate(TICKER, _ctx(_book(), secs_left=850, grain=GRAIN))
    assert [(p.side, p.purpose) for p in props] == [("yes", "ENTRY")]


def test_c_refuses_past_the_opening_window(flip):
    """Past OPEN_OPENING_WINDOW_S into the window (secs_into 100 > 90), FLIP
    does NOT enter — the −15/−16 mid-market class is retired."""
    assert flip.evaluate(TICKER, _ctx(_book(), secs_left=800,
                                      grain=GRAIN)) == []


def test_c_cutoff_uses_secs_into_not_secs_left(flip):
    """ADVERSARY guard (b): the cutoff is secs-INTO = FLIP_WINDOW_SEC − secs,
    NOT secs-left — a sign error would invert it (refuse the open, admit
    mid-market). At the boundary: 90s in enters, 91s in refuses."""
    boundary = FLIP_WINDOW_SEC - config.OPEN_OPENING_WINDOW_S     # 810 s-left
    assert flip.evaluate(TICKER, _ctx(_book(), secs_left=boundary,
                                      grain=GRAIN))                # 90s in: enters
    flip.windows.clear()
    assert flip.evaluate(TICKER, _ctx(_book(), secs_left=boundary - 1,
                                      grain=GRAIN)) == []          # 91s in: refused
    flip.windows.clear()
    assert flip.evaluate(TICKER, _ctx(_book(), secs_left=FLIP_WINDOW_SEC - 5,
                                      grain=GRAIN))                # 5s in: enters


def test_c_still_requires_a_two_sided_cheap_side(flip):
    """ADVERSARY guard (a): even in the opening window, FLIP never fires into a
    one-sided book — no cheap side, skip."""
    assert flip.evaluate(TICKER, _ctx(_book(yes=48, no=None),
                                      secs_left=850, grain=GRAIN)) == []


# ── A: THE COMPLETE PER-TRADE DATA POINT ───────────────────────────────────
def _run_trade(flip, ledger, entry_spot=66_400, exit_spot=66_455):
    """Enter at the open (spot known), fill, post the take, then book a
    take-fill exit — returns after the FLIP_SWING record is written."""
    props = flip.evaluate(TICKER, _ctx(_book(yes=48, no=49), secs_left=850,
                                       spot=entry_spot, grain=GRAIN))
    flip.on_submitted(props[0], "E1", CLOSE - 850)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 48, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 48, CLOSE - 848)
    # a custody poll posts the take (middle 53 for a 48c entry) and stamps the
    # exit observation (spot/book)
    flip.evaluate(TICKER, _ctx(_book(yes=53, no=45), secs_left=790,
                               spot=exit_spot))
    # the take fills at its posted price → note_exit writes the record
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 53, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 53, CLOSE - 700, count=1)


def test_a_swing_record_is_the_full_data_point(flip, ledger):
    """The forensic record: entry/exit SPOT prices, captured spread, secs-into
    at entry AND exit, the reason tag, and book depth both ends — a trade
    reconstructable after the fact."""
    _run_trade(flip, ledger, entry_spot=66_400, exit_spot=66_455)
    r = _swing_row(ledger)
    # the money
    assert r["entry_price"] == 48 and r["exit_price"] == 53
    assert r["gross_cents"] == 5
    # spot prices — the BTC move, reconstructable (not just the delta)
    assert r["entry_spot"] == 66_400 and r["exit_spot"] == 66_455
    # precise window timing — the ≤90s-vs-mid distinction
    assert r["entry_secs_into"] == 50.0        # 900 − 850
    assert r["exit_secs_into"] == 110.0        # 900 − 790
    # the exact exit reason tag — no silent exit
    assert r["exit_reason"] == "TAKE_FILL"
    # book state both ends + the posted gouge level
    assert r["entry_spread"] == 1 and r["entry_cheap_bid"] == 48
    assert r["entry_depth"] == [10, 10]
    assert r["posted_take"] == 53              # _take_price(48) = max(52, 48+5)


def test_a_entry_why_carries_spot_timing_and_book(flip):
    """The Telegram-readable entry line carries spot, secs-into, and book —
    enough to read the trade live."""
    props = flip.evaluate(TICKER, _ctx(_book(yes=48, no=49), secs_left=850,
                                       spot=66_412, grain=GRAIN))
    why = props[0].why
    assert "spot 66,412" in why and "into 50s" in why
    assert "book y48/n49" in why and "depth 10/10" in why


def test_a_walk_down_exit_is_tagged(flip, ledger):
    """A walked-down exit records exit_reason WALK_DOWN — the exact reason, not
    a silent exit."""
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {
        "entry": 44, "count": 1, "take_oid": None, "take_proposed": True,
        "take_px": 52, "collapse_polls": 0, "catastrophe_polls": 0,
        "det_ts": None, "entry_oid": None, "defer_polls": 0, "hold": False,
        "fill_ts": CLOSE - 850 - 400, "entry_meta": {"spot": 1, "secs_into": 50}}
    # a poll in the walk window steps the take down and tags WALK_DOWN
    flip._open_custody(w, TICKER, EVENT, _book(yes=44, no=55),
                       _ctx(_book(yes=44, no=55), 500), 500, CLOSE - 500)
    assert w.opens["yes"]["exit_reason"] == "WALK_DOWN"


# ── B: THE MONEY CURVE ─────────────────────────────────────────────────────
def _swing(ledger, surface, posted, reason, market=TICKER):
    surface.write_row("FLIP", market, f"w-{market}", "FLIP_SWING",
                      detail=json.dumps({"posted_take": posted,
                                         "exit_reason": reason,
                                         "took_swing": reason == "TAKE_FILL"}))


def test_b_fill_rate_by_price_curve(flip, ledger, surface):
    """Of the takes posted at each price, what % filled (TAKE_FILL) vs
    walked-down/cut. The curve that reveals the optimal gouge."""
    # posted 52: 2 of 4 filled; posted 50: 3 of 3 filled — distinct markets so
    # the surface dedup (per lane/market/state) does not collapse the rows
    for i, reason in enumerate(["TAKE_FILL", "TAKE_FILL", "WALK_DOWN",
                                "HANDOFF_LOSER"]):
        _swing(ledger, surface, 52, reason, market=f"{TICKER}-A{i}")
    for i in range(3):
        _swing(ledger, surface, 50, "TAKE_FILL", market=f"{TICKER}-B{i}")
    curve = ops.flip_fill_rate_by_price(ledger)
    assert "50c:100%(3/3)" in curve and "52c:50%(2/4)" in curve


def test_b_pack_includes_the_money_curve(flip, ledger, surface):
    """The daily pack surfaces the fill-rate-by-price curve."""
    from relay_engine.ledger import CashProtocol
    _swing(ledger, surface, 52, "TAKE_FILL")
    pack = ops.daily_pack(ledger, surface, CashProtocol(ledger))
    assert "FILL-RATE BY POSTED PRICE" in pack


def test_b_empty_curve_is_graceful(ledger):
    assert "no concluded takes" in ops.flip_fill_rate_by_price(ledger)
