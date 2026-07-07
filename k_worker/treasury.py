"""Chunk 8 — Treasury waterfall.

Per settled WIN: tax (30%) -> Drew's long-term savings, operator fee (5%) -> Drew,
remainder (65%) -> engine book. Losses reduce engine book only.
Accruals never clawed back (withhold-first).
Rates env-tunable; Drew amends by ruling in the daily log.
"""

import os
import time
import logging
from typing import Dict, Optional

from . import store, notify

log = logging.getLogger("k_worker.treasury")

TAX_RATE = float(os.environ.get("TREASURY_TAX_RATE", "0.30"))
OPERATOR_FEE = float(os.environ.get("TREASURY_OPERATOR_FEE", "0.05"))
PAYOUT_MIN = float(os.environ.get("TREASURY_PAYOUT_MIN", "5.00"))
SEED = float(os.environ.get("TREASURY_SEED", "12.38"))

FLOOR_MILESTONES = [
    (60.0, 40.0),
    (35.0, 25.0),
    (20.0, 15.0),
]


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


def get_totals() -> Dict[str, float]:
    return {
        "engine_book": _get_float("treasury_engine_book", SEED),
        "accrued_tax": _get_float("treasury_accrued_tax"),
        "accrued_fee": _get_float("treasury_accrued_fee"),
        "paid_tax": _get_float("treasury_paid_tax"),
        "paid_fee": _get_float("treasury_paid_fee"),
    }


def tradeable_balance(cash: float) -> float:
    """Cash minus accrued tax and fee — what the engine may trade with."""
    t = get_totals()
    return cash - t["accrued_tax"] - t["accrued_fee"]


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

    total_accrued = new_tax + new_fee
    if total_accrued >= PAYOUT_MIN:
        _send_payout_alert(new_tax, new_fee, new_book)

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


_last_invariant_alert_ts: float = 0.0
_last_invariant_drift: float = 0.0
_INVARIANT_THROTTLE_SEC = 1800


def check_invariant(total_balance: float) -> Optional[str]:
    """Verify total_balance ~ engine_book + accrued_tax + accrued_fee.
    Returns None if OK, drift description string if drift > $0.05.
    Alert hygiene: first occurrence, then only on change > $0.10 or every 30 min."""
    global _last_invariant_alert_ts, _last_invariant_drift
    t = get_totals()
    expected = t["engine_book"] + t["accrued_tax"] + t["accrued_fee"]
    drift = abs(total_balance - expected)
    if drift > 0.05:
        msg = (f"INVARIANT DRIFT: balance=${total_balance:.2f} vs "
               f"expected=${expected:.2f} (book=${t['engine_book']:.2f} + "
               f"tax=${t['accrued_tax']:.2f} + fee=${t['accrued_fee']:.2f}) "
               f"drift=${drift:.2f}")
        log.error(f"[TREASURY] {msg}")
        now = time.time()
        drift_change = abs(drift - _last_invariant_drift)
        should_alert = (
            _last_invariant_alert_ts == 0.0
            or drift_change > 0.10
            or (now - _last_invariant_alert_ts) >= _INVARIANT_THROTTLE_SEC
        )
        if should_alert:
            notify.alert(f"Treasury: {msg}")
            _last_invariant_alert_ts = now
            _last_invariant_drift = drift
        return msg
    return None


def _floor_for_book(book: float) -> Optional[float]:
    for threshold, floor in FLOOR_MILESTONES:
        if book >= threshold:
            return floor
    return None


def _send_payout_alert(accrued_tax: float, accrued_fee: float, book: float) -> None:
    total = accrued_tax + accrued_fee
    lines = [
        f"💰 TREASURY PAYOUT DUE: withdraw ${total:.2f}",
        f"  savings ${accrued_tax:.2f} (tax) + fee ${accrued_fee:.2f}",
    ]
    floor = _floor_for_book(book)
    if floor is not None and book > floor:
        excess = book - floor
        lines.append(f"  scrape: book ${book:.2f} > floor ${floor:.2f} → excess ${excess:.2f}")
    lines.append("Run `python -m k_worker.treasury_paid` after withdrawal.")
    notify.alert("\n".join(lines))


def mark_paid() -> None:
    """Move accrued to paid (after manual withdrawal)."""
    t = get_totals()
    new_paid_tax = t["paid_tax"] + t["accrued_tax"]
    new_paid_fee = t["paid_fee"] + t["accrued_fee"]

    log.warning(f"[TREASURY] PAYOUT: tax ${t['accrued_tax']:.2f} → paid (lifetime ${new_paid_tax:.2f}), "
                f"fee ${t['accrued_fee']:.2f} → paid (lifetime ${new_paid_fee:.2f})")

    _set_float("treasury_paid_tax", new_paid_tax)
    _set_float("treasury_paid_fee", new_paid_fee)
    _set_float("treasury_accrued_tax", 0.0)
    _set_float("treasury_accrued_fee", 0.0)

    notify.send(
        f"💰 TREASURY PAID: tax ${t['accrued_tax']:.2f} + fee ${t['accrued_fee']:.2f} "
        f"= ${t['accrued_tax'] + t['accrued_fee']:.2f}\n"
        f"Lifetime: tax ${new_paid_tax:.2f} · fee ${new_paid_fee:.2f}"
    )


def format_hourly() -> str:
    t = get_totals()
    owed = t["accrued_tax"] + t["accrued_fee"]
    return f"book ${t['engine_book']:.2f} | owed-to-Drew ${owed:.2f}"


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
