"""Custodian (P22) — Chunk 4 / C.5.

DUMP exit MECHANICS recovered from BOTH named sources:
  - `reference/legacy_dump_bot.py` (the quarantined parts shelf): the trigger
    family surface (prob flip / drop, market flip, price danger, endgame guard).
  - branch `claude/trading-contract-math-2biFE` (bot.py::should_dump_position +
    _btc_is_safe, read at commit 85b6c9c): the full decision ORDER and the
    mechanics the monolith's truncated tail only hinted at —
      1. hard/soft DOLLAR stops first, loss estimated as WORST-OF the actual
         exit bid and the probability estimate (the prob can lag; the bid is
         what the market will actually pay),
      2. endgame guard (never cut in the last seconds),
      3. late hold-to-settle with a spot danger-buffer override,
      4. early exit on underwater positions inside the entry window,
      5. grace period after entry,
      6. bankroll-proportional caps (fraction of balance / of position cost)
         that fire BEFORE the spot-safety override,
      7. catastrophic per-contract stop,
      8. THE MASTER OVERRIDE — if spot is safely on our side of the boundary,
         the book is lying: HOLD no matter what it says,
      9. only when spot is NOT safe: rapid-drop bail (last-N-seconds window),
         hard per-contract stop, windowed-peak reversal bail (threshold
         tightens after profit, widens during settling), probability floor.
Mechanics only — NO parameter values were borrowed (§F): every number lives in
per-lane CutParams set at wiring time, pending Drew's per-lane rulings.

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
winning or losing; the spot-safety check reads distance from the boundary with
the same buffer whichever side of profit the position sits on.

Custodian exits ATTRIBUTE TO THE OPENING LANE's Wilson cells (C.2): fills are
recorded under the opening lane with action=CUSTODIAN_EXIT.
"""

import logging
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

from . import config
from .errors import FatalIntegrityError
from .gateway import Gateway, Order

log = logging.getLogger("relay.custodian")

CATASTROPHIC_PROB = 0.05  # passthrough lanes still cut below this survival probability


@dataclass
class CutParams:
    """Per-lane triggers. Values are set at lane wiring, NOT borrowed from any
    legacy bot (§F). Scaled by size tier: discipline tightens with size."""
    # dollar stops (worst-of bid/prob loss estimate)
    hard_stop_usd: float
    soft_stop_usd: float
    # windows and guards
    min_time_remaining_s: float      # endgame guard: never cut inside this
    hold_to_settle_s: float          # late hold: suppress cuts in the last N s...
    spot_danger_buffer_usd: float    # ...unless spot is within this of the boundary
    grace_period_s: float            # no cuts this soon after entry
    early_exit_window_s: float       # underwater early-exit applies inside this window
    early_exit_loss_fraction: float  # ...when loss >= this fraction of max possible loss
    # bankroll protection (fires before spot safety)
    max_loss_fraction_of_balance: float
    max_loss_fraction_of_cost: float
    catastrophic_loss_cents: int     # absolute per-contract backstop
    # spot-safety master override
    spot_safe_buffer_early_usd: float
    spot_safe_buffer_late_usd: float
    spot_safe_cutoff_s: float        # below this time-to-close, use the late buffer
    # bail thresholds (only when spot is NOT safe)
    rapid_drop_threshold: float      # prob drop within rapid window
    rapid_drop_window_s: float
    max_loss_cents_per_contract: int
    reversal_threshold: float
    reversal_threshold_settling: float
    reversal_threshold_profit: float
    profit_tighten_above_entry: float
    peak_window_s: float             # windowed peak (not all-time — avoids ratcheting)
    proactive_after_s: float         # "settling" phase ends here
    prob_floor: float
    passthrough: bool = False        # cut-disabled-except-catastrophic (Lane F)

    def scaled(self, size_tier: str) -> "CutParams":
        # Size buys discipline: PROBE=1.0 (base), LEAN/CLEAR tighten. Loss
        # tolerances shrink; drop thresholds shrink; the floor rises.
        factor = {config.TIER_PROBE: 1.0, config.TIER_LEAN: 1.15,
                  config.TIER_CLEAR: 1.30}.get(size_tier, 1.0)
        if factor == 1.0:
            return self
        return replace(
            self,
            hard_stop_usd=self.hard_stop_usd / factor,
            soft_stop_usd=self.soft_stop_usd / factor,
            max_loss_fraction_of_balance=self.max_loss_fraction_of_balance / factor,
            max_loss_fraction_of_cost=self.max_loss_fraction_of_cost / factor,
            catastrophic_loss_cents=int(self.catastrophic_loss_cents / factor),
            rapid_drop_threshold=self.rapid_drop_threshold / factor,
            max_loss_cents_per_contract=int(self.max_loss_cents_per_contract / factor),
            reversal_threshold=self.reversal_threshold / factor,
            reversal_threshold_settling=self.reversal_threshold_settling / factor,
            reversal_threshold_profit=self.reversal_threshold_profit / factor,
            prob_floor=min(0.99, self.prob_floor * factor),
            spot_safe_buffer_early_usd=self.spot_safe_buffer_early_usd * factor,
            spot_safe_buffer_late_usd=self.spot_safe_buffer_late_usd * factor,
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
    entry_time: float = 0.0
    resting_exit_id: Optional[str] = None
    # windowed prob tracking (2biFE mechanic: windowed peak, rapid drop)
    prob_history: List[Tuple[float, float]] = field(default_factory=list)
    peak_prob: float = 0.0


def spot_is_safe(side: str, spot: Optional[float], lo: Optional[float],
                 hi: Optional[float], secs_to_close: float,
                 p: CutParams) -> Tuple[bool, float]:
    """THE key check (2biFE::_btc_is_safe, generalized): if spot is on our side
    of the boundary by more than the buffer, the book is lying — hold.
    Returns (is_safe, distance_usd)."""
    if spot is None:
        return False, 0.0
    buffer = (p.spot_safe_buffer_late_usd if secs_to_close < p.spot_safe_cutoff_s
              else p.spot_safe_buffer_early_usd)
    if side == "yes" and lo is not None:
        distance = spot - lo
        return distance >= buffer, distance
    elif side == "no" and hi is not None:
        distance = hi - spot
        return distance >= buffer, distance
    elif side == "no" and lo is not None:
        distance = lo - spot
        return distance >= buffer, distance
    return False, 0.0  # can't determine — not safe


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
    def should_cut(self, pos: OpenPosition, now: float, secs_remaining: float,
                   p_win: float, exit_bid_cents: Optional[int],
                   spot: Optional[float], boundary_lo: Optional[float],
                   boundary_hi: Optional[float],
                   balance_usd: float = 0.0) -> Optional[str]:
        """The recovered DUMP decision, in the 2biFE order. Returns the trigger
        name or None (hold)."""
        base = self.params.get(pos.lane)
        if base is None:
            return None
        p = base.scaled(pos.size_tier)

        entry = pos.entry_price_cents
        qty = pos.count

        # loss estimates: worst-of the prob estimate and the actual bid
        exit_prob_cents = int(p_win * 100)
        loss_prob_usd = (entry - exit_prob_cents) * qty / 100.0
        exit_bid = exit_bid_cents if exit_bid_cents is not None else exit_prob_cents
        loss_bid_usd = (entry - exit_bid) * qty / 100.0
        worst_loss_usd = max(loss_prob_usd, loss_bid_usd)

        if p.passthrough:
            # Lane F passthrough (4.4): cut-disabled-except-catastrophic.
            if p_win < CATASTROPHIC_PROB:
                return "CATASTROPHIC"
            if (entry - exit_prob_cents) >= p.catastrophic_loss_cents:
                return "CATASTROPHIC"
            return None

        # 1. Hard/soft dollar stops — fire FIRST, no exceptions
        if worst_loss_usd >= p.hard_stop_usd:
            return "STOP_LOSS_HARD"
        if worst_loss_usd >= p.soft_stop_usd:
            return "STOP_LOSS_SOFT"

        # 2. Endgame guard: never cut inside the window (stops above excepted)
        if secs_remaining < p.min_time_remaining_s:
            return None

        # 3. Late hold-to-settle, with the spot danger-buffer override
        if pos.entry_time > 0 and secs_remaining <= p.hold_to_settle_s:
            safe, dist = spot_is_safe(pos.side, spot, boundary_lo, boundary_hi,
                                      secs_remaining, p)
            if safe and dist >= p.spot_danger_buffer_usd:
                # spot safely ours — hold to settlement; bankroll cap still rules
                if balance_usd > 0 and loss_prob_usd > balance_usd * p.max_loss_fraction_of_balance:
                    return "LATE_BANKROLL_CAP"
                return None
            # spot near the boundary — fall through to normal cut logic

        # 4. Early exit on underwater positions (inside the entry window)
        time_in_trade = now - pos.entry_time if pos.entry_time > 0 else float("inf")
        if time_in_trade <= p.early_exit_window_s:
            max_possible_loss_usd = entry * qty / 100.0
            if loss_bid_usd >= max_possible_loss_usd * p.early_exit_loss_fraction:
                return "EARLY_EXIT_UNDERWATER"

        # 5. Grace period
        if time_in_trade < p.grace_period_s:
            return None

        # windowed prob tracking (peak over last peak_window_s; rapid drop)
        pos.peak_prob = max(pos.peak_prob, p_win)
        pos.prob_history.append((now, p_win))
        pos.prob_history = [(t, v) for t, v in pos.prob_history
                            if t >= now - p.peak_window_s]
        windowed_peak = max(v for _, v in pos.prob_history)
        drop_from_peak = windowed_peak - p_win
        in_settling = time_in_trade < p.proactive_after_s
        rapid = [(t, v) for t, v in pos.prob_history
                 if t >= now - p.rapid_drop_window_s]
        rapid_drop = (max(v for _, v in rapid) - p_win) if len(rapid) >= 3 else 0.0

        # 6. Bankroll-proportional caps — BEFORE spot safety
        if balance_usd > 0 and loss_prob_usd > balance_usd * p.max_loss_fraction_of_balance:
            return "BANKROLL_CAP"
        position_cost_usd = entry * qty / 100.0
        if loss_prob_usd > position_cost_usd * p.max_loss_fraction_of_cost:
            return "POSITION_CAP"

        # 7. Catastrophic per-contract backstop — BEFORE spot safety
        if (entry - exit_prob_cents) >= p.catastrophic_loss_cents:
            return "CATASTROPHIC"

        # 8. MASTER OVERRIDE: spot safely on our side -> the book is lying, HOLD
        safe, dist = spot_is_safe(pos.side, spot, boundary_lo, boundary_hi,
                                  secs_remaining, p)
        if safe:
            return None

        # 9. Spot NOT safe — bail checks
        if rapid_drop >= p.rapid_drop_threshold:
            return "RAPID_DROP"
        if (entry - exit_prob_cents) >= p.max_loss_cents_per_contract:
            return "HARD_STOP_PER_CONTRACT"
        entry_prob = pos.entry_p_win
        gain_above_entry = windowed_peak - entry_prob
        threshold = (p.reversal_threshold_profit
                     if gain_above_entry >= p.profit_tighten_above_entry
                     else p.reversal_threshold)
        if in_settling:
            threshold = p.reversal_threshold_settling
        if drop_from_peak >= threshold:
            return "REVERSAL"
        if p_win < p.prob_floor:
            return "PROB_FLOOR"
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
