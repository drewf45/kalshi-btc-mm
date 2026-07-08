"""Load and query the delta table CSV.

Provides P(cross) lookups for the H8 dual-gate and F top-rung guard.
Boot-safe: missing/corrupt CSV -> TABLE_ABSENT alert, static gates only.
"""

import csv
import math
import os
import logging
from typing import Optional, Dict, Tuple

log = logging.getLogger("k_worker.delta_table_loader")

_TABLE: Dict[Tuple[int, int, str], float] = {}
_N_CACHE: Dict[Tuple[int, int], int] = {}
_LOADED = False
_ALERTED = False

CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "delta_table.csv")

DISTANCE_STEP = 5
TIME_GRID = [10, 30, 60, 120, 180, 300, 600, 900]


def load(path: Optional[str] = None) -> bool:
    """Load delta_table.csv into memory. Returns True on success."""
    global _TABLE, _LOADED, _ALERTED, _N_CACHE
    csv_path = path or CSV_PATH
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            table = {}
            n_cache = {}
            for row in reader:
                d = int(row["distance_usd"])
                t = int(row["secs_remaining"])
                s = row["session"]
                p = float(row["p_cross"])
                n = int(row["n"])
                table[(d, t, s)] = p
                if s == "ALL":
                    n_cache[(d, t)] = n
        _TABLE = table
        _N_CACHE = n_cache
        _LOADED = True
        log.info(f"[DELTA_TABLE] Loaded {len(table)} cells from {csv_path}")
        return True
    except FileNotFoundError:
        log.warning(f"[DELTA_TABLE] CSV not found: {csv_path}")
        _LOADED = False
        return False
    except Exception as e:
        log.error(f"[DELTA_TABLE] Failed to load {csv_path}: {e}")
        _LOADED = False
        return False


def is_loaded() -> bool:
    return _LOADED


def alert_if_absent() -> bool:
    """Send TABLE_ABSENT alert once per boot. Returns True if absent."""
    global _ALERTED
    if _LOADED:
        return False
    if not _ALERTED:
        _ALERTED = True
        try:
            from . import notify
            notify.alert("TABLE_ABSENT: delta_table.csv missing or corrupt — static gates only")
        except Exception:
            pass
        log.warning("[DELTA_TABLE] TABLE_ABSENT: operating on static gates only")
    return True


def _round_distance_down(distance_usd: float) -> int:
    """Round distance DOWN to the $5 grid (conservative)."""
    return max(50, int(distance_usd // DISTANCE_STEP) * DISTANCE_STEP)


def _nearest_time_down(secs: float) -> int:
    """Round time DOWN to the nearest grid point."""
    best = TIME_GRID[0]
    for t in TIME_GRID:
        if t <= secs:
            best = t
        else:
            break
    return best


def _wilson_ub(p: float, n: int, z: float = 1.96) -> float:
    """Wilson score upper bound given p_hat and n."""
    if n == 0:
        return 1.0
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return min(1.0, centre + spread)


def p_cross(distance_usd: float, secs_remaining: float,
            session: str = "ALL") -> Optional[float]:
    """Look up P(cross) from the table.

    Distance rounded DOWN to $5 grid (lower d -> higher P = conservative).
    Time rounded down to nearest grid point.
    Returns None if table not loaded or cell not found.
    """
    if not _LOADED:
        return None
    d = _round_distance_down(distance_usd)
    if d > 2000:
        d = 2000
    t = _nearest_time_down(secs_remaining)
    return _TABLE.get((d, t, session))


def h8_table_verdict(distance_usd: float, secs_remaining: float,
                     session: str = "ALL") -> dict:
    """H8 dual-gate table verdict.

    Returns dict:
      qualified: bool or None (None = table absent)
      p_cross: float or None
      wilson_ub: float or None
      distance_grid: int (distance rounded to $5 grid)
      reason: str
    """
    if not _LOADED:
        return {"qualified": None, "p_cross": None, "wilson_ub": None,
                "distance_grid": 0, "reason": "TABLE_ABSENT"}

    d = _round_distance_down(distance_usd)
    if d > 2000:
        d = 2000
    t = _nearest_time_down(secs_remaining)
    p = _TABLE.get((d, t, session))

    if p is None:
        return {"qualified": None, "p_cross": None, "wilson_ub": None,
                "distance_grid": d, "reason": "CELL_MISSING"}

    n = _N_CACHE.get((d, t), 250000)
    w_ub = _wilson_ub(p, n)
    qualified = w_ub <= 0.01

    return {
        "qualified": qualified,
        "p_cross": p,
        "wilson_ub": w_ub,
        "distance_grid": d,
        "reason": "PASS" if qualified else f"wilson_ub={w_ub:.4f}>0.01",
    }


def f_top_rung_verdict(distance_usd: float, secs_remaining: float,
                       session: str = "ALL") -> dict:
    """F top-rung guard: table verdict for the T-900-600 rung.

    Table says no -> SKIP_TABLE_UNQUALIFIED.
    "No" = P(cross) is negligible (boundary too far for early entry).

    Returns dict:
      qualified: bool or None (None = table absent, static gate decides)
      p_cross: float or None
      distance_grid: int
      reason: str
    """
    if not _LOADED:
        return {"qualified": None, "p_cross": None,
                "distance_grid": 0, "reason": "TABLE_ABSENT"}

    d = _round_distance_down(distance_usd)
    if d > 2000:
        d = 2000
    t = _nearest_time_down(secs_remaining)
    p = _TABLE.get((d, t, session))

    if p is None:
        return {"qualified": None, "p_cross": None,
                "distance_grid": d, "reason": "CELL_MISSING"}

    qualified = p > 0.001

    return {
        "qualified": qualified,
        "p_cross": p,
        "distance_grid": d,
        "reason": "PASS" if qualified else f"p_cross={p:.6f}<=0.001",
    }
