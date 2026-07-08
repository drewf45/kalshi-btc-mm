"""Load and query the delta table CSV.

R1: Refuses synthetic data by construction — checks manifest source field
    and SHA-256 integrity. Synthetic CSV = TABLE_ABSENT path + alert.
R2: All gates use wilson_ub (computed with effective_n = distinct 15-min
    windows), never the point estimate p_cross.

Boot-safe: missing/corrupt/synthetic CSV -> TABLE_ABSENT alert, static gates.
"""

import csv
import json
import hashlib
import math
import os
import logging
from typing import Optional, Dict, Tuple

log = logging.getLogger("k_worker.delta_table_loader")

_TABLE: Dict[Tuple[int, int, str], dict] = {}
_LOADED = False
_ALERTED = False
_REFUSAL_REASON: Optional[str] = None

DISTANCE_STEP = 5
TIME_GRID = [10, 30, 60, 120, 180, 300, 600, 900]


def _data_dir() -> str:
    db_path = os.environ.get("K_WORKER_DB", "")
    if db_path:
        return os.path.dirname(db_path)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_csv_path() -> str:
    return os.path.join(_data_dir(), "delta_table.csv")


def _default_manifest_path() -> str:
    return os.path.join(_data_dir(), "candles_manifest.json")


def load(path: Optional[str] = None) -> bool:
    """Load delta_table.csv into memory.

    R1: Reads candles_manifest.json alongside the CSV and REFUSES to load when:
      (a) source is not a real Coinbase pull (contains 'Synthetic')
      (b) csv_sha256 doesn't match the actual file
    Refusal -> TABLE_ABSENT path + alert naming the reason.
    Returns True on success.
    """
    global _TABLE, _LOADED, _ALERTED, _REFUSAL_REASON
    csv_path = path or _default_csv_path()
    manifest_dir = os.path.dirname(csv_path)
    manifest_path = os.path.join(manifest_dir, "candles_manifest.json")

    # R1(a): Check manifest exists and source is real
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except FileNotFoundError:
        _REFUSAL_REASON = "manifest missing"
        log.warning(f"[DELTA_TABLE] Manifest not found: {manifest_path}")
        _LOADED = False
        return False
    except Exception as e:
        _REFUSAL_REASON = f"manifest corrupt: {e}"
        log.warning(f"[DELTA_TABLE] Manifest unreadable: {e}")
        _LOADED = False
        return False

    source = manifest.get("source", "")
    if "Synthetic" in source or "synthetic" in source.lower():
        _REFUSAL_REASON = f"synthetic data ({source[:60]})"
        log.warning(f"[DELTA_TABLE] REFUSED: synthetic data — {source}")
        _LOADED = False
        return False

    if "Coinbase" not in source and "coinbase" not in source.lower():
        _REFUSAL_REASON = f"unknown source ({source[:60]})"
        log.warning(f"[DELTA_TABLE] REFUSED: source not Coinbase — {source}")
        _LOADED = False
        return False

    # R1(b): SHA-256 integrity
    try:
        actual_sha = hashlib.sha256(open(csv_path, "rb").read()).hexdigest()
    except FileNotFoundError:
        _REFUSAL_REASON = "CSV file missing"
        log.warning(f"[DELTA_TABLE] CSV not found: {csv_path}")
        _LOADED = False
        return False

    expected_sha = manifest.get("csv_sha256", "")
    if actual_sha != expected_sha:
        _REFUSAL_REASON = f"SHA mismatch (actual={actual_sha[:16]} != expected={expected_sha[:16]})"
        log.warning(f"[DELTA_TABLE] REFUSED: SHA-256 mismatch")
        _LOADED = False
        return False

    # Load CSV
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            table = {}
            for row in reader:
                d = int(row["distance_usd"])
                t = int(row["secs_remaining"])
                s = row["session"]
                table[(d, t, s)] = {
                    "p_cross": float(row["p_cross"]),
                    "n": int(row["n"]),
                    "effective_n": int(row["effective_n"]),
                    "wilson_ub": float(row["wilson_ub"]),
                }
        _TABLE = table
        _LOADED = True
        _REFUSAL_REASON = None
        log.info(f"[DELTA_TABLE] Loaded {len(table)} cells from {csv_path} "
                 f"(source: {source[:40]}, sha: {actual_sha[:16]})")
        return True
    except Exception as e:
        _REFUSAL_REASON = f"CSV parse error: {e}"
        log.error(f"[DELTA_TABLE] Failed to parse CSV: {e}")
        _LOADED = False
        return False


def is_loaded() -> bool:
    return _LOADED


def refusal_reason() -> Optional[str]:
    return _REFUSAL_REASON


def alert_if_absent() -> bool:
    """Send TABLE_ABSENT alert once per boot. Returns True if absent."""
    global _ALERTED
    if _LOADED:
        return False
    if not _ALERTED:
        _ALERTED = True
        reason = _REFUSAL_REASON or "unknown"
        try:
            from . import notify
            notify.alert(f"TABLE_ABSENT: delta_table.csv not loaded — {reason}. Static gates only.")
        except Exception:
            pass
        log.warning(f"[DELTA_TABLE] TABLE_ABSENT ({reason}): operating on static gates only")
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


def _lookup(distance_usd: float, secs_remaining: float,
            session: str = "ALL") -> Optional[dict]:
    """Raw cell lookup. Returns dict with p_cross, n, effective_n, wilson_ub."""
    if not _LOADED:
        return None
    d = _round_distance_down(distance_usd)
    if d > 2000:
        d = 2000
    t = _nearest_time_down(secs_remaining)
    return _TABLE.get((d, t, session))


def p_cross(distance_usd: float, secs_remaining: float,
            session: str = "ALL") -> Optional[float]:
    """Point estimate P(cross). Use wilson_ub for gating decisions."""
    cell = _lookup(distance_usd, secs_remaining, session)
    return cell["p_cross"] if cell else None


def wilson_ub(distance_usd: float, secs_remaining: float,
              session: str = "ALL") -> Optional[float]:
    """Wilson upper bound on P(cross), computed with effective_n."""
    cell = _lookup(distance_usd, secs_remaining, session)
    return cell["wilson_ub"] if cell else None


def h8_table_verdict(distance_usd: float, secs_remaining: float,
                     session: str = "ALL") -> dict:
    """H8 dual-gate table verdict.

    Gates on wilson_ub <= 0.01 (R2: never the point estimate).
    Distance rounded DOWN to $5 grid.

    Returns dict:
      qualified: bool or None (None = table absent)
      p_cross, wilson_ub: float or None
      distance_grid: int
      reason: str
    """
    if not _LOADED:
        return {"qualified": None, "p_cross": None, "wilson_ub": None,
                "distance_grid": 0, "reason": "TABLE_ABSENT"}

    d = _round_distance_down(distance_usd)
    if d > 2000:
        d = 2000
    cell = _lookup(distance_usd, secs_remaining, session)

    if cell is None:
        return {"qualified": None, "p_cross": None, "wilson_ub": None,
                "distance_grid": d, "reason": "CELL_MISSING"}

    qualified = cell["wilson_ub"] <= 0.01

    return {
        "qualified": qualified,
        "p_cross": cell["p_cross"],
        "wilson_ub": cell["wilson_ub"],
        "distance_grid": d,
        "reason": "PASS" if qualified else f"wilson_ub={cell['wilson_ub']:.4f}>0.01",
    }


def f_top_rung_verdict(distance_usd: float, secs_remaining: float,
                       session: str = "ALL") -> dict:
    """F top-rung guard: table verdict for the T-900-600 rung.

    Gates on wilson_ub (R2): if wilson_ub for P(cross) is negligible
    (boundary too far), the early entry is underwater -> SKIP.
    "Table says no" = wilson_ub of P(cross) <= 0.001.

    Returns dict:
      qualified: bool or None (None = table absent, static gate decides)
      p_cross, wilson_ub: float or None
      distance_grid: int
      reason: str
    """
    if not _LOADED:
        return {"qualified": None, "p_cross": None, "wilson_ub": None,
                "distance_grid": 0, "reason": "TABLE_ABSENT"}

    cell = _lookup(distance_usd, secs_remaining, session)
    d = _round_distance_down(distance_usd)
    if d > 2000:
        d = 2000

    if cell is None:
        return {"qualified": None, "p_cross": None, "wilson_ub": None,
                "distance_grid": d, "reason": "CELL_MISSING"}

    # Qualified = crossing probability is NOT negligible = boundary close enough
    qualified = cell["wilson_ub"] > 0.001

    return {
        "qualified": qualified,
        "p_cross": cell["p_cross"],
        "wilson_ub": cell["wilson_ub"],
        "distance_grid": d,
        "reason": "PASS" if qualified else f"wilson_ub={cell['wilson_ub']:.6f}<=0.001",
    }


def table_status() -> dict:
    """Status summary for the Daily Review Pack."""
    if not _LOADED:
        from . import delta_table_builder
        if delta_table_builder.is_building():
            return {"status": "BUILDING", "detail": "Background build in progress"}
        return {"status": "ABSENT", "detail": _REFUSAL_REASON or "not loaded"}

    manifest_p = _default_manifest_path()
    try:
        with open(manifest_p) as f:
            m = json.load(f)
        return {
            "status": "LOADED",
            "days": m.get("days_covered", 0),
            "built_at": m.get("built_at", "?"),
            "detail": f"{m.get('days_covered', 0):.0f}d, built {m.get('built_at', '?')}",
        }
    except Exception:
        return {"status": "LOADED", "detail": "manifest unreadable"}
