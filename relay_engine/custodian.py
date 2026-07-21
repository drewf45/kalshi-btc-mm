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

import json
import logging
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

from . import config, failures
from .errors import FatalIntegrityError
from .gateway import Gateway, Order

log = logging.getLogger("relay.custodian")

CATASTROPHIC_PROB = 0.05  # salvage-class lanes still cut below this survival probability


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
    salvage_enabled: bool = False    # P19 2.5 (was passthrough): cut-disabled-except-catastrophic-AND-salvage

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
    # P19 §2.1: the SALVAGE anchor, computed at custody registration from the
    # delta table via spotlead's semantics. None = no anchor (table absent at
    # entry) = salvage disabled for this position, tagged.
    d_entry: Optional[float] = None
    t_entry: Optional[float] = None
    p_entry: Optional[float] = None
    # P19 §2.2/2.3: the salvage state machine — ONE attempt per position.
    salvage_strikes: int = 0
    salvage_attempted: bool = False
    salvage_oid: Optional[str] = None
    salvage_ts: float = 0.0
    # SALV-1: the reason tape — which gag is active, how often each gagged,
    # whether salvage fired, and the one-summary latch.
    salvage_gag_reason: Optional[str] = None
    salvage_gag_counts: Dict[str, int] = field(default_factory=dict)
    salvage_gag_transitions: int = 0
    salvage_fired: Optional[str] = None
    salvage_summary_emitted: bool = False


def salvage_params() -> CutParams:
    """P19 §2.5 — the promotion: F (and H8) register with the custodian as
    cut-disabled-except-catastrophic-AND-salvage. The catastrophic backstop
    (CATASTROPHIC_PROB + a 90c/contract collapse) finally becomes REACHABLE —
    this resolves P16's STOP-AND-REPORT. All other triggers are inert: the
    salvage_enabled branch returns before they are read."""
    return CutParams(
        hard_stop_usd=999.0, soft_stop_usd=999.0,
        min_time_remaining_s=2, hold_to_settle_s=0,
        spot_danger_buffer_usd=0, grace_period_s=0,
        early_exit_window_s=0, early_exit_loss_fraction=1.0,
        max_loss_fraction_of_balance=1.0, max_loss_fraction_of_cost=1.0,
        catastrophic_loss_cents=90,
        spot_safe_buffer_early_usd=0.01, spot_safe_buffer_late_usd=0.01,
        spot_safe_cutoff_s=60,
        rapid_drop_threshold=1.0, rapid_drop_window_s=10,
        max_loss_cents_per_contract=100,
        reversal_threshold=1.0, reversal_threshold_settling=1.0,
        reversal_threshold_profit=1.0, profit_tighten_above_entry=1.0,
        peak_window_s=30, proactive_after_s=0, prob_floor=0.0,
        salvage_enabled=True,
    )


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
        # P14 §2.1: on-demand fills sweep for one market (the 3s cadence's
        # at-the-cut-boundary version) — wired by the runner; no-op in shadow.
        self.resweep = lambda market: None

    def ledger_remaining(self, pos: OpenPosition) -> int:
        """P14 §2.2: the position recomputed from BOOKED fills — the ledger,
        not the in-memory pos object, is the only truth a cut may act on.
        A (market, lane) with NO fills rows at all (shadow-adopted, no
        booking evidence either way) falls back to the pos object — there is
        no contrary evidence to outrank it."""
        rows = self.ledger.db.execute(
            "SELECT action, COALESCE(SUM(count),0) FROM fills"
            " WHERE market=? AND lane=? GROUP BY action",
            (pos.market, pos.lane)).fetchall()
        if not rows:
            return pos.count
        net = 0
        for action, cnt in rows:
            net += cnt if action == "ENTRY" else -cnt
        return max(0, net)

    def set_lane_params(self, lane: str, params: CutParams) -> None:
        self.params[lane] = params

    def adopt(self, pos: OpenPosition, disabled_reason: str = None) -> None:
        self.positions[f"{pos.market}:{pos.lane}"] = pos
        # SALV-1 §2.4: registration event, ONCE per position — armed with
        # its anchor values, or disabled LOUDLY with its reason.
        try:
            if pos.p_entry is not None:
                self.surface.write_row(
                    pos.lane, pos.market, f"w-{pos.market}", "SALVAGE_ARMED",
                    detail=json.dumps({
                        "d": pos.d_entry, "t": pos.t_entry,
                        "p_entry": round(pos.p_entry, 4)}))
            else:
                self.surface.write_row(
                    pos.lane, pos.market, f"w-{pos.market}",
                    "SALVAGE_DISABLED_TAGGED",
                    detail=json.dumps(
                        {"reason": disabled_reason or "NO_ANCHOR"}))
        except Exception:
            pass  # registration narration never blocks custody

    # ------------------------------------------------------------------
    def kill_lane(self, lane: str) -> None:
        """Lane-kill halts ENTRIES only. Open positions stay custodied, loudly."""
        self.killed_lanes.add(lane)
        self.gateway.halt_entries(f"LANE_KILL:{lane}")
        open_here = [p for p in self.positions.values() if p.lane == lane]
        log.warning("LANE KILL %s: entries halted; %d open position(s) custodied to conclusion",
                    lane, len(open_here))

    # ------------------------------------------------------------------
    def tick(self, books: dict, close_ts_of, now: float,
             balance_usd: float = 0.0, spot=None, boundaries=None) -> list:
        """P3.4: the live custody loop — every open position, every cycle,
        winning or losing. Reads the leg's mark from the book, runs should_cut,
        executes via the baton with crossfire. Returns [(market, lane, trigger)]
        for cuts executed this tick."""
        cuts = []
        for key, pos in list(self.positions.items()):
            book = books.get(pos.market)
            if book is None:
                continue
            close_ts = close_ts_of(pos.market)
            if close_ts is None:
                continue
            mark = (book.best_yes_bid() if pos.side == "yes"
                    else book.best_no_bid())
            if mark is None:
                continue
            blo, bhi = (boundaries or {}).get(pos.market, (None, None))
            # P19 §2: the SALVAGE machine runs for salvage-enabled lanes
            # BEFORE the classic triggers (it is gentler: maker-first).
            base = self.params.get(pos.lane)
            if base is not None and base.salvage_enabled:
                salvaged = self._salvage_tick(pos, book, mark,
                                              close_ts - now, now, spot,
                                              blo, bhi)
                if salvaged:
                    cuts.append((pos.market, pos.lane, salvaged))
                    continue
            trigger = self.should_cut(
                pos, now=now, secs_remaining=close_ts - now,
                p_win=mark / 100.0, exit_bid_cents=mark, spot=spot,
                boundary_lo=blo, boundary_hi=bhi, balance_usd=balance_usd)
            if trigger:
                self.execute_cut(pos, mark, book, trigger, crossfire=True)
                cuts.append((pos.market, pos.lane, trigger))
        return cuts

    # ── P19 §2: SALVAGE — the custodian earns Lane F ───────────────────
    def salvage_in_progress(self, market: str) -> bool:
        """§2.4: one hand exits, the other waits — HUNT entries on this
        market are suppressed until the salvage resolves (position gone)."""
        return any(p.market == market and p.salvage_attempted
                   for p in self.positions.values())

    def _held_p(self, pos: OpenPosition, spot: float, strike: float,
                t_rem: float) -> Optional[float]:
        from . import delta
        d = abs(spot - strike)
        ps = delta.p_survive(d, t_rem)
        if ps is None:
            return None
        on_side = "yes" if spot >= strike else "no"
        return ps if on_side == pos.side else 1.0 - ps

    def _note_salvage_gag(self, pos: OpenPosition, reason: str,
                          **ctx) -> None:
        """SALV-1 §2: the seven gags get a voice — STATE-CHANGE-ONLY rows
        (Engineer: no write amplification), flap-capped (Adversary: an
        oscillating reason cannot hide in volume; the summary still counts
        every tick), with the delta-table inputs riding along (Scientist:
        TABLE_GAP traces to specific missing cells for the A1-A5 loop)."""
        pos.salvage_gag_counts[reason] = \
            pos.salvage_gag_counts.get(reason, 0) + 1
        if reason == pos.salvage_gag_reason:
            return
        pos.salvage_gag_reason = reason
        pos.salvage_gag_transitions += 1
        if pos.salvage_gag_transitions > config.SALVAGE_GAG_MAX_TRANSITIONS:
            return
        try:
            self.surface.write_row(
                pos.lane, pos.market, f"w-{pos.market}", "SALVAGE_GAG",
                detail=json.dumps({"reason": f"SALVAGE_GAG {reason}", **ctx}))
        except Exception:
            pass  # the tape never blocks the tick

    def emit_salvage_summary(self, pos: OpenPosition, exit_trigger: str,
                             realized_cents=None) -> None:
        """SALV-1 §2.3: ONE summary per position at conclusion — gagged
        tick totals per reason, whether salvage fired, the exit trigger,
        realized cents. The data source for DODGED_LOSS vs SALVAGE_REGRET
        and the K/S tuning (thresholds untouched in this order)."""
        if pos.salvage_summary_emitted:
            return
        pos.salvage_summary_emitted = True
        try:
            self.surface.write_row(
                pos.lane, pos.market, f"w-{pos.market}", "SALVAGE_SUMMARY",
                detail=json.dumps({
                    "gagged": pos.salvage_gag_counts,
                    "salvage_fired": pos.salvage_fired,
                    "exit_trigger": exit_trigger,
                    "realized_cents": realized_cents,
                    "entry": pos.entry_price_cents}))
        except Exception:
            pass

    def _salvage_tick(self, pos: OpenPosition, book, mark: int, t_rem: float,
                      now: float, spot, blo, bhi) -> Optional[str]:
        """§2.2 the trigger (needle-collapse, 2 sustained ticks) + §2.3 the
        execution: maker at the held side's best bid, crossfire after R.
        Spot BLIND → no salvage (catastrophic path unchanged beneath)."""
        # stage 2: a maker salvage is resting — R-second clock
        if pos.salvage_oid is not None:
            if now - pos.salvage_ts >= config.SALVAGE_R_S:
                state = self.gateway.cancel_tristate(pos.salvage_oid)
                pos.salvage_oid = None
                pos.resting_exit_id = None
                if state == "UNKNOWN":
                    failures.fail("BATON_VIOLATION",
                                  f"salvage maker {pos.market} unverifiable "
                                  f"before crossfire", fatal=True,
                                  market=pos.market, lane=pos.lane)
                pos.salvage_fired = "SALVAGE_CROSSFIRE"
                self.execute_cut(pos, mark, book, "SALVAGE_CROSSFIRE",
                                 crossfire=True)
                return "SALVAGE_CROSSFIRE"
            return None
        # WO-BOTH-LANES-MARKET-TRUE (build 50) — F's PRICE SALVAGE, the
        # catastrophic-tail bound. TABLE-FREE and SPOT-BLIND-PROOF: it runs
        # BEFORE the anchor/spot guards below, because the exact favorite that
        # rode to the −90 backstop (the −$2.77 3-lot dump) was the one with no
        # table cell / no spot to prove the collapse. A favorite that has
        # slipped >= F_SALVAGE_SLIP_POINTS from its entry has lost the high
        # confidence that bought it — a 95c favorite never slips 40pts on noise,
        # that is a decisive reversal — so recover now (~−40) instead of riding
        # to −90. IMMEDIATE crossfire (the slip IS the decision, no maker wait).
        # One attempt (salvage_attempted latches); NO re-entry (Wall 3 single-
        # entry keeps the ticker out of lane F for the rest of the window).
        if (pos.lane == "F" and not pos.salvage_attempted
                and mark <= pos.entry_price_cents
                            - config.F_SALVAGE_SLIP_POINTS):
            pos.salvage_attempted = True
            pos.salvage_fired = "SALVAGE_SLIP"
            self.execute_cut(pos, mark, book, "SALVAGE_SLIP", crossfire=True)
            return "SALVAGE_SLIP"
        if pos.salvage_attempted or pos.p_entry is None:
            # one attempt per position; no anchor = disabled — SAID (SALV-1)
            self._note_salvage_gag(
                pos, "SPENT" if pos.salvage_attempted else "NO_ANCHOR",
                mark=mark, t_rem=round(t_rem, 1))
            return None
        if spot is None:
            self._note_salvage_gag(pos, "BLIND", mark=mark,
                                   t_rem=round(t_rem, 1))
            return None      # BLIND: no salvage, backstop unchanged
        if t_rem <= config.SALVAGE_T_FLOOR_S:
            self._note_salvage_gag(pos, "T_FLOOR", mark=mark,
                                   t_rem=round(t_rem, 1))
            return None
        from . import spotlead as _sl
        strike = _sl.pick_strike(spot, blo, bhi)
        if strike is None:
            self._note_salvage_gag(pos, "NO_STRIKE", mark=mark,
                                   t_rem=round(t_rem, 1))
            return None
        p_held = self._held_p(pos, spot, strike, t_rem)
        if p_held is None:
            # Scientist: (d, t_rem) ride along — the gap names its cell
            self._note_salvage_gag(pos, "TABLE_GAP",
                                   d=round(abs(spot - strike), 1),
                                   t_rem=round(t_rem, 1), mark=mark)
            return None      # table gap now: no evidence, no salvage
        drop_pts = (p_held - pos.p_entry) * 100.0
        fair = p_held * 100.0
        if (drop_pts <= -config.SALVAGE_K_POINTS
                and fair < pos.entry_price_cents - config.SALVAGE_S_CENTS):
            pos.salvage_strikes += 1
        else:
            pos.salvage_strikes = 0
            self._note_salvage_gag(pos, "BELOW_K",
                                   p_held=round(p_held, 3),
                                   drop_pts=round(drop_pts, 1),
                                   fair=round(fair, 1), mark=mark,
                                   d=round(abs(spot - strike), 1),
                                   t_rem=round(t_rem, 1))
            return None
        if pos.salvage_strikes < 2:
            self._note_salvage_gag(pos, "STRIKES_1",
                                   p_held=round(p_held, 3),
                                   drop_pts=round(drop_pts, 1),
                                   fair=round(fair, 1), mark=mark,
                                   d=round(abs(spot - strike), 1),
                                   t_rem=round(t_rem, 1))
            return None      # sustained 2 consecutive ticks — no knives

        # TRIGGERED — §2.3: tri-state cancel artifacts, re-derive, maker.
        pos.salvage_attempted = True
        if pos.resting_exit_id is not None:
            state = self.gateway.cancel_tristate(pos.resting_exit_id)
            if state == "UNKNOWN":
                failures.fail("BATON_VIOLATION",
                              f"resting exit {pos.resting_exit_id} on "
                              f"{pos.market} unverifiable before salvage",
                              fatal=True, market=pos.market, lane=pos.lane)
            pos.resting_exit_id = None
        self.resweep(pos.market)
        remaining = self.ledger_remaining(pos)
        if remaining <= 0:
            log.warning("SALVAGE SKIPPED %s — position already flat (P14)",
                        pos.market)
            self.positions.pop(f"{pos.market}:{pos.lane}", None)
            return None
        d_now = abs(spot - strike)
        est_save = mark - fair
        casefile = (f"needle {drop_pts:+.0f}pts "
                    f"(d {pos.d_entry:.0f}→{d_now:.0f}, "
                    f"T-{int(t_rem // 60)}:{int(t_rem % 60):02d})"
                    if pos.d_entry is not None else
                    f"needle {drop_pts:+.0f}pts (d ?→{d_now:.0f})")
        order = Order(
            lane=pos.lane, event=pos.event, market=pos.market, side=pos.side,
            action="sell", price_cents=mark, count=remaining,
            size_tier=pos.size_tier, purpose="EXIT",
            reason=f"SALVAGE maker — {casefile} · est save {est_save:.0f}¢")
        result = self.gateway.submit(order, book)
        pos.salvage_oid = result.order_id
        pos.salvage_ts = now
        pos.resting_exit_id = result.order_id
        pos.salvage_fired = "SALVAGE_MAKER"
        import json as _json
        self.surface.write_row(
            pos.lane, pos.market, f"w-{pos.market}", "SALVAGE",
            detail=_json.dumps({"side": pos.side,
                                "entry": pos.entry_price_cents,
                                "mark": mark, "delta_p": round(drop_pts, 1),
                                "d_entry": pos.d_entry, "d_now": round(d_now, 1),
                                "fair": round(fair, 1),
                                "est_save": round(est_save, 1)}))
        self.gateway.alert_fn(
            f"✂️ SALVAGE {pos.lane} sold {pos.side}@{mark}¢ "
            f"(cost {pos.entry_price_cents}) — {casefile} · "
            f"est save {est_save:.0f}¢")
        log.warning("SALVAGE [%s] %s maker@%dc — %s", pos.lane, pos.market,
                    mark, casefile)
        return None  # maker resting; the cut (if any) comes at stage 2

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

        if p.salvage_enabled:
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
        # P24 §3 belt: a zero can NEVER mean "infinite profit" again — the
        # DUMP-ported reversal doctrine tightens on REAL gain over entry.
        # (The 0.0-placeholder at adoption made every position look
        # infinitely profitable to this one rule; fixed at the writer too.)
        entry_prob = pos.entry_p_win or (pos.entry_price_cents / 100.0)
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
                    trigger: str, crossfire: bool = True) -> Optional[str]:
        """BATON LIFECYCLE (P14): cancel the resting exit FIRST (tri-state —
        the venue's terminal states are truth, not violations), RE-DERIVE the
        position from the ledger, THEN cut exactly what the ledger proves we
        hold at this instant. A cut may never sell what we do not hold.
        crossfire=True (the default for a CUT) prices to fill NOW — the one
        deliberate cross in the engine, confined to CUT by the gateway."""
        key = f"{pos.market}:{pos.lane}"
        if pos.resting_exit_id is not None:
            state = self.gateway.cancel_tristate(pos.resting_exit_id)
            if state == "UNKNOWN":
                # genuinely unverifiable — the one remaining FATAL
                failures.fail("BATON_VIOLATION",
                              f"resting exit {pos.resting_exit_id} on {pos.market} "
                              f"could not be verified canceled before cut ({trigger})",
                              fatal=True, market=pos.market, lane=pos.lane,
                              trigger=trigger)
            # CANCELED or ALREADY_TERMINAL: it is gone either way — proceed,
            # but NEVER straight to the cut (§2 re-derivation below).
            pos.resting_exit_id = None

        # §2: re-derive before EVERY cut (all triggers) — sweep, then ledger.
        self.resweep(pos.market)
        remaining = self.ledger_remaining(pos)
        if remaining <= 0:
            log.warning("CUT SKIPPED [%s] %s — position already flat "
                        "(race with fill)", trigger, pos.market)
            self.emit_salvage_summary(pos, "FLAT_RACE")   # SALV-1 §2.3
            self.positions.pop(key, None)
            return None  # the round-trip line already told the story

        # sell exactly the ledger's remaining count; risk-reducing -> skips walls
        cut = Order(
            lane=pos.lane, event=pos.event, market=pos.market, side=pos.side,
            action="sell", price_cents=cut_price_cents, count=remaining,
            size_tier=pos.size_tier, purpose="CUT", crossfire=crossfire,
            reason=trigger,  # P13 §1: every rule that spends money signs its work
        )
        result = self.gateway.submit(cut, book)
        # P24 §2.2: the cut's entrance — the venue's OWN fee off the order
        # response, through the one fee parser (never a multiplier guess).
        from . import venue as _venue
        fee_cents = _venue.parse_response_fee(result.resp, remaining)
        transport = self.ladder.custodian_transport() if self.ladder else "WS"
        log.warning("CUSTODIAN CUT [%s] %s lane=%s count=%d @%dc fee=%dc "
                    "transport=%s", trigger, pos.market, pos.lane, remaining,
                    cut_price_cents, fee_cents, transport)
        self.ledger.record_fill(pos.market, pos.lane, pos.side, "CUSTODIAN_EXIT",
                                cut_price_cents, remaining, pos.size_tier,
                                fee_cents=fee_cents)
        # SALV-1 §2.3: the position concludes here — one summary, always
        self.emit_salvage_summary(pos, trigger,
                                  realized_cents=cut_price_cents
                                  - pos.entry_price_cents)
        del self.positions[key]
        return result.order_id
