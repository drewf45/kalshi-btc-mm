"""Chunk 3 — Surface logger + shadow log. Append-only SQLite store.

Every KXBTC15M market gets one row — ENTER or SKIP.
Shadow rows: every market not entered, every locked band,
plus read-only observation of ETH/SOL/XRP 15M.

THE PHRASING LAW: every row states side, cost_per_contract_cents,
yes_quote_cents, and breakeven_pct. "price" standing alone is banned.
"""

import os
import time
import sqlite3
import logging
import threading
from typing import Optional
from dataclasses import dataclass, asdict

log = logging.getLogger("k_worker.store")

DB_PATH = os.environ.get("K_WORKER_DB", "k_worker_surface.db")

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def init_db() -> None:
    """Create the surface table if it doesn't exist."""
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS surface (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            market_ticker           TEXT NOT NULL,
            decision_ts             REAL NOT NULL,
            seconds_to_expiry       REAL,
            yes_quote_cents         INTEGER,
            side                    TEXT,
            cost_per_contract_cents INTEGER,
            breakeven_pct           REAL,
            book_depth_at_touch     INTEGER,
            yes_ask_cents           INTEGER,
            no_ask_cents            INTEGER,
            spread_cents            INTEGER,
            feed_lag_ms             REAL,
            action                  TEXT NOT NULL,
            skip_reason             TEXT,
            why_tag                 TEXT,
            order_type              TEXT,
            fill_cost_cents         INTEGER,
            fill_ts                 REAL,
            slippage_cents          INTEGER,
            fee_cents               INTEGER,
            contracts               INTEGER DEFAULT 1,
            dollars_at_risk         REAL,
            post_fill_drift_cents   INTEGER,
            resolution              TEXT,
            pnl_net                 REAL,
            settled_ts              REAL,
            env                     TEXT DEFAULT 'live-observed'
        )
    """)
    _conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_surface_ticker ON surface(market_ticker)
    """)
    _conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_surface_action ON surface(action)
    """)
    _conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_surface_env ON surface(env)
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS market_ledger (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            market_ticker   TEXT NOT NULL UNIQUE,
            settled_ts      REAL,
            result          TEXT,
            realized_cents  REAL DEFAULT 0,
            best_available_cents REAL DEFAULT 0,
            regret_missed   REAL DEFAULT 0,
            regret_avoided  REAL DEFAULT 0,
            outcome_class   TEXT
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS context_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date_start TEXT,
            date_end   TEXT,
            label      TEXT NOT NULL
        )
    """)
    for col, typ in [
        ("spot_price", "REAL"), ("boundary_lo", "REAL"), ("boundary_hi", "REAL"),
        ("distance", "REAL"), ("distance_pct", "REAL"), ("lane", "TEXT"),
        ("session_tag", "TEXT"), ("vol_regime", "TEXT"), ("winner_clip_cents", "REAL"),
        ("order_id", "TEXT"),
        ("reprice_count", "INTEGER"),
    ]:
        try:
            _conn.execute(f"ALTER TABLE surface ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    _conn.commit()
    log.info(f"[STORE] Initialized: {DB_PATH}")


@dataclass
class SurfaceRow:
    market_ticker: str
    decision_ts: float
    action: str  # ENTER or SKIP
    seconds_to_expiry: Optional[float] = None
    yes_quote_cents: Optional[int] = None
    side: Optional[str] = None
    cost_per_contract_cents: Optional[int] = None
    breakeven_pct: Optional[float] = None
    book_depth_at_touch: Optional[int] = None
    yes_ask_cents: Optional[int] = None
    no_ask_cents: Optional[int] = None
    spread_cents: Optional[int] = None
    feed_lag_ms: Optional[float] = None
    skip_reason: Optional[str] = None
    why_tag: Optional[str] = None
    order_type: Optional[str] = None
    fill_cost_cents: Optional[int] = None
    fill_ts: Optional[float] = None
    slippage_cents: Optional[int] = None
    fee_cents: Optional[int] = None
    contracts: int = 1
    dollars_at_risk: Optional[float] = None
    post_fill_drift_cents: Optional[int] = None
    resolution: Optional[str] = None
    pnl_net: Optional[float] = None
    settled_ts: Optional[float] = None
    env: str = "live-observed"
    spot_price: Optional[float] = None
    boundary_lo: Optional[float] = None
    boundary_hi: Optional[float] = None
    distance: Optional[float] = None
    distance_pct: Optional[float] = None
    lane: Optional[str] = None
    session_tag: Optional[str] = None
    vol_regime: Optional[str] = None
    winner_clip_cents: Optional[float] = None


def insert_row(row: SurfaceRow) -> int:
    """Insert a surface row. Returns the row id."""
    d = asdict(row)
    cols = list(d.keys())
    vals = [d[c] for c in cols]
    placeholders = ",".join(["?"] * len(cols))
    col_str = ",".join(cols)

    with _lock:
        cur = _conn.execute(
            f"INSERT INTO surface ({col_str}) VALUES ({placeholders})",
            vals,
        )
        _conn.commit()
        row_id = cur.lastrowid
    log.info(f"[STORE] {row.action} {row.market_ticker} side={row.side} "
             f"cost={row.cost_per_contract_cents}¢ env={row.env} (id={row_id})")
    return row_id


def update_fill(row_id: int, fill_cost_cents: int, fill_ts: float,
                slippage_cents: int, fee_cents: int) -> None:
    """Update a row after fill confirmation."""
    with _lock:
        _conn.execute(
            "UPDATE surface SET fill_cost_cents=?, fill_ts=?, slippage_cents=?, fee_cents=? WHERE id=?",
            (fill_cost_cents, fill_ts, slippage_cents, fee_cents, row_id),
        )
        _conn.commit()


def update_drift(row_id: int, drift_cents: int) -> None:
    """Update post-fill drift (mark 10s after fill)."""
    with _lock:
        _conn.execute(
            "UPDATE surface SET post_fill_drift_cents=? WHERE id=?",
            (drift_cents, row_id),
        )
        _conn.commit()


def update_settlement(row_id: int, resolution: str, pnl_net: float,
                      settled_ts: float) -> None:
    """Update a row with settlement result."""
    with _lock:
        _conn.execute(
            "UPDATE surface SET resolution=?, pnl_net=?, settled_ts=? WHERE id=?",
            (resolution, pnl_net, settled_ts, row_id),
        )
        _conn.commit()


def query_band_stats(cost_band_lo: int, cost_band_hi: int,
                     env: str = "live-traded", lane: str = "main") -> dict:
    """Query win stats for a cost band using COUNT(DISTINCT market_ticker).
    Excludes obs_stale, obs_dup, and side=NULL rows."""
    with _lock:
        row = _conn.execute(
            """SELECT
                COUNT(DISTINCT CASE WHEN resolution IN ('win','obs_win') THEN market_ticker END),
                COUNT(DISTINCT CASE WHEN resolution IN ('loss','obs_loss') THEN market_ticker END),
                SUM(CASE WHEN resolution IN ('win','loss') THEN COALESCE(pnl_net,0) ELSE 0 END)
               FROM surface
               WHERE env=? AND action='ENTER'
               AND CAST(cost_per_contract_cents AS INTEGER) >= ?
               AND CAST(cost_per_contract_cents AS INTEGER) <= ?
               AND resolution IN ('win','loss','obs_win','obs_loss')
               AND side IS NOT NULL
               AND COALESCE(lane,'main')=?""",
            (env, cost_band_lo, cost_band_hi, lane),
        ).fetchone()
    wins = row[0] if row else 0
    losses = row[1] if row else 0
    n = wins + losses
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "win_pct": wins / n if n > 0 else 0.0,
        "net_pnl": float(row[2] or 0) if row else 0.0,
    }


def unresolved_tickers() -> list:
    """Return distinct tickers with at least one unresolved row."""
    with _lock:
        rows = _conn.execute(
            "SELECT DISTINCT market_ticker FROM surface WHERE resolution IS NULL"
        ).fetchall()
    return [r[0] for r in rows]


def get_unresolved_rows(ticker: str) -> list:
    """Return unresolved rows for a ticker: (id, action, side, cost_cents, fee_cents)."""
    with _lock:
        rows = _conn.execute(
            """SELECT id, action, side, cost_per_contract_cents,
                      COALESCE(fee_cents, 0)
               FROM surface WHERE market_ticker=? AND resolution IS NULL""",
            (ticker,),
        ).fetchall()
    return rows


def approx_close_ts(ticker: str) -> float:
    """Approximate close timestamp for a ticker from its most recent surface row."""
    with _lock:
        row = _conn.execute(
            """SELECT decision_ts, seconds_to_expiry FROM surface
               WHERE market_ticker=? AND seconds_to_expiry IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
    if row and row[0] and row[1]:
        return row[0] + row[1]
    return 0.0


def query_band_stats_combined(cost_band_lo: int, cost_band_hi: int) -> dict:
    """Combined stats from live-traded AND live-observed rows.
    Uses COUNT(DISTINCT market_ticker). Excludes obs_stale, obs_dup, side=NULL."""
    traded = query_band_stats(cost_band_lo, cost_band_hi, "live-traded")
    with _lock:
        row = _conn.execute(
            """SELECT
                COUNT(DISTINCT CASE WHEN resolution='obs_win' THEN market_ticker END),
                COUNT(DISTINCT CASE WHEN resolution='obs_loss' THEN market_ticker END)
               FROM surface
               WHERE env='live-observed'
               AND CAST(cost_per_contract_cents AS INTEGER) >= ?
               AND CAST(cost_per_contract_cents AS INTEGER) <= ?
               AND resolution IN ('obs_win', 'obs_loss')
               AND side IS NOT NULL
               AND COALESCE(lane,'main')='main'""",
            (cost_band_lo, cost_band_hi),
        ).fetchone()
    obs_wins = row[0] if row else 0
    obs_losses = row[1] if row else 0
    obs_n = obs_wins + obs_losses
    return {
        "traded": traded,
        "obs_n": obs_n,
        "obs_wins": obs_wins,
        "obs_losses": obs_losses,
        "obs_win_pct": obs_wins / obs_n if obs_n > 0 else 0.0,
        "combined_n": traded["n"] + obs_n,
        "combined_wins": traded["wins"] + obs_wins,
    }


def dedup_historical_skips() -> int:
    """Mark pre-fix duplicate SKIP rows as obs_dup (append-only — not deleted).
    For each (market_ticker, cost_per_contract_cents), keep the LAST row,
    set all earlier duplicates' resolution to obs_dup."""
    with _lock:
        dupes_resolved = _conn.execute(
            """SELECT market_ticker, cost_per_contract_cents, MAX(id) as keep_id
               FROM surface
               WHERE env='live-observed' AND action='SKIP'
               AND resolution IN ('obs_win', 'obs_loss')
               GROUP BY market_ticker, cost_per_contract_cents
               HAVING COUNT(*) > 1"""
        ).fetchall()

        updated = 0
        for ticker, cost, keep_id in dupes_resolved:
            cur = _conn.execute(
                """UPDATE surface SET resolution='obs_dup'
                   WHERE market_ticker=? AND cost_per_contract_cents=?
                   AND env='live-observed' AND action='SKIP'
                   AND resolution IN ('obs_win', 'obs_loss')
                   AND id != ?""",
                (ticker, cost, keep_id),
            )
            updated += cur.rowcount

        dupes_null = _conn.execute(
            """SELECT market_ticker, cost_per_contract_cents, MAX(id) as keep_id
               FROM surface
               WHERE env='live-observed' AND action='SKIP'
               AND resolution IS NULL
               GROUP BY market_ticker, cost_per_contract_cents
               HAVING COUNT(*) > 1"""
        ).fetchall()

        for ticker, cost, keep_id in dupes_null:
            cur = _conn.execute(
                """UPDATE surface SET resolution='obs_dup'
                   WHERE market_ticker=? AND cost_per_contract_cents=?
                   AND env='live-observed' AND action='SKIP'
                   AND resolution IS NULL
                   AND id != ?""",
                (ticker, cost, keep_id),
            )
            updated += cur.rowcount

        if updated:
            _conn.commit()
            log.info(f"[STORE] Dedup: marked {updated} historical duplicate rows as obs_dup")
    return updated


def query_drift_stats() -> dict:
    """S4: Drift and time-bucket stats for the weekly review."""
    with _lock:
        rows = _conn.execute(
            """SELECT resolution, post_fill_drift_cents, seconds_to_expiry
               FROM surface
               WHERE env='live-traded' AND action='ENTER'
               AND resolution IN ('win', 'loss')
               AND post_fill_drift_cents IS NOT NULL"""
        ).fetchall()
    win_drifts = [r[1] for r in rows if r[0] == "win"]
    loss_drifts = [r[1] for r in rows if r[0] == "loss"]

    buckets = {"180-120": 0, "120-60": 0, "60-10": 0}
    for r in rows:
        if r[0] != "loss":
            continue
        tte = r[2] or 0
        if tte >= 120:
            buckets["180-120"] += 1
        elif tte >= 60:
            buckets["120-60"] += 1
        else:
            buckets["60-10"] += 1

    return {
        "win_mean_drift": sum(win_drifts) / len(win_drifts) if win_drifts else 0.0,
        "loss_mean_drift": sum(loss_drifts) / len(loss_drifts) if loss_drifts else 0.0,
        "win_count": len(win_drifts),
        "loss_count": len(loss_drifts),
        "loss_buckets": buckets,
    }


def query_band_friction(cost_band_lo: int, cost_band_hi: int,
                        env: str = "live-traded") -> Optional[float]:
    """Compute measured friction for a cost band from filled rows.
    friction = (sum_fees + sum_abs_slippage) / sum_cost.
    Returns None if no fills exist for the band."""
    with _lock:
        row = _conn.execute(
            """SELECT SUM(COALESCE(fee_cents,0)),
                      SUM(ABS(COALESCE(slippage_cents,0))),
                      SUM(COALESCE(cost_per_contract_cents,0))
               FROM surface
               WHERE env=? AND action='ENTER'
               AND CAST(cost_per_contract_cents AS INTEGER) >= ?
               AND CAST(cost_per_contract_cents AS INTEGER) <= ?
               AND fill_cost_cents IS NOT NULL""",
            (env, cost_band_lo, cost_band_hi),
        ).fetchone()
    if row is None or row[2] is None or row[2] == 0:
        return None
    total_fees = row[0] or 0
    total_slip = row[1] or 0
    total_cost = row[2]
    return (total_fees + total_slip) / total_cost


def get_state(key: str) -> Optional[str]:
    """Read a value from the state table."""
    with _lock:
        row = _conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_state(key: str, value: str) -> None:
    """Write a value to the state table (upsert)."""
    with _lock:
        _conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=?",
            (key, value, value),
        )
        _conn.commit()


def daily_stats(env: str = "live-traded") -> dict:
    """Today's stats."""
    import datetime as dt
    today_start = dt.datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    with _lock:
        rows = _conn.execute(
            """SELECT resolution, pnl_net, fee_cents FROM surface
               WHERE env=? AND action='ENTER' AND decision_ts >= ?
               AND resolution IS NOT NULL""",
            (env, today_start),
        ).fetchall()
    wins = sum(1 for r in rows if r[0] == "win")
    losses = sum(1 for r in rows if r[0] == "loss")
    n = wins + losses
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "win_pct": wins / n if n > 0 else 0.0,
        "net_pnl": sum(r[1] or 0 for r in rows),
        "total_fees": sum((r[2] or 0) for r in rows) / 100.0,
    }


# ── Winner clip + market ledger (Fix 5) ────────────────────────

def update_winner_clip(row_id: int, clip_cents: float) -> None:
    with _lock:
        _conn.execute(
            "UPDATE surface SET winner_clip_cents=? WHERE id=?",
            (clip_cents, row_id),
        )
        _conn.commit()


def upsert_market_ledger(ticker: str, settled_ts: float, result: str,
                         realized_cents: float, best_available_cents: float,
                         regret_missed: float, regret_avoided: float,
                         outcome_class: str) -> None:
    with _lock:
        _conn.execute(
            """INSERT INTO market_ledger
               (market_ticker, settled_ts, result, realized_cents,
                best_available_cents, regret_missed, regret_avoided, outcome_class)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(market_ticker) DO UPDATE SET
                settled_ts=?, result=?, realized_cents=?,
                best_available_cents=?, regret_missed=?, regret_avoided=?,
                outcome_class=?""",
            (ticker, settled_ts, result, realized_cents, best_available_cents,
             regret_missed, regret_avoided, outcome_class,
             settled_ts, result, realized_cents, best_available_cents,
             regret_missed, regret_avoided, outcome_class),
        )
        _conn.commit()


def query_caution_ledger() -> dict:
    """Daily caution ledger from market_ledger."""
    import datetime as dt
    today_start = dt.datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    with _lock:
        rows = _conn.execute(
            """SELECT outcome_class, realized_cents, best_available_cents,
                      regret_missed, regret_avoided
               FROM market_ledger WHERE settled_ts >= ?""",
            (today_start,),
        ).fetchall()
    captured = sum(r[1] for r in rows if r[0] == "CAPTURED") / 100.0
    missed = sum(r[3] for r in rows if r[0] == "MISSED_CLIP") / 100.0
    savings = sum(r[4] for r in rows if r[0] == "DODGED_LOSS") / 100.0
    took_loss = sum(abs(r[1]) for r in rows if r[0] == "TOOK_LOSS") / 100.0
    return {
        "captured": captured,
        "cost_of_caution": missed,
        "caution_savings": savings,
        "took_loss": took_loss,
        "net_caution": savings - missed,
        "n": len(rows),
    }


def check_conflicting_resolutions() -> list:
    """Find tickers with conflicting resolutions in the same cell. Returns list of ticker strings."""
    with _lock:
        rows = _conn.execute(
            """SELECT market_ticker, CAST(cost_per_contract_cents AS INTEGER) as band
               FROM surface
               WHERE resolution IN ('win','loss','obs_win','obs_loss')
               AND side IS NOT NULL
               GROUP BY market_ticker, band
               HAVING COUNT(DISTINCT CASE WHEN resolution IN ('win','obs_win') THEN 1
                                          WHEN resolution IN ('loss','obs_loss') THEN 2 END) > 1"""
        ).fetchall()
    return [f"{r[0]}@{r[1]}c" for r in rows]


# ── H8 stats (Fix 4) ───────────────────────────────────────────

def query_h8_grid() -> list:
    """H8 grid: cells (cost_bucket x distance_bucket x time_band)."""
    COST_BUCKETS = [(65, 79), (80, 89), (90, 94)]
    DIST_BUCKETS = [("close", 0, 0.0005), ("mid", 0.0005, 0.0015), ("far", 0.0015, 1.0)]
    TIME_BUCKETS = [(180, 120), (120, 60), (60, 10)]
    cells = []
    with _lock:
        for clo, chi in COST_BUCKETS:
            for dlabel, dlo, dhi in DIST_BUCKETS:
                for thi, tlo in TIME_BUCKETS:
                    row = _conn.execute(
                        """SELECT
                            COUNT(DISTINCT CASE WHEN resolution IN ('win','obs_win') THEN market_ticker END),
                            COUNT(DISTINCT CASE WHEN resolution IN ('loss','obs_loss') THEN market_ticker END)
                           FROM surface
                           WHERE CAST(cost_per_contract_cents AS INTEGER) >= ?
                           AND CAST(cost_per_contract_cents AS INTEGER) <= ?
                           AND distance_pct >= ? AND distance_pct < ?
                           AND seconds_to_expiry >= ? AND seconds_to_expiry < ?
                           AND resolution IN ('win','loss','obs_win','obs_loss')
                           AND side IS NOT NULL
                           AND COALESCE(lane,'main') IN ('main','h8_probe')""",
                        (clo, chi, dlo, dhi, tlo, thi),
                    ).fetchone()
                    w = row[0] if row else 0
                    l = row[1] if row else 0
                    n = w + l
                    if n > 0:
                        cells.append({
                            "cost": f"{clo}-{chi}", "dist": dlabel,
                            "time": f"{thi}-{tlo}", "n": n, "wins": w,
                            "win_pct": w / n, "be": clo / 100.0,
                        })
    return cells


def query_context_stats() -> dict:
    """Win% and margin per session_tag and vol_regime."""
    result = {"sessions": {}, "vol_regimes": {}}
    with _lock:
        for tag_col, dest in [("session_tag", "sessions"), ("vol_regime", "vol_regimes")]:
            rows = _conn.execute(
                f"""SELECT {tag_col},
                    COUNT(DISTINCT CASE WHEN resolution IN ('win','obs_win') THEN market_ticker END),
                    COUNT(DISTINCT CASE WHEN resolution IN ('loss','obs_loss') THEN market_ticker END),
                    SUM(CASE WHEN resolution IN ('win','loss') THEN COALESCE(pnl_net,0) ELSE 0 END)
                   FROM surface
                   WHERE {tag_col} IS NOT NULL
                   AND resolution IN ('win','loss','obs_win','obs_loss')
                   AND side IS NOT NULL
                   GROUP BY {tag_col}"""
            ).fetchall()
            for tag, w, l, pnl in rows:
                n = w + l
                result[dest][tag] = {
                    "n": n, "wins": w, "losses": l,
                    "win_pct": w / n if n > 0 else 0.0,
                    "net_pnl": float(pnl or 0),
                }
    return result


def query_h8_probe_daily() -> dict:
    """Today's H8 probe stats for budget/kill checks."""
    import datetime as dt
    today_start = dt.datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    with _lock:
        rows = _conn.execute(
            """SELECT resolution, cost_per_contract_cents FROM surface
               WHERE lane='h8_probe' AND decision_ts >= ?
               AND action='ENTER'""",
            (today_start,),
        ).fetchall()
    at_risk = sum(r[1] or 0 for r in rows) / 100.0
    wins = sum(1 for r in rows if r[0] == "win")
    losses = sum(1 for r in rows if r[0] == "loss")
    return {"at_risk": at_risk, "wins": wins, "losses": losses, "n": len(rows)}


def update_order_id(row_id: int, order_id: str) -> None:
    """Store the exchange order_id on a surface row after placement."""
    with _lock:
        _conn.execute(
            "UPDATE surface SET order_id=? WHERE id=?",
            (order_id, row_id),
        )
        _conn.commit()


def get_nofill_enter_rows(limit: int = 200) -> list:
    """Return ENTER rows with resolution='no_fill' for reconciliation.
    Returns list of (id, market_ticker, order_id, cost_per_contract_cents, fee_cents)."""
    with _lock:
        rows = _conn.execute(
            """SELECT id, market_ticker, order_id, cost_per_contract_cents,
                      COALESCE(fee_cents, 0)
               FROM surface
               WHERE action='ENTER' AND resolution='no_fill'
               AND order_id IS NOT NULL
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return rows


def clear_resolution(row_id: int) -> None:
    """Clear resolution so a row can be re-settled (used by reconciler)."""
    with _lock:
        _conn.execute(
            "UPDATE surface SET resolution=NULL, pnl_net=NULL, settled_ts=NULL WHERE id=?",
            (row_id,),
        )
        _conn.commit()


def count_fills_today(env: str = "live-traded") -> int:
    """Count ENTER rows that have a fill (fill_cost_cents IS NOT NULL) today."""
    import datetime as dt
    today_start = dt.datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    with _lock:
        row = _conn.execute(
            """SELECT COUNT(*) FROM surface
               WHERE env=? AND action='ENTER' AND decision_ts >= ?
               AND fill_cost_cents IS NOT NULL""",
            (env, today_start),
        ).fetchone()
    return row[0] if row else 0


def update_reprice(row_id: int, new_cost: float, new_order_id: str,
                   reprice_count: int) -> None:
    """Update surface row after a reprice — new cost basis, new order_id, reprice count."""
    with _lock:
        _conn.execute(
            """UPDATE surface SET cost_per_contract_cents=?, order_id=?,
               reprice_count=? WHERE id=?""",
            (new_cost, new_order_id, reprice_count, row_id),
        )
        _conn.commit()


def update_why_tag(row_id: int, why_tag: str, skip_reason: str = None) -> None:
    """Update why_tag (and optionally skip_reason) on a surface row."""
    with _lock:
        if skip_reason is not None:
            _conn.execute(
                "UPDATE surface SET why_tag=?, skip_reason=? WHERE id=?",
                (why_tag, skip_reason, row_id),
            )
        else:
            _conn.execute(
                "UPDATE surface SET why_tag=? WHERE id=?",
                (why_tag, row_id),
            )
        _conn.commit()


def query_clip_by_time_band() -> list:
    """Win rate + avg clip (pnl) per T_BAND for the scoreboard."""
    T_BANDS = [(180, 120), (120, 60), (60, 10)]
    results = []
    with _lock:
        for hi, lo in T_BANDS:
            row = _conn.execute(
                """SELECT
                    COUNT(DISTINCT CASE WHEN resolution IN ('win','obs_win') THEN market_ticker END),
                    COUNT(DISTINCT CASE WHEN resolution IN ('loss','obs_loss') THEN market_ticker END),
                    AVG(CASE WHEN resolution IN ('win','loss') THEN pnl_net END)
                   FROM surface
                   WHERE seconds_to_expiry >= ? AND seconds_to_expiry < ?
                   AND resolution IN ('win','loss','obs_win','obs_loss')
                   AND side IS NOT NULL
                   AND COALESCE(lane,'main')='main'""",
                (lo, hi),
            ).fetchone()
            wins = row[0] if row else 0
            losses = row[1] if row else 0
            n = wins + losses
            avg_clip = row[2] if row else 0
            results.append({
                "band": f"T-{hi}-{lo}",
                "n": n, "wins": wins, "losses": losses,
                "win_pct": wins / n if n > 0 else 0,
                "avg_clip": float(avg_clip or 0),
            })
    return results


def query_median_depth() -> Optional[int]:
    """Median book_depth_at_touch for ENTER rows (capacity gauge)."""
    with _lock:
        rows = _conn.execute(
            """SELECT book_depth_at_touch FROM surface
               WHERE action='ENTER' AND book_depth_at_touch IS NOT NULL
               ORDER BY book_depth_at_touch"""
        ).fetchall()
    if not rows:
        return None
    depths = [r[0] for r in rows]
    mid = len(depths) // 2
    if len(depths) % 2 == 0:
        return (depths[mid - 1] + depths[mid]) // 2
    return depths[mid]


def get_covered_tickers_today() -> set:
    """Return set of KXBTC15M tickers with at least one surface row today."""
    import datetime as dt
    today_start = dt.datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    with _lock:
        rows = _conn.execute(
            """SELECT DISTINCT market_ticker FROM surface
               WHERE market_ticker LIKE 'KXBTC15M%' AND decision_ts >= ?""",
            (today_start,),
        ).fetchall()
    return {r[0] for r in rows}


def query_h8_probe_lifetime() -> dict:
    """Lifetime H8 probe stats for probe kill check."""
    with _lock:
        row = _conn.execute(
            """SELECT
                COUNT(CASE WHEN resolution='win' THEN 1 END),
                COUNT(CASE WHEN resolution='loss' THEN 1 END)
               FROM surface WHERE lane='h8_probe' AND action='ENTER'
               AND resolution IN ('win','loss')"""
        ).fetchone()
    wins = row[0] if row else 0
    losses = row[1] if row else 0
    return {"wins": wins, "losses": losses}
