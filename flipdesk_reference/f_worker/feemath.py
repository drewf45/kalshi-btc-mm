# f_worker/feemath.py
# W8: fee arithmetic uses EXACT per-order roundup. Any exit that would cross the
# spread prices its taker fee BEFORE deciding.
#
# Kalshi's published trading-fee formula (general markets):
#     fee = roundup( rate * C * P * (1 - P) )   , P in dollars, result in dollars
# rounded UP to the next whole cent, PER ORDER (not per contract). We work in cents.
#
# Pinned truth (rate=0.07): a single contract at 50c => 0.07*1*0.5*0.5 = $0.0175
# => rounds up to 2c. That matches Kalshi's own worked example, so it anchors the
# pinned tests. Maker (resting, non-crossing) orders on these markets pay 0 fee.

import math

# Kalshi general fee rate. Kept as a module constant so the pinned tests lock it.
FEE_RATE = 0.07


def fee_cents(price_cents: int, count: int, rate: float = FEE_RATE) -> int:
    """Exact per-order taker fee in cents, rounded UP to the whole cent.

    price_cents: fill/limit price of the contract in cents (1..99).
    count: number of contracts in the order.
    """
    if count <= 0:
        return 0
    p = max(0.0, min(1.0, price_cents / 100.0))
    raw_dollars = rate * count * p * (1.0 - p)
    raw_cents = raw_dollars * 100.0
    # Round UP to the next whole cent. Guard tiny FP dust so 1.9999999 -> 2 not 3.
    return int(math.ceil(round(raw_cents, 9)))


def bundle_cost_cents(yes_price: int, no_price: int) -> int:
    """Combined cost of a YES leg + NO leg (the bundle). W1 compares this to entry_line."""
    return int(yes_price) + int(no_price)


def entry_ok(yes_price: int, no_price: int, entry_line: int) -> bool:
    """W1 predicate: a completed bundle is legal only if combined cost <= entry_line.

    Entry legs are posted as resting maker bids (0 fee), so the wall is on price only.
    """
    return bundle_cost_cents(yes_price, no_price) <= int(entry_line)


def single_leg_ok(fill_price: int, single_leg_max: int) -> bool:
    """W2 predicate: a lone leg may only be HELD if its fill price <= single_leg_max."""
    return int(fill_price) <= int(single_leg_max)


def net_after_taker_exit(entry_price: int, exit_price: int, count: int,
                         rate: float = FEE_RATE) -> int:
    """Net cents on a crossing (taker) exit of `count` contracts on one side.

    Buy at entry_price, sell/close at exit_price, minus the taker fee on the exit.
    Used by the manager before it decides to cross a spread (W8: price the fee first).
    Returns net cents (can be negative).
    """
    gross = (int(exit_price) - int(entry_price)) * int(count)
    fee = fee_cents(exit_price, count, rate=rate)
    return gross - fee


def exit_clears_hurdle(entry_price: int, exit_price: int, count: int,
                       hurdle_cents: int, rate: float = FEE_RATE) -> bool:
    """True if a taker exit nets at least hurdle_cents AFTER its own fee (W8)."""
    return net_after_taker_exit(entry_price, exit_price, count, rate=rate) >= int(hurdle_cents)
