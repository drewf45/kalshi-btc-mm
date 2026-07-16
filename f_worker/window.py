# f_worker/window.py
# THE STATE MACHINE — the One-Shot Law as code (BUILD ORDER §3).
#
#   IDLE -> SEEKING  (market discovered, D4 gate passes)
#   IDLE -> SAT_OUT  (D4 gate fails; logged, a POSITIVE stat)
#   SEEKING -> HOLDING (>=1 leg filled and legally holdable)
#   SEEKING -> EXITING (one leg filled but not holdable -> immediate salvage)
#   SEEKING -> SAT_OUT (no fill)
#   HOLDING -> HOLDING (partial resolution; reprice/salvage walk)
#   HOLDING -> EXITING (T-90 flat wall trips with inventory)
#   HOLDING -> DONE    (both flipped, or bundle rode floor to settlement, or lone resolved)
#   EXITING -> DONE
#   SAT_OUT (terminal)   DONE (terminal)
#
# There is NO path out of DONE except the next window's OPEN — which is a brand-new
# Window object, not a transition. Every transition is an append-only ledger row with
# evidence (prices, book, clock). The machine holds NO exchange handles: it only
# tracks state + inventory and records. The manager acts; the machine remembers.

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# States
IDLE = "IDLE"
SEEKING = "SEEKING"
HOLDING = "HOLDING"
EXITING = "EXITING"
DONE = "DONE"
SAT_OUT = "SAT_OUT"

TERMINAL = {DONE, SAT_OUT}

# Legal adjacency. DONE and SAT_OUT have NO outgoing edges (the wall on §3).
_EDGES: Dict[str, set] = {
    IDLE: {SEEKING, SAT_OUT},
    SEEKING: {HOLDING, EXITING, SAT_OUT},
    HOLDING: {HOLDING, EXITING, DONE},
    EXITING: {DONE},
    DONE: set(),
    SAT_OUT: set(),
}


class IllegalTransition(RuntimeError):
    """An edge not in the state graph was attempted — e.g. anything out of DONE."""


@dataclass
class Leg:
    side: str            # "yes" or "no"
    entry_price: int     # fill price in cents
    count: int
    order_id: Optional[str] = None
    resolved: bool = False       # flipped, salvaged, or settled
    exit_price: Optional[int] = None
    exit_kind: Optional[str] = None   # flip / salvage / floor / market_out


@dataclass
class Window:
    window_id: str
    market_ticker: str
    event_ticker: Optional[str]
    open_ts: Optional[int]
    close_ts: Optional[int]
    rung: int
    lots: int

    state: str = IDLE
    mode: Optional[str] = None            # "bundle" | "lone" | None
    legs: List[Leg] = field(default_factory=list)

    # entry order handles (for cancel-at-phase-end)
    yes_entry_oid: Optional[str] = None
    no_entry_oid: Optional[str] = None

    def __post_init__(self):
        self._ledger = None  # injected via bind_ledger

    # ---- ledger binding (kept off the dataclass fields so tests can construct freely) ----
    def bind_ledger(self, ledger: Any) -> "Window":
        self._ledger = ledger
        return self

    # ---- transitions ----
    def transition(self, to_state: str, evidence: Optional[Dict[str, Any]] = None) -> None:
        """The ONLY way state changes. Validates the edge, then appends a ledger row.
        Raises IllegalTransition on any edge not in the graph (incl. all edges from a
        terminal state)."""
        frm = self.state
        if to_state not in _EDGES.get(frm, set()):
            raise IllegalTransition(f"{frm} -> {to_state} is not a legal edge")
        self.state = to_state
        if self._ledger is not None:
            self._ledger.record_transition(self.window_id, frm, to_state, evidence or {})

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    # ---- inventory helpers ----
    def add_fill(self, side: str, price: int, count: int, order_id: Optional[str] = None) -> None:
        self.legs.append(Leg(side=side, entry_price=int(price), count=int(count), order_id=order_id))

    def held_legs(self) -> List[Leg]:
        return [l for l in self.legs if not l.resolved]

    def open_sides(self) -> List[str]:
        return [l.side for l in self.held_legs()]

    def leg_for(self, side: str) -> Optional[Leg]:
        for l in self.legs:
            if l.side == side and not l.resolved:
                return l
        return None

    def resolve_leg(self, side: str, exit_price: Optional[int], exit_kind: str) -> None:
        l = self.leg_for(side)
        if l is not None:
            l.resolved = True
            l.exit_price = exit_price
            l.exit_kind = exit_kind

    def entry_cost_cents(self) -> int:
        return sum(l.entry_price * l.count for l in self.legs)
