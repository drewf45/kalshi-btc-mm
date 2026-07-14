"""Per-order roundup fee + entry inequality — pure math, no I/O.

Kalshi fee formula (per order):
  fee_cents = ceil(base_rate × series_mult × contracts × P × (1−P) × 100)

where P = price_cents / 100 (the probability).
base_rate: 0.07 taker, 0.0175 maker.
"""

import math
from typing import Tuple

TAKER_BASE = 0.07
MAKER_BASE = 0.0175


def order_fee_cents(mult: float, contracts: int, price_cents: int) -> int:
    """Roundup fee for one order. mult = base_rate × series_mult."""
    if contracts <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    fee_dollars = mult * contracts * p * (1.0 - p)
    return max(0, math.ceil(fee_dollars * 100.0))


def entry_ok(price_cents: int, mult: float, contracts: int,
             min_net_cents: float) -> Tuple[bool, float]:
    """Check entry inequality: net clip per contract ≥ min_net_cents.
    Returns (passes, total_net_clip_cents)."""
    gross_cents = (100 - price_cents) * contracts
    fee = order_fee_cents(mult, contracts, price_cents)
    net = gross_cents - fee
    per_ct = net / contracts if contracts > 0 else 0
    return (per_ct >= min_net_cents, float(net))


def taker_mult(series_mult: float = 1.0) -> float:
    return TAKER_BASE * series_mult


def maker_mult(series_mult: float = 1.0) -> float:
    return MAKER_BASE * series_mult
