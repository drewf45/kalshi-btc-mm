"""Chunk 5 — Discipline + external watcher.

Tail-loss kill: 3 losses in 60 min -> full halt + ALERT.
Drawdown rail: balance < $5.00 -> halt.
State persisted to SQLite via store.get_state/set_state.
"""

import json
import time
import logging
from typing import Optional

from . import notify, store

log = logging.getLogger("k_worker.discipline")

TAIL_LOSS_COUNT = 3
TAIL_LOSS_WINDOW_SEC = 3600  # 60 minutes
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


def _set_halted(reason: str) -> None:
    store.set_state("halted", reason)


def _clear_halted() -> None:
    store.set_state("halted", "")


def record_loss() -> None:
    """Record a loss. Check tail-loss kill."""
    now = time.time()
    recent = _load_loss_ts()
    recent.append(now)
    cutoff = now - TAIL_LOSS_WINDOW_SEC
    recent = [ts for ts in recent if ts >= cutoff]
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
    """Check drawdown rail. Halt if balance < floor."""
    if balance_usd < DRAWDOWN_FLOOR_USD:
        reason = f"balance=${balance_usd:.2f} < floor=${DRAWDOWN_FLOOR_USD:.2f}"
        _set_halted(reason)
        msg = (
            f"DRAWDOWN HALT: balance=${balance_usd:.2f} < "
            f"floor=${DRAWDOWN_FLOOR_USD:.2f}.\n"
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
