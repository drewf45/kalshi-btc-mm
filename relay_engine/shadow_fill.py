"""WO-2026-07-25-L §P1 — THE PESSIMISTIC SHADOW FILL MODEL (Scientist owns it).

A shadow lane rehearses at full speed but places nothing; to earn its way back
to live (the P3 protocol) it must show conversion under a fill model that CANNOT
flatter it. An optimistic shadow fill is the instrument lying — the desk would
buy its promotion with fantasy fills. So the rule is deliberately pessimistic and
its assumptions print in the pack:

  A resting maker order fills ONLY when the tape shows the book trading AT or
  THROUGH its price, AFTER it has rested at least one poll. Being *at* the touch
  is not a fill; the market must actually reach the price.

Derived from best-bid book samples (the only reliable observable — there is no
trade-print feed; book_snapshots stores raw frames, book.py parses them):

  BUY  yes @P  fills iff later  no_bid >= 100 - P   (the yes-ask 100-no_bid <= P)
  BUY  no  @P  fills iff later  yes_bid >= 100 - P
  SELL yes @P  fills iff later  yes_bid >= P        (a buyer lifts our offer)
  SELL no  @P  fills iff later  no_bid  >= P

An order that never sees its through-price before the window ends is
EXPIRED-UNFILLED — logged, never silently assumed filled.
"""

import logging
from typing import Optional

log = logging.getLogger("relay.shadow_fill")

# The pessimistic model's assumptions, printed in the daily pack (Scientist's
# load-bearing honesty — the reader sees exactly what "shadow filled" means).
MODEL_STATEMENT = (
    "SHADOW FILL MODEL (pessimistic): a maker order fills only when the book "
    "trades AT or THROUGH its price after ≥1 poll of rest (buy fills when the "
    "opposing bid reaches 100−price; sell fills when its own bid reaches price). "
    "At-the-touch is not a fill; unfilled by window end = expired. No optimism, "
    "no mid-fills — shadow conversion is never flattered."
)


def _through_price(action: str, side: str, price_cents: int,
                   yes_bid: Optional[int], no_bid: Optional[int]) -> bool:
    """Did the book trade AT or THROUGH a resting maker order's price? Pure and
    pessimistic; None on either bid → no evidence → not filled."""
    if action == "buy":
        opp = no_bid if side == "yes" else yes_bid
        return opp is not None and opp >= 100 - price_cents
    # sell (maker exit): our own side's bid must reach the offer
    own = yes_bid if side == "yes" else no_bid
    return own is not None and own >= price_cents


def would_fill(order, book, rested_polls: int) -> bool:
    """The fill decision for one resting SHADOW order against the CURRENT book.
    Pessimistic on every axis: needs ≥1 poll of rest AND the through-price."""
    if rested_polls < 1:                       # must rest before it can fill
        return False
    if book is None:
        return False
    yb = book.best_yes_bid()
    nb = book.best_no_bid()
    return _through_price(order.action, order.side, int(order.price_cents),
                          yb, nb)
