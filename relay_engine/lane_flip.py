"""Lane FLIP — PORTED from the flipdesk tree's k_worker/flip_mode.py (920 lines,
LIVE-PROVEN this morning). P3.2. This is not a stub designed here; it is a
proven lane rehomed behind the relay's lane interface.

What comes WHOLE (internal walls + signals, values = this morning's traded
knobs, all DREW-DEFAULT):
  - both-sides entry under the combined-<=99c wall (FLIP_LINE) with side-max
    (FLIP_SIDE_MAX), one post per side per trip, no re-peg, no chase
  - pair-or-nothing / pair grace (FLIP_PAIR_GRACE), lone-leg max (FLIP_LONE_MAX)
  - the ratchet: max trips per window, OFI re-entry gate (tick-direction +
    book-lean agree, never fade the tape), curfew (no new entries after
    T-FLIP_CURFEW), scratch sit-out, window stop
  - stop-streak: two consecutive stopped windows -> lane kill (maps onto the
    per-lane kill rule; entries halt, positions custodied — R2)
  - pure signal helpers byte-identical: _tick_direction, _book_lean, _ofi_side,
    _spot_adverse, _scratch_reason

What CHANGES (the relay laws that outrank port fidelity — P3 Part V):
  - ORDERS ROUTE THROUGH gateway.submit (walls + attribution). flip_mode
    called the client directly; here every entry and every take is a
    gateway Order with lane="FLIP", so a flip bundle is reconstructible
    per-lane from surface rows alone (acceptance-tested).
  - ONE EXIT OWNER: the lane proposes entries and COMPUTES the take quote
    (entry+X, passive EXIT through the gateway, registered as the position's
    resting exit with the custodian). Scratch REASONS become custodian
    cut-params (flip_cut_params below); the CUSTODIAN executes every
    scratch/flatten via the baton with crossfire on CUT only. The lane runs
    no exit loop.
  - cross-400 normalization (Adversary): a post-only cross rejection is a
    NORMAL reject — the lane re-proposes at the fresh join next cycle; never
    a bypass, never a retry dance inside the lane.

Win/loss path symmetry: both sides of the book are quoted by the same rule;
a netted bundle and a scratched leg book through the same gateway/fills path.

P21 "THE DOCTRINE ENGINE" OVERTURNED the pair machinery above: the venue
nets one account's sides (Drew: "Kalshi closed that loophole"), so a
pair-bundle was a fiction — PAIR is RETIRED (zero pair posts, graded).
FLIP now runs two intents: HUNT (P18 — fast, needle-triggered, Job-B
bails) and OPEN (A4 — both sides in the open band + grain streak >= 2,
maker join the grain side, one lot) with THE PATIENT HOLD (A5 — inside
the undetermined band no stop/scratch/time-box; exits are exactly
TAKE / DETERMINED-AGAINST / CURFEW). The pure pair-era helpers below
(ofi_side, scratch_reason, pair_grace_expired) remain as documentation
of the retired law; the docstrings carry the citations.
"""

import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import config
from .custodian import CutParams
from .flip_math import entry_args
from .gateway import Order

log = logging.getLogger("relay.lane_flip")

# ── knobs — DREW-DEFAULT at this morning's traded values ───────────────────
FLIP_WINDOW_SEC = 900       # DREW-DEFAULT
FLIP_ENTRY_SEC = 300        # DREW-DEFAULT: watch the first ~5 min
FLIP_LINE = 99              # DREW-DEFAULT: combined bundle wall
FLIP_LONE_MAX = 49          # DREW-DEFAULT
FLIP_SIDE_MAX = 49          # DREW-DEFAULT: post a side the 1st time <= this
FLIP_X = 4                  # DREW-DEFAULT: flip at entry+X
FLIP_FLAT_AT = 90           # DREW-DEFAULT: everything dies at T-90
FLIP_STOP_CENTS = 25        # DREW-DEFAULT: window stop
FLIP_PAUSE_AFTER_STOPS = 2  # DREW-DEFAULT: stop-streak -> lane kill
FLIP_PAIR_GRACE = 30        # DREW-DEFAULT: opposite bid works this long after 1st fill
FLIP_SCRATCH_S = 3          # DREW-DEFAULT: scratch if mark <= entry-S
FLIP_MARKOUT_STOP = 2       # DREW-DEFAULT: markout <= -this & worsening
FLIP_OFI_TICKS = 4          # DREW-DEFAULT: spot ticks that must agree for re-entry
FLIP_CURFEW = 240           # DREW-DEFAULT: no new entries after T-this
FLIP_MAX_TRIPS = 4          # DREW-DEFAULT
FLIP_SCRATCH_SITOUT = 3     # DREW-DEFAULT: sit the window out at N scratches
# FLIP-COUNT-2 Adversary(b): a partial fill that never resolves defers exits
# at most this many polls, then the unfilled remainder is CANCELLED and the
# position proceeds at its booked size (fail toward a known state, loud).
# Custody mechanism, not a trading threshold.
FLIP_STUCK_PARTIAL_POLLS = 3
# WO-UNCOVERED-FLATTEN: a cover attempt gets this many cycles to CONFIRM a
# resting exit (a determined CUT books via async fill; a maker take rests
# next cycle). Past it, a leg still uncovered while its cover keeps
# proposing-and-rejecting (the 192145 self-net void) is escalated:
# reconcile → retry → FLATTEN → FATAL, one stage per cycle. Custody
# mechanism, not a trading threshold.
FLIP_HEAL_GRACE_CYCLES = 2


# ── pure signal helpers — byte-identical to flip_mode.py:284-347 ───────────
def tick_direction(ticks, n: int) -> Optional[str]:
    """Sign of the last n spot deltas: all up -> 'yes' (rising favors above-strike),
    all down -> 'no', mixed/insufficient -> None."""
    if ticks is None or len(ticks) < n + 1:
        return None
    recent = [t for t in ticks if t is not None][-(n + 1):]
    if len(recent) < n + 1:
        return None
    deltas = [recent[i + 1] - recent[i] for i in range(n)]
    if all(d > 0 for d in deltas):
        return "yes"
    if all(d < 0 for d in deltas):
        return "no"
    return None


def book_lean(yes_bid_qty: Optional[int], no_bid_qty: Optional[int]) -> Optional[str]:
    """Which side the book leans by best-bid size, or None if unclear/absent."""
    if yes_bid_qty is None or no_bid_qty is None:
        return None
    if yes_bid_qty > no_bid_qty:
        return "yes"
    if no_bid_qty > yes_bid_qty:
        return "no"
    return None


def ofi_side(ticks, lean_side: Optional[str]) -> Optional[str]:
    """OFI GATE — fail closed. Returns the side to join iff the last FLIP_OFI_TICKS
    spot deltas ALL agree AND the book lean agrees; otherwise None (no signal /
    disagreement / would-fade -> wait). Never fades the tape."""
    d = tick_direction(ticks, FLIP_OFI_TICKS)
    if d is None or lean_side is None or lean_side != d:
        return None
    return d


def spot_adverse(side: str, spot: Optional[float], lo, hi) -> bool:
    """Is spot on the losing side of the strike for the held leg? Conservative: only
    a single clear threshold counts; a two-sided range or missing data -> not adverse."""
    if spot is None:
        return False
    strike = None
    if lo is not None and hi is None:
        strike = lo
    elif hi is not None and lo is None:
        strike = hi
    if strike is None:
        return False
    return (spot < strike) if side == "yes" else (spot > strike)


def scratch_reason(side: str, entry_cents: int, leg_mark, markout_30,
                   prev_markout, spot_adverse_polls: int) -> Optional[str]:
    """The scratch signal — cross out NOW when any of (a) leg mark <= entry-S,
    (b) spot through the strike sustained >=2 polls, (c) markout at +30s <= -stop
    and worsening. Else None. (These REASONS are also encoded as the custodian's
    FLIP cut-params — the custodian executes, this function documents.)"""
    if leg_mark is not None and leg_mark <= entry_cents - FLIP_SCRATCH_S:
        return f"mark {leg_mark}<=entry-{FLIP_SCRATCH_S}"                     # (a)
    if spot_adverse_polls >= 2:
        return "spot through strike (2 polls)"                                # (b)
    if (markout_30 is not None and markout_30 <= -FLIP_MARKOUT_STOP
            and prev_markout is not None and markout_30 < prev_markout):
        return f"markout {markout_30}c worsening"                             # (c)
    return None


def flip_cut_params() -> CutParams:
    """P21 A5 OVERTURNED the P3-era scratch mapping here: FLIP exits are
    INTENT-BASED and lane-owned (HUNT: Job-B fast bails · OPEN: the patient
    hold — no stop, no scratch, no time-box while the book is undetermined).
    The custodian keeps ONLY the catastrophic backstop. SOURCE: the −11¢
    (10:30) and −12¢ (11:17) round-trips were OPEN-intent positions killed
    by fast-intent stops — cited as the last of their kind."""
    return CutParams(
        hard_stop_usd=999.0, soft_stop_usd=999.0,
        min_time_remaining_s=2, hold_to_settle_s=0,
        spot_danger_buffer_usd=0, grace_period_s=0,
        early_exit_window_s=0, early_exit_loss_fraction=1.0,
        max_loss_fraction_of_balance=1.0, max_loss_fraction_of_cost=1.0,
        catastrophic_loss_cents=90,
        spot_safe_buffer_early_usd=0.01, spot_safe_buffer_late_usd=0.01,
        spot_safe_cutoff_s=FLIP_FLAT_AT,
        rapid_drop_threshold=1.0, rapid_drop_window_s=10,
        max_loss_cents_per_contract=100,
        reversal_threshold=1.0, reversal_threshold_settling=1.0,
        reversal_threshold_profit=1.0, profit_tighten_above_entry=1.0,
        peak_window_s=30, proactive_after_s=0, prob_floor=0.0,
    )


# ── per-window lane state ──────────────────────────────────────────────────
@dataclass
class FlipWindow:
    market: str
    close_ts: float
    posted: Dict[str, dict] = field(default_factory=dict)   # side -> {oid, price, ts}
    fills: Dict[str, int] = field(default_factory=dict)     # side -> entry cents
    first_fill_ts: Optional[float] = None
    takes_posted: Dict[str, str] = field(default_factory=dict)  # held side -> exit oid
    # FLIP-COUNT-1: the resting exit's COUNT per rung-A side (the invariant
    # compares booked-held against covered size, not covered existence).
    take_counts: Dict[str, int] = field(default_factory=dict)
    uncovered_paged: set = field(default_factory=set)  # sides paged this window
    # FLIP-COUNT-2 §3.2: a fill landing while its bucket is CLOSING buffers
    # here (side -> {entry, count, bucket}) and re-opens position-aware the
    # moment the old leg's exit accounting concludes — never re-increments
    # a closed record, never falls to the legacy w.fills path.
    late_fills: Dict[str, dict] = field(default_factory=dict)
    uncovered_healed: set = field(default_factory=set)  # §3.3 cover-once guard
    # WO-UNCOVERED-FLATTEN: an uncovered leg is not HEALED until a resting
    # exit CONFIRMS in the book (the 192145 leg rode 45→70→settle because
    # the heal marked intent done while every cover self-net-rejected into
    # the void). heal_attempts: side -> escalation stage (0 heal proposed,
    # 1 reconcile+retry, 2 flatten fired). flatten_oids: side -> (oid, n)
    # of a CONFIRMED flatten crossing to close.
    heal_attempts: Dict[str, int] = field(default_factory=dict)
    heal_grace: Dict[str, int] = field(default_factory=dict)
    heal_covered: Dict[str, int] = field(default_factory=dict)
    flatten_oids: Dict[str, tuple] = field(default_factory=dict)
    # P-FLIP-THESIS-1 §1 (Scientist: LOG-ONLY until measured): continuity —
    # does the prior window's settlement direction predict this entry side?
    continuity_logged: bool = False
    # WO-BLEED-1: HUNT never averages down — the 20→13→8 "converging" chase
    # was each level re-entering as a fresh event. One direction per
    # window; re-entry only ABOVE the prior entry; one loss sits the
    # window out. OPEN is untouched.
    hunt_dir: Optional[str] = None
    hunt_last_entry: Optional[int] = None
    hunt_lost: bool = False
    hunt_refuse_logged: bool = False
    trips: int = 0
    scratches: int = 0
    window_realized: int = 0
    done: bool = False
    sitout_paged: bool = False   # P13 §4: the sit-out pages ONCE
    spot_ticks: List[Optional[float]] = field(default_factory=list)
    # P18 HUNT state: the pending confirm and the open hunt positions.
    hunt_pending: Optional[dict] = None            # {side, confirms, last_cost}
    hunts: Dict[str, dict] = field(default_factory=dict)  # side -> position state
    hunt_count: int = 0
    # P21 A4/A5 OPEN state: side -> {entry, fill_ts, take_oid, take_proposed,
    # collapse_polls, done}. The patient hold lives here.
    opens: Dict[str, dict] = field(default_factory=dict)
    open_no_grain_logged: bool = False   # OPEN_NO_GRAIN tags ONCE per window
    # P26 §3.1: ONE SHOT PER WINDOW — set on ANY OPEN exit (take, determined,
    # yield — the Adversary: takes too), cleared ONLY at rollover (the
    # note_exit pop was the located loophole). Re-proposals tag themselves.
    open_consumed: bool = False
    open_consumed_logged: bool = False
    open_geometry_logged: bool = False   # OPEN_BAD_GEOMETRY tags once


class LaneFlip:
    """The FLIP lane behind the relay interface: proposes entries and take
    quotes each cycle; the gateway walls them; the custodian owns exits."""

    name = "FLIP"

    def __init__(self, gateway, custodian=None, stats=None):
        self.gateway = gateway
        self.custodian = custodian
        self.stats = stats  # lane_losses_recent for the kill rule
        self.windows: Dict[str, FlipWindow] = {}
        self.stop_streak = 0
        self.killed = False
        self.alert_fn = lambda msg: None  # P13 §4: the sit-out page (runner wires)
        if custodian is not None:
            custodian.set_lane_params("FLIP", flip_cut_params())

    # -- helpers ------------------------------------------------------------
    def _window(self, market: str, close_ts: float) -> FlipWindow:
        w = self.windows.get(market)
        if w is None or w.close_ts != close_ts:
            w = FlipWindow(market=market, close_ts=close_ts)
            self.windows[market] = w
        return w

    def _net(self, market: str, event: str) -> int:
        if self.gateway is None:
            return 0
        return self.gateway.positions.get((event, market, "FLIP"), 0)

    def _booked_held(self, market: str, side: str) -> Optional[int]:
        """FLIP-COUNT-1 §2.2: the booked ledger's held count for a FLIP
        side — unsettled ENTRY counts minus exit counts. Returns None when
        the ledger holds no ENTRY rows for the side (no truth to clamp to;
        in-memory counts govern — ledger-less unit paths stay honest)."""
        ledger = getattr(self.gateway, "ledger", None) if self.gateway else None
        if ledger is None:
            return None
        held, entries = ledger.db.execute(
            "SELECT COALESCE(SUM(CASE WHEN action='ENTRY' THEN count"
            " ELSE -count END), 0), SUM(CASE WHEN action='ENTRY' THEN 1"
            " ELSE 0 END) FROM fills WHERE market=? AND lane='FLIP'"
            " AND side=? AND settled=0", (market, side)).fetchone()
        if not entries:
            return None
        return max(0, int(held))

    def _exit_count(self, market: str, side: str, rec_count: int) -> int:
        """FLIP-COUNT-1 (Adversary): an exit sells min(memory, booked-held),
        clamped >= 0 — the lane must never offer more contracts than the
        ledger says the account holds."""
        booked = self._booked_held(market, side)
        if booked is None:
            return rec_count
        return max(0, min(rec_count, booked))

    def _entry_in_flight(self, market: str, side: str) -> bool:
        """FLIP-COUNT-2 §3.1: venue truth — a FLIP ENTRY order for this side
        is PARTIALLY filled and still resting (the gateway keeps an order in
        `resting` until its full count books; `filled_counts` says whether
        any part has). Exits act only on fully-booked positions — the
        cash-protocol quiescence posture, never act mid-settlement."""
        if self.gateway is None:
            return False
        for oid, o in getattr(self.gateway, "resting", {}).items():
            if (o.lane == "FLIP" and o.market == market and o.side == side
                    and o.purpose == "ENTRY" and o.action == "buy"
                    and self.gateway.filled_counts.get(oid, 0) > 0):
                return True
        return False

    def _defer_or_cancel_partial(self, rec: dict, market: str,
                                 side: str) -> bool:
        """§3.1 + Adversary(b): returns True while the exit should DEFER
        (entry partially filled, under the poll budget). At the budget the
        unfilled remainder is CANCELLED — the position proceeds at its
        booked size, loudly (fail toward a known state)."""
        if not self._entry_in_flight(market, side):
            rec["defer_polls"] = 0
            return False
        rec["defer_polls"] = rec.get("defer_polls", 0) + 1
        if rec["defer_polls"] < FLIP_STUCK_PARTIAL_POLLS:
            return True
        from . import failures
        for oid, o in list(getattr(self.gateway, "resting", {}).items()):
            if (o.lane == "FLIP" and o.market == market and o.side == side
                    and o.purpose == "ENTRY" and o.action == "buy"):
                self.gateway.cancel(oid)
        failures.fail("FLIP_STUCK_PARTIAL",
                      f"{market} {side}: entry partial unresolved for "
                      f"{FLIP_STUCK_PARTIAL_POLLS} polls — remainder "
                      "cancelled, exiting the booked size",
                      fatal=False, alert=True, market=market, side=side)
        rec["defer_polls"] = 0
        return False

    def _promote_late_fill(self, w: FlipWindow, market: str, side: str,
                           now: float) -> None:
        """FLIP-COUNT-2 §3.2: the closing bucket concluded — the buffered
        late leg re-opens as a FRESH position-aware custody record (clamped
        to booked truth), so it gets the normal exit machinery. Its entry
        price is the fill's own (the blended anchor rides with it —
        Engineer: a reopened leg with no entry would silently disable the
        loss-term geometry)."""
        lf = w.late_fills.pop(side, None)
        if lf is None:
            return
        n = self._exit_count(market, side, lf["count"])
        if n <= 0:
            return                      # venue says nothing is held: no leg
        if lf["bucket"] == "hunts":
            w.hunts[side] = {"entry": lf["entry"], "fill_ts": now,
                             "count": n, "take_oid": None,
                             "take_proposed": False, "be_ts": None,
                             "be_repriced": False, "entry_oid": None,
                             "defer_polls": 0}
        else:
            w.opens[side] = {"entry": lf["entry"], "fill_ts": now,
                             "count": n, "take_oid": None,
                             "take_proposed": False, "collapse_polls": 0, "catastrophe_polls": 0,
                             "det_ts": None,
                             "entry_oid": None, "defer_polls": 0}
        log.warning("FLIP_LATE_FILL_REOPEN %s %s x%d @ %dc — late leg gets "
                    "a position-aware exit", market, side, n, lf["entry"])

    def note_fill(self, market: str, side: str, price_cents: int, now: float,
                  count: int = 1) -> None:
        """Called by the fills wiring when a FLIP entry books. Updates window
        state (custody counts, trip accounting). count rides in so an
        earned-tier fill (P22: LEAN=2 lots) exits at its full size."""
        w = self.windows.get(market)
        if w is None:
            return
        # FLIP-COUNT-1 §2.1 (the orphaned second contract): a SECOND
        # same-side fill MERGES into the existing custody bucket — the
        # popped mode marker must never route it to the legacy rung-A dict
        # (whose only exit sold a literal 1; today's ×2 no@48 left one
        # contract riding to $0). Entry blends count-weighted (Engineer:
        # the take math stays honest) and any resting take is cancelled so
        # custody re-proposes at the merged size and blended entry.
        for bucket in (w.hunts, w.opens):
            rec = bucket.get(side)
            if rec is not None:
                if not rec.get("done"):
                    add = max(1, count)
                    total = rec["count"] + add
                    rec["entry"] = int(round((rec["entry"] * rec["count"]
                                              + price_cents * add) / total))
                    rec["count"] = total
                    if rec.get("take_oid") and self.gateway is not None:
                        self.gateway.cancel(rec["take_oid"])
                        rec["take_oid"] = None
                    rec["take_proposed"] = False
                    w.posted.pop(side, None)
                    log.warning("FLIP-COUNT-1 merge %s %s: +%d -> count %d @ "
                                "blended %dc", market, side, add, total,
                                rec["entry"])
                    return
                # FLIP-COUNT-2 §3.2 (the 191030 race): the bucket is CLOSING
                # — its exit already fired between the two halves of the
                # fill. NEVER re-increment a closed record (that made the
                # second contract invisible to every exit). Buffer the late
                # leg; note_exit promotes it to a fresh position-aware
                # record the moment the old leg's accounting concludes.
                lf = w.late_fills.get(side)
                add = max(1, count)
                if lf is None:
                    w.late_fills[side] = {
                        "entry": price_cents, "count": add,
                        "bucket": "opens" if bucket is w.opens else "hunts"}
                else:
                    total = lf["count"] + add
                    lf["entry"] = int(round((lf["entry"] * lf["count"]
                                             + price_cents * add) / total))
                    lf["count"] = total
                w.posted.pop(side, None)
                log.warning("FLIP-COUNT-2 late fill %s %s x%d @ %dc buffered "
                            "— bucket closing; reopens when the old leg "
                            "concludes", market, side, add, price_cents)
                return
        # P18: a hunt fill opens hunt custody, never the pair machinery
        if (w.posted.get(side) or {}).get("mode") == "HUNT":
            entry_oid = (w.posted.get(side) or {}).get("oid")
            w.posted.pop(side, None)
            w.hunt_count += 1
            w.trips += 1   # hunts consume the same ratchet discipline
            w.hunts[side] = {"entry": price_cents, "fill_ts": now,
                             "count": max(1, count),
                             "take_oid": None, "take_proposed": False,
                             "be_ts": None, "be_repriced": False,
                             "entry_oid": entry_oid, "defer_polls": 0}
            return
        # P21 A4: an OPEN fill starts THE PATIENT HOLD — its own custody,
        # never the retired pair machinery, never HUNT's fast jobs.
        if (w.posted.get(side) or {}).get("mode") == "OPEN":
            entry_oid = (w.posted.get(side) or {}).get("oid")
            w.posted.pop(side, None)
            w.trips += 1   # the ratchet still counts every trip
            w.opens[side] = {"entry": price_cents, "fill_ts": now,
                             "count": max(1, count),
                             "take_oid": None, "take_proposed": False,
                             "collapse_polls": 0, "catastrophe_polls": 0,
                             "det_ts": None,
                             "entry_oid": entry_oid, "defer_polls": 0}
            return
        w.fills[side] = price_cents
        if w.first_fill_ts is None:
            w.first_fill_ts = now
            w.trips += 1
        if len(w.fills) == 2:
            cap = 100 - (w.fills["yes"] + w.fills["no"])
            w.window_realized += cap
            log.warning("FLIP rung A netted %s: %d+%d -> +%dc", market,
                        w.fills["yes"], w.fills["no"], cap)

    def note_exit(self, market: str, side: str, exit_price_cents: int,
                  now: float, count: int = 1) -> None:
        """Called by the fills wiring when a FLIP exit/cut books. Realizes the
        leg against its entry and resets the trip slot (the ratchet's next trip
        may then open, R1-gated). FLIP-COUNT-1: count rides in — a merged
        bucket realizes ×count, decrements, and survives until depleted (a
        partial fill must never orphan the remainder's accounting)."""
        w = self.windows.get(market)
        if w is None:
            return
        n = max(1, count)
        # WO-UNCOVERED-FLATTEN: a booked exit CHANGES custody — the heal
        # clock resets (a reopened leg deserves fresh grace, a partial
        # close is progress). The flatten's pending-close claim retires as
        # it lands.
        w.heal_attempts.pop(side, None)
        w.heal_grace.pop(side, None)
        w.heal_covered.pop(side, None)
        if side in w.flatten_oids:
            oid, remaining = w.flatten_oids[side]
            if remaining - n <= 0:
                w.flatten_oids.pop(side, None)
            else:
                w.flatten_oids[side] = (oid, remaining - n)
        # P18: a hunt exit realizes against ITS entry and leaves the pair
        # slots untouched — the two modes never share accounting state.
        hunt = w.hunts.get(side)
        if hunt is not None:
            sold = min(n, hunt["count"])
            realized = (exit_price_cents - hunt["entry"]) * sold
            w.window_realized += realized
            if realized < 0:
                w.scratches += 1
                # WO-BLEED-1 §2.2: one HUNT loss ends the window's hunting
                w.hunt_lost = True
            hunt["count"] -= sold
            if hunt["count"] <= 0:
                w.hunts.pop(side, None)
                # FLIP-COUNT-2 §3.2: the old leg concluded — a buffered
                # late fill re-opens NOW, position-aware, never orphaned
                self._promote_late_fill(w, market, side, now)
            return
        # P21 A5: an OPEN exit realizes against ITS entry. NO scratch count —
        # the patient hold has no scratches, only TAKE / DETERMINED / YIELD.
        o = w.opens.get(side)
        if o is not None:
            sold = min(n, o["count"])
            w.window_realized += (exit_price_cents - o["entry"]) * sold
            o["count"] -= sold
            if o["count"] <= 0:
                w.opens.pop(side, None)
                # WO-FLIP-CHEAP-LIVE §3 — the two instruments fire at
                # CONCLUSION (Engineer: never at entry; took_swing is
                # unknowable until the position is flat)
                self._log_swing_outcome(market, o["entry"],
                                        exit_price_cents, now,
                                        o.get("fill_ts", now))
                self._promote_late_fill(w, market, side, now)
            # P26 §3.1: ANY OPEN exit consumes the window's one shot — the
            # pop above no longer re-opens the door (the located loophole).
            w.open_consumed = True
            return
        entry_px = w.fills.get(side)
        if entry_px is not None and len(w.fills) < 2:
            realized = (exit_price_cents - entry_px) * n
            w.window_realized += realized
            if realized < 0:
                w.scratches += 1
        # trip over: clear the slot for a possible OFI-gated re-entry
        w.posted.clear()
        w.fills.clear()
        w.takes_posted.clear()
        w.take_counts.clear()
        w.first_fill_ts = None

    def note_window_result(self, market: str, realized_cents: int) -> None:
        """Window close accounting. P27 §1b OVERTURNED the stop-streak lane
        kill (R2's per-lane kill semantics — the same class as fh8's Wall
        3c): the streak still COUNTS for the packs, but it kills nothing.
        The account halt is THE stop; nothing else stops entries, ever."""
        stopped = realized_cents <= -FLIP_STOP_CENTS
        self.stop_streak = self.stop_streak + 1 if stopped else 0
        if stopped and self.stop_streak >= FLIP_PAUSE_AFTER_STOPS:
            log.warning("FLIP stop-streak %d (reporting only — P27: the "
                        "per-lane kill is dead; the account halt governs)",
                        self.stop_streak)

    # -- the cycle ----------------------------------------------------------
    def evaluate(self, market: str, ctx: dict) -> List[Order]:
        """Returns the cycle's proposals (0..n Orders). The runner submits them
        in arbitration order; wall rejections (including venue post-only cross)
        are normal — the lane re-proposes at the fresh join next cycle."""
        if self.killed:
            return []
        now = ctx.get("now", time.time())
        close_ts = ctx.get("close_ts")
        book = ctx.get("book")
        if close_ts is None or book is None:
            return []
        secs = close_ts - now
        if secs > FLIP_WINDOW_SEC + 5 or secs < FLIP_FLAT_AT:
            return []
        w = self._window(market, close_ts)
        if w.done:
            return []
        event = market.rsplit("-", 1)[0]
        w.spot_ticks.append(ctx.get("spot"))

        proposals: List[Order] = []
        yes_bid, no_bid = book.best_yes_bid(), book.best_no_bid()

        # 1) TAKE QUOTES for held legs (entry+X, passive EXIT — the one exit
        #    proposal; custodian owns it from registration)
        net = self._net(market, event)
        for side, entry_px in list(w.fills.items()):
            held = (net > 0 and side == "yes") or (net < 0 and side == "no")
            # WO-UNCOVERED-FLATTEN: a custody bucket (incl. a healed
            # record) owns the side's exit — rung-A never double-covers
            if side in w.opens or side in w.hunts:
                continue
            if held and side not in w.takes_posted and len(w.fills) < 2:
                q = entry_px + FLIP_X
                # FLIP-COUNT-1 §2.2: the take sells the BOOKED held size,
                # never a literal 1 (today's orphan: ×2 filled, ×1 sold,
                # the survivor rode to $0). Ledger truth governs; the
                # in-memory fallback is 1 only when no ledger exists.
                booked = self._booked_held(market, side)
                take_n = booked if booked is not None else 1
                if take_n <= 0:
                    continue
                proposals.append(Order(
                    lane="FLIP", event=event, market=market, side=side,
                    action="sell", price_cents=q, count=take_n,
                    size_tier=config.TIER_PROBE, purpose="EXIT",
                    reason=f"take entry+{FLIP_X}"))

        # 1.5) P18 HUNT custody — JOB A takes, JOB B bails, curfew flat.
        # Runs EVERY cycle regardless of the discipline gates below (risk
        # management never waits its turn).
        proposals.extend(self._hunt_custody(w, market, event, book, secs, now))

        # 1.6) P21 A5 OPEN custody — THE PATIENT HOLD's three exits. Also
        # every cycle, before any entry gate.
        proposals.extend(self._open_custody(w, market, event, book, ctx,
                                            secs, now))

        # 1.7) FLIP-COUNT-1 §2.3 + WO-UNCOVERED-FLATTEN: every booked-held
        # contract is covered by a CONFIRMED resting exit, this cycle's
        # proposal, or a flatten — never bare, never a ride to settlement.
        self._check_uncovered(w, market, proposals, book=book, event=event)

        # 2) ENTRIES — curfew, trips, sit-out, R1-flat, the OPEN setup
        past_curfew = secs <= FLIP_CURFEW
        if w.trips >= FLIP_MAX_TRIPS or w.scratches >= FLIP_SCRATCH_SITOUT:
            # P13 §4: sit-out visibility — one page, then quiet discipline
            if w.scratches >= FLIP_SCRATCH_SITOUT and not w.sitout_paged:
                w.sitout_paged = True
                self.alert_fn(f"🚪 FLIP sitting out {market} — "
                              f"{w.scratches} scratches")
            return proposals
        if w.window_realized <= -FLIP_STOP_CENTS:
            if not w.done:
                w.done = True
                self.note_window_result(market, w.window_realized)
            return proposals
        if past_curfew:
            return proposals

        # P18 §1: mode selection — a live needle signal makes this HUNT
        # ground; PAIR posts only on genuinely two-way books with NO needle.
        sl = ctx.get("spotlead")
        needle_active = (sl is not None
                         and sl.delta_p >= config.HUNT_NEEDLE_POINTS)
        # P19 §2.4: one hand exits, the other waits — a salvage in progress
        # suppresses HUNT entries; the reversal hunt may fire AFTER, never
        # during.
        if ctx.get("salvage_active"):
            w.hunt_pending = None
        else:
            proposals.extend(self._hunt_entry(w, market, event, book, sl, secs))
        if needle_active:
            # P21 A6 — HUNT SENIORITY: a live confirmed needle means the
            # market is MOVING; OPEN's premise (undetermined, herd-priced)
            # is void while it lasts. Hunt owns the floor.
            return proposals

        # P21 A4 — LANE OPEN (PAIR retired 0718: the venue nets one
        # account's sides, so the pair-bundle was a fiction). Setup: BOTH
        # sides inside the open band AND the grain runs >= OPEN_MIN_GRAIN.
        # P26 §2/§3 tightened the lane that leaked: entries T-15→T-8 only
        # (§3.3), ONE shot per window (§3.1), risk geometry proven (§3.4),
        # and the PROOF LAW — OPEN fires on its OWN scoreboard margin >= 0,
        # or in explicit PROBE mode while its cells fill. The lane argues
        # from its receipts or sits.
        if w.opens or w.posted or w.fills:
            return proposals               # one open position/post at a time
        if w.open_consumed:
            if not w.open_consumed_logged:
                w.open_consumed_logged = True
                log.info("OPEN_WINDOW_CONSUMED %s — one story per window; "
                         "re-propose refused until rollover", market)
            return proposals               # §3.1: one shot per window
        if secs <= config.OPEN_ENTRY_CUTOFF:
            return proposals               # §3.3: OPEN's window is T-15→T-8
        if self._net(market, event) != 0:
            return proposals               # R1 WALL: one position at a time
        # WO-FLIP-EVERY-MARKET-LIQUIDITY (build 49): the "both sides in
        # OPEN_BAND" gate is RETIRED — it rejected biased opens (the expensive
        # side out of band) and made FLIP wait for a balanced book, backwards.
        # FLIP is the LIQUIDITY PROVIDER: it buys the cheap side of EVERY
        # biased open. The only entry filter now is the CHEAP side being
        # BUYABLE (OPEN_ENTRY_FLOOR..OPEN_MAX_ENTRY), plus the true-50/50 skip
        # (no cheap side) and the needle trend-guard above (never buy a market
        # genuinely running). Two-sided book required (need both bids to find
        # the cheap side).
        if yes_bid is None or no_bid is None:
            return proposals               # no two-sided book: no cheap side
        # WO-FLIP-IMMEDIATE-ENTRY (build 47): the grain-wait is RETIRED. The
        # liquidity-hold capstone overturned "waiting IS the setup" — FLIP
        # buys the opening IMBALANCE the instant it is cheap; it does not wait
        # for a grain streak to confirm (grain=None returned no proposals
        # window after window and structurally blocked the lane while F
        # traded). The entry side is the CHEAP side of the band — the lower
        # bid, the side the pile-in abandoned; grain, if present, only INFORMS
        # the why, it no longer gates. The TREND-GUARD is already in force
        # above: needle_active (HUNT seniority, delta_p >= HUNT_NEEDLE_POINTS)
        # returns before this gate on ANY live spot trend — stricter than the
        # WO's "decisive trend against the cheap side" — so FLIP never buys
        # into a market that is genuinely running. The empirical gate stays the
        # measured swing/fill rate (Instrument 1) below and the resting-take
        # fill rate after entry; PROOF stays measured, only the entry opens.
        g = ctx.get("grain")
        if yes_bid == no_bid:
            if not w.open_no_grain_logged:
                w.open_no_grain_logged = True
                log.info("OPEN_NO_IMBALANCE %s — band book but sides equal "
                         "(y%dc/n%dc): no cheap side to provide against",
                         market, yes_bid, no_bid)
            return proposals               # true 50/50: no imbalance to buy
        side = "yes" if yes_bid < no_bid else "no"   # the cheap (abandoned) side
        join = yes_bid if side == "yes" else no_bid
        if not (config.OPEN_ENTRY_FLOOR <= join <= config.OPEN_MAX_ENTRY_CENTS):
            if not w.open_geometry_logged:
                w.open_geometry_logged = True
                log.info("OPEN_CHEAP_OUT_OF_RANGE %s — cheap %s %dc outside "
                         "buyable [%d,%d]: too paid-up or too near-worthless",
                         market, side, join, config.OPEN_ENTRY_FLOOR,
                         config.OPEN_MAX_ENTRY_CENTS)
            return proposals               # cheap side not buyable this window
        # WO-FLIP-LIQUIDITY-HOLD (build 45): the risk/reward GEOMETRY GATE is
        # GONE. Build 43 grounded it on the scalp stop as the "real bail" —
        # but the liquidity-hold model REMOVES that stop: a low mark after a
        # FLIP buy is ILLIQUIDITY (the opening pile-in, no buyers on your side
        # yet), not a loss to bail from. The position holds through it as the
        # resting liquidity provider, so there is no pre-trade risk/reward
        # rejection to make. The ENTRY filter is band membership (buy the
        # cheap pile-in, above); the EMPIRICAL gate is the resting-take fill
        # (reversion) rate, measured by Instrument 1 at the 1-lot cap (§4) and
        # proven before any size increase. (Removing this gate un-grounds the
        # build-43 coherence check honestly: with no scalp stop there is no
        # bail to size risk against — an honest risk-to-catastrophe check
        # would just close the lane, contra the hold-through-illiquidity
        # thesis.)
        # WO-FLIP-CHEAP-LIVE §2.2 — THE TWO-SIDED SWING GATE: buy the
        # cheap side only when the table says the swing to the take is
        # more likely than the cut firing first. p_cross(d_strike, t) =
        # P(spot touches the strike within the window) — the take REQUIRES
        # the touch, every cut-first path is a no-touch path, so one cell
        # answers both legs (Engineer: same p_cross, apples-to-apples).
        # Table/spot absent -> permissive (live-proof wants data; the
        # UNPROVEN tagging already narrates the blindness).
        swing = self._swing_gate(ctx, now, join, side, market)
        if swing is not None and not swing["ok"]:
            if not w.open_no_grain_logged:   # reuse the once-per-window mute
                w.open_no_grain_logged = True
                log.info("OPEN_SWING_REFUSED %s — %s (measured took_swing "
                         "%.2f n=%d; shadow p_up=%s p_down=%s): "
                         "losing-cheap, not oversold-cheap", market,
                         swing["reason"], swing["p"], swing["n"],
                         swing.get("p_up"), swing.get("p_down"))
            return proposals
        # P27 §2(b) OVERTURNED the margin GATE: entry proceeds on band +
        # grain + geometry + one-shot (the doctrine); the cell's margin
        # PRINTS on the why either way — the scoreboard informs daily,
        # governs never. (OPEN_CELL_NEGATIVE's sit died with the ruling.)
        from . import scoring
        s = scoring.score(self.gateway.ledger, "OPEN",
                          scoring.price_cell(join))
        proof = (f"margin {s['margin']:+.2f} (n={s['n']}"
                 + (", info)" if s["margin"] < 0 else ")"))
        # P-FLIP-THESIS-1 §1 — THE CONTINUITY SIGNAL, LOG-ONLY (Scientist:
        # it votes on entry side only after the data shows edge; the
        # Adversary: it may never override a determined cut)
        self._log_continuity(w, market, side)
        grain_note = (f"grain~{g.get('direction')}x{g.get('length')} informs"
                      if g else "grain~none")
        proposals.append(Order(
            lane="FLIP", event=event, market=market, side=side,
            action="buy", price_cents=join, count=1,
            size_tier=config.TIER_PROBE, purpose="ENTRY",
            band=(config.OPEN_ENTRY_FLOOR, config.OPEN_MAX_ENTRY_CENTS),
            rest_fp=book.best_fp(side),
            why=f"OPEN liquidity {side}@{join}c cheap (spread "
                f"{abs(yes_bid - no_bid)}c) · book y{yes_bid}/n{no_bid} · "
                f"rest→middle {config.OPEN_MIDDLE_TARGET}c · {proof} · "
                + (swing["why"] if swing is not None else "swing~untabled")
                + f" · {grain_note}"))
        return proposals

    @staticmethod
    def _take_price(entry: int) -> int:
        """WO-FLIP-EVERY-MARKET-LIQUIDITY (build 49) — the resting take,
        toward the MIDDLE scaled by entry depth: the 50/50 middle where the
        hedgers are forced to transact, floored fee-safe at entry+MIN so a
        near-middle entry still clears fees, capped at the 99c tick. The
        cheaper the entry, the bigger the gouge (buy 39 → rest 52 = +13; buy
        49 → rest 54 = +5)."""
        return min(99, max(config.OPEN_MIDDLE_TARGET,
                           entry + config.OPEN_TAKE_MIN))

    @staticmethod
    def held_price(side: str, book):
        """WO-FLIP-SIDE-ORIENT: THE canonical FLIP price — the bid for the
        HELD side (the held contract's own price, which rises when the held
        side wins). Every FLIP exit quantity (entry, take, band floor,
        catastrophe, cut) is expressed against THIS, so a NO@46 and a
        YES@46 receive mathematically identical treatment relative to their
        own scale — the geometry is symmetric by construction. (F stays
        one-directional; only FLIP looks both ways.) The exit path already
        read the held-side bid; this names it so no raw-YES value ever
        leaks into FLIP exit math."""
        return book.best_yes_bid() if side == "yes" else book.best_no_bid()

    @staticmethod
    def _take_cents(contracts: int) -> int:
        """WO-FLIP-GOAL-TAKE: the goal-bounded take in cents — the reliable
        convergence move, not a rare +20. The per-window book goal divided by
        the contracts held, clamped to a fee-safe floor and today's ceiling:
            take = clamp(ceil(BOOK_GOAL / held), OPEN_TAKE_MIN, OPEN_TAKE_MAX)
        At 1 lot this is the goal itself (bank the nickel); as size grows the
        per-contract take shrinks toward the floor and volume carries the goal
        (CAPSTONE Part B). The floor clears the ~2c round-trip taker fee with
        margin, so the take is always net-positive — never sub-fee."""
        n = max(1, int(contracts))
        goal_per_contract = math.ceil(config.WINDOW_BOOK_GOAL_CENTS / n)
        return max(config.OPEN_TAKE_MIN,
                   min(config.OPEN_TAKE_MAX, goal_per_contract))

    def _take_target(self, market: str, side: str, rec_count: int) -> int:
        """The goal-bounded take for a held position — sized to the BOOKED
        held count (Engineer's flag: the take math must match reality, not the
        memory count), falling back to the record count only when the ledger
        holds no truth (unit paths). Recomputes on every proposal, so a
        second same-side fill (which cancels the resting take and re-proposes)
        lands a fresh goal-bounded target at the new size."""
        booked = self._booked_held(market, side)
        contracts = booked if (booked is not None and booked > 0) else rec_count
        return self._take_cents(contracts)

    def _measured_swing_rate(self, band_cell: int):
        """WO-SWING-GATE-EVENT §4.1: Instrument 1's ground truth — the
        rolling took_swing rate for this entry's price band (FLIP_SWING
        rows). Returns (rate, n) or (None, 0) when the surface is absent.
        This is the MEASURED event the gate should test, not the
        strike-touch proxy that rubber-stamped every cheap entry."""
        from . import scoring
        ledger = getattr(self.gateway, "ledger", None) if self.gateway else None
        if ledger is None:
            return None, 0
        try:
            rows = ledger.db.execute(
                "SELECT detail FROM surface_rows WHERE state='FLIP_SWING'"
                " ORDER BY id DESC LIMIT 200").fetchall()
        except Exception:
            return None, 0
        took, n = 0, 0
        for (d,) in rows:
            try:
                r = json.loads(d)
            except Exception:
                continue
            if scoring.price_cell(int(r.get("entry_price", -1))) != band_cell:
                continue
            n += 1
            if r.get("took_swing"):
                took += 1
        return (took / n if n else None), n

    def _shadow_two_barrier(self, ctx: dict, join: int, t_rem: float,
                            side: str):
        """WO-SWING-GATE-EVENT §2 (SHADOW — logged, never live yet), now
        SIDE-ORIENTED (WO-FLIP-SIDE-ORIENT §C.3): P(held contract reaches
        join+TAKE before the band-floor cut), each CONTRACT-PRICE barrier
        translated into the spot move that reprices the HELD contract that
        far. THE SIGN THE GATE MUST CARRY: the table's p_cross = P(spot
        crosses the strike) = P(YES wins), so a YES contract's price IS
        p_cross but a NO contract's price is its COMPLEMENT (P(NO wins) =
        1 − p_cross). The retired code used join/100 for both sides — a
        NO@46 was mapped to P(cross)=0.46 when its real implied P(cross)
        is 0.54. Fixed: NO looks up the complement, so a NO position and
        its YES mirror get symmetric p_up/p_down. Drives nothing until it
        tracks Instrument 1. Returns (p_up, p_down) or (None, None)."""
        from . import delta
        if not delta.is_loaded() or t_rem <= 0:
            return None, None
        # WO-FLIP-GOAL-TAKE: the barrier is the ACTUAL take the fresh entry
        # rests (goal-bounded at a 1-lot probe), so the shadow measures
        # reachability of the reachable take, not the retired +20.
        take_px = min(99, join + self._take_cents(1))
        cut_px = max(1, config.OPEN_UNDETERMINED_BAND[0])   # band floor

        def _dist(held_px):
            # held-side WIN probability = held price / 100; the table
            # prices in P(YES wins) = p_cross, so NO's target is the
            # complement (the side sign, Part C.3)
            p_hold = held_px / 100.0
            p_cross_target = p_hold if side == "yes" else 1.0 - p_hold
            return delta.distance_for_p(p_cross_target, t_rem)
        d0, d_up, d_down = _dist(join), _dist(take_px), _dist(cut_px)
        if d0 is None or d_up is None or d_down is None:
            return None, None
        p_up = delta.p_cross(abs(d0 - d_up), t_rem)
        p_down = delta.p_cross(abs(d0 - d_down), t_rem)
        return p_up, p_down

    def _swing_gate(self, ctx: dict, now: float, join: int,
                    side: str, market: str) -> Optional[dict]:
        """WO-SWING-GATE-EVENT: the old gate measured P(spot crosses the
        strike) = p_cross(|spot−strike|, t) — trivially ~0.89 for every
        cheap entry (a cheap side is cheap BECAUSE spot sits near the
        strike, so d is tiny), so it rubber-stamped falling knives. The
        LIVE gate now tests Instrument 1's MEASURED took_swing rate for
        the entry's price band; below OPEN_SWING_MIN_SAMPLES it is
        permissive (the 1-lot cap bounds the risk while data accrues). §2's
        two-barrier price model + the retired strike-touch proxy ride along
        as SHADOW fields for calibration. None only when spot/time is
        absent (permissive, tagged)."""
        from . import delta, scoring, spotlead as _sl
        close_ts = ctx.get("close_ts")
        if close_ts is None:
            return None
        t_rem = close_ts - now
        if t_rem <= 0:
            return None
        band = scoring.price_cell(join)
        rate, n = self._measured_swing_rate(band)
        p_up, p_down = self._shadow_two_barrier(ctx, join, t_rem, side)
        # the retired strike-touch proxy, kept as a SHADOW to quantify the
        # bug (constant ~0.89 across bands is the symptom)
        proxy = None
        spot = ctx.get("spot")
        if spot is not None and delta.is_loaded():
            strike = _sl.pick_strike(spot, ctx.get("boundary_lo"),
                                     ctx.get("boundary_hi"))
            if strike is not None:
                proxy = delta.p_cross(abs(spot - strike), t_rem)
        # LIVE decision: the measured rate is ground truth once it exists
        calibrated = rate is not None and n >= config.OPEN_SWING_MIN_SAMPLES
        if calibrated:
            ok = rate >= config.OPEN_SWING_MIN_P
            reason = (f"measured took_swing {rate:.2f} "
                      f"{'>=' if ok else '<'} {config.OPEN_SWING_MIN_P} "
                      f"(n={n}, band {band}-{band + 4}c)")
        else:
            ok = True   # calibrating: permissive, bounded by the 1-lot cap
            reason = (f"calibrating (n={n}<{config.OPEN_SWING_MIN_SAMPLES}); "
                      "1-lot cap bounds the risk")
        # a compare row per entry — predicted vs measured, for §3 calibration
        surface = getattr(self.custodian, "surface", None) \
            if self.custodian is not None else None
        if surface is not None:
            try:
                surface.write_row(
                    "FLIP", market, f"w-{market}", "SWING_GATE_COMPARE",
                    detail=json.dumps({
                        "band": band, "join": join,
                        "measured_rate": rate, "n": n,
                        "shadow_p_up": p_up, "shadow_p_down": p_down,
                        "old_proxy_p": proxy}))
            except Exception:
                pass
        p_up_s = f"{p_up:.2f}" if p_up is not None else "na"
        p_down_s = f"{p_down:.2f}" if p_down is not None else "na"
        why = (f"swing measured={rate:.2f}n{n}" if calibrated
               else f"swing calibrating n{n}") + \
              f" · shadow p_up={p_up_s}/p_down={p_down_s}"
        return {"ok": ok, "p": rate if rate is not None else 0.0, "n": n,
                "reason": reason, "p_up": p_up, "p_down": p_down,
                "proxy": proxy, "why": why}

    # ── P18 THE DETECTIVE: hunt entry (§2) + the two jobs (§3) ─────────────
    def _hunt_entry(self, w: FlipWindow, market: str, event: str, book,
                    sl, secs: float) -> List[Order]:
        """The three questions as arithmetic — ALL required. ONE hunt per
        displacement event (the runner re-anchors on submit)."""
        if secs <= FLIP_CURFEW:            # no hunt entries after the handoff
            w.hunt_pending = None
            return []
        if sl is None:                     # spot BLIND / table absent / no move
            w.hunt_pending = None
            return []
        if sl.delta_p < config.HUNT_NEEDLE_POINTS:      # gate A
            w.hunt_pending = None
            return []
        side = sl.side                     # always WITH spot, never against
        # WO-BLEED-1 §2.2: one HUNT loss in the window sits the rest out —
        # the "so many of so many" doctrine applied intra-window.
        if w.hunt_lost:
            w.hunt_pending = None
            return []
        if side in w.hunts or side in w.posted or side in w.fills:
            return []                      # one open hunt/leg per side
        join = book.best_yes_bid() if side == "yes" else book.best_no_bid()
        if join is None:
            return []
        # WO-BLEED-1 §2.1: HUNT never averages down. One direction per
        # window; a re-entry at OR BELOW the prior HUNT entry (Adversary:
        # <=, never <) is a collapsing market, not a fresh opportunity —
        # "converging" meant "the book paused," not "the needle recovered."
        if w.hunt_dir is not None and side != w.hunt_dir:
            w.hunt_pending = None
            if not w.hunt_refuse_logged:
                w.hunt_refuse_logged = True
                log.info("HUNT_REFUSE_LOWER %s %s: direction locked to %s "
                         "this window (no flip-flop)", market, side,
                         w.hunt_dir)
            return []
        if (w.hunt_last_entry is not None
                and join <= w.hunt_last_entry):
            w.hunt_pending = None
            if not w.hunt_refuse_logged:
                w.hunt_refuse_logged = True
                log.info("HUNT_REFUSE_LOWER %s %s: this %dc <= prior %dc — "
                         "no averaging down; sit out (WO-BLEED-1)",
                         market, side, join, w.hunt_last_entry)
            return []
        gap = sl.fair_cents - join
        if gap < config.HUNT_GAP_CENTS:                 # gate B
            w.hunt_pending = None
            return []
        # gate C: convergence + sustained confirm (lane_p's flicker-proof
        # pattern applied to the SPOT move) — the book may tick toward spot
        # or sit flat; ACTIVELY repricing against kills the hunt.
        pend = w.hunt_pending
        if pend is None or pend["side"] != side:
            w.hunt_pending = {"side": side, "confirms": 1, "last_cost": join}
            return []
        if join < pend["last_cost"]:
            w.hunt_pending = None          # the crowd is fighting the move
            return []
        pend["confirms"] += 1
        pend["last_cost"] = join
        if pend["confirms"] < config.HUNT_CONFIRM_FRAMES:
            return []
        w.hunt_pending = None
        why = (f"HUNT {sl.casefile()} · gap {gap:.0f} · converging")
        return [Order(
            lane="FLIP", event=event, market=market, side=side,
            action="buy", price_cents=join, count=1,
            size_tier=config.TIER_PROBE, purpose="ENTRY",
            band=config.HUNT_BAND, rest_fp=book.best_fp(side), why=why)]

    def _hunt_custody(self, w: FlipWindow, market: str, event: str, book,
                      secs: float, now: float) -> List[Order]:
        """§3 — JOB A: flip it (take at entry+T the instant the fill books).
        JOB B: hold nothing (breakeven reprice, then flatten). TIME-BOX and
        CURFEW flat. No averaging, no thesis-defense — inventory is F's
        privilege, not the flip's."""
        props: List[Order] = []
        for side, h in list(w.hunts.items()):
            if h.get("done"):
                continue
            # FLIP-COUNT-2 §3.1: an exit fires only on a FULLY-BOOKED
            # position — a partial in flight defers (bounded, then the
            # remainder cancels loudly). The 191030 race dies here.
            if self._defer_or_cancel_partial(h, market, side):
                continue
            mark = self.held_price(side, book)   # WO-FLIP-SIDE-ORIENT: canonical held-side price
            # JOB A: the take, posted the instant the entry fills.
            # FLIP-COUNT-1: sized to min(memory, booked-held), never more.
            if h["take_oid"] is None and not h.get("take_proposed"):
                n = self._exit_count(market, side, h["count"])
                if n <= 0:
                    h["done"] = True
                    continue
                h["take_proposed"] = True
                props.append(Order(
                    lane="FLIP", event=event, market=market, side=side,
                    action="sell",
                    price_cents=h["entry"] + config.HUNT_TAKE_CENTS,
                    count=n, size_tier=config.TIER_PROBE, purpose="EXIT",
                    reason=f"hunt take entry+{config.HUNT_TAKE_CENTS}"))
                continue
            flatten_reason = None
            if secs <= FLIP_CURFEW:
                flatten_reason = "hunt curfew flat (T-curfew handoff to F)"
            elif mark is not None and mark <= h["entry"] - 2:
                flatten_reason = "hunt bail mark<=entry-2"
            elif (h.get("be_ts") is not None
                  and now - h["be_ts"] >= config.HUNT_BAIL_R_S):
                flatten_reason = (f"hunt bail not-out-in-"
                                  f"{int(config.HUNT_BAIL_R_S)}s")
            elif now - h["fill_ts"] >= config.HUNT_TIMEBOX_M_S:
                flatten_reason = (f"hunt time-box "
                                  f"{int(config.HUNT_TIMEBOX_M_S)}s")
            if flatten_reason:
                if h["take_oid"] is not None and self.gateway is not None:
                    self.gateway.cancel(h["take_oid"])
                    h["take_oid"] = None
                px = mark if mark is not None else h["entry"]
                h["done"] = True
                n = self._exit_count(market, side, h["count"])
                if n > 0:
                    props.append(Order(
                        lane="FLIP", event=event, market=market, side=side,
                        action="sell", price_cents=px, count=n,
                        size_tier=config.TIER_PROBE, purpose="CUT",
                        crossfire=True, reason=flatten_reason))
                continue
            # JOB B step 1: breakeven trigger — reprice the take to entry
            if (mark is not None and mark <= h["entry"]
                    and not h.get("be_repriced")):
                h["be_repriced"] = True
                h["be_ts"] = now
                if h["take_oid"] is not None and self.gateway is not None:
                    self.gateway.cancel(h["take_oid"])
                    h["take_oid"] = None
                h["take_proposed"] = True
                n = self._exit_count(market, side, h["count"])
                if n <= 0:
                    h["done"] = True
                    continue
                props.append(Order(
                    lane="FLIP", event=event, market=market, side=side,
                    action="sell", price_cents=h["entry"], count=n,
                    size_tier=config.TIER_PROBE, purpose="EXIT",
                    reason="hunt breakeven reprice (mark<=entry)"))
        return props

    # ── P21 A5 THE PATIENT HOLD · P26 §3 the evacuation fork ───────────────
    def _open_custody(self, w: FlipWindow, market: str, event: str, book,
                      ctx: dict, secs: float, now: float) -> List[Order]:
        """Inside the undetermined band there is NO stop, NO scratch, NO
        time-box — wiggles inside the geometry are the product working.
        Exits are EXACTLY three, and the executor FORKS BY INTENT (P26
        §3.2: TAKE rests as a maker; evacuations CROSS at best NOW — the
        `maker unfilled 10s` slide cost the 31/20/33 fills on ~35 triggers):
          TAKE       — entry + goal-bounded take (WO-FLIP-GOAL-TAKE:
                       clamp(book-goal/held, MIN, MAX)), resting from the fill
          DETERMINED-AGAINST — mark below max(band floor, entry−drop)
                       (§3.4 geometry v2), or ΔP-collapse >= K sustained
                       2 polls → crossfire IMMEDIATELY
          YIELD      — flat by T-OPEN_FLAT_BY (§3.3 YIELD_TO_F: F's floor
                       is F's; the SELF_NET storm class dies by schedule)"""
        props: List[Order] = []
        sl = ctx.get("spotlead")
        for side, o in list(w.opens.items()):
            if o.get("done"):
                continue
            # FLIP-COUNT-2 §3.1: never exit a partial — defer until the
            # entry is fully booked (bounded; then cancel the remainder)
            if self._defer_or_cancel_partial(o, market, side):
                continue
            mark = self.held_price(side, book)   # WO-FLIP-SIDE-ORIENT: canonical held-side price
            # TAKE — posted the instant the entry books (the maker intent).
            # FLIP-COUNT-1: sized to min(memory, booked-held), never more.
            # WO-FLIP-EVERY-MARKET-LIQUIDITY (build 49): the take rests toward
            # the MIDDLE scaled by entry depth (_take_price) — buy cheap, sell
            # to the forced hedgers at the 50/50; the cheaper the entry, the
            # bigger the gouge. Held-side price (orientation-correct, build 41).
            if o["take_oid"] is None and not o.get("take_proposed"):
                n = self._exit_count(market, side, o["count"])
                if n <= 0:
                    o["done"] = True
                    continue
                o["take_proposed"] = True
                take_px = self._take_price(o["entry"])
                o["take_px"] = take_px          # B3 walks this down late
                props.append(Order(
                    lane="FLIP", event=event, market=market, side=side,
                    action="sell", price_cents=take_px,
                    count=n, size_tier=config.TIER_PROBE,
                    purpose="EXIT",
                    reason=f"open take → middle {take_px}c "
                           f"(entry {o['entry']}, gouge +{take_px - o['entry']})"))
                continue
            # P-FLIP-THESIS-1 §4 — HOLD-INSTEAD-OF-SCALP: at the patience
            # assessment, a side deciding in FLIP's favor hard enough that
            # F would be buying it (mark through F's band floor) converts —
            # never sell the scalp and re-buy high; the 49c basis IS the
            # settlement position. Determined-against stays armed on the
            # hold (Engineer: a hold-to-settle is not exempt).
            if (not o.get("hold")
                    and now - o["fill_ts"] >= config.OPEN_PATIENCE_S
                    and mark is not None):
                from .lane_fh8 import COST_BAND_LO_INT
                if mark >= COST_BAND_LO_INT:
                    self._convert_to_hold(o, market, side, mark, "F-agrees",
                                          now=now)
                    continue
            # §3.5 T-10 BOOK-AWARE HANDOFF (replaces the blind YIELD_TO_F
            # flat): per position, never reflexive. Winner (mark at/above
            # basis) -> LEFT TO F as hold-to-settle at FLIP's cheap basis;
            # loser -> SOLD now, never dumped into the settlement zone.
            if secs <= config.OPEN_FLAT_BY and not o.get("hold"):
                if mark is None:
                    continue    # no book truth: never a BLIND flat; retry
                if mark >= o["entry"]:
                    self._convert_to_hold(o, market, side, mark,
                                          "T-10-handoff-winner", now=now)
                    continue
                self._cancel_resting(o)
                o["done"] = True
                n = self._exit_count(market, side, o["count"])
                if n > 0:
                    props.append(Order(
                        lane="FLIP", event=event, market=market, side=side,
                        action="sell", price_cents=mark, count=n,
                        size_tier=config.TIER_PROBE,
                        purpose="CUT", crossfire=True,
                        reason="open T-10 handoff: clear loser before "
                               "F's window"))
                continue
            if o.get("hold"):
                # held to settlement — no scalp take, no yield; the
                # determined-against floor below still guards it
                pass
            # DETERMINED-AGAINST — WO-FLIP-LIQUIDITY-HOLD (build 45): a low
            # mark after a FLIP buy is ILLIQUIDITY (the opening pile-in, no
            # buyers on your side yet), NOT a loss. The position HOLDS through
            # it as the resting liquidity provider — the take above waits for
            # the reversion. So the reactive early stops are GONE: the build-43
            # scalp stop (entry−6) and the post-patience band floor both sold
            # inventory during the exact illiquidity you must hold through.
            # What REMAINS is the collapse backstop (Adversary-mandated §2.3):
            # a CONFIRMED collapse is a real move, not illiquidity noise, and
            # still cuts even in the passive hold —
            #   (1) SPOT decided against — sustained 2 polls (the market ran
            #       away hard), ANY time
            #   (3) CATASTROPHE floor (20) — the deep P&L-blind backstop
            # The loss-side ENDGAME exit is the T-10 handoff above (now the
            # PRIMARY loss exit): an unreverted loser is cleared before F's
            # window, never ridden to settlement.
            catastrophe = config.OPEN_CATASTROPHE_FLOOR
            collapse = (sl is not None and sl.side != side
                        and sl.delta_p >= config.OPEN_DETERMINED_K_POINTS)
            o["collapse_polls"] = o["collapse_polls"] + 1 if collapse else 0
            # WO-FLIP-CATASTROPHE-ILLIQUIDITY (build 48): the catastrophe PRICE
            # floor must tell a THIN-BOOK low bid (illiquidity — the opening
            # pile-in, HELD) from a GENUINE collapse. A fresh cheap entry's
            # held-side bid sits low because there are no buyers YET; the
            # single-poll price cut was reading that emptiness as collapse and
            # dumping at the bottom (the 2-minute catastrophic exit). It now
            # fires only when the low is (a) REAL — depth on the held side, not
            # a 1-lot thin quote; (b) PAST the opening-illiquidity window (a
            # fresh entry's low bid is the setup); and (c) SUSTAINED >= 2 polls.
            # A real move is caught by the spot-decided branch regardless.
            held_depth = book.visible_depth(side, mark) if mark is not None else 0
            past_opening = now - o["fill_ts"] >= config.OPEN_OPENING_WINDOW_S
            cat_hit = (mark is not None and mark <= catastrophe
                       and held_depth >= config.OPEN_CATASTROPHE_MIN_DEPTH
                       and past_opening)
            o["catastrophe_polls"] = \
                o.get("catastrophe_polls", 0) + 1 if cat_hit else 0
            determined = None
            if o["collapse_polls"] >= 2:
                determined = (f"open determined-against: SPOT decided — "
                              f"ΔP-collapse {sl.delta_p:.0f}pts sustained "
                              "(a real move, not illiquidity — cut in the hold)")
            elif o["catastrophe_polls"] >= 2:
                determined = (f"open determined-against: CATASTROPHE floor "
                              f"{side} {mark}c <= {catastrophe}c — a REAL "
                              f"sustained low (depth {held_depth}, past the "
                              "opening window), not thin-book illiquidity")
            if determined:
                self._cancel_resting(o)
                o["done"] = True
                n = self._exit_count(market, side, o["count"])
                if n > 0:
                    props.append(Order(
                        lane="FLIP", event=event, market=market, side=side,
                        action="sell",
                        price_cents=mark if mark is not None else max(1, o["entry"] - 1),
                        count=n, size_tier=config.TIER_PROBE,
                        purpose="CUT", crossfire=True,
                        reason=f"{determined} · evacuate now"))
            elif (not o.get("hold") and o.get("take_px") is not None
                  and past_opening
                  and config.OPEN_FLAT_BY < secs <= config.OPEN_WALK_START_S):
                # WO-FLIP-EVERY-MARKET-LIQUIDITY B3 (build 49) — the ACTIVE
                # late-window walk-down. No cut is determined and no reversion
                # has lifted the middle-take; as the clock runs toward T-10,
                # step the resting MAKER take DOWN from the middle toward
                # scratch (linear over [WALK_START, FLAT_BY]), re-posting lower,
                # so a non-reverting position exits gracefully near breakeven
                # late — NEVER ridden unfilled into a catastrophic bell dump.
                # Never below scratch (the T-10 handoff owns a loser). LATE
                # window only — the early hold is protected by the catastrophe-
                # illiquidity guards above; this is not a reactive early cut.
                span = config.OPEN_WALK_START_S - config.OPEN_FLAT_BY
                frac = max(0.0, min(1.0, (secs - config.OPEN_FLAT_BY) / span))
                full = self._take_price(o["entry"])
                stepped = max(o["entry"],
                              int(round(o["entry"] + frac * (full - o["entry"]))))
                if stepped < o["take_px"]:
                    self._cancel_resting(o)
                    n = self._exit_count(market, side, o["count"])
                    if n > 0:
                        o["take_px"] = stepped
                        o["take_proposed"] = True
                        props.append(Order(
                            lane="FLIP", event=event, market=market, side=side,
                            action="sell", price_cents=stepped, count=n,
                            size_tier=config.TIER_PROBE, purpose="EXIT",
                            reason=f"open walk-down → {stepped}c "
                                   f"(T-{int(secs)}s: middle→scratch, "
                                   f"entry {o['entry']})"))
        return props

    def _check_uncovered(self, w: FlipWindow, market: str,
                         proposals: List[Order], book=None,
                         event: str = None) -> None:
        """FLIP-COUNT-1 §2.3 + WO-UNCOVERED-FLATTEN: COVER OR FLATTEN,
        NEVER BARE. A leg is not healed until a resting exit CONFIRMS in
        the book against the booked-held count (the 192145 leg rode
        45→70→settle because the old heal marked its INTENT healed while
        every cover self-net-rejected into the void). The escalation, one
        stage per cycle:
          detect  → page + reconcile custody vs broker + revive the record
          stage 0 → the cover proposed; if it CONFIRMS (take_oid) → healed
          stage 1 → cover failed to land: §2.2 self-net reconcile (cancel
                    conflicting resting sells, clamp counts to booked) +
                    ONE retry
          stage 2 → still bare: FLATTEN at market NOW, crossfire, clamped
                    to booked-net (Engineer) — a bounded loss beats an
                    unbounded ride
          after   → a flatten that itself fails is the only FATAL, and it
                    fires with the close already attempted (§2.4: never
                    FATAL with a naked leg still open and untried)."""
        from . import failures
        proposing = {p.side for p in proposals
                     if p.action == "sell" and p.purpose in ("EXIT", "CUT")}
        for side in ("yes", "no"):
            held = self._booked_held(market, side)
            if held is None or held <= 0:
                w.heal_attempts.pop(side, None)
                w.heal_covered.pop(side, None)
                continue
            covered = 0
            provenance = []
            h = w.hunts.get(side)
            if h is not None and h.get("take_oid") is not None:
                covered += h.get("take_count", h["count"])
                provenance.append(f"hunts:{h['count']}")
            o = w.opens.get(side)
            if o is not None and o.get("take_oid") is not None:
                covered += o.get("take_count", o["count"])
                provenance.append(f"opens:{o['count']}")
            if side in w.takes_posted:
                covered += w.take_counts.get(side, 1)
                provenance.append(f"fills:{w.fills.get(side)}")
            if side in w.flatten_oids:
                covered += w.flatten_oids[side][1]   # a crossing close
            if held <= covered:
                # CONFIRMED resting exit(s) against the booked count —
                # only now is the side healed (§2.1: never on intent).
                w.heal_attempts.pop(side, None)
                w.heal_grace.pop(side, None)
                w.heal_covered.pop(side, None)
                continue
            # P-FLIP-THESIS-1: a HOLD-TO-SETTLE deliberately rests no exit
            if ((h is not None and h.get("hold"))
                    or (o is not None and o.get("hold"))):
                continue
            # a freshly-revived/live record awaiting its FIRST propose
            # cycle is not yet a failed attempt — the take proposes next
            if ((h is not None and not h.get("done")
                 and not h.get("take_proposed"))
                    or (o is not None and not o.get("done")
                        and not o.get("take_proposed"))):
                continue
            # WO-UNCOVERED-FLATTEN: a leg is healed only when a resting
            # exit CONFIRMS (covered rises). heal_grace gives a cover
            # attempt up to GRACE cycles to confirm (async CUT fills, maker
            # rests); a confirmed cover raises `covered` → progress reset.
            # heal_attempts drives the escalation once grace is spent — the
            # self-net void that proposes-and-rejects forever crosses grace
            # and escalates reconcile → retry → FLATTEN → FATAL.
            prev_cov = w.heal_covered.get(side)
            w.heal_covered[side] = covered
            if prev_cov is not None and covered > prev_cov:
                w.heal_attempts.pop(side, None)   # progress — reset
                w.heal_grace.pop(side, None)
            gap = held - covered
            esc = w.heal_attempts.get(side, 0)
            if esc == 0 and side in proposing:
                g = w.heal_grace.get(side, 0) + 1
                w.heal_grace[side] = g
                if g <= FLIP_HEAL_GRACE_CYCLES:
                    # WO-HALT-ORPHAN §2A: the TRANSIENT (a cover in flight
                    # this cycle) is INFO, never a page — the page is
                    # reserved for a leg that fails grace (below). Firing
                    # on the transient trained the eye to skip the ONE
                    # real naked leg.
                    if g == 1:
                        log.info("FLIP_UNCOVERED transient %s %s: held %d > "
                                 "covered %d, cover in flight — grace",
                                 market, side, held, covered)
                    continue     # a cover is in flight; let it confirm
            w.heal_grace.pop(side, None)
            if esc == 0:
                # §2.1 detect + reconcile + revive: page ONCE, reconcile
                # against broker truth (cancel the unconfirmable stale
                # sells), heal the WHOLE remaining leg as a once-proposing
                # opens record
                if side not in w.uncovered_paged:
                    w.uncovered_paged.add(side)
                    failures.fail(
                        "FLIP_UNCOVERED_LEG",
                        f"{market} {side}: held {held} > covered {covered}",
                        fatal=False, alert=True, market=market, side=side,
                        held=held, covered=covered,
                        buckets=";".join(provenance) or "none")
                self._reconcile_side(w, market, side)
                still = self._booked_held(market, side)
                if still is None or still <= 0:
                    # §2.2 phantom gap: broker says nothing to cover
                    w.heal_attempts.pop(side, None)
                    w.heal_covered.pop(side, None)
                    continue
                self._heal_uncovered(w, market, side, still)
                w.heal_attempts[side] = 1
            elif esc == 1:
                # §2.2 the ONE retry: reconcile again, re-propose fresh
                self._reconcile_side(w, market, side)
                for bucket in (w.hunts, w.opens):
                    rec = bucket.get(side)
                    if rec is not None and rec["count"] > 0:
                        rec["done"] = False
                        rec["take_proposed"] = False
                        rec["take_oid"] = None
                w.heal_attempts[side] = 2
            elif esc == 2:
                # §2.3 THE DEADLINE: cover unconfirmed after reconcile +
                # retry — FLATTEN at market NOW, booked-net clamped
                # (Engineer), never bare
                mark = (book.best_yes_bid() if side == "yes"
                        else book.best_no_bid()) if book is not None else None
                if mark is None:
                    continue    # no book truth this cycle; retry the flatten
                n = self._exit_count(market, side, gap)
                if n <= 0:
                    w.heal_attempts.pop(side, None)
                    w.heal_covered.pop(side, None)
                    continue
                for bucket in (w.hunts, w.opens):
                    rec = bucket.get(side)
                    if rec is not None:
                        rec["done"] = True
                ev = event or market.rsplit("-", 1)[0]
                proposals.append(Order(
                    lane="FLIP", event=ev, market=market, side=side,
                    action="sell", price_cents=mark, count=n,
                    size_tier=config.TIER_PROBE, purpose="CUT",
                    crossfire=True,
                    reason="FLIP_UNCOVERED_FLATTENED: cover unconfirmed "
                           "after reconcile+retry — never bare"))
                failures.fail(
                    "FLIP_UNCOVERED_FLATTENED",
                    f"{market} {side}: x{n} flattened at {mark}c — cover "
                    "could not be confirmed after reconcile+retry; a "
                    "bounded loss now beats an unbounded ride",
                    fatal=False, alert=True, market=market, side=side,
                    count=n, price=mark)
                w.heal_attempts[side] = 3
            else:
                # §2.4 the flatten itself never registered a close — a leg
                # that can neither cover nor close is a true integrity
                # fault; stop with the close already attempted, never
                # FATAL with a naked untried leg.
                failures.fail(
                    "FLIP_UNCOVERED_UNHEALABLE",
                    f"{market} {side}: held {held} > covered {covered} "
                    "after reconcile, retry, AND a flatten attempt — a leg "
                    "that can neither cover nor close is an integrity "
                    "fault; stopping with it loudly flagged",
                    fatal=True, market=market, side=side,
                    held=held, covered=covered)

    def _reconcile_side(self, w: FlipWindow, market: str, side: str) -> None:
        """§2.2 SELF-NET RECONCILE: the venue refusing the cover means the
        custody count and broker truth disagree. Clamp every record on the
        side to the booked ledger (a phantom gap dies here — broker says
        smaller, nothing to cover) and cancel EVERY conflicting resting
        FLIP sell on the side (the stale-order artifact behind the
        self-net storm) so the fresh cover can land."""
        booked = self._booked_held(market, side)
        for bucket in (w.hunts, w.opens):
            rec = bucket.get(side)
            if rec is None:
                continue
            if booked is not None and rec["count"] > booked:
                log.warning("FLIP_SELFNET_RECONCILE %s %s: custody count "
                            "%d -> booked %d (phantom gap corrected)",
                            market, side, rec["count"], booked)
                rec["count"] = booked
                if booked <= 0:
                    bucket.pop(side, None)
                    continue
            if rec.get("take_oid") is not None and self.gateway is not None:
                self.gateway.cancel(rec["take_oid"])
                rec["take_oid"] = None
        stale = w.takes_posted.pop(side, None)
        w.take_counts.pop(side, None)
        if stale is not None and self.gateway is not None:
            self.gateway.cancel(stale)
        if self.gateway is not None:
            for oid, o in list(getattr(self.gateway, "resting", {}).items()):
                if (o.lane == "FLIP" and o.market == market
                        and o.side == side and o.action == "sell"):
                    self.gateway.cancel(oid)

    def _heal_uncovered(self, w: FlipWindow, market: str, side: str,
                        gap: int) -> None:
        """§3.3: cover the uncovered — revive the closed record (its own
        entry price keeps realization honest) or open a fresh one at the
        ledger's booked entry, sized to the gap. WO-UNCOVERED-FLATTEN:
        this is INTENT only — the side is healed when the exit CONFIRMS,
        never here."""
        w.late_fills.pop(side, None)          # absorbed into the healed leg
        for bucket in (w.hunts, w.opens):
            rec = bucket.get(side)
            if rec is not None:               # revive the closed record
                rec.update(done=False, take_proposed=False, take_oid=None,
                           count=gap, defer_polls=0)
                log.warning("FLIP_UNCOVERED self-heal %s %s: revived closed "
                            "record x%d @ %dc", market, side, gap,
                            rec["entry"])
                return
        entry = None
        ledger = getattr(self.gateway, "ledger", None) if self.gateway else None
        if ledger is not None:
            row = ledger.db.execute(
                "SELECT price_cents FROM fills WHERE market=? AND lane='FLIP'"
                " AND side=? AND action='ENTRY' AND settled=0"
                " ORDER BY id DESC LIMIT 1", (market, side)).fetchone()
            entry = row[0] if row else None
        w.opens[side] = {"entry": entry if entry is not None else 50,
                         "fill_ts": 0.0, "count": gap, "take_oid": None,
                         "take_proposed": False, "collapse_polls": 0, "catastrophe_polls": 0,
                         "det_ts": None, "entry_oid": None,
                         "defer_polls": 0}
        log.warning("FLIP_UNCOVERED self-heal %s %s: opened fresh record "
                    "x%d @ %sc (booked entry)", market, side, gap, entry)

    def _log_continuity(self, w: FlipWindow, market: str, side: str) -> None:
        """P-FLIP-THESIS-1 §1: the crowd wants the last regime to continue
        (bear begets bear). LOG-ONLY — once per window, agree/disagree
        between the prior settled window's direction and the chosen entry
        side. It earns a VOTE only after measurement shows edge; today it
        builds the dataset."""
        if w.continuity_logged:
            return
        ledger = getattr(self.gateway, "ledger", None) if self.gateway else None
        if ledger is None:
            return
        try:
            row = ledger.db.execute(
                "SELECT market, settled_yes FROM window_outcomes"
                " WHERE market != ? ORDER BY ts DESC LIMIT 1",
                (market,)).fetchone()
        except Exception:
            return
        if row is None:
            return
        w.continuity_logged = True
        prior = "yes" if row[1] else "no"
        log.info("OPEN_CONTINUITY %s prior_window=%s (%s) side=%s agree=%s "
                 "(log-only — votes after measurement)", market, prior,
                 row[0], side, side == prior)

    def _log_swing_outcome(self, market: str, entry: int, exit_px: int,
                           now: float, fill_ts: float,
                           held_to_settle: bool = False) -> None:
        """WO-FLIP-CHEAP-LIVE §3 INSTRUMENT 1+2: every cheap OPEN entry
        logs its conclusion — after 20-30 rows this IS the measured swing
        rate (the 80% stops being a guess). Losers additionally audit the
        salvage floor: a cut past the band-floor expectation flags
        FLIP_FLOOR_BREACH — the assumption whose failure inverts the EV,
        caught on the FIRST loser, before the rate-halt could see a
        streak. Per-contract cents."""
        surface = getattr(self.custodian, "surface", None) \
            if self.custodian is not None else None
        if surface is None:
            return
        gross = exit_px - entry
        # WO-FLIP-GOAL-TAKE: "took" now means the REACHABLE take fired — the
        # exit made at least the goal-bounded floor move (OPEN_TAKE_MIN), not
        # the retired +20. This is the instrument §4 wants: with a reachable
        # target the took-rate rises sharply where the +20 rarely printed.
        took = exit_px >= entry + config.OPEN_TAKE_MIN - 1
        detail = {"market": market, "entry_price": entry,
                  "took_swing": bool(took), "exit_price": exit_px,
                  "gross_cents": gross, "salvaged": gross < 0,
                  "secs_to_swing": round(max(0.0, now - fill_ts), 1),
                  "held_to_settle": held_to_settle}
        try:
            surface.write_row("FLIP", market, f"w-{market}", "FLIP_SWING",
                              detail=json.dumps(detail))
        except Exception:
            return   # the instrument never blocks custody accounting
        if gross < 0:
            floor_expected = entry - config.OPEN_UNDETERMINED_BAND[0]
            loss = -gross
            ok = loss <= floor_expected + config.FLIP_FLOOR_SLIP_CENTS
            try:
                surface.write_row(
                    "FLIP", market, f"w-{market}", "FLIP_LOSER_CUT",
                    detail=json.dumps({
                        "market": market, "entry": entry,
                        "cut_price": exit_px, "loss_cents": loss,
                        "floor_expected": floor_expected, "ok": bool(ok)}))
            except Exception:
                return
            if not ok:
                from . import failures
                failures.fail(
                    "FLIP_FLOOR_BREACH",
                    f"{market}: loser cut {loss}c past the band-floor "
                    f"expectation {floor_expected}c (+"
                    f"{config.FLIP_FLOOR_SLIP_CENTS}c slip) — the -15c "
                    "salvage assumption is BREAKING; the EV table inverts "
                    "if losers ride",
                    fatal=False, alert=True, market=market, entry=entry,
                    cut_price=exit_px, loss=loss)

    def _convert_to_hold(self, o: dict, market: str, side: str, mark,
                         reason: str, now: float = None) -> None:
        """P-FLIP-THESIS-1 §4/§3.5: the scalp becomes the settlement
        position — cancel the resting take, keep the inventory at FLIP's
        cheap basis, and let F stand down (shared inventory). The position
        stays under the determined-against floor and the custodian's
        backstop (salvage-anchored at fill time by the B1 wiring) — a held
        winner that reverses is still cut, never ridden to zero."""
        self._cancel_resting(o)
        o["hold"] = True
        o["take_proposed"] = True     # the scalp take never re-proposes
        log.warning("FLIP_HOLD_TO_SETTLE %s %s basis=%dc mark=%sc x%d "
                    "reason=%s — left to F at the cheaper basis",
                    market, side, o["entry"], mark, o["count"], reason)
        # WO-FLIP-CHEAP-LIVE §3: a hold-conversion IS a swing that arrived
        # (mark at/above basis or through F's band) — instrument it at the
        # conversion mark; the settlement print covers the rest (§1).
        if mark is not None:
            self._log_swing_outcome(market, o["entry"], int(mark),
                                    now if now is not None else time.time(),
                                    o.get("fill_ts", 0.0),
                                    held_to_settle=True)

    def held(self, market: str) -> Dict[str, dict]:
        """§4 THE SHARED INVENTORY: side -> {count, basis} of live FLIP
        custody (scalp or hold) on this market. F consults this BEFORE
        buying a decided side (Adversary: atomic per cycle — F evaluates
        first in registry order and fills book between cycles, so this
        read IS the cycle-start snapshot)."""
        w = self.windows.get(market)
        if w is None:
            return {}
        out: Dict[str, dict] = {}
        for bucket in (w.hunts, w.opens):
            for side, rec in bucket.items():
                if rec.get("done") and not rec.get("hold"):
                    continue
                if rec["count"] > 0:
                    out[side] = {"count": rec["count"],
                                 "basis": rec["entry"]}
        for side, px in w.fills.items():
            out.setdefault(side, {"count": 1, "basis": px})
        return out

    def _cancel_resting(self, o: dict) -> None:
        """Cancel an OPEN position's resting exit (take or determined maker)
        through the gateway; the slot re-registers on the next submit."""
        if o["take_oid"] is not None and self.gateway is not None:
            self.gateway.cancel(o["take_oid"])
            o["take_oid"] = None

    def on_submitted(self, order: Order, order_id: str, now: float) -> None:
        """Runner callback after a successful gateway submit."""
        w = self.windows.get(order.market)
        if w is None:
            return
        if order.purpose == "ENTRY":
            mode = ("HUNT" if order.why.startswith("HUNT")
                    else "OPEN" if order.why.startswith("OPEN")
                    else "PAIR")   # PAIR retired (P21 A4) — legacy tag only
            w.posted[order.side] = {"oid": order_id, "price": order.price_cents,
                                    "ts": now, "mode": mode}
            if mode == "HUNT":
                # WO-BLEED-1: the window's direction locks at the first
                # hunt; re-entries must beat this price (no averaging down)
                w.hunt_dir = order.side
                w.hunt_last_entry = order.price_cents
        elif order.purpose == "EXIT":
            # FLIP-COUNT-1 §2.3: the COUNT registers beside the oid — the
            # uncovered-leg invariant compares sizes, not existence.
            if order.side in w.hunts and w.hunts[order.side]["take_oid"] is None:
                w.hunts[order.side]["take_oid"] = order_id  # P18 JOB A registered
                w.hunts[order.side]["take_count"] = order.count
            elif (order.side in w.opens
                  and w.opens[order.side]["take_oid"] is None):
                w.opens[order.side]["take_oid"] = order_id  # P21 A5 resting exit
                w.opens[order.side]["take_count"] = order.count
            else:
                w.takes_posted[order.side] = order_id
                w.take_counts[order.side] = order.count
            # register the take as the position's resting exit — custodian owns it
            if self.custodian is not None:
                pos = self.custodian.positions.get(f"{order.market}:FLIP")
                if pos is not None:
                    pos.resting_exit_id = order_id
        elif (order.purpose == "CUT"
              and "FLIP_UNCOVERED_FLATTENED" in (order.reason or "")):
            # WO-UNCOVERED-FLATTEN: the flatten CONFIRMED with the venue —
            # it counts as cover-pending-close; the FATAL never fires while
            # a confirmed close is crossing.
            w.flatten_oids[order.side] = (order_id, order.count)

    def pair_grace_expired(self, market: str, now: float) -> Optional[str]:
        """After the grace, the un-filled opposite entry is dropped (its order id
        returned for the runner to cancel through the gateway)."""
        w = self.windows.get(market)
        if w is None or w.first_fill_ts is None:
            return None
        if now - w.first_fill_ts <= FLIP_PAIR_GRACE:
            return None
        for side, rec in list(w.posted.items()):
            if side not in w.fills:
                del w.posted[side]
                return rec["oid"]
        return None
