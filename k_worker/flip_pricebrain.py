"""k_worker/flip_pricebrain.py — WO-PREDATOR B3: settlement is a 60s TWAP (CF Benchmarks
average over the close window vs the open). This encodes that truth: per held leg it computes
whether the running close-window average is LOCKED — no plausible remaining move can flip it
across the strike — so a last-seconds spot wobble does not trigger a needless scratch.

Pure + crypto-free. Conservative by construction: it only ever claims a lock it can prove
against a max-ADVERSE remainder, and it is only consulted in the final ≤60s (never earlier).
"""

from typing import Optional


def close_window_avg(samples) -> Optional[float]:
    """The running average of spot samples taken during the close window (the TWAP estimate)."""
    return (sum(samples) / len(samples)) if samples else None


def locked_margin(side: str, strike: Optional[float], samples, secs_left: Optional[float],
                  max_move_per_s: float, poll_sec: float = 3.0) -> bool:
    """True iff the leg is PROVABLY safe — even a max-adverse remainder cannot pull the
    close-window average across the strike. 'yes' wants avg ≥ strike; 'no' wants avg ≤ strike.
    Returns False whenever it cannot prove the lock (missing data, or the remainder could flip)."""
    if not samples or strike is None or secs_left is None or secs_left <= 0:
        return False
    n = len(samples)
    total = sum(samples)
    cur = samples[-1]
    n_rem = max(0, int(round(secs_left / max(0.5, poll_sec))))
    if n_rem == 0:                                   # window essentially closed — average alone
        avg = total / n
        return avg > strike if side == "yes" else avg < strike
    adverse_total = max_move_per_s * secs_left       # worst cumulative move over the remainder
    worst = (cur - adverse_total) if side == "yes" else (cur + adverse_total)
    worst_avg = (total + n_rem * worst) / (n + n_rem)
    return (worst_avg > strike) if side == "yes" else (worst_avg < strike)
