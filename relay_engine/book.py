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
    # True-touch fp strings (the parts' law: orders rest at the exact touch,
    # subpenny precision — venue.place_order_maker v2_price_str). Kept per
    # level when the wire speaks dollars; None on cents-int books.
    yes_fp: Dict[int, str] = field(default_factory=dict)
    no_fp: Dict[int, str] = field(default_factory=dict)
    last_update_ts: float = 0.0
    transport: str = "WS"  # "WS" or "EXPLORATION" (REST poll rows, feed-parity law)
    # P10 §1/§2: a book is only trustworthy on a snapshot FOUNDATION — deltas
    # over an empty book build fiction. `poisoned` marks an incoherent book
    # (yes+no > 101, which the venue cannot produce); a clean snapshot clears it.
    has_snapshot: bool = False
    poisoned: bool = False
    last_seq: Optional[int] = None  # venue delta sequence; a gap = missed frames

    def apply_snapshot(self, yes_bids: Dict[int, int], no_bids: Dict[int, int],
                       ts: float, yes_fp: Dict[int, str] = None,
                       no_fp: Dict[int, str] = None) -> None:
        self.yes_bids = {int(p): int(q) for p, q in yes_bids.items() if int(q) > 0}
        self.no_bids = {int(p): int(q) for p, q in no_bids.items() if int(q) > 0}
        self.yes_fp = dict(yes_fp or {})
        self.no_fp = dict(no_fp or {})
        self.last_update_ts = ts
        self.has_snapshot = True
        if self.coherent():
            self.poisoned = False  # a clean snapshot is the cure (P10 §2.1)

    def apply_delta(self, side: str, price_cents: int, delta: int, ts: float,
                    fp: str = None) -> None:
        levels = self.yes_bids if side == "yes" else self.no_bids
        fps = self.yes_fp if side == "yes" else self.no_fp
        q = levels.get(int(price_cents), 0) + int(delta)
        if q > 0:
            levels[int(price_cents)] = q
            if fp is not None:
                fps[int(price_cents)] = fp
        else:
            levels.pop(int(price_cents), None)
            fps.pop(int(price_cents), None)
        self.last_update_ts = ts

    def coherent(self) -> bool:
        """P10 §2: the venue's bids-only book can never sum past 101 (100 plus
        the 1c minimum tick of overlap a race can show). yes 97 + no 55 = 152
        is a lie — some frame corrupted state."""
        yb, nb = self.best_yes_bid(), self.best_no_bid()
        if yb is None or nb is None:
            return True
        return yb + nb <= 101

    def best_fp(self, side: str):
        """The exact fp string at the side's best level, or None (int books)."""
        best = self.best_yes_bid() if side == "yes" else self.best_no_bid()
        if best is None:
            return None
        return (self.yes_fp if side == "yes" else self.no_fp).get(best)

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


def touch_view(ob: "OrderBook"):
    """P7 §1 — THE single book adapter: one book, one truth. Every lane's touch
    read comes through here (F/H8's favorite, FLIP's joins, D's cheap side, the
    custodian's marks all derive from the same OrderBook instance per market).
    Property-tested: fields equal the book's canonical queries, always."""
    from .lane_fh8 import TouchBook
    yb, nb = ob.best_yes_bid(), ob.best_no_bid()
    return TouchBook(
        yes_bid=yb, no_bid=nb,
        yes_ask=(100 - nb) if nb is not None else None,
        no_ask=(100 - yb) if yb is not None else None,
        yes_bid_qty=ob.visible_depth("yes", yb) if yb is not None else 0,
        no_bid_qty=ob.visible_depth("no", nb) if nb is not None else 0,
        yes_bid_fp=ob.best_fp("yes"),
        no_bid_fp=ob.best_fp("no"),
    )
