"""Custodian (P22) — Chunk 4 / C.5.

DUMP exit MECHANICS recovered from reference/legacy_dump_bot.py (the quarantined
parts shelf): the trigger family is probability-flip, probability-drop,
market-flip, and price-danger-vs-sigma, guarded by a minimum-time-remaining
window. Mechanics only — no parameter values were borrowed (§F: borrow
mechanics from anything; borrow parameters/edge from nothing). The
`claude/trading-contract-math-2biFE` branch named in C.5 is not present in this
repo's remotes; noted, not blocking (the mechanics above stand on their own).

Generalized to PER-LANE cut params SCALED BY SIZE TIER (size buys discipline:
bigger tiers cut earlier/tighter, never looser).

Baton lifecycle (4.2): cancel-resting-exit THEN cut, atomically verified,
fail-loud on partial. Venue decrease/reduce_by is available for partial
reductions.

Kill semantics (4.3): lane-kill halts ENTRIES only; open positions are custodied
to conclusion, loudly.

Lane F is PASSTHROUGH (4.4): cut-disabled-except-catastrophic until the
custodian earns F on its own clean weeks — Drew's gate.

Ratification assumed (A3): P22 custodies new lanes only.

Win/loss path symmetry: the custodian runs on every open position every tick,
winning or losing; triggers read displacement magnitude and direction the same
way on both sides.

Custodian exits ATTRIBUTE TO THE OPENING LANE's Wilson cells (C.2): fills are
recorded under the opening lane with action=CUSTODIAN_EXIT.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from . import config
from .errors import FatalIntegrityError
from .gateway import Gateway, Order

log = logging.getLogger("relay.custodian")

CATASTROPHIC_PROB = 0.05  # passthrough lanes still cut below this survival probability


@dataclass
class CutParams:
    """Per-lane triggers. Scaled by size tier: discipline tightens with size."""
    prob_flip_below: float        # cut if p(win) drops below this
    prob_drop_fraction: float     # cut if p(win) fell by this fraction from entry
    market_flip_below: float      # cut if market-implied p drops below this
    price_danger_sigmas: float    # cut if spot is within this many sigma of the strike
    min_time_remaining_s: float   # never cut inside this window (mechanics from legacy DUMP)
    passthrough: bool = False     # cut-disabled-except-catastrophic (Lane F)

    def scaled(self, size_tier: str) -> "CutParams":
        # Size buys discipline: PROBE=1.0 (base), LEAN/CLEAR tighten.
        factor = {config.TIER_PROBE: 1.0, config.TIER_LEAN: 1.15,
                  config.TIER_CLEAR: 1.30}.get(size_tier, 1.0)
        return CutParams(
            prob_flip_below=min(0.99, self.prob_flip_below * factor),
            prob_drop_fraction=self.prob_drop_fraction / factor,
            market_flip_below=min(0.99, self.market_flip_below * factor),
            price_danger_sigmas=self.price_danger_sigmas * factor,
            min_time_remaining_s=self.min_time_remaining_s,
            passthrough=self.passthrough,
        )


@dataclass
class OpenPosition:
    event: str
    market: str
    lane: str                    # OPENING lane — owns attribution forever
    side: str
    count: int
    entry_price_cents: int
    entry_p_win: float
    size_tier: str
    resting_exit_id: Optional[str] = None


class Custodian:
    def __init__(self, gateway: Gateway, ledger, surface, ladder=None):
        self.gateway = gateway
        self.ledger = ledger
        self.surface = surface
        self.ladder = ladder  # DegradeLadder; custodian falls to REST when WS is lost
        self.params: Dict[str, CutParams] = {}
        self.killed_lanes: set = set()
        self.positions: Dict[str, OpenPosition] = {}  # key: market:lane

    def set_lane_params(self, lane: str, params: CutParams) -> None:
        self.params[lane] = params

    def adopt(self, pos: OpenPosition) -> None:
        self.positions[f"{pos.market}:{pos.lane}"] = pos

    # ------------------------------------------------------------------
    def kill_lane(self, lane: str) -> None:
        """Lane-kill halts ENTRIES only. Open positions stay custodied, loudly."""
        self.killed_lanes.add(lane)
        self.gateway.halt_entries(f"LANE_KILL:{lane}")
        open_here = [p for p in self.positions.values() if p.lane == lane]
        log.warning("LANE KILL %s: entries halted; %d open position(s) custodied to conclusion",
                    lane, len(open_here))

    # ------------------------------------------------------------------
    def should_cut(self, pos: OpenPosition, p_win: float, market_p: float,
                   spot_sigma_distance: float, seconds_remaining: float) -> Optional[str]:
        """The generalized DUMP decision. Returns the trigger name or None."""
        base = self.params.get(pos.lane)
        if base is None:
            return None
        p = base.scaled(pos.size_tier)
        if p.passthrough:
            # Lane F passthrough: catastrophic only, until the custodian earns F.
            return "CATASTROPHIC" if p_win < CATASTROPHIC_PROB else None
        if seconds_remaining < p.min_time_remaining_s:
            return None  # legacy DUMP mechanic: never cut inside the endgame window
        if p_win < p.prob_flip_below:
            return "PROB_FLIP"
        if pos.entry_p_win > 0 and (pos.entry_p_win - p_win) / pos.entry_p_win >= p.prob_drop_fraction:
            return "PROB_DROP"
        if market_p < p.market_flip_below:
            return "MARKET_FLIP"
        if spot_sigma_distance <= p.price_danger_sigmas:
            return "PRICE_DANGER"
        return None

    # ------------------------------------------------------------------
    def execute_cut(self, pos: OpenPosition, cut_price_cents: int, book,
                    trigger: str) -> str:
        """BATON LIFECYCLE: cancel the resting exit FIRST, verify, THEN cut.
        Fail-loud on any partial state — a position with both a resting exit and
        a cut order live is an integrity violation, not a retry."""
        key = f"{pos.market}:{pos.lane}"
        if pos.resting_exit_id is not None:
            canceled = self.gateway.cancel(pos.resting_exit_id)
            if not canceled:
                raise FatalIntegrityError(
                    f"baton violation: resting exit {pos.resting_exit_id} on {pos.market} "
                    f"could not be verified canceled before cut ({trigger})")
            pos.resting_exit_id = None
        # sell the held side; risk-reducing -> skips walls, allowed in every feed state
        cut = Order(
            lane=pos.lane, event=pos.event, market=pos.market, side=pos.side,
            action="sell", price_cents=cut_price_cents, count=pos.count,
            size_tier=pos.size_tier, purpose="CUT",
        )
        result = self.gateway.submit(cut, book)
        transport = self.ladder.custodian_transport() if self.ladder else "WS"
        log.warning("CUSTODIAN CUT [%s] %s lane=%s count=%d @%dc transport=%s",
                    trigger, pos.market, pos.lane, pos.count, cut_price_cents, transport)
        self.ledger.record_fill(pos.market, pos.lane, pos.side, "CUSTODIAN_EXIT",
                                cut_price_cents, pos.count, pos.size_tier)
        del self.positions[key]
        return result.order_id
