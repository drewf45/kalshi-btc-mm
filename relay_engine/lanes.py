"""Lanes — all five registered; every lane evaluates every market every cycle (C.4).

A Pass is a FIRST-CLASS TERMINAL ROW: a lane that declines a market says so on
the surface, with a reason, every window.

F and H8 are GATED ON B1 (§A5): their ladder/gate logic ports ONLY from the live
k_worker tree Drew provides — never from memory, never from the quarantined
monolith. Until B1 arrives they evaluate to PASS(GATED_ON_B1) so the surface
census stays clean and the shadow loop exercises the full path.
D / MM / P are SHADOW/STUB per charter §5 — registered, passing, awaiting their
chunks (D and P are Chunk 6; MM is Chunk 8).

This module holds no capital state; proposals go to the gateway, which owns the
walls. (Win/loss path symmetry lives where the capital lives.)
"""

from dataclasses import dataclass
from typing import List, Optional

from . import config
from .gateway import Order


@dataclass
class Decision:
    lane: str
    market: str
    proposal: Optional[Order]  # None = Pass
    pass_reason: str = ""


class Lane:
    name = "?"

    def evaluate(self, market: str, ctx: dict) -> Decision:
        raise NotImplementedError


class GatedOnB1Lane(Lane):
    """F / H8 placeholder: no logic until the live tree (B1) is provided."""

    def __init__(self, name: str):
        self.name = name

    def evaluate(self, market: str, ctx: dict) -> Decision:
        return Decision(self.name, market, None, pass_reason="GATED_ON_B1_NOT_PORTED")


class StubLane(Lane):
    """D / MM / P shadow stubs: registered and looking, proposing nothing yet."""

    def __init__(self, name: str, chunk: str):
        self.name = name
        self.chunk = chunk

    def evaluate(self, market: str, ctx: dict) -> Decision:
        return Decision(self.name, market, None, pass_reason=f"STUB_AWAITING_{self.chunk}")


def build_registry() -> List[Lane]:
    """All five lanes, always. Order is stable for the surface census."""
    return [
        GatedOnB1Lane("F"),
        GatedOnB1Lane("H8"),
        StubLane("D", "CHUNK_6"),
        StubLane("MM", "CHUNK_8"),
        StubLane("P", "CHUNK_6"),
    ]


LANE_D_BAND = (config.LANE_D_FLOOR_CENTS, 99)  # floor per DREW-DEFAULT, pending Chunk 2
