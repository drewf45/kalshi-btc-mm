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
                     env: str = "live-traded") -> dict:
    """Query win stats for a cost band. Returns {n, wins, losses, win_pct}."""
    with _lock:
        rows = _conn.execute(
            """SELECT resolution, pnl_net FROM surface
               WHERE env=? AND action='ENTER'
               AND cost_per_contract_cents >= ? AND cost_per_contract_cents <= ?
               AND resolution IS NOT NULL""",
            (env, cost_band_lo, cost_band_hi),
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
    }


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
