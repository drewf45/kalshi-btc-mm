"""Lane D — ported SHAPES from the flipdesk tree's d_worker (3,234 lines,
P3.3). The d_worker's organs, rehomed and hard-pinned:

  classify  (d_worker/classify.py): pure verdict function, evidence dict on
             every row, SKIP verdicts are first-class
  reserve   (d_worker/budget.py): the inside body's single capital mechanism —
             the scanner MUST reserve before every seed; the veto is absolute
             and instant; every decision is an APPEND-ONLY row
  watchdog  (d_worker/watchdog.py): re-verify evidence on every open position;
             broken evidence -> abandon (custodian executes the exit)
  governor  : the relay gateway's token bucket (already one per engine)

HARD-PINNED KXBTC15M (Engineer flag TRUE-KNOB): d_worker's classify is
series-generic (weather visible at classify.py:128) — that generic scanner
shape is the FUTURE MULTI-SERIES SEAM. Noted here; not built.

Band: 50-80c cheap side, floor 60c DREW-DEFAULT (pending the forgone-clip
data ruling 50c vs 60c). Delta-gated: the entry qualifies only on delta-table
evidence (the cost-implied breakeven upper-tail guard — same family as F's
top-rung guard); TABLE_ABSENT -> SKIP_CANT_VERIFY (D is evidence-born; unlike
F, no static gate substitutes). Resting recovery exit (the baton) + custody
from birth.

Win/loss path symmetry: the reserve ledger writes GRANTED and DENIED through
the same append-only table; the watchdog re-verifies winners and losers on
the same cadence.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import config, delta
from .custodian import CutParams
from .gateway import Order

log = logging.getLogger("relay.lane_d")

# ── knobs (DREW-DEFAULT) ───────────────────────────────────────────────────
D_BAND_LO = 50                    # band floor candidate (Chunk 2 rules 50 vs 60)
D_FLOOR_CENTS = config.LANE_D_FLOOR_CENTS  # 60c DREW-DEFAULT, pending data
D_BAND_HI = 80
D_RECOVERY_X = 5                  # resting recovery exit at entry+X (DREW-DEFAULT)
D_BOOK_CAP_USD = 3.0              # d_worker budget shape: book cap (DREW-DEFAULT)
D_PER_MARKET_CAP_LOTS = 1         # 1-lot per market (R2: one-lot start)
D_GUARD_HAIRCUT = 0.5             # cost-implied breakeven haircut (guard family)

D_SCHEMA = """
CREATE TABLE IF NOT EXISTS d_budget_decisions (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    cost_cents INTEGER NOT NULL,
    decision TEXT NOT NULL,     -- GRANTED | DENIED | CONVERTED | RELEASED
    reason TEXT NOT NULL
);
"""


@dataclass
class DVerdict:
    """The classify shape: a verdict with its evidence, SKIPs first-class."""
    ticker: str
    verdict: str                  # SEED | SKIP_*
    side: Optional[str] = None
    cost_cents: Optional[int] = None
    evidence: dict = field(default_factory=dict)


def classify(market: str, book, secs_to_expiry: float,
             spot: Optional[float], boundary_lo: Optional[float],
             boundary_hi: Optional[float]) -> DVerdict:
    """Pure verdict function (the d_worker classify shape, KXBTC15M-pinned).
    Cheap-side candidate in [floor, 80], delta-gated on table evidence."""
    if not market.startswith("KXBTC15M"):
        return DVerdict(market, "SKIP_WRONG_SERIES",
                        evidence={"reason": "hard-pinned KXBTC15M; multi-series is a seam, not a lane"})
    yb, nb = book.best_yes_bid(), book.best_no_bid()
    # the CHEAP side: the lower-priced of the two bids inside the band
    candidates = []
    if yb is not None:
        candidates.append(("yes", yb))
    if nb is not None:
        candidates.append(("no", nb))
    if not candidates:
        return DVerdict(market, "SKIP_NO_BOOK")
    side, cost = min(candidates, key=lambda x: x[1])
    evidence = {"side": side, "cost": cost, "floor": D_FLOOR_CENTS, "band_hi": D_BAND_HI}
    if cost < D_FLOOR_CENTS:
        return DVerdict(market, "SKIP_UNDER_FLOOR", side, cost, evidence)
    if cost > D_BAND_HI:
        return DVerdict(market, "SKIP_OVER_BAND", side, cost, evidence)

    # delta gate — evidence-born: no table, no trade
    if spot is None or (boundary_lo is None and boundary_hi is None):
        evidence["reason"] = "no spot/boundary evidence"
        return DVerdict(market, "SKIP_CANT_VERIFY", side, cost, evidence)
    if side == "yes":
        dist = spot - (boundary_hi if boundary_hi is not None else boundary_lo)
    else:
        dist = (boundary_lo if boundary_lo is not None else boundary_hi) - spot
    dist_usd = abs(dist)
    tv = delta.f_top_rung_verdict(dist_usd, secs_to_expiry, cost_cents=cost)
    evidence["table"] = {k: tv[k] for k in ("qualified", "wilson_ub", "distance_grid", "reason")}
    if tv["qualified"] is None:
        return DVerdict(market, "SKIP_CANT_VERIFY", side, cost, evidence)
    if tv["qualified"] is False:
        return DVerdict(market, "SKIP_TABLE_RISK", side, cost, evidence)
    return DVerdict(market, "SEED", side, cost, evidence)


class DBudget:
    """The inside body (d_worker/budget.py shape): reserve -> convert -> release,
    absolute veto, append-only decisions."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.ledger.db.executescript(D_SCHEMA)
        self.ledger.db.commit()
        self.reserved: Dict[str, int] = {}   # ticker -> cents reserved
        self.at_risk: Dict[str, int] = {}    # ticker -> cents converted
        self.evidence_broken_pending = 0

    def _decide(self, ticker: str, cost_cents: int, decision: str, reason: str) -> None:
        self.ledger.db.execute(
            "INSERT INTO d_budget_decisions (ts, ticker, cost_cents, decision, reason)"
            " VALUES (?,?,?,?,?)", (time.time(), ticker, cost_cents, decision, reason))
        self.ledger.db.commit()

    def reserve(self, ticker: str, cost_cents: int, lane_killed: bool = False) -> bool:
        if lane_killed:
            self._decide(ticker, cost_cents, "DENIED", "lane killed")
            return False
        if self.evidence_broken_pending > 0:
            self._decide(ticker, cost_cents, "DENIED",
                         f"{self.evidence_broken_pending} EVIDENCE_BROKEN pending")
            return False
        if ticker in self.reserved or ticker in self.at_risk:
            self._decide(ticker, cost_cents, "DENIED",
                         f"per-market cap {D_PER_MARKET_CAP_LOTS} lot")
            return False
        committed = sum(self.reserved.values()) + sum(self.at_risk.values())
        if (committed + cost_cents) / 100.0 > D_BOOK_CAP_USD:
            self._decide(ticker, cost_cents, "DENIED",
                         f"book cap ${D_BOOK_CAP_USD:.2f}")
            return False
        self.reserved[ticker] = cost_cents
        self._decide(ticker, cost_cents, "GRANTED", "all gates passed")
        return True

    def convert(self, ticker: str) -> None:
        """Reservation -> at_risk (the seed was placed)."""
        cents = self.reserved.pop(ticker, 0)
        if cents:
            self.at_risk[ticker] = cents
            self._decide(ticker, cents, "CONVERTED", "seed placed")

    def release(self, ticker: str, reason: str) -> None:
        cents = self.reserved.pop(ticker, None) or self.at_risk.pop(ticker, None) or 0
        self._decide(ticker, cents, "RELEASED", reason)


class LaneD:
    """The D lane: sweep -> classify -> reserve -> seed; watchdog re-verifies."""

    name = "D"

    def __init__(self, gateway=None, custodian=None, ledger=None):
        self.gateway = gateway
        self.custodian = custodian
        self.budget = DBudget(ledger) if ledger is not None else None
        self.killed = False
        self.seeded: Dict[str, DVerdict] = {}
        if custodian is not None:
            custodian.set_lane_params("D", d_cut_params())

    def evaluate(self, market: str, ctx: dict) -> Optional[Order]:
        """One sweep step. Returns an entry proposal or None (verdict recorded
        by the caller from last_verdict)."""
        book = ctx.get("book")
        close_ts = ctx.get("close_ts")
        now = ctx.get("now", time.time())
        if book is None or close_ts is None:
            self.last_verdict = DVerdict(market, "SKIP_NO_BOOK")
            return None
        secs = close_ts - now
        if secs < 60:
            self.last_verdict = DVerdict(market, "SKIP_TOO_LATE")
            return None
        v = classify(market, book, secs, ctx.get("spot"),
                     ctx.get("boundary_lo"), ctx.get("boundary_hi"))
        self.last_verdict = v
        if v.verdict != "SEED":
            return None
        if market in self.seeded:
            self.last_verdict = DVerdict(market, "SKIP_ALREADY_SEEDED", v.side, v.cost_cents)
            return None
        # reserve BEFORE seed — the inside body's veto is absolute
        if self.budget is not None and not self.budget.reserve(
                market, v.cost_cents, lane_killed=self.killed):
            self.last_verdict = DVerdict(market, "SKIP_INSIDE_DENIED", v.side,
                                         v.cost_cents, v.evidence)
            return None
        self.seeded[market] = v
        return Order(
            lane="D", event=market.rsplit("-", 1)[0], market=market,
            side=v.side, action="buy", price_cents=v.cost_cents,
            count=D_PER_MARKET_CAP_LOTS, size_tier=config.TIER_PROBE,
            purpose="ENTRY", band=(D_FLOOR_CENTS, D_BAND_HI),
            rest_fp=book.best_fp(v.side))  # true-touch resting

    def recovery_exit(self, market: str) -> Optional[Order]:
        """The resting recovery baton: entry+X passive exit, custodied."""
        v = self.seeded.get(market)
        if v is None:
            return None
        return Order(
            lane="D", event=market.rsplit("-", 1)[0], market=market,
            side=v.side, action="sell", price_cents=v.cost_cents + D_RECOVERY_X,
            count=D_PER_MARKET_CAP_LOTS, size_tier=config.TIER_PROBE,
            purpose="EXIT")

    def watchdog_tick(self, market: str, ctx: dict) -> Optional[str]:
        """Re-verify evidence on an open seed (the d_worker watchdog shape).
        Returns 'EVIDENCE_BROKEN' when the table gate no longer holds — the
        caller abandons via the custodian (one exit owner)."""
        v = self.seeded.get(market)
        if v is None:
            return None
        book = ctx.get("book")
        close_ts = ctx.get("close_ts")
        now = ctx.get("now", time.time())
        if book is None or close_ts is None:
            return None
        rv = classify(market, book, close_ts - now, ctx.get("spot"),
                      ctx.get("boundary_lo"), ctx.get("boundary_hi"))
        if rv.verdict in ("SEED", "SKIP_ALREADY_SEEDED", "SKIP_UNDER_FLOOR",
                          "SKIP_OVER_BAND"):
            return None  # evidence still stands (band drift is price, not evidence)
        if rv.verdict in ("SKIP_TABLE_RISK", "SKIP_CANT_VERIFY"):
            if self.budget is not None:
                self.budget.evidence_broken_pending += 1
                self.budget.release(market, f"EVIDENCE_BROKEN: {rv.verdict}")
            log.warning("D WATCHDOG: evidence broken on %s (%s) — abandoning",
                        market, rv.verdict)
            return "EVIDENCE_BROKEN"
        return None

    def clear_broken(self) -> None:
        """After the abandon executes, the inside body unblocks new seeds."""
        if self.budget is not None and self.budget.evidence_broken_pending > 0:
            self.budget.evidence_broken_pending -= 1


def d_cut_params() -> CutParams:
    """Custody from birth. Numbers are DREW-DEFAULT placeholders in the
    conservative direction (his to rule, like every lane's)."""
    return CutParams(
        hard_stop_usd=0.50, soft_stop_usd=0.40,
        min_time_remaining_s=30,
        hold_to_settle_s=30, spot_danger_buffer_usd=75,
        grace_period_s=10,
        early_exit_window_s=300, early_exit_loss_fraction=0.30,
        max_loss_fraction_of_balance=0.05, max_loss_fraction_of_cost=0.60,
        catastrophic_loss_cents=40,
        spot_safe_buffer_early_usd=100, spot_safe_buffer_late_usd=50,
        spot_safe_cutoff_s=60,
        rapid_drop_threshold=0.05, rapid_drop_window_s=10,
        max_loss_cents_per_contract=25,
        reversal_threshold=0.12, reversal_threshold_settling=0.20,
        reversal_threshold_profit=0.08, profit_tighten_above_entry=0.05,
        peak_window_s=30, proactive_after_s=30,
        prob_floor=0.40,
    )
