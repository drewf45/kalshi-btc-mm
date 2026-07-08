"""Chunk 8 — Treasury waterfall.

Per settled WIN: tax (30%) -> Drew's long-term savings, operator fee (5%) -> Drew,
remainder (65%) -> engine book. Losses reduce engine book only.
Accruals never clawed back (withhold-first).
Rates env-tunable; Drew amends by ruling in the daily log.
"""

import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Optional

from . import store, notify

log = logging.getLogger("k_worker.treasury")

NY = ZoneInfo("America/New_York")

TAX_RATE = float(os.environ.get("TREASURY_TAX_RATE", "0.30"))
OPERATOR_FEE = float(os.environ.get("TREASURY_OPERATOR_FEE", "0.05"))
SEED = float(os.environ.get("TREASURY_SEED", "12.38"))

FLOOR_MILESTONES = [
    (60.0, 40.0),
    (35.0, 25.0),
    (20.0, 15.0),
]

OPERATING_FLOOR = 5.00


def _get_float(key: str, default: float = 0.0) -> float:
    raw = store.get_state(key)
    if raw is not None:
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass
    return default


def _set_float(key: str, value: float) -> None:
    store.set_state(key, f"{value:.6f}")


_was_genesis = False


def init_book() -> None:
    global _was_genesis
    if store.get_state("treasury_engine_book") is None:
        _was_genesis = True
        _set_float("treasury_engine_book", SEED)
        log.info(f"[TREASURY] Initialized engine book at seed=${SEED:.2f}")


from dataclasses import dataclass as _dataclass


@_dataclass
class TreasurySnapshot:
    book: float
    accrued_tax: float
    accrued_fee: float


def snapshot() -> TreasurySnapshot:
    """Current treasury state as a snapshot (book + accruals)."""
    t = get_totals()
    return TreasurySnapshot(
        book=t["engine_book"],
        accrued_tax=t["accrued_tax"],
        accrued_fee=t["accrued_fee"],
    )


def is_genesis() -> bool:
    """True if treasury was freshly seeded this boot (no prior state)."""
    return _was_genesis


def set_book(value: float) -> None:
    """Set the engine book directly (used by boot reconcile)."""
    _set_float("treasury_engine_book", value)
    log.warning(f"[TREASURY] Book set to ${value:.2f}")


def rebuild_accruals(tax: float, fee: float) -> None:
    """Redistribute from book to accruals without changing total system value.
    Used on fresh-store boots where book = live_bal (already includes all P&L).
    Subtracts tax+fee from book, sets accruals — total unchanged."""
    _set_float("treasury_accrued_tax", tax)
    _set_float("treasury_accrued_fee", fee)
    book = _get_float("treasury_engine_book", SEED)
    new_book = book - tax - fee
    _set_float("treasury_engine_book", new_book)
    log.warning(f"[TREASURY] Rebuild accruals: tax=${tax:.3f} fee=${fee:.3f} "
                f"book ${book:.2f} → ${new_book:.2f} (total unchanged)")


def get_totals() -> Dict[str, float]:
    return {
        "engine_book": _get_float("treasury_engine_book", SEED),
        "accrued_tax": _get_float("treasury_accrued_tax"),
        "accrued_fee": _get_float("treasury_accrued_fee"),
        "paid_tax": _get_float("treasury_paid_tax"),
        "paid_fee": _get_float("treasury_paid_fee"),
    }


def accrued_total() -> float:
    """Total owed to Drew (tax + fee)."""
    t = get_totals()
    return t["accrued_tax"] + t["accrued_fee"]


def tradeable_balance(cash: float) -> float:
    """Tradeable capital = cash minus what's owed to Drew.
    Belt-and-suspenders: if invariant is drifting, trade on the smaller claim."""
    t = get_totals()
    after_owed = cash - t["accrued_tax"] - t["accrued_fee"]
    return min(after_owed, t["engine_book"])


def waterfall(pnl: float) -> Dict:
    """Slice a win's pnl through the waterfall. Only call on wins with pnl > 0."""
    tax = pnl * TAX_RATE
    fee = pnl * OPERATOR_FEE
    remainder = pnl - tax - fee

    totals = get_totals()
    new_book = totals["engine_book"] + remainder
    new_tax = totals["accrued_tax"] + tax
    new_fee = totals["accrued_fee"] + fee

    _set_float("treasury_engine_book", new_book)
    _set_float("treasury_accrued_tax", new_tax)
    _set_float("treasury_accrued_fee", new_fee)

    log.info(f"[TREASURY] WIN split: tax +${tax:.3f} fee +${fee:.3f} book +${remainder:.3f} "
             f"→ book=${new_book:.2f} accrued_tax=${new_tax:.3f} accrued_fee=${new_fee:.3f}")

    return {
        "tax": tax,
        "fee": fee,
        "remainder": remainder,
        "engine_book": new_book,
        "accrued_tax": new_tax,
        "accrued_fee": new_fee,
        "treasury_line": (
            f"→ tax +${tax:.3f} | fee +${fee:.3f} | book +${remainder:.3f} "
            f"‖ accrued: tax ${new_tax:.2f} · fee ${new_fee:.2f} · book ${new_book:.2f}"
        ),
    }


def record_loss(pnl: float) -> None:
    """Deduct a loss from engine book only. pnl should be negative."""
    book = _get_float("treasury_engine_book", SEED)
    new_book = book + pnl
    _set_float("treasury_engine_book", new_book)
    log.info(f"[TREASURY] LOSS: book ${pnl:+.3f} → ${new_book:.2f}")


# ── Payout detection ──────────────────────────────────────────────

_payout_detected_ts: float = 0.0
_payout_detected_amount: float = 0.0
_PAYOUT_MATCH_TOL = 0.10
_PAYOUT_RENOTIFY_SEC = 86400


def is_payout_match(drop: float) -> bool:
    """True if this balance drop matches accrued (Drew withdrew his cut)."""
    owed = accrued_total()
    if owed < 0.01:
        return False
    return abs(drop - owed) <= _PAYOUT_MATCH_TOL


def record_payout_detected(drop: float) -> None:
    """Suppress the drift alert and post a PAYOUT DETECTED message."""
    global _payout_detected_ts, _payout_detected_amount
    _payout_detected_ts = time.time()
    _payout_detected_amount = drop
    owed = accrued_total()
    notify.send(
        f"\U0001f4b8 PAYOUT DETECTED? balance -${drop:.2f} matches owed ${owed:.2f} "
        f"— confirm with: python -m k_worker.treasury_paid {drop:.2f}"
    )
    log.warning(f"[TREASURY] Payout detected: drop=${drop:.2f} owed=${owed:.2f}")


def has_unconfirmed_payout() -> bool:
    return _payout_detected_ts > 0.0


def check_unconfirmed_payout() -> None:
    """Re-alert if a detected payout goes unconfirmed for >24h."""
    global _payout_detected_ts
    if _payout_detected_ts == 0.0:
        return
    elapsed = time.time() - _payout_detected_ts
    if elapsed >= _PAYOUT_RENOTIFY_SEC:
        notify.alert(
            f"UNCONFIRMED PAYOUT: ${_payout_detected_amount:.2f} detected "
            f"{elapsed / 3600:.0f}h ago — confirm or investigate"
        )
        _payout_detected_ts = time.time()


# ── Invariant check ───────────────────────────────────────────────

_last_invariant_alert_ts: float = 0.0
_last_invariant_drift: float = 0.0
_INVARIANT_THROTTLE_SEC = 1800


def check_invariant(total_balance: float) -> Optional[str]:
    """Verify total_balance ~ engine_book + accrued_tax + accrued_fee.
    Returns None if OK, drift description string if drift > $0.05.
    Payout-matching drops are suppressed (posted as PAYOUT DETECTED instead).
    Alert hygiene: first occurrence, then only on change > $0.10 or every 30 min."""
    global _last_invariant_alert_ts, _last_invariant_drift
    t = get_totals()
    expected = t["engine_book"] + t["accrued_tax"] + t["accrued_fee"]
    drift = total_balance - expected
    abs_drift = abs(drift)

    if abs_drift <= 0.05:
        return None

    # Payout detection: balance dropped by ~accrued (Drew withdrew)
    if drift < 0 and is_payout_match(abs(drift)):
        record_payout_detected(abs(drift))
        return None

    # Check unconfirmed payouts (re-alert after 24h)
    check_unconfirmed_payout()

    msg = (f"INVARIANT DRIFT: balance=${total_balance:.2f} vs "
           f"expected=${expected:.2f} (book=${t['engine_book']:.2f} + "
           f"tax=${t['accrued_tax']:.2f} + fee=${t['accrued_fee']:.2f}) "
           f"drift=${drift:+.2f}")
    log.error(f"[TREASURY] {msg}")
    now = time.time()
    drift_change = abs(abs_drift - _last_invariant_drift)
    should_alert = (
        _last_invariant_alert_ts == 0.0
        or drift_change > 0.10
        or (now - _last_invariant_alert_ts) >= _INVARIANT_THROTTLE_SEC
    )
    if should_alert:
        notify.alert(f"Treasury: {msg}")
        _last_invariant_alert_ts = now
        _last_invariant_drift = abs_drift
    return msg


def _floor_for_book(book: float) -> Optional[float]:
    for threshold, floor in FLOOR_MILESTONES:
        if book >= threshold:
            return floor
    return None


# ── Payout confirmation ──────────────────────────────────────────

def mark_paid(amount: Optional[float] = None) -> None:
    """Move accrued to paid (after manual withdrawal confirmation).
    Writes an epochs row, resets accrued, recomputes book so invariant goes green."""
    global _payout_detected_ts, _payout_detected_amount
    t = get_totals()
    owed = t["accrued_tax"] + t["accrued_fee"]

    if amount is not None and abs(amount - owed) > _PAYOUT_MATCH_TOL:
        log.warning(f"[TREASURY] mark_paid amount=${amount:.2f} vs owed=${owed:.2f} "
                    f"— mismatch > ${_PAYOUT_MATCH_TOL}, proceeding anyway")

    paid_amount = amount if amount is not None else owed

    new_paid_tax = t["paid_tax"] + t["accrued_tax"]
    new_paid_fee = t["paid_fee"] + t["accrued_fee"]

    # Adjust book downward by the paid amount (money left the account)
    new_book = t["engine_book"]
    book_adjustment = paid_amount - owed
    if abs(book_adjustment) > 0.001:
        new_book = t["engine_book"] + book_adjustment
        _set_float("treasury_engine_book", new_book)

    log.warning(f"[TREASURY] PAYOUT: tax ${t['accrued_tax']:.2f} → paid (lifetime ${new_paid_tax:.2f}), "
                f"fee ${t['accrued_fee']:.2f} → paid (lifetime ${new_paid_fee:.2f})")

    _set_float("treasury_paid_tax", new_paid_tax)
    _set_float("treasury_paid_fee", new_paid_fee)
    _set_float("treasury_accrued_tax", 0.0)
    _set_float("treasury_accrued_fee", 0.0)

    store.record_epoch(t["engine_book"], new_book,
                       -paid_amount, "PAYOUT")

    _payout_detected_ts = 0.0
    _payout_detected_amount = 0.0

    notify.send(
        f"\U0001f4b8 TREASURY PAID: tax ${t['accrued_tax']:.2f} + fee ${t['accrued_fee']:.2f} "
        f"= ${owed:.2f} (withdrawn ${paid_amount:.2f})\n"
        f"Lifetime: tax ${new_paid_tax:.2f} · fee ${new_paid_fee:.2f}"
    )


# ── Daily payout notice (08:00 ET) ────────────────────────────────

def is_payout_notice_time() -> bool:
    """True during the 08:00-08:05 ET window."""
    now = datetime.now(NY)
    return now.hour == 8 and now.minute < 5


def send_payout_notice(live_balance: float) -> None:
    """Post the daily payout notice at 08:00 ET."""
    t = get_totals()
    owed = t["accrued_tax"] + t["accrued_fee"]
    if owed < 0.01:
        return

    tradeable_after = tradeable_balance(live_balance) - owed
    if tradeable_after < OPERATING_FLOOR:
        safe_amount = max(0, tradeable_balance(live_balance) - OPERATING_FLOOR)
        if safe_amount < 0.01:
            notify.send(
                f"\U0001f4b8 PAYOUT NOTICE — you are owed ${owed:.2f} "
                f"(tax ${t['accrued_tax']:.3f} + fee ${t['accrued_fee']:.3f}). "
                f"Floor protection: tradeable ${tradeable_balance(live_balance):.2f} "
                f"< floor ${OPERATING_FLOOR:.2f} after payout — hold until book grows."
            )
        else:
            notify.send(
                f"\U0001f4b8 PAYOUT NOTICE — you are owed ${owed:.2f} "
                f"(tax ${t['accrued_tax']:.3f} + fee ${t['accrued_fee']:.3f}). "
                f"Floor protection: safe partial ${safe_amount:.2f} of ${owed:.2f}. "
                f"Withdraw ${safe_amount:.2f} on Kalshi, then confirm: "
                f"python -m k_worker.treasury_paid {safe_amount:.2f}"
            )
    else:
        notify.send(
            f"\U0001f4b8 PAYOUT NOTICE — you are owed ${owed:.2f} "
            f"(tax ${t['accrued_tax']:.3f} + fee ${t['accrued_fee']:.3f}). "
            f"Withdraw ${owed:.2f} on Kalshi, then confirm: "
            f"python -m k_worker.treasury_paid {owed:.2f}"
        )


# ── Display formatters ────────────────────────────────────────────

def format_hourly(cash: float = 0.0, pv: float = 0.0) -> str:
    t = get_totals()
    owed = t["accrued_tax"] + t["accrued_fee"]
    trd = tradeable_balance(cash) if cash > 0 else t["engine_book"]
    account = cash + pv
    return (f"Tradeable ${trd:.2f} | owed-to-Drew ${owed:.2f} | "
            f"account ${account:.2f} | positions ${pv:.2f}")


def format_scoreboard() -> str:
    t = get_totals()
    lines = [" TREASURY"]
    lines.append(f"  Engine book:  ${t['engine_book']:.2f}")
    lines.append(f"  Accrued tax:  ${t['accrued_tax']:.3f}")
    lines.append(f"  Accrued fee:  ${t['accrued_fee']:.3f}")
    lines.append(f"  Lifetime tax: ${t['paid_tax']:.2f}")
    lines.append(f"  Lifetime fee: ${t['paid_fee']:.2f}")
    floor = _floor_for_book(t["engine_book"])
    if floor is not None:
        lines.append(f"  Floor:        ${floor:.2f}")
    owed = t["accrued_tax"] + t["accrued_fee"]
    lines.append(f"  Total owed:   ${owed:.2f}")
    return "\n".join(lines)
