"""WO-2026-07-28-X X6 — INTEGER CENTS AT THE ACCRUAL, ROUNDED AT EVERY BOUNDARY.

The venue ticks in 0.1c; fills carried those ticks as FLOATS, so contracts×price
accrual sprayed representation dust (`8.999999999999986c` for a true 9c). Money
math in floats violates the append-only surface's integrity promise cent by cent.

The fix: carry money as INTEGER DECI-CENTS (tenth-cents) through the accrual —
contracts×price is exact in deci-cents — and round HALF-EVEN to whole cents at
every booking/display boundary. The float spray becomes unrepresentable: an
integer has no dust, and half-even rounding at the boundary is deterministic and
bias-free. A value that was truly a whole number of cents (the common case) round-
trips losslessly; a genuine half-cent tick is preserved exactly until it books.
"""

from decimal import ROUND_HALF_EVEN, Decimal


def to_decicents(price_cents) -> int:
    """A cent price (possibly a 0.1c venue tick, float or int) → integer
    DECI-CENTS (tenth-cents). 96.5c → 965; 97 → 970. Exact: the venue's finest
    grain is 0.1c, so ×10 lands on an integer (rounded half-even for float dust)."""
    return int(Decimal(str(price_cents)).scaleb(1).to_integral_value(
        rounding=ROUND_HALF_EVEN))


def cents_half_even(decicents: int) -> int:
    """Integer DECI-CENTS → whole CENTS, rounded HALF-EVEN (banker's rounding —
    no upward bias across many settlements). 35 → 4 (3.5c→4), 965 → 96 (96.5c→96),
    90 → 9. The one place sub-cent accrual collapses to the booked integer."""
    return int((Decimal(int(decicents)) / 10).to_integral_value(
        rounding=ROUND_HALF_EVEN))


def round_cents_half_even(value_cents) -> int:
    """A cents value carrying float dust (a legacy REAL sum) → the clean integer
    cent, half-even. The boundary guard for any money number that reaches a
    display or a booked field still typed as float."""
    return int(Decimal(str(value_cents)).to_integral_value(
        rounding=ROUND_HALF_EVEN))
