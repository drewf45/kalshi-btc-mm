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
"""

import logging
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
    """The scratch reasons as custodian cut-params (one exit owner — Trader lens).
    Mapping, knob-for-knob:
      (a) mark <= entry-S       -> max_loss_cents_per_contract = FLIP_SCRATCH_S
      (b) spot through strike   -> spot-safety buffers at 0 (any adverse side cuts
                                   once the custodian's spot check fails)
      (c) markout worsening     -> reversal_threshold = FLIP_MARKOUT_STOP/100 over
                                   a 30s windowed peak
    The dollar stops bound the window at the stop level; the endgame guard is
    T-(FLIP_FLAT_AT) — at T-90 the custodian flattens rather than holds."""
    return CutParams(
        hard_stop_usd=FLIP_STOP_CENTS / 100.0,
        soft_stop_usd=FLIP_STOP_CENTS / 100.0,
        min_time_remaining_s=2,             # flatten runs to close-2 (flip's rejoin_exp)
        hold_to_settle_s=0,                 # flip never holds to settle — it flattens at T-90
        spot_danger_buffer_usd=0,
        grace_period_s=0,                   # scratch logic live from the fill
        early_exit_window_s=0,
        early_exit_loss_fraction=1.0,
        max_loss_fraction_of_balance=0.05,
        max_loss_fraction_of_cost=1.0,
        catastrophic_loss_cents=FLIP_LONE_MAX + 1,
        spot_safe_buffer_early_usd=0.01,    # (b): any adverse side = not safe
        spot_safe_buffer_late_usd=0.01,
        spot_safe_cutoff_s=FLIP_FLAT_AT,
        rapid_drop_threshold=FLIP_MARKOUT_STOP / 100.0,
        rapid_drop_window_s=10,
        max_loss_cents_per_contract=FLIP_SCRATCH_S,    # (a)
        reversal_threshold=FLIP_MARKOUT_STOP / 100.0,  # (c)
        reversal_threshold_settling=FLIP_MARKOUT_STOP / 100.0,
        reversal_threshold_profit=FLIP_MARKOUT_STOP / 100.0,
        profit_tighten_above_entry=1.0,     # no profit-tightening in flip's book
        peak_window_s=30,
        proactive_after_s=0,
        prob_floor=0.0,                     # flip has no probability floor
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

    def note_fill(self, market: str, side: str, price_cents: int, now: float) -> None:
        """Called by the fills wiring when a FLIP entry books. Updates window
        state (pair math, trip accounting)."""
        w = self.windows.get(market)
        if w is None:
            return
        # P18: a hunt fill opens hunt custody, never the pair machinery
        if (w.posted.get(side) or {}).get("mode") == "HUNT":
            w.posted.pop(side, None)
            w.hunt_count += 1
            w.trips += 1   # hunts consume the same ratchet discipline
            w.hunts[side] = {"entry": price_cents, "fill_ts": now,
                             "take_oid": None, "take_proposed": False,
                             "be_ts": None, "be_repriced": False}
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

    def note_exit(self, market: str, side: str, exit_price_cents: int, now: float) -> None:
        """Called by the fills wiring when a FLIP exit/cut books. Realizes the
        leg against its entry and resets the trip slot (the ratchet's next trip
        may then open, R1-gated)."""
        w = self.windows.get(market)
        if w is None:
            return
        # P18: a hunt exit realizes against ITS entry and leaves the pair
        # slots untouched — the two modes never share accounting state.
        hunt = w.hunts.pop(side, None)
        if hunt is not None:
            realized = exit_price_cents - hunt["entry"]
            w.window_realized += realized
            if realized < 0:
                w.scratches += 1
            return
        entry_px = w.fills.get(side)
        if entry_px is not None and len(w.fills) < 2:
            realized = exit_price_cents - entry_px
            w.window_realized += realized
            if realized < 0:
                w.scratches += 1
        # trip over: clear the slot for a possible OFI-gated re-entry
        w.posted.clear()
        w.fills.clear()
        w.takes_posted.clear()
        w.first_fill_ts = None

    def note_window_result(self, market: str, realized_cents: int) -> None:
        """Window close accounting: feed the stop-streak (two consecutive
        stopped windows -> lane kill, R2's per-lane kill semantics)."""
        stopped = realized_cents <= -FLIP_STOP_CENTS
        self.stop_streak = self.stop_streak + 1 if stopped else 0
        if stopped and self.stop_streak >= FLIP_PAUSE_AFTER_STOPS and not self.killed:
            self.killed = True
            if self.custodian is not None:
                self.custodian.kill_lane("FLIP")
            log.warning("FLIP PAUSE — %d consecutive stopped windows; lane killed "
                        "(entries halt, positions custodied)", self.stop_streak)

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
        yq = book.visible_depth("yes", yes_bid) if yes_bid is not None else 0
        nq = book.visible_depth("no", no_bid) if no_bid is not None else 0

        # 1) TAKE QUOTES for held legs (entry+X, passive EXIT — the one exit
        #    proposal; custodian owns it from registration)
        net = self._net(market, event)
        for side, entry_px in list(w.fills.items()):
            held = (net > 0 and side == "yes") or (net < 0 and side == "no")
            if held and side not in w.takes_posted and len(w.fills) < 2:
                q = entry_px + FLIP_X
                proposals.append(Order(
                    lane="FLIP", event=event, market=market, side=side,
                    action="sell", price_cents=q, count=1,
                    size_tier=config.TIER_PROBE, purpose="EXIT",
                    reason=f"take entry+{FLIP_X}"))

        # 1.5) P18 HUNT custody — JOB A takes, JOB B bails, curfew flat.
        # Runs EVERY cycle regardless of the discipline gates below (risk
        # management never waits its turn).
        proposals.extend(self._hunt_custody(w, market, event, book, secs, now))

        # 2) ENTRIES — window phase, curfew, trips, sit-out, R1-flat, OFI
        in_entry_phase = secs > FLIP_WINDOW_SEC - FLIP_ENTRY_SEC
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
            return proposals  # hunt owns the floor while the needle is live

        re_entry = w.trips > 0 and not w.fills  # a new trip after a completed one
        if re_entry:
            if self._net(market, event) != 0:
                return proposals  # R1 WALL: one position at a time
            side_gate = ofi_side(w.spot_ticks, book_lean(yq, nq))
            sides = [side_gate] if side_gate else []
        elif w.trips == 0 and in_entry_phase:
            # RULING 2 (P15, ratified): PAIR-FORMABLE OR NOTHING. The first
            # trip posts only when BOTH sides can legally post (each within
            # side-max, combined within the line) — a lone leg is never
            # OPENED on purpose (the 00:14 tape's no@34 cost 8¢ at 8:01).
            # Pair-grace still governs a pair whose second leg dies later.
            if (yes_bid is not None and no_bid is not None
                    and yes_bid <= FLIP_SIDE_MAX and no_bid <= FLIP_SIDE_MAX
                    and yes_bid + no_bid <= FLIP_LINE):
                sides = ["yes", "no"]
            else:
                sides = []
        else:
            sides = []

        for side in sides:
            if side in w.posted or side in w.fills:
                continue
            join = yes_bid if side == "yes" else no_bid
            fp = book.best_fp(side)  # true-touch resting (the parts' law)
            if join is None or join > FLIP_SIDE_MAX:
                continue
            # combined wall: the SECOND side posts only if the bundle stays
            # <= line against the RESTING first bid (not a stale book)
            if w.posted:
                first = next(iter(w.posted.values()))
                if first["price"] + join > FLIP_LINE:
                    continue
            proposals.append(Order(
                lane="FLIP", event=event, market=market, side=side,
                action="buy", price_cents=join, count=1,
                size_tier=config.TIER_PROBE, purpose="ENTRY",
                band=(1, FLIP_SIDE_MAX), rest_fp=fp,
                why=f"pair-post ≤{FLIP_SIDE_MAX} · y{yes_bid}/n{no_bid}"))
        return proposals

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
        if side in w.hunts or side in w.posted or side in w.fills:
            return []                      # one open hunt/leg per side
        join = book.best_yes_bid() if side == "yes" else book.best_no_bid()
        if join is None:
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
            mark = book.best_yes_bid() if side == "yes" else book.best_no_bid()
            # JOB A: the take, posted the instant the entry fills
            if h["take_oid"] is None and not h.get("take_proposed"):
                h["take_proposed"] = True
                props.append(Order(
                    lane="FLIP", event=event, market=market, side=side,
                    action="sell",
                    price_cents=h["entry"] + config.HUNT_TAKE_CENTS,
                    count=1, size_tier=config.TIER_PROBE, purpose="EXIT",
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
                props.append(Order(
                    lane="FLIP", event=event, market=market, side=side,
                    action="sell", price_cents=px, count=1,
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
                props.append(Order(
                    lane="FLIP", event=event, market=market, side=side,
                    action="sell", price_cents=h["entry"], count=1,
                    size_tier=config.TIER_PROBE, purpose="EXIT",
                    reason="hunt breakeven reprice (mark<=entry)"))
        return props

    def on_submitted(self, order: Order, order_id: str, now: float) -> None:
        """Runner callback after a successful gateway submit."""
        w = self.windows.get(order.market)
        if w is None:
            return
        if order.purpose == "ENTRY":
            w.posted[order.side] = {"oid": order_id, "price": order.price_cents,
                                    "ts": now,
                                    "mode": ("HUNT" if order.why.startswith("HUNT")
                                             else "PAIR")}
        elif order.purpose == "EXIT":
            if order.side in w.hunts and w.hunts[order.side]["take_oid"] is None:
                w.hunts[order.side]["take_oid"] = order_id  # P18 JOB A registered
            else:
                w.takes_posted[order.side] = order_id
            # register the take as the position's resting exit — custodian owns it
            if self.custodian is not None:
                pos = self.custodian.positions.get(f"{order.market}:FLIP")
                if pos is not None:
                    pos.resting_exit_id = order_id

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
