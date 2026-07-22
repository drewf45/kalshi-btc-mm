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


def _book(yes=40, no=49, yq=10, nq=10):
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
    """FLIP enters buying the opening pile-in — secs_into 50 (<= 90). The
    FAVORED (higher-priced) side in band [50,70] is what it buys (yes@60)."""
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=850,
                                       grain=GRAIN))
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
    fav = _book(yes=60, no=40)                                    # favored yes@60
    assert flip.evaluate(TICKER, _ctx(fav, secs_left=boundary,
                                      grain=GRAIN))                # 90s in: enters
    flip.windows.clear()
    assert flip.evaluate(TICKER, _ctx(_book(yes=60, no=40),
                                      secs_left=boundary - 1,
                                      grain=GRAIN)) == []          # 91s in: refused
    flip.windows.clear()
    assert flip.evaluate(TICKER, _ctx(_book(yes=60, no=40),
                                      secs_left=FLIP_WINDOW_SEC - 5,
                                      grain=GRAIN))                # 5s in: enters


def test_c_still_requires_a_two_sided_cheap_side(flip):
    """ADVERSARY guard (a): even in the opening window, FLIP never fires into a
    one-sided book — no cheap side, skip."""
    assert flip.evaluate(TICKER, _ctx(_book(yes=48, no=None),
                                      secs_left=850, grain=GRAIN)) == []


# ── A: THE COMPLETE PER-TRADE DATA POINT ───────────────────────────────────
def _run_trade(flip, ledger, entry_spot=66_400, exit_spot=66_455):
    """Enter at the open (spot known) on the FAVORED side (yes@60, in band),
    fill, post the take, then book a take-fill exit — returns after the
    FLIP_SWING record is written."""
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=850,
                                       spot=entry_spot, grain=GRAIN))
    flip.on_submitted(props[0], "E1", CLOSE - 850)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 60, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 60, CLOSE - 848)
    # a custody poll posts the take (entry+20 = 80 for a 60c entry) and stamps
    # the exit observation (spot/book)
    flip.evaluate(TICKER, _ctx(_book(yes=52, no=45), secs_left=790,
                               spot=exit_spot))
    # the take fills at its posted price → note_exit writes the record
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 80, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 80, CLOSE - 700, count=1)


def test_a_swing_record_is_the_full_data_point(flip, ledger):
    """The forensic record: entry/exit SPOT prices, captured spread, secs-into
    at entry AND exit, the reason tag, and book depth both ends — a trade
    reconstructable after the fact."""
    _run_trade(flip, ledger, entry_spot=66_400, exit_spot=66_455)
    r = _swing_row(ledger)
    # the money — favored yes@60, take +20 = 80
    assert r["entry_price"] == 60 and r["exit_price"] == 80
    assert r["gross_cents"] == 20
    # spot prices — the BTC move, reconstructable (not just the delta)
    assert r["entry_spot"] == 66_400 and r["exit_spot"] == 66_455
    # precise window timing — the ≤90s-vs-mid distinction
    assert r["entry_secs_into"] == 50.0        # 900 − 850
    assert r["exit_secs_into"] == 110.0        # 900 − 790
    # the exact exit reason tag — no silent exit
    assert r["exit_reason"] == "TAKE_FILL"
    # book state both ends + the posted gouge level (favored bid recorded)
    assert r["entry_spread"] == 20 and r["entry_cheap_bid"] == 60
    assert r["entry_depth"] == [10, 10]
    assert r["posted_take"] == 80              # _take_price(60) = entry+20 cap90


def test_a_entry_why_carries_spot_timing_and_book(flip):
    """The Telegram-readable entry line carries spot, secs-into, and book —
    enough to read the trade live."""
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=850,
                                       spot=66_412, grain=GRAIN))
    why = props[0].why
    assert "spot 66,412" in why and "into 50s" in why
    assert "book y60/n40" in why and "depth 10/10" in why


def test_a_momentum_stop_exit_is_tagged(flip, ledger):
    """WO-2026-07-22-E: WALK_DOWN is RETIRED. An adverse move on the favored
    side now exits via the MOMENTUM STOP (entry−10, sustained 2 polls) and
    records exit_reason MOMENTUM_STOP — the exact reason, not a silent exit,
    carried onto the FLIP_SWING record."""
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {
        "entry": 60, "count": 1, "take_oid": None, "take_proposed": True,
        "take_px": 80, "collapse_polls": 0, "catastrophe_polls": 0,
        "det_ts": None, "entry_oid": None, "defer_polls": 0, "hold": False,
        "fill_ts": CLOSE - 850, "entry_meta": {"spot": 1, "secs_into": 50}}
    # mark 50 <= stop_px (entry−10 = 50); one poll arms, two polls fire the stop
    for _ in range(2):
        flip._open_custody(w, TICKER, EVENT, _book(yes=50, no=45),
                           _ctx(_book(yes=50, no=45), 500), 500, CLOSE - 500)
    assert w.opens["yes"]["exit_reason"] == "MOMENTUM_STOP"
    # and the tag lands on the FLIP_SWING forensic record at conclusion
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 50, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 50, CLOSE - 500, count=1)
    assert _swing_row(ledger)["exit_reason"] == "MOMENTUM_STOP"


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
