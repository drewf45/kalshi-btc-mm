"""Delta table — borrowed WHOLE from the live tree (B1: k_worker/delta_table_loader.py,
vendored byte-identical at reference/live_k_worker/delta_table_loader.py).

Adapted ONLY in module wiring (imports, alert hook); every threshold, grid,
refusal rule, and verdict is the live code's. Per C.3:
  - API = gate-input only (`p_cross`, `wilson_ub`, verdicts) — no scheduler role.
  - Settlement truth anchors to floor_strike/expiration_value on the market
    record, never generic spot (`settlement_anchor` below enforces it).

R1: Refuses synthetic data by construction — checks manifest source field
    and SHA-256 integrity. Synthetic CSV = TABLE_ABSENT path + alert.
R2: All gates use wilson_ub (computed with effective_n = distinct 15-min
    windows), never the point estimate p_cross.

Boot-safe: missing/corrupt/synthetic CSV -> TABLE_ABSENT alert, static gates.
"""

import csv
import json
import hashlib
import os
import logging
from typing import Optional, Dict, Tuple

log = logging.getLogger("relay.delta")

_TABLE: Dict[Tuple[int, int, str], dict] = {}
_LOADED = False
_ALERTED = False
_REFUSAL_REASON: Optional[str] = None

# Alert hook (relay ops wires Telegram here; tests wire a list.append)
_alert_fn = lambda msg: None


def set_alert_fn(fn) -> None:
    global _alert_fn
    _alert_fn = fn


DISTANCE_STEP = 5
TIME_GRID = [10, 30, 60, 120, 180, 300, 600, 900]


def _data_dir() -> str:
    db_path = os.environ.get("RELAY_DB_PATH", "")
    if db_path and os.path.dirname(db_path):
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
                cell = {
                    "p_cross": float(row["p_cross"]),
                    "n": int(row["n"]),
                    "effective_n": int(row["effective_n"]),
                    "wilson_ub": float(row["wilson_ub"]),
                }
                # WO-2026-07-24-J P1: the SETTLE surface — p_end (window CLOSES
                # beyond the strike, the question the market prices) rides the
                # SAME table as one new column family. Absent on legacy tables →
                # not in the cell → the accessors return None → HUNT stays BLIND
                # (exactly the touch table's own absence discipline).
                if row.get("p_end") not in (None, ""):
                    cell["p_end"] = float(row["p_end"])
                    cell["p_end_n"] = int(row.get("p_end_n") or row["n"])
                    cell["p_end_wilson_lb"] = float(row["p_end_wilson_lb"])
                table[(d, t, s)] = cell
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
            _alert_fn(f"TABLE_ABSENT: delta_table.csv not loaded — {reason}. Static gates only.")
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


_MISS_LOGGED: set = set()   # P26 §1.3: (d, t, session) grid gaps, once each


def _lookup(distance_usd: float, secs_remaining: float,
            session: str = "ALL") -> Optional[dict]:
    """Raw cell lookup. Returns dict with p_cross, n, effective_n, wilson_ub."""
    if not _LOADED:
        return None
    d = _round_distance_down(distance_usd)
    if d > 2000:
        d = 2000
    t = _nearest_time_down(secs_remaining)
    cell = _TABLE.get((d, t, session))
    if cell is None:
        # P26 §1.3 (P25 §6): a miss WITH a loaded table names its (d, t) —
        # grid gaps become Saturday data, not mysteries. Once per pair.
        # DIAG-1 §1.3: PERSISTED as a failures row (alert=False) — the
        # in-memory set dies at restart; the interrogator reads the ledger.
        key = (d, t, session)
        if key not in _MISS_LOGGED and len(_MISS_LOGGED) < 500:
            _MISS_LOGGED.add(key)
            log.warning("[DELTA_TABLE] cell miss with LOADED table: "
                        "d=%d t=%d session=%s — grid gap, log for regrid",
                        d, t, session)
            try:
                from . import failures
                failures.fail("TABLE_CELL_MISS",
                              f"grid gap: d={d} t={t} session={session}",
                              alert=False, d=d, t=t, session=session)
            except Exception:
                pass  # the diagnostic never blocks the lookup
    return cell


def p_cross(distance_usd: float, secs_remaining: float,
            session: str = "ALL") -> Optional[float]:
    """Point estimate P(cross). Use wilson_ub for gating decisions."""
    cell = _lookup(distance_usd, secs_remaining, session)
    return cell["p_cross"] if cell else None


def distance_for_p(target_p: float, secs_remaining: float,
                   session: str = "ALL") -> Optional[float]:
    """WO-SWING-GATE-EVENT §2: invert the table — the distance at which
    P(cross)=target_p in the time left. p_cross falls monotonically with
    distance (farther = less likely to touch), so scan the $5 grid for the
    first cell whose p_cross drops at/below the target and interpolate.
    Used to translate a CONTRACT-PRICE barrier (an implied probability)
    into the spot move that reprices the contract that far. None when the
    table is absent or the target lies outside the grid's range."""
    if not _LOADED or secs_remaining <= 0:
        return None
    prev_d, prev_p = None, None
    d = 50
    while d <= 2000:
        p = p_cross(d, secs_remaining, session)
        if p is not None:
            if p <= target_p:
                if prev_d is None or prev_p is None or prev_p == p:
                    return float(d)
                # linear interpolation between the bracketing grid cells
                frac = (prev_p - target_p) / (prev_p - p)
                return prev_d + frac * (d - prev_d)
            prev_d, prev_p = d, p
        d += DISTANCE_STEP
    return None


def wilson_ub(distance_usd: float, secs_remaining: float,
              session: str = "ALL") -> Optional[float]:
    """Wilson upper bound on P(cross), computed with effective_n."""
    cell = _lookup(distance_usd, secs_remaining, session)
    return cell["wilson_ub"] if cell else None


def p_survive(distance_usd: float, secs_remaining: float,
              session: str = "ALL") -> Optional[float]:
    """Survival probability = 1 - p_cross. Gate-input only."""
    p = p_cross(distance_usd, secs_remaining, session)
    return None if p is None else 1.0 - p


def p_end(distance_usd: float, secs_remaining: float,
          session: str = "ALL") -> Optional[float]:
    """WO-2026-07-24-J P1: the SETTLE question — P(the window CLOSES at least
    `distance_usd` from where it started in the time left), the empirical
    analog of a contract settling beyond a strike that far away. The point
    estimate; GATE on p_end_wilson_lb (the conservative LOWER bound). None when
    the table is absent OR carries no settle surface (legacy touch-only) — HUNT
    is BLIND, never a guess, exactly like the touch table."""
    cell = _lookup(distance_usd, secs_remaining, session)
    return cell.get("p_end") if cell else None


def p_end_wilson_lb(distance_usd: float, secs_remaining: float,
                    session: str = "ALL") -> Optional[float]:
    """The Wilson LOWER bound on p_end, computed with effective_n — the number
    HUNT's edge gate reads (never the point estimate; the Adversary's thin-cell
    guard). None = table/surface absent → BLIND."""
    cell = _lookup(distance_usd, secs_remaining, session)
    return cell.get("p_end_wilson_lb") if cell else None


def settle_loaded() -> bool:
    """True when the loaded table carries the SETTLE surface (p_end). A legacy
    touch-only table returns False → HUNT's forward gate stays BLIND."""
    if not _LOADED:
        return False
    for cell in _TABLE.values():
        return "p_end" in cell
    return False


def h8_table_verdict(distance_usd: float, secs_remaining: float,
                     session: str = "ALL") -> dict:
    """H8 dual-gate table verdict.

    Gates on wilson_ub <= 0.01 (R2: never the point estimate) — the
    delta-gate >=99% survival, stated as a 1% crossing upper bound.
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


F_TOPRUNG_HAIRCUT = float(os.environ.get("F_TOPRUNG_HAIRCUT", "0.5"))
LOW_TAIL_UB = 0.001


def f_top_rung_verdict(distance_usd: float, secs_remaining: float,
                       cost_cents: float,
                       session: str = "ALL") -> dict:
    """F top-rung guard (T-900-600) — P3 (0708), Drew's ruling banked.

    Guard two-tail rule (doctrine 0708): a probability gate must state BOTH
    tails. The UPPER tail is the trade-killing one — at cost c cents, the
    breakeven crossing probability is (100 - c)/100; any cell whose wilson_ub
    exceeds a haircut of that breakeven is negative-EV and MUST skip.
      qualified = wilson_ub <= breakeven_p * F_TOPRUNG_HAIRCUT
    (haircut 0.5 default per safest-price doctrine; env-tunable.)

    The old low-tail skip (wilson_ub <= 0.001 -> skip) is REMOVED per
    capital-gate ruling #3 — the adverse-selection theory was UNPROVEN.
    Low-tail cells now trade and are TAGGED (low_tail=True) so the tape
    can prove or kill the theory from settlements.

    Returns dict:
      qualified: bool or None (None = table absent, static gate decides)
      p_cross, wilson_ub: float or None
      distance_grid: int
      low_tail: bool (tag-only, never a skip)
      breakeven_p: float
      reason: str
    """
    breakeven_p = max(0.0, (100.0 - float(cost_cents)) / 100.0)
    gate = breakeven_p * F_TOPRUNG_HAIRCUT

    if not _LOADED:
        return {"qualified": None, "p_cross": None, "wilson_ub": None,
                "distance_grid": 0, "low_tail": False,
                "breakeven_p": breakeven_p, "reason": "TABLE_ABSENT"}

    cell = _lookup(distance_usd, secs_remaining, session)
    d = _round_distance_down(distance_usd)
    if d > 2000:
        d = 2000

    if cell is None:
        return {"qualified": None, "p_cross": None, "wilson_ub": None,
                "distance_grid": d, "low_tail": False,
                "breakeven_p": breakeven_p, "reason": "CELL_MISSING"}

    wub = cell["wilson_ub"]
    qualified = wub <= gate
    low_tail = wub <= LOW_TAIL_UB

    return {
        "qualified": qualified,
        "p_cross": cell["p_cross"],
        "wilson_ub": wub,
        "distance_grid": d,
        "low_tail": low_tail,
        "breakeven_p": breakeven_p,
        "reason": ("PASS" if qualified
                   else f"wilson_ub={wub:.4f}>gate={gate:.4f}(be={breakeven_p:.2f}*hc={F_TOPRUNG_HAIRCUT})"),
    }


def table_status() -> dict:
    """Status summary for the daily pack."""
    if not _LOADED:
        from . import delta_builder
        if delta_builder.is_building():
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


# ---------------------------------------------------------------------------
# Settlement-anchor law (kept from the pre-B1 stub; not part of the loader)
# ---------------------------------------------------------------------------

def settlement_anchor(market_record: dict) -> Optional[float]:
    """Settlement truth = floor_strike / expiration_value on the market record.
    Generic spot is not an acceptable anchor and never will be."""
    if not isinstance(market_record, dict) or (
            "floor_strike" not in market_record and "expiration_value" not in market_record):
        raise ValueError(
            "market record lacks floor_strike/expiration_value — refusing to anchor "
            "settlement truth to generic spot")
    v = market_record.get("floor_strike", market_record.get("expiration_value"))
    return float(v) if v is not None else None
