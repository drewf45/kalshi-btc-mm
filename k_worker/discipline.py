"""Chunk 5 — Discipline + external watcher.

Tail-loss kill: 3 losses in 60 min -> full halt + ALERT.
Drawdown rail: balance < $5.00 -> halt.
State persisted to SQLite via store.get_state/set_state.
"""

import os
import json
import time
import logging
from typing import Optional

from . import notify, store

log = logging.getLogger("k_worker.discipline")

TAIL_LOSS_COUNT = 3
TAIL_LOSS_WINDOW_SEC = 3600  # 60 minutes
# WO-MORNING §2: the kill learns MAGNITUDE — only losses ≥ this (cents) count toward the
# 3-in-60, so routine 1–2¢ LONE_DECLINED flattens don't read as tail losses.
TAIL_LOSS_MIN_CENTS = int(os.environ.get("TAIL_LOSS_MIN_CENTS", "10"))
DRAWDOWN_FLOOR_USD = 5.00


def _load_loss_ts() -> list:
    raw = store.get_state("loss_ts")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return []


def _save_loss_ts(ts_list: list) -> None:
    store.set_state("loss_ts", json.dumps(ts_list))


def _ts_of(entry) -> float:
    """A loss entry is [ts, window_id] (new) or a bare ts float (legacy)."""
    return entry[0] if isinstance(entry, (list, tuple)) else entry


def _wid_of(entry) -> Optional[str]:
    return entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else None


def _set_halted(reason: str) -> None:
    store.set_state("halted", reason)


def _clear_halted() -> None:
    store.set_state("halted", "")


def record_loss(loss_cents: Optional[float] = None, window_id: Optional[str] = None,
                outcome_tag: Optional[str] = None) -> None:
    """Record a settled WINDOW loss and check the tail-loss kill. WO-MORNING §2: the kill
    learns magnitude and shape —
      • losses < TAIL_LOSS_MIN_CENTS don't count (routine noise);
      • LONE_DECLINED windows never count (the pair rule's bounded cost of business);
      • each window counts at most once (per window_id), so one window's two legs can't
        double-count.
    Legacy no-arg callers (directional lanes) keep the old count-every-loss behavior."""
    now = time.time()
    if outcome_tag == "LONE_DECLINED":
        return
    if loss_cents is not None and abs(loss_cents) < TAIL_LOSS_MIN_CENTS:
        return
    recent = _load_loss_ts()
    cutoff = now - TAIL_LOSS_WINDOW_SEC
    recent = [e for e in recent if _ts_of(e) >= cutoff]
    if window_id is not None and any(_wid_of(e) == window_id for e in recent):
        return                               # this window is already counted
    recent.append([now, window_id])
    _save_loss_ts(recent)

    if len(recent) >= TAIL_LOSS_COUNT:
        reason = f"{TAIL_LOSS_COUNT} losses in {TAIL_LOSS_WINDOW_SEC // 60}min"
        _set_halted(reason)
        msg = (
            f"TAIL-LOSS KILL: {TAIL_LOSS_COUNT} losses in "
            f"{TAIL_LOSS_WINDOW_SEC // 60} minutes.\n"
            f"Engine halted. Run `python -m k_worker.reset` to resume."
        )
        log.error(f"[DISCIPLINE] {msg}")
        notify.alert(msg)


def record_win() -> None:
    """Record a win (no action needed, but clears are logged)."""
    pass


def check_drawdown(balance_usd: float) -> None:
    """Check drawdown rail against tradeable balance (excludes accruals)."""
    from . import treasury
    tradeable = treasury.tradeable_balance(balance_usd)
    if tradeable < DRAWDOWN_FLOOR_USD:
        reason = f"tradeable=${tradeable:.2f} < floor=${DRAWDOWN_FLOOR_USD:.2f} (cash=${balance_usd:.2f})"
        _set_halted(reason)
        msg = (
            f"DRAWDOWN HALT: tradeable=${tradeable:.2f} < "
            f"floor=${DRAWDOWN_FLOOR_USD:.2f} (cash=${balance_usd:.2f}).\n"
            f"Engine halted. Run `python -m k_worker.reset` to resume."
        )
        log.error(f"[DISCIPLINE] {msg}")
        notify.alert(msg)


def is_halted() -> bool:
    """Check if the engine is halted (reads from persistent store)."""
    reason = store.get_state("halted")
    if reason:
        return True
    return False


def halt_reason() -> Optional[str]:
    return store.get_state("halted") or None


def reset() -> None:
    """Operator reset — clear halt state and loss history."""
    _clear_halted()
    _save_loss_ts([])
    log.warning("[DISCIPLINE] Reset by operator")
    notify.send("Engine RESUMED by operator reset.")
