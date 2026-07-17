"""Budget ledger — the inside body's single mechanism for capital coordination.

The watchdog owns this ledger. The scanner MUST call reserve() before every
seed; if denied, the seed is tagged SKIP_INSIDE_DENIED and the scanner moves on.
The inside body's veto is absolute and instant.

Reservation protocol:
  1. Outside calls reserve(ticker, cost_cents, dclass, time_to_settle_sec)
  2. Inside grants only if ALL pass: live_halt clear, capital limits, class cap,
     per-market cap (1-lot initially), lockup budget, no EVIDENCE_BROKEN pending.
  3. Grant → outside places, reservation converts to at_risk via convert().
  4. Settlement or abandon → inside releases via release().

Every decision is an append-only row in budget_decisions.
"""

import time
import json
import logging
from typing import Optional
from dataclasses import dataclass

from . import dstore

log = logging.getLogger("d_worker.budget")

DEFAULT_BOOK_CAP_USD = 3.0
DEFAULT_LOCKUP_DAYS_CAP = 50.0
DEFAULT_PER_CLASS_CAP_USD = 2.0
DEFAULT_PER_MARKET_CAP_LOTS = 1


@dataclass
class ReserveResult:
    granted: bool
    reason: str
    reservation_id: Optional[int] = None


def init_budget() -> None:
    """Initialize budget_state from dstate if not present."""
    if dstore.get_state("budget_book_cap_usd") is None:
        dstore.set_state("budget_book_cap_usd", str(DEFAULT_BOOK_CAP_USD))
    if dstore.get_state("budget_lockup_days_cap") is None:
        dstore.set_state("budget_lockup_days_cap", str(DEFAULT_LOCKUP_DAYS_CAP))
    if dstore.get_state("budget_per_class_cap_usd") is None:
        dstore.set_state("budget_per_class_cap_usd", str(DEFAULT_PER_CLASS_CAP_USD))
    if dstore.get_state("budget_per_market_cap_lots") is None:
        dstore.set_state("budget_per_market_cap_lots",
                         str(DEFAULT_PER_MARKET_CAP_LOTS))


def _book_cap_usd() -> float:
    return float(dstore.get_state("budget_book_cap_usd") or DEFAULT_BOOK_CAP_USD)


def _lockup_days_cap() -> float:
    return float(dstore.get_state("budget_lockup_days_cap") or DEFAULT_LOCKUP_DAYS_CAP)


def _per_class_cap_usd() -> float:
    return float(dstore.get_state("budget_per_class_cap_usd")
                 or DEFAULT_PER_CLASS_CAP_USD)


def _per_market_cap_lots() -> int:
    return int(dstore.get_state("budget_per_market_cap_lots")
               or DEFAULT_PER_MARKET_CAP_LOTS)


def reserve(ticker: str, cost_cents: int, dclass: str,
            time_to_settle_sec: float, side: str = None) -> ReserveResult:
    """Request capital reservation from the inside body.
    Returns ReserveResult with granted/denied and reason."""

    # Gate 1: live_halt
    if dstore.get_state("live_halt") == "1":
        reason = "live_halt active"
        dstore.insert_budget_decision(ticker, cost_cents, dclass, "DENIED", reason)
        return ReserveResult(False, reason)

    # Gate 2: halt_promotion (classifier invariant break)
    if dstore.get_state("halt_promotion") == "1":
        reason = "halt_promotion active"
        dstore.insert_budget_decision(ticker, cost_cents, dclass, "DENIED", reason)
        return ReserveResult(False, reason)

    # Gate 3: evidence_broken pending
    watch = dstore.watch_stats_since(0)
    broken_total = watch.get("broken", 0)
    broken_cleared = int(dstore.get_state("evidence_broken_cleared") or "0")
    if broken_total > broken_cleared:
        reason = f"EVIDENCE_BROKEN pending ({broken_total - broken_cleared} uncleared)"
        dstore.insert_budget_decision(ticker, cost_cents, dclass, "DENIED", reason)
        return ReserveResult(False, reason)

    # Gate 4: book cap (at_risk + reserved + cost ≤ book_cap)
    ledger = get_ledger_summary()
    cost_usd = cost_cents / 100.0
    book_cap = _book_cap_usd()
    if ledger["at_risk_usd"] + ledger["reserved_usd"] + cost_usd > book_cap:
        reason = (f"book_cap exceeded: "
                  f"at_risk ${ledger['at_risk_usd']:.2f} + "
                  f"reserved ${ledger['reserved_usd']:.2f} + "
                  f"cost ${cost_usd:.2f} > cap ${book_cap:.2f}")
        dstore.insert_budget_decision(ticker, cost_cents, dclass, "DENIED", reason)
        return ReserveResult(False, reason)

    # Gate 5: per-class cap
    class_cap = _per_class_cap_usd()
    class_exposure = _class_exposure_usd(dclass)
    if class_exposure + cost_usd > class_cap:
        reason = f"class_cap exceeded: {dclass} ${class_exposure:.2f} + ${cost_usd:.2f} > ${class_cap:.2f}"
        dstore.insert_budget_decision(ticker, cost_cents, dclass, "DENIED", reason)
        return ReserveResult(False, reason)

    # Gate 6: per-market cap
    market_lots = _market_lots(ticker)
    max_lots = _per_market_cap_lots()
    if market_lots >= max_lots:
        reason = f"per_market_cap: {ticker} has {market_lots}/{max_lots} lots"
        dstore.insert_budget_decision(ticker, cost_cents, dclass, "DENIED", reason)
        return ReserveResult(False, reason)

    # Gate 7: lockup budget
    lockup_days = cost_cents * (time_to_settle_sec / 86400.0) / 100.0
    current_lockup = ledger["lockup_days"]
    lockup_cap = _lockup_days_cap()
    if current_lockup + lockup_days > lockup_cap:
        reason = f"lockup_cap: {current_lockup:.1f} + {lockup_days:.1f} > {lockup_cap:.1f}"
        dstore.insert_budget_decision(ticker, cost_cents, dclass, "DENIED", reason)
        return ReserveResult(False, reason)

    # All gates passed — grant reservation
    res_id = dstore.insert_budget_decision(
        ticker, cost_cents, dclass, "RESERVED", "all gates passed")
    log.info(f"[BUDGET] RESERVED: {ticker} {cost_cents}¢ {dclass} (id={res_id})")
    return ReserveResult(True, "granted", res_id)


def convert(reservation_id: int) -> bool:
    """Convert a reservation to at_risk (order placed successfully)."""
    return dstore.update_budget_decision(reservation_id, "AT_RISK")


def release(reservation_id: int, outcome: str = "SETTLED") -> bool:
    """Release capital back to the pool (settlement or abandon)."""
    return dstore.update_budget_decision(reservation_id, outcome)


def deny_all(reason: str) -> None:
    """Set live_halt — all future reserve() calls denied until cleared."""
    dstore.set_state("live_halt", "1")
    dstore.set_state("live_halt_reason", reason)
    log.warning(f"[BUDGET] live_halt SET: {reason}")


def clear_halt() -> None:
    """Clear live_halt (Drew-only, via env var)."""
    dstore.set_state("live_halt", "0")
    dstore.set_state("live_halt_reason", "")
    log.info("[BUDGET] live_halt CLEARED")


def get_ledger_summary() -> dict:
    """Current ledger state: at_risk, reserved, lockup days."""
    return dstore.budget_ledger_summary()


def _class_exposure_usd(dclass: str) -> float:
    """Total at_risk + reserved for a given dclass."""
    return dstore.budget_class_exposure(dclass)


def _market_lots(ticker: str) -> int:
    """Count of active (RESERVED or AT_RISK) entries for a ticker."""
    return dstore.budget_market_lots(ticker)
