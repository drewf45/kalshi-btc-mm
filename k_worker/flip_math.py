"""k_worker/flip_math.py — WO-LANE-FLIP complement translation (crypto-free, pinned).

The proven engine buys only; every flip intent is expressed through place_order_maker
(kalshi.py:668) as a YES-leg bid/ask. Kalshi nets complements to flat, so an EXIT is a
complement BUY. These pure functions encode the four intents so the math is unit-tested
without importing the (cryptography-bound) client.

    intent                      place_order_maker(side, price_cents)   → V2 (side, yes¢)
    entry  buy  YES @p          ('yes', p)                              bid  p
    entry  buy  NO  @p          ('no',  p)                              ask  100-p
    exit   held YES @q          ('no',  100-q)  (buy NO@100-q)          ask  q
    exit   held NO  @q          ('yes', 100-q)  (buy YES@100-q)         bid  100-q
"""

from typing import Optional, Tuple


def entry_args(leg_side: str, price_cents: int) -> Tuple[str, int]:
    """place_order_maker args to BUY (enter) the given leg at price_cents."""
    if leg_side not in ("yes", "no"):
        raise ValueError(f"leg_side must be yes/no, got {leg_side}")
    return leg_side, int(price_cents)


def exit_args(held_side: str, exit_price_cents: int) -> Tuple[str, int]:
    """place_order_maker args to FLATTEN a held leg at exit_price_cents, via the
    complement buy (the engine has no sell/taker path — buying the complement nets flat)."""
    if held_side not in ("yes", "no"):
        raise ValueError(f"held_side must be yes/no, got {held_side}")
    complement = "no" if held_side == "yes" else "yes"
    return complement, 100 - int(exit_price_cents)


def v2_of(side: str, price_cents: int) -> Tuple[str, int]:
    """Mirror place_order_maker's V2 mapping: side 'yes' -> bid at price; side 'no' ->
    ask YES at (100 - price). Returns (v2_side, yes_terms_price_cents)."""
    if side == "yes":
        return "bid", int(price_cents)
    return "ask", 100 - int(price_cents)


def bundle_cost(yes_bid: int, no_bid: int) -> int:
    """Combined cost of joining both best bids — the FLIP entry bundle (≤ FLIP_LINE)."""
    return int(yes_bid) + int(no_bid)


def mode_enabled(raw: Optional[str]) -> bool:
    """F-2: this branch's k_worker flips BY DEFAULT (no env needed). KW_MODE=OFF is the
    one-variable kill switch (engine up, flip quiet). Anything else (incl. unset) runs."""
    return (raw or "FLIP").strip().upper() != "OFF"
