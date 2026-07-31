"""True-touch fp resting — the parts-comparison gap, closed.

The parts rest orders at the EXACT touch: `place_order_maker` sends
`v2_price_str` (subpenny fp-dollars) so a resting order joins the true level,
not the floored int cent. This suite proves the fp string survives the whole
spine: wire frame -> Feed -> OrderBook -> touch_view -> favorite_side ->
EvalResult.rest_fp -> Order.rest_fp. Cents-int books (no fp on the wire)
yield None at every stage — never a fabricated string.
"""

import json

from relay_engine import lane_fh8
from relay_engine.book import OrderBook, touch_view
from relay_engine.feed import DegradeLadder, Feed
from relay_engine.lanes import FH8Shared

TICKER = "KXBTC15M-26JUL1815-T118249.99"


def snapshot(yes, no):
    return json.dumps({"type": "orderbook_snapshot",
                       "msg": {"market_ticker": TICKER, "yes": yes, "no": no}})


def delta(side, price, d):
    return json.dumps({"type": "orderbook_delta",
                       "msg": {"market_ticker": TICKER, "side": side,
                               "price": price, "delta": d}})


# ── wire -> book -> touch_view ──────────────────────────────────────────────
def test_dollars_snapshot_retains_fp():
    feed = Feed(DegradeLadder())
    feed.handle_frame(snapshot([["0.4550", "100.00"]], [["0.3025", "50.00"]]),
                      now=1.0)
    book = feed.books[TICKER]
    assert book.yes_bids == {45: 100}          # floored int view unchanged
    assert book.best_fp("yes") == "0.4550"     # exact touch preserved
    assert book.best_fp("no") == "0.3025"
    tb = touch_view(book)
    assert tb.yes_bid == 45 and tb.yes_bid_fp == "0.4550"
    assert tb.no_bid == 30 and tb.no_bid_fp == "0.3025"


def test_delta_moves_fp_with_the_touch():
    feed = Feed(DegradeLadder())
    feed.handle_frame(snapshot([["0.4550", "100.00"]], [["0.3025", "50.00"]]),
                      now=1.0)
    # a new better yes level arrives with its own fp string
    feed.handle_frame(delta("yes", "0.4675", "20.00"), now=2.0)
    book = feed.books[TICKER]
    assert book.best_yes_bid() == 46
    assert book.best_fp("yes") == "0.4675"
    # the level empties -> its fp is dropped, touch falls back to the old fp
    feed.handle_frame(delta("yes", "0.4675", "-20.00"), now=3.0)
    assert book.best_yes_bid() == 45
    assert book.best_fp("yes") == "0.4550"
    assert 46 not in book.yes_fp


def test_cents_int_book_yields_none_never_a_guess():
    feed = Feed(DegradeLadder())
    feed.handle_frame(snapshot([[45, 100]], [[30, 50]]), now=1.0)
    book = feed.books[TICKER]
    assert book.best_fp("yes") is None and book.best_fp("no") is None
    tb = touch_view(book)
    assert tb.yes_bid_fp is None and tb.no_bid_fp is None


def test_empty_book_best_fp_none():
    assert OrderBook(market=TICKER).best_fp("yes") is None


# ── touch_view -> favorite_side: subpenny decides ties ──────────────────────
def test_favorite_side_decides_on_subpenny():
    # both sides floor to 45c; the fp strings differ below the penny — the
    # true favorite is the yes side (45.50 > 45.40), and its fp rides along.
    tb = lane_fh8.TouchBook(yes_bid=45, no_bid=45,
                            yes_bid_fp="0.4550", no_bid_fp="0.4540")
    side, cost_d, yes_quote, fp = lane_fh8.favorite_side(tb)
    assert side == "yes"
    assert float(cost_d) == 45.50
    assert fp == "0.4550"


# ── evaluate -> EvalResult -> Order ─────────────────────────────────────────
def test_eval_result_and_order_carry_rest_fp():
    feed = Feed(DegradeLadder())
    feed.handle_frame(snapshot([["0.9750", "10.00"]], [["0.0150", "10.00"]]),
                      now=1.0)
    tb = touch_view(feed.books[TICKER])
    res = lane_fh8.evaluate(TICKER, tb, secs_to_expiry=120.0, cash_usd=100.0,
                            state=lane_fh8.FH8State(), stats=lane_fh8.FH8Stats())
    assert res.side == "yes" and res.cost_cents == 97
    assert res.rest_fp == "0.9750"
    order = FH8Shared().to_order(TICKER, res)
    assert order.rest_fp == "0.9750"
    assert order.price_cents == 97


def test_order_rest_fp_none_on_int_book():
    feed = Feed(DegradeLadder())
    feed.handle_frame(snapshot([[97, 10]], [[2, 10]]), now=1.0)
    tb = touch_view(feed.books[TICKER])
    res = lane_fh8.evaluate(TICKER, tb, secs_to_expiry=120.0, cash_usd=100.0,
                            state=lane_fh8.FH8State(), stats=lane_fh8.FH8Stats())
    assert res.rest_fp is None
    assert FH8Shared().to_order(TICKER, res).rest_fp is None
