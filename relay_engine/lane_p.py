"""Lane P — spike-fade. THE ONLY NEW BUILD in P3 (no prior art in any tree).

Thesis: a sharp displacement of the book away from the external prior, when
the tape does NOT confirm a real move, mean-reverts — P fades it by buying
the side the spike cheapened, one lot, custodied from birth.

Trigger (ALL must hold — fail closed):
  1. an external prior is present (ctx['prior_p']; no prior, no trade)
  2. displacement = |book_implied - prior| within [P_DISPLACEMENT_MIN,
     P_DISPLACEMENT_MAX] — 0.05 trigger DREW-DEFAULT inside the 0.03-0.10
     ruling band; beyond the band it is a MOVE, not a spike
  3. the displacement is SUSTAINED: P_CONFIRM_FRAMES consecutive FRESH frames
     (the whipsaw negative spec: a one-frame flicker never triggers; a stale
     frame never counts; an oscillation resets the count)
  4. the tape does not confirm the spike: if the last FLIP_OFI_TICKS spot
     deltas all agree with the spike's direction, it is real — stand down
     (borrowed feature: lane_flip.tick_direction, already ported)
  5. the fade side's entry price <= P_FADE_MAX_CENTS (cheap side only)

The whipsaw negative spec (the Feb-27 shape) ships as this lane's unit tests
FIRST — sequences P must NOT trigger on: single-frame flickers, stale frames,
oscillating spikes, sub-band and over-band displacements, tape-confirmed moves.

Win/loss path symmetry: the fade triggers on |displacement| regardless of
which side got cheap; custody applies the same cut-params either way.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from . import config
from .custodian import CutParams
from .gateway import Order
from .lane_flip import tick_direction, FLIP_OFI_TICKS

log = logging.getLogger("relay.lane_p")

P_DISPLACEMENT_MIN = 0.03   # ruling band floor
P_DISPLACEMENT = 0.05       # DREW-DEFAULT trigger threshold (P3 go-live constant)
P_DISPLACEMENT_MAX = 0.10   # ruling band cap: beyond this it's a move, not a spike
P_CONFIRM_FRAMES = 2        # sustained displacement frames (flicker-proof)
P_FADE_MAX_CENTS = 49       # fade entries stay on the cheap side
P_STALENESS_LIMIT_S = config.STALENESS_LIMIT_SECONDS


@dataclass
class PState:
    confirm_count: int = 0
    spike_direction: Optional[str] = None   # which side the book spiked TOWARD
    last_frame_ts: float = -1.0


class LaneP:
    name = "P"

    def __init__(self, gateway=None, custodian=None):
        self.gateway = gateway
        self.custodian = custodian
        self.states: Dict[str, PState] = {}
        self.entered: set = set()
        if custodian is not None:
            custodian.set_lane_params("P", p_cut_params())

    def evaluate(self, market: str, ctx: dict) -> tuple:
        """Returns (proposal_or_None, reason)."""
        book = ctx.get("book")
        close_ts = ctx.get("close_ts")
        now = ctx.get("now", time.time())
        prior = ctx.get("prior_p")
        st = self.states.setdefault(market, PState())

        if book is None or close_ts is None:
            return None, "NO_BOOK"
        if close_ts - now < 60:
            return None, "TOO_LATE"
        if market in self.entered:
            return None, "ALREADY_ENTERED"
        if prior is None:
            return None, "NO_PRIOR"  # fail closed: no external prior, no trade

        yb, nb = book.best_yes_bid(), book.best_no_bid()
        if yb is None or nb is None:
            st.confirm_count = 0
            return None, "NO_BOOK"

        # NEGATIVE SPEC: stale frames never count and RESET the confirmation
        if book.is_stale(now, P_STALENESS_LIMIT_S):
            st.confirm_count = 0
            return None, "STALE_FRAME"
        # NEGATIVE SPEC: the same frame re-read never double-counts
        if book.last_update_ts == st.last_frame_ts:
            return None, "NO_NEW_FRAME"
        st.last_frame_ts = book.last_update_ts

        implied = (yb + (100 - nb)) / 200.0  # mid of bid and derived ask
        displacement = implied - prior
        direction = "yes" if displacement > 0 else "no"  # book spiked toward this side

        if abs(displacement) < P_DISPLACEMENT:
            st.confirm_count = 0
            st.spike_direction = None
            return None, "DISPLACEMENT_UNDER_TRIGGER"
        if abs(displacement) > P_DISPLACEMENT_MAX:
            st.confirm_count = 0
            st.spike_direction = None
            return None, "MOVE_NOT_SPIKE"

        # NEGATIVE SPEC: oscillation resets — direction must be consistent
        if st.spike_direction is not None and st.spike_direction != direction:
            st.confirm_count = 1
            st.spike_direction = direction
            return None, "WHIPSAW_RESET"
        st.spike_direction = direction
        st.confirm_count += 1
        if st.confirm_count < P_CONFIRM_FRAMES:
            return None, f"CONFIRMING_{st.confirm_count}/{P_CONFIRM_FRAMES}"

        # NEGATIVE SPEC: a tape-confirmed move is not faded
        tape = tick_direction(ctx.get("spot_ticks"), FLIP_OFI_TICKS)
        if tape is not None and tape == direction:
            st.confirm_count = 0
            return None, "TAPE_CONFIRMS_MOVE"

        # fade: buy the side the spike CHEAPENED (the opposite side)
        fade_side = "no" if direction == "yes" else "yes"
        join = nb if fade_side == "no" else yb
        if join is None or join > P_FADE_MAX_CENTS:
            return None, "FADE_SIDE_NOT_CHEAP"

        st.confirm_count = 0
        st.spike_direction = None
        self.entered.add(market)
        return Order(
            lane="P", event=market.rsplit("-", 1)[0], market=market,
            side=fade_side, action="buy", price_cents=join, count=1,
            size_tier=config.TIER_PROBE, purpose="ENTRY",
            band=(1, P_FADE_MAX_CENTS)), "FADE"


def p_cut_params() -> CutParams:
    """Custody from birth — conservative DREW-DEFAULT placeholders (his to rule)."""
    return CutParams(
        hard_stop_usd=0.30, soft_stop_usd=0.25,
        min_time_remaining_s=30,
        hold_to_settle_s=0, spot_danger_buffer_usd=0,
        grace_period_s=5,
        early_exit_window_s=120, early_exit_loss_fraction=0.25,
        max_loss_fraction_of_balance=0.03, max_loss_fraction_of_cost=0.50,
        catastrophic_loss_cents=30,
        spot_safe_buffer_early_usd=100, spot_safe_buffer_late_usd=50,
        spot_safe_cutoff_s=60,
        rapid_drop_threshold=0.04, rapid_drop_window_s=10,
        max_loss_cents_per_contract=15,
        reversal_threshold=0.10, reversal_threshold_settling=0.15,
        reversal_threshold_profit=0.06, profit_tighten_above_entry=0.05,
        peak_window_s=30, proactive_after_s=30,
        prob_floor=0.30,
    )
