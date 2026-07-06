"""Chunk 5 — Discipline + external watcher.

Tail-loss kill: 3 losses in 60 min → full halt + ALERT.
Drawdown rail: balance < $5.00 → halt.
"""

import time
import logging
from typing import Optional

from . import notify

log = logging.getLogger("k_worker.discipline")

TAIL_LOSS_COUNT = 3
TAIL_LOSS_WINDOW_SEC = 3600  # 60 minutes
DRAWDOWN_FLOOR_USD = 5.00
RESET_FILE = "/tmp/k_worker_reset"

_recent_losses: list = []  # list of timestamps
_halted = False
_halt_reason: Optional[str] = None


def record_loss() -> None:
    """Record a loss. Check tail-loss kill."""
    global _halted, _halt_reason
    now = time.time()
    _recent_losses.append(now)
    # Prune old entries
    cutoff = now - TAIL_LOSS_WINDOW_SEC
    _recent_losses[:] = [ts for ts in _recent_losses if ts >= cutoff]

    if len(_recent_losses) >= TAIL_LOSS_COUNT:
        _halted = True
        _halt_reason = f"{TAIL_LOSS_COUNT} losses in {TAIL_LOSS_WINDOW_SEC // 60}min"
        msg = (
            f"TAIL-LOSS KILL: {TAIL_LOSS_COUNT} losses in "
            f"{TAIL_LOSS_WINDOW_SEC // 60} minutes.\n"
            f"Engine halted. Create {RESET_FILE} to resume."
        )
        log.error(f"[DISCIPLINE] {msg}")
        notify.alert(msg)


def record_win() -> None:
    """Record a win (no action needed, but clears are logged)."""
    pass


def check_drawdown(balance_usd: float) -> None:
    """Check drawdown rail. Halt if balance < floor."""
    global _halted, _halt_reason
    if balance_usd < DRAWDOWN_FLOOR_USD:
        _halted = True
        _halt_reason = f"balance=${balance_usd:.2f} < floor=${DRAWDOWN_FLOOR_USD:.2f}"
        msg = (
            f"DRAWDOWN HALT: balance=${balance_usd:.2f} < "
            f"floor=${DRAWDOWN_FLOOR_USD:.2f}.\n"
            f"Engine halted. Create {RESET_FILE} to resume."
        )
        log.error(f"[DISCIPLINE] {msg}")
        notify.alert(msg)


def is_halted() -> bool:
    """Check if the engine is halted. Auto-resets if reset file exists."""
    global _halted, _halt_reason
    if not _halted:
        return False
    # Check for operator reset
    import os
    if os.path.exists(RESET_FILE):
        try:
            os.remove(RESET_FILE)
        except Exception:
            pass
        log.warning("[DISCIPLINE] Reset file found — resuming")
        notify.send("Engine RESUMED by operator reset file.")
        _halted = False
        _halt_reason = None
        _recent_losses.clear()
        return False
    return True


def halt_reason() -> Optional[str]:
    return _halt_reason
