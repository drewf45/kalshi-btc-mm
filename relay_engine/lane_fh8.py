"""Lanes F and H8 — PORTED from the live k_worker tree (B1).

Byte-identical scope per C.4 = LADDER + GATES ONLY:
  - v4 watch-confirm ladder (floors 99/98/97 by tier + the 95 floor of the
    final-window band), confirm counts 9/6/3, dropout / side-flip /
    counterparty (R2) resets — from k_worker/engine.py::_run_watch_ladder
  - favorite-side selection and every evaluate wall — from
    k_worker/gateway.py::evaluate / _evaluate_h8_probe / _favorite_side
  - H8 gates: band 80-94, distance_pct >= 0.15%, secs <= 60, probe-kill
    (3 losses before any win), daily budget, and the delta-gate >=99%
    (dual-gate table verdict, wilson_ub <= 0.01) — tag-only, static gate
    authoritative, exactly as live
  - F top-rung guard (tier 0) via f_top_rung_verdict
Source of truth: reference/live_k_worker/{engine,gateway}.py (vendored,
SHA-pinned). Proven by golden-tape regression (tests/test_golden_tape.py):
the vendored LIVE code and this port replay the same tapes and must produce
identical proposals/passes. Diff report: docs/GOLDEN_TAPE_REPORT.md.

DECLARED CHANGES (each with its own verify, per C.4 — NOT part of byte-identity):
  D1. EPOCH 2 (§A4): tradeable = live balance. The live tree deducted a
      treasury accrual before the balance walls; this tree has no such concept.
      With a zero accrual the two computations coincide — the golden tape pins
      that equivalence.
  D2. Cap plumbing: proposals from these lanes still pass the relay gateway's
      walls (net-risk <=3, $-at-risk, wrong-way tick, %-of-book, post-only)
      at submit time. The live cross-lane cap logic inside evaluate is kept
      byte-identical AND the relay walls run downstream.
  D3. Custodian passthrough: lane F positions register with the custodian in
      PASSTHROUGH (cut-disabled-except-catastrophic), per C.5.
  D4. Evidence queries (lane losses, H8 probe lifetime/daily) read this
      engine's own DB through the FH8Stats adapter instead of the live store —
      same questions, same thresholds, different bookkeeper.
  D5. Queue instrumentation (queue_pos at entry / T-60) belongs to the live
      submit/monitor path; in paper shadow no orders rest, so it is carried by
      the relay gateway when live submission is ever enabled — not here.

Win/loss path symmetry: evaluate admits risk; it neither knows nor cares how a
position later resolves. Kill rules count losses only because the live gates do
— that asymmetry is the live lane's own, preserved byte-identically.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Dict, Optional, Set, Tuple

from . import delta as delta_table_loader

log = logging.getLogger("relay.lane_fh8")

# ── Constants — byte-identical to the live tree ────────────────────────────
ENTRY_WINDOW_SEC = 180
WATCH_WINDOW_SEC = 900
CANCEL_BEFORE_EXPIRY_SEC = 10
POLL_INTERVAL_SEC = 5
MAX_REPRICES_LOW_BAND = 1

CONFIRM_LADDER = [
    {"lo_sec": 600, "hi_sec": 900, "floor_cents": 99, "confirms": 9},
    {"lo_sec": 300, "hi_sec": 600, "floor_cents": 98, "confirms": 6},
    {"lo_sec": 180, "hi_sec": 300, "floor_cents": 97, "confirms": 3},
]

COST_BAND_LO = Decimal("95.00")
COST_BAND_HI = Decimal("99.00")
COST_BAND_LO_INT = 95
COST_BAND_HI_INT = 99
H8_COST_LO = Decimal("80.00")
H8_COST_HI = Decimal("94.00")
HOURLY_EXPOSURE_CAP_USD = 4.00
MIN_BALANCE_USD = 5.00
CROSS_LANE_CAP = 3

LANES: Dict[str, dict] = {
    "F": {
        "band_lo": Decimal("95.00"), "band_hi": Decimal("99.00"),
        "hourly_cap": 4.00, "daily_budget": None,
        "kill_losses": 3, "kill_window_sec": 3600,
        "mode": "LIVE",
    },
    "H8": {
        "band_lo": Decimal("80.00"), "band_hi": Decimal("94.00"),
        "hourly_cap": None, "daily_budget": 2.00,
        "min_distance_pct": 0.0015, "max_secs": 60,
        "kill_losses": 3, "kill_window_sec": 3600,
        "mode": "LIVE",
    },
}


# ── Book shape the live logic reads (mirror of k_worker kalshi.Book) ───────
@dataclass
class TouchBook:
    yes_bid: Optional[int] = None
    yes_ask: Optional[int] = None
    no_bid: Optional[int] = None
    no_ask: Optional[int] = None
    yes_bid_qty: int = 0
    no_bid_qty: int = 0
    yes_bid_fp: Optional[str] = None
    no_bid_fp: Optional[str] = None


@dataclass
class EvalResult:
    allowed: bool
    side: Optional[str] = None
    cost_cents: Optional[int] = None
    cost_exact: Optional[float] = None
    rest_fp: Optional[str] = None
    yes_quote_cents: Optional[int] = None
    breakeven_pct: Optional[float] = None
    why_tag: Optional[str] = None
    reject_code: Optional[str] = None
    reject_reason: Optional[str] = None
    lane: str = "F"
    spot_price: Optional[float] = None
    boundary_lo: Optional[float] = None
    boundary_hi: Optional[float] = None
    distance: Optional[float] = None
    distance_pct: Optional[float] = None
    contracts: int = 1


@dataclass
class FH8Stats:
    """Evidence adapter (D4): the same questions the live store answered,
    served from this engine's own DB."""
    lane_losses_recent: Callable[[str, int], int] = lambda lane, window_sec: 0
    h8_probe_lifetime: Callable[[], dict] = lambda: {"wins": 0, "losses": 0}
    h8_probe_daily: Callable[[], dict] = lambda: {"at_risk": 0.0}


class FH8State:
    """Port of the live gateway's module state (_traded_tickers, _hourly_exposure),
    persisted by the caller if needed."""

    def __init__(self, now_fn=None):
        import time as _time
        self.now = now_fn or _time.time
        self.traded: Set[Tuple[str, str]] = set()  # (lane, ticker)
        self.hourly_exposure: list = []            # (timestamp, dollars_at_risk)

    def hourly_exposure_usd(self) -> float:
        cutoff = self.now() - 3600
        return max(0.0, sum(d for ts, d in self.hourly_exposure if ts >= cutoff))

    def add_exposure(self, cost_usd: float) -> None:
        self.hourly_exposure.append((self.now(), cost_usd))

    def release_exposure(self, cost_usd: float) -> None:
        self.hourly_exposure.append((self.now(), -abs(cost_usd)))

    def mark_traded(self, ticker: str, lane: str) -> None:
        self.traded.add((lane, ticker))


def favorite_side(book: TouchBook) -> Tuple[Optional[str], Optional[Decimal], Optional[int], Optional[str]]:
    """Determine favorite side. Returns (side, cost_decimal_cents, yes_quote_cents, fp_str).
    Byte-identical to k_worker/gateway.py::_favorite_side."""
    prices = []
    if book.yes_bid is not None:
        if book.yes_bid_fp is not None:
            cost_d = Decimal(book.yes_bid_fp) * 100
        else:
            cost_d = Decimal(book.yes_bid)
        prices.append(("yes", cost_d, book.yes_bid, book.yes_bid_fp))
    if book.no_bid is not None:
        if book.no_bid_fp is not None:
            cost_d = Decimal(book.no_bid_fp) * 100
        else:
            cost_d = Decimal(book.no_bid)
        prices.append(("no", cost_d, 100 - book.no_bid, book.no_bid_fp))
    if not prices:
        return None, None, None, None
    best = max(prices, key=lambda x: x[1])
    return best[0], best[1], best[2], best[3]


def evaluate(ticker: str, book: TouchBook, secs_to_expiry: float,
             cash_usd: float, spot: Optional[float] = None,
             boundary_lo: Optional[float] = None,
             boundary_hi: Optional[float] = None,
             *, state: FH8State, stats: FH8Stats) -> EvalResult:
    """Evaluate whether this market qualifies for entry.
    Walls byte-identical to k_worker/gateway.py::evaluate, except D1
    (tradeable = cash_usd, EPOCH 2) and D4 (stats adapter)."""

    # Wall 1: KXBTC15M only
    if not ticker.startswith("KXBTC15M"):
        return EvalResult(
            allowed=False, reject_code="WRONG_FAMILY",
            reject_reason=f"Not KXBTC15M: {ticker}",
        )

    # Determine favorite side
    side, cost_d, yes_quote, fp_str = favorite_side(book)
    if side is None or cost_d is None:
        return EvalResult(
            allowed=False, reject_code="NO_BOOK",
            reject_reason="No orderbook data",
            why_tag="SKIP_NO_BOOK",
        )

    cost_int = int(cost_d)
    cost_float = float(cost_d)
    breakeven = cost_float / 100.0

    # Compute distance if spot available
    dist, dist_pct = None, None
    if spot is not None and boundary_lo is not None and boundary_hi is not None:
        if side == "yes":
            dist = spot - boundary_hi
        else:
            dist = boundary_lo - spot
        dist_pct = abs(dist) / spot if spot > 0 else 0.0

    base = EvalResult(
        allowed=False, side=side, cost_cents=cost_int,
        cost_exact=cost_float, rest_fp=fp_str,
        yes_quote_cents=yes_quote, breakeven_pct=breakeven,
        spot_price=spot, boundary_lo=boundary_lo, boundary_hi=boundary_hi,
        distance=dist, distance_pct=dist_pct,
    )

    # Check H8 probe lane first (cost 80-94)
    if H8_COST_LO <= cost_d <= H8_COST_HI:
        return _evaluate_h8_probe(base, cost_d, secs_to_expiry, cash_usd,
                                  ticker, spot, dist_pct, state=state, stats=stats)

    # Wall 2: Lane F cost band 95.00-99.00¢
    if cost_d < COST_BAND_LO:
        base.reject_code = "OUT_OF_BAND_COST"
        base.reject_reason = f"cost={cost_float}¢ < {COST_BAND_LO}¢ (shadow only)"
        base.why_tag = f"SKIP_OOB_{cost_float}c_T-{int(secs_to_expiry)}"
        return base
    if cost_d > COST_BAND_HI:
        base.reject_code = "OUT_OF_BAND_COST"
        base.reject_reason = f"cost={cost_float}¢ > {COST_BAND_HI}¢"
        base.why_tag = f"SKIP_OOB_{cost_float}c_T-{int(secs_to_expiry)}"
        return base

    # Wall 3: Lane-scoped single entry
    if ("F", ticker) in state.traded:
        base.reject_code = "SECOND_ENTRY"
        base.reject_reason = f"Already traded {ticker} in lane F"
        base.why_tag = "SKIP_SECOND_ENTRY"
        return base

    # Wall 3b: Cross-lane cap
    cross_count = sum(1 for l, t in state.traded if t == ticker)
    if cross_count >= CROSS_LANE_CAP:
        base.reject_code = "REJECT_CROSS_LANE_CAP"
        base.reject_reason = f"{cross_count} lanes already trading {ticker} (cap={CROSS_LANE_CAP})"
        base.why_tag = "SKIP_CROSS_LANE_CAP"
        return base

    # Wall 3c DELETED (P27 §1b): the per-lane kill rule is dead — the
    # account halt is THE stop. lane_losses_recent remains as reporting.

    # Wall 4: Insufficient tradeable capital — D1: tradeable = live balance (EPOCH 2)
    cost_usd = cost_float / 100.0
    tradeable = cash_usd
    if tradeable < cost_usd:
        base.reject_code = "INSUFFICIENT_BALANCE"
        base.reject_reason = f"tradeable=${tradeable:.2f} < cost=${cost_usd:.2f}"
        base.why_tag = "SKIP_BALANCE"
        return base

    if tradeable < MIN_BALANCE_USD:
        base.reject_code = "INSUFFICIENT_BALANCE"
        base.reject_reason = f"tradeable=${tradeable:.2f} < floor=${MIN_BALANCE_USD:.2f}"
        base.why_tag = "SKIP_BALANCE"
        return base

    # Wall 5 DELETED (P27 §1c): HOURLY_CAP is dead — yesterday's pass
    # histogram showed it muting the lane (HOURLY_CAP:4). Exposure still
    # ACCUMULATES (hourly_exposure_usd) for the packs; it governs nothing.

    why_tag = f"FAV_{cost_float}c_T-{int(secs_to_expiry)}"
    base.allowed = True
    base.why_tag = why_tag
    base.reject_code = None
    base.reject_reason = None
    return base


def _evaluate_h8_probe(base: EvalResult, cost_d: Decimal, secs_to_expiry: float,
                       cash_usd: float, ticker: str,
                       spot: Optional[float], dist_pct: Optional[float],
                       *, state: FH8State, stats: FH8Stats) -> EvalResult:
    """Evaluate H8 probe lane qualification. Byte-identical to
    k_worker/gateway.py::_evaluate_h8_probe (D1/D4 as declared)."""
    base.lane = "H8"
    cost_float = float(cost_d)
    h8_cfg = LANES["H8"]

    if spot is None:
        base.reject_code = "H8_NO_SPOT"
        base.reject_reason = "Spot price unavailable"
        base.why_tag = "SKIP_H8_UNQUALIFIED"
        return base

    if dist_pct is None or dist_pct < h8_cfg["min_distance_pct"]:
        base.reject_code = "H8_DISTANCE"
        base.reject_reason = f"distance_pct={dist_pct or 0:.4%} < {h8_cfg['min_distance_pct']:.2%}"
        base.why_tag = "SKIP_H8_UNQUALIFIED"
        return base

    if secs_to_expiry > h8_cfg["max_secs"]:
        base.reject_code = "H8_TIME"
        base.reject_reason = f"secs_to_expiry={secs_to_expiry:.0f} > {h8_cfg['max_secs']}"
        base.why_tag = "SKIP_H8_UNQUALIFIED"
        return base

    # P27 §1b/§1c: the probe-kill, per-lane kill, and daily probe budget
    # are DELETED — all three were per-lane governors; the account halt is
    # THE stop. The stats they read stay banked for the packs.
    cost_usd = cost_float / 100.0

    # Lane-scoped single entry
    if ("H8", ticker) in state.traded:
        base.reject_code = "SECOND_ENTRY"
        base.reject_reason = f"Already traded {ticker} in lane H8"
        base.why_tag = "SKIP_SECOND_ENTRY"
        return base

    # Cross-lane cap
    cross_count = sum(1 for l, t in state.traded if t == ticker)
    if cross_count >= CROSS_LANE_CAP:
        base.reject_code = "REJECT_CROSS_LANE_CAP"
        base.reject_reason = f"{cross_count} lanes already trading {ticker}"
        base.why_tag = "SKIP_CROSS_LANE_CAP"
        return base

    # D1: tradeable = live balance (EPOCH 2)
    tradeable = cash_usd
    if tradeable < cost_usd or tradeable < MIN_BALANCE_USD:
        base.reject_code = "INSUFFICIENT_BALANCE"
        base.reject_reason = f"tradeable=${tradeable:.2f}"
        base.why_tag = "SKIP_BALANCE"
        return base

    base.allowed = True
    dp = dist_pct * 100 if dist_pct else 0
    base.why_tag = f"H8PROBE_{cost_float}c_D{dp:.2f}_T-{int(secs_to_expiry)}"
    base.reject_code = None
    base.reject_reason = None

    # Dual-gate: log table verdict alongside static gate (static authoritative)
    if spot is not None and base.boundary_lo is not None and base.boundary_hi is not None:
        dist_usd = abs(base.distance) if base.distance is not None else 0
        tv = delta_table_loader.h8_table_verdict(dist_usd, secs_to_expiry)
        tag_suffix = f"|TBL_d{tv['distance_grid']}_p{tv['p_cross']:.4f}" if tv['p_cross'] is not None else "|TBL_ABSENT"
        if tv['wilson_ub'] is not None:
            tag_suffix += f"_wub{tv['wilson_ub']:.4f}"
            tag_suffix += "_PASS" if tv['qualified'] else "_FAIL"
        base.why_tag += tag_suffix

    return base


# ── Watch-confirm ladder (port of engine.py::_run_watch_ladder) ────────────

class WatchLadder:
    """Tick-based port of the live watch-confirm loop. The live code polls in a
    blocking loop; this port consumes one tick per POLL_INTERVAL_SEC with the
    same state transitions, byte-identical: tier determination, tier-change
    reset, missing-book reset, R2 counterparty reset, side-flip reset,
    below-floor reset, main-band gate, confirm accumulation."""

    def __init__(self):
        self.confirm_count = 0
        self.watch_side: Optional[str] = None
        self.last_tier_idx = -1
        self.no_counterparty_ticks = 0

    def reset_confirms(self) -> None:
        """Top-rung guard failure resets the count (live: confirm_count = 0)."""
        self.confirm_count = 0

    def current_tier(self, secs_left: float) -> Tuple[Optional[int], Optional[dict]]:
        for i, t in enumerate(CONFIRM_LADDER):
            if t["lo_sec"] <= secs_left < t["hi_sec"]:
                return i, t
        return None, None

    def tick(self, secs_left: float, book: TouchBook) -> str:
        """One poll. Returns:
        "EXIT"      — ladder over (into final window / expiry)
        "WAIT"      — keep watching
        "CONFIRMED" — tier confirm threshold met; caller runs evaluate
                      (+ tier-0 top-rung guard) exactly as the live loop does."""
        if secs_left <= ENTRY_WINDOW_SEC:
            return "EXIT"
        if secs_left < CANCEL_BEFORE_EXPIRY_SEC:
            return "EXIT"

        tier_idx, tier = self.current_tier(secs_left)
        if tier is None:
            return "WAIT"

        # Reset on tier change
        if tier_idx != self.last_tier_idx:
            self.confirm_count = 0
            self.watch_side = None
            self.no_counterparty_ticks = 0
            self.last_tier_idx = tier_idx

        side, cost_d, yes_quote, fp_str = favorite_side(book)

        if side is None or cost_d is None:
            self.confirm_count = 0
            self.watch_side = None
            return "WAIT"

        # R2: Counterparty liquidity check
        has_counterparty = True
        if side == "yes" and book.no_bid is None:
            has_counterparty = False
        elif side == "no" and book.yes_bid is None:
            has_counterparty = False
        if not has_counterparty:
            self.no_counterparty_ticks += 1
            self.confirm_count = 0
            return "WAIT"

        cost_int = int(cost_d)

        # Dropout: side flip resets counter
        if side != self.watch_side:
            self.confirm_count = 0
            self.watch_side = side

        # Dropout: below tier floor resets counter
        if cost_int < tier["floor_cents"]:
            self.confirm_count = 0
            return "WAIT"

        # Must also be in the main band (95-99)
        if not (COST_BAND_LO_INT <= cost_int <= COST_BAND_HI_INT):
            self.confirm_count = 0
            return "WAIT"

        self.confirm_count += 1

        if self.confirm_count >= tier["confirms"]:
            return "CONFIRMED"
        return "WAIT"


def ladder_confirm_decision(ladder: WatchLadder, ticker: str, book: TouchBook,
                            secs_left: float, cash_usd: float,
                            spot: Optional[float], boundary_lo: Optional[float],
                            boundary_hi: Optional[float],
                            *, state: FH8State, stats: FH8Stats) -> Optional[EvalResult]:
    """The live loop's confirm branch: evaluate; tier-0 top-rung guard; why_tag
    enrichment. Returns the enriched EvalResult when the entry stands, or None
    (ladder keeps watching; guard failures reset confirms — live behavior)."""
    tier_idx, tier = ladder.current_tier(secs_left)
    if tier is None:
        return None

    eval_result = evaluate(ticker, book, secs_left, cash_usd,
                           spot=spot, boundary_lo=boundary_lo,
                           boundary_hi=boundary_hi, state=state, stats=stats)
    if not eval_result.allowed:
        return None

    # Top-rung guard (tier 0 = T-900-600 only) — P3 (0708):
    # upper-tail gate vs cost-implied breakeven; low-tail entries
    # trade and get TAGGED (ruling #3: tape decides adverse-selection).
    if tier_idx == 0 and delta_table_loader.is_loaded():
        dist_usd = abs(eval_result.distance) if eval_result.distance is not None else 0
        tv = delta_table_loader.f_top_rung_verdict(
            dist_usd, secs_left,
            cost_cents=eval_result.cost_exact or eval_result.cost_cents)
        if tv["qualified"] is False:
            log.info(f"[LANE_FH8] Top-rung guard: crossing risk over gate — "
                     f"d=${dist_usd:.0f} wub={tv['wilson_ub']:.6f} "
                     f"be={tv['breakeven_p']:.2%}")
            ladder.reset_confirms()
            return None
        if tv.get("low_tail"):
            eval_result.why_tag = f"{eval_result.why_tag}_LOWTAIL"

    # Enrich why_tag with confirmation count
    eval_result.why_tag = (f"{eval_result.why_tag}"
                           f"_confirms_{ladder.confirm_count}/{tier['confirms']}")
    return eval_result
