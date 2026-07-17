"""Order book in canonical YES terms.

The Kalshi book is BIDS-ONLY RECIPROCAL: the venue publishes yes-bids and no-bids;
every ask is derived (yes_ask = 100 - best_no_bid). ALL price math in this engine
goes through `to_yes_terms` — the ONE canonical conversion function (C.3). No other
module converts sides.

Win/loss path symmetry: this module holds no capital state; it renders the same
book to entry logic and exit logic — neither side gets a privileged view.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional


def to_yes_terms(side: str, price_cents: int) -> int:
    """THE canonical YES-terms conversion. side in {"yes","no"}; returns the
    equivalent YES price in cents. The only side-conversion function in the tree."""
    if side == "yes":
        return int(price_cents)
    if side == "no":
        return 100 - int(price_cents)
    raise ValueError(f"unknown side: {side!r}")


@dataclass
class OrderBook:
    """Bids-only reciprocal book for one market, with staleness stamps (C.3)."""

    market: str
    yes_bids: Dict[int, int] = field(default_factory=dict)  # price_cents -> contracts
    no_bids: Dict[int, int] = field(default_factory=dict)
    last_update_ts: float = 0.0
    transport: str = "WS"  # "WS" or "EXPLORATION" (REST poll rows, feed-parity law)

    def apply_snapshot(self, yes_bids: Dict[int, int], no_bids: Dict[int, int], ts: float) -> None:
        self.yes_bids = {int(p): int(q) for p, q in yes_bids.items() if int(q) > 0}
        self.no_bids = {int(p): int(q) for p, q in no_bids.items() if int(q) > 0}
        self.last_update_ts = ts

    def apply_delta(self, side: str, price_cents: int, delta: int, ts: float) -> None:
        levels = self.yes_bids if side == "yes" else self.no_bids
        q = levels.get(int(price_cents), 0) + int(delta)
        if q > 0:
            levels[int(price_cents)] = q
        else:
            levels.pop(int(price_cents), None)
        self.last_update_ts = ts

    # -- canonical views (all derived from yes-bid/no-bid, nothing else) --
    def best_yes_bid(self) -> Optional[int]:
        return max(self.yes_bids) if self.yes_bids else None

    def best_no_bid(self) -> Optional[int]:
        return max(self.no_bids) if self.no_bids else None

    def best_yes_ask(self) -> Optional[int]:
        nb = self.best_no_bid()
        return 100 - nb if nb is not None else None

    def visible_depth(self, side: str, price_cents: int) -> int:
        """Visible contracts resting at the level an order would join (sizing wall input)."""
        levels = self.yes_bids if side == "yes" else self.no_bids
        return levels.get(int(price_cents), 0)

    def total_bid_depth(self, side: str) -> int:
        levels = self.yes_bids if side == "yes" else self.no_bids
        return sum(levels.values())

    def is_stale(self, now: float, limit_seconds: float) -> bool:
        return (now - self.last_update_ts) > limit_seconds
