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
    _conn.execute("PRAGMA busy_timeout=15000")
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
    # WO-LANE-FLIP-3 §3: append-only, one row per flip window at DONE. The desk's own
    # crossing study — Saturday's tuning of FLIP_X and the line reads from these columns.
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS flip_windows (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              REAL NOT NULL,
            close_ts        INTEGER,
            tag             TEXT,
            ticker          TEXT,
            entry_yes       INTEGER,
            entry_no        INTEGER,
            join_yes        INTEGER,
            join_no         INTEGER,
            post_dt_yes     REAL,
            post_dt_no      REAL,
            booksum_yes     INTEGER,
            booksum_no      INTEGER,
            bundle_cost     INTEGER,
            exit_yes        INTEGER,
            exit_no         INTEGER,
            capture_a_cents INTEGER,
            capture_b_cents INTEGER,
            realized_cents  INTEGER,
            mtm_open_cents  INTEGER,
            spread_yes      INTEGER,
            spread_no       INTEGER,
            sigma_at_gate   REAL,
            ttff_s          REAL,
            ttflat_s        REAL,
            outcome_tag     TEXT,
            broker_flat     INTEGER
        )
    """)
    _conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_flip_windows_ts ON flip_windows(ts)
    """)
    # WO-VISION — the markout study (per fill) and the trip ledger (per managed position).
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS flip_markouts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fill_ts     REAL NOT NULL,
            ticker      TEXT,
            side        TEXT,
            entry_cents INTEGER,
            m10         INTEGER,
            m30         INTEGER,
            m60         INTEGER
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS flip_trips (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             REAL NOT NULL,
            ticker         TEXT,
            tag            TEXT,
            side           TEXT,
            entry_cents    INTEGER,
            outcome        TEXT,
            realized_cents INTEGER
        )
    """)
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_flip_trips_ts ON flip_trips(ts)")
    # WO-MORNING §1: a live DB created before WO-4/WO-6 lacks the newer flip_windows columns
    # (the CREATE above only fires on a fresh table). Guarded-ALTER them so window INSERTs
    # with every column succeed on legacy schemas. ALTER is idempotent via the except.
    for col, typ in [
        ("join_yes", "INTEGER"), ("join_no", "INTEGER"),
        ("post_dt_yes", "REAL"), ("post_dt_no", "REAL"),
        ("booksum_yes", "INTEGER"), ("booksum_no", "INTEGER"),
    ]:
        try:
            _conn.execute(f"ALTER TABLE flip_windows ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS epochs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL NOT NULL,
            old_book   REAL,
            new_book   REAL,
            delta      REAL,
            reason     TEXT,
            boot_id    TEXT
        )
    """)
    for col, typ in [
        ("spot_price", "REAL"), ("boundary_lo", "REAL"), ("boundary_hi", "REAL"),
        ("distance", "REAL"), ("distance_pct", "REAL"), ("lane", "TEXT"),
        ("session_tag", "TEXT"), ("vol_regime", "TEXT"), ("winner_clip_cents", "REAL"),
        ("order_id", "TEXT"),
        ("reprice_count", "INTEGER"),
        ("queue_pos_entry", "INTEGER"),
        ("queue_pos_t60", "INTEGER"),
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
    lane: str = "F"
    session_tag: Optional[str] = None
    vol_regime: Optional[str] = None
    winner_clip_cents: Optional[float] = None
    order_id: Optional[str] = None       # WO-5 §1: order-id lineage is the tagging doctrine


def series_allowed(ticker: Optional[str], allowlist) -> bool:
    """WO-5 §2: True if the ticker's series (the segment before the first '-') is in the
    allowlist. Keeps personal positions in the shared account out of the bot's books."""
    if not ticker:
        return False
    series = str(ticker).split("-", 1)[0].strip().upper()
    return series in {str(s).strip().upper() for s in allowlist}


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
                     env: str = "live-traded", lane: str = "F") -> dict:
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
               AND COALESCE(lane,'F')=?""",
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
    """Return distinct tickers with at least one unresolved row.
    P1 (0708): 'timeout' rows are filled positions whose settlement lagged the
    5-min engine poll — they MUST re-enter the sweep or they become ghost
    trades (no waterfall, no Wilson N). Terminal-label completeness law."""
    with _lock:
        rows = _conn.execute(
            "SELECT DISTINCT market_ticker FROM surface "
            "WHERE resolution IS NULL OR resolution='timeout'"
        ).fetchall()
    return [r[0] for r in rows]


def get_unresolved_rows(ticker: str) -> list:
    """Return unresolved rows: (id, action, side, cost_cents, fee_cents, contracts,
    fill_cost_cents, order_id).
    Includes resolution='timeout' (P1 0708) — filled, settlement lagged, never
    resolved. pnl was stamped 0.0 so re-settling waterfalls exactly once."""
    with _lock:
        rows = _conn.execute(
            """SELECT id, action, side, cost_per_contract_cents,
                      COALESCE(fee_cents, 0), COALESCE(contracts, 1),
                      fill_cost_cents, order_id
               FROM surface WHERE market_ticker=?
               AND (resolution IS NULL OR resolution='timeout')""",
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
               AND COALESCE(lane,'F')='F'""",
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


def et_midnight_ts() -> float:
    """Today's midnight in America/New_York as a Unix timestamp."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    return now_et.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def daily_stats(env: str = "live-traded") -> dict:
    """Today's stats."""
    today_start = et_midnight_ts()
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
    today_start = et_midnight_ts()
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
    """Find tickers with conflicting resolutions in the same cell+side.
    Side-flips (same ticker, different sides) are expected, not corruption."""
    with _lock:
        rows = _conn.execute(
            """SELECT market_ticker, CAST(cost_per_contract_cents AS INTEGER) as band, side
               FROM surface
               WHERE resolution IN ('win','loss','obs_win','obs_loss')
               AND side IS NOT NULL
               GROUP BY market_ticker, band, side
               HAVING COUNT(DISTINCT CASE WHEN resolution IN ('win','obs_win') THEN 1
                                          WHEN resolution IN ('loss','obs_loss') THEN 2 END) > 1"""
        ).fetchall()
    return [f"{r[0]}@{r[1]}c/{r[2]}" for r in rows]


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
                           AND COALESCE(lane,'F') IN ('F','H8')""",
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
    """Today's H8 probe stats for budget/kill checks.
    P5 (0708): confirmed zero-fills (no_fill / cancelled_external /
    order_rejected) release their budget — a $2/day budget was burning
    half a day's probes on trades that never existed. blind_standdown
    stays counted (could be filled — safest-price doctrine); if the
    reconciler later upgrades a released row to filled, its resolution
    changes and it counts again."""
    today_start = et_midnight_ts()
    with _lock:
        rows = _conn.execute(
            """SELECT resolution, cost_per_contract_cents FROM surface
               WHERE lane='H8' AND decision_ts >= ?
               AND action='ENTER'""",
            (today_start,),
        ).fetchall()
    _ZERO_FILL = ("no_fill", "cancelled_external", "order_rejected")
    at_risk = sum((r[1] or 0) for r in rows if r[0] not in _ZERO_FILL) / 100.0
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
    """Legacy alias — use get_unconfirmed_enter_rows."""
    return get_unconfirmed_enter_rows(limit)


def get_unconfirmed_enter_rows(limit: int = 200) -> list:
    """Return ENTER rows whose resolution was assigned without a broker-confirmed fill.
    Covers no_fill, cancelled_external, blind_standdown — the reconciler checks
    broker fills for all of these since cancels can race fills.
    Returns list of (id, market_ticker, order_id, cost_per_contract_cents, fee_cents, side)."""
    with _lock:
        rows = _conn.execute(
            """SELECT id, market_ticker, order_id, cost_per_contract_cents,
                      COALESCE(fee_cents, 0), side
               FROM surface
               WHERE action='ENTER'
               AND resolution IN ('no_fill', 'cancelled_external', 'blind_standdown')
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
    today_start = et_midnight_ts()
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


def update_queue_pos(row_id: int, field: str, value: int) -> None:
    """P9 (0708): stamp queue position on a surface row.
    field must be 'queue_pos_entry' or 'queue_pos_t60'."""
    if field not in ("queue_pos_entry", "queue_pos_t60"):
        raise ValueError(f"bad queue field {field}")
    with _lock:
        _conn.execute(f"UPDATE surface SET {field}=? WHERE id=?", (value, row_id))
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


def update_session_vol(row_id: int, session_tag: str, vol_regime: str) -> None:
    """Update session_tag and vol_regime on a surface row."""
    with _lock:
        _conn.execute(
            "UPDATE surface SET session_tag=?, vol_regime=? WHERE id=?",
            (session_tag, vol_regime, row_id),
        )
        _conn.commit()


T_BANDS = [(900, 600), (600, 300), (300, 180), (180, 120), (120, 60), (60, 10)]


def query_clip_by_time_band() -> list:
    """Win rate + avg clip per T_BAND. Separates live-traded from observed;
    clip comes from traded rows only (same population as traded_n)."""
    today_start = et_midnight_ts()
    results = []
    with _lock:
        for hi, lo in T_BANDS:
            traded = _conn.execute(
                """SELECT
                    COUNT(CASE WHEN resolution='win' THEN 1 END),
                    COUNT(CASE WHEN resolution='loss' THEN 1 END),
                    AVG(pnl_net)
                   FROM surface
                   WHERE env='live-traded' AND action='ENTER'
                   AND seconds_to_expiry >= ? AND seconds_to_expiry < ?
                   AND resolution IN ('win','loss')
                   AND side IS NOT NULL
                   AND COALESCE(lane,'F')='F'""",
                (lo, hi),
            ).fetchone()
            t_wins = traded[0] if traded else 0
            t_losses = traded[1] if traded else 0
            t_clip = traded[2] if traded else 0

            obs = _conn.execute(
                """SELECT
                    COUNT(DISTINCT CASE WHEN resolution='obs_win' THEN market_ticker END),
                    COUNT(DISTINCT CASE WHEN resolution='obs_loss' THEN market_ticker END)
                   FROM surface
                   WHERE env='live-observed'
                   AND seconds_to_expiry >= ? AND seconds_to_expiry < ?
                   AND resolution IN ('obs_win','obs_loss')
                   AND side IS NOT NULL
                   AND COALESCE(lane,'F')='F'""",
                (lo, hi),
            ).fetchone()
            obs_n = (obs[0] or 0) + (obs[1] or 0) if obs else 0

            today_traded = _conn.execute(
                """SELECT
                    COUNT(CASE WHEN resolution='win' THEN 1 END),
                    COUNT(CASE WHEN resolution='loss' THEN 1 END),
                    AVG(pnl_net)
                   FROM surface
                   WHERE env='live-traded' AND action='ENTER'
                   AND seconds_to_expiry >= ? AND seconds_to_expiry < ?
                   AND resolution IN ('win','loss')
                   AND side IS NOT NULL
                   AND COALESCE(lane,'F')='F'
                   AND decision_ts >= ?""",
                (lo, hi, today_start),
            ).fetchone()
            td_wins = today_traded[0] if today_traded else 0
            td_losses = today_traded[1] if today_traded else 0
            td_clip = today_traded[2] if today_traded else 0

            today_obs = _conn.execute(
                """SELECT
                    COUNT(DISTINCT CASE WHEN resolution='obs_win' THEN market_ticker END),
                    COUNT(DISTINCT CASE WHEN resolution='obs_loss' THEN market_ticker END)
                   FROM surface
                   WHERE env='live-observed'
                   AND seconds_to_expiry >= ? AND seconds_to_expiry < ?
                   AND resolution IN ('obs_win','obs_loss')
                   AND side IS NOT NULL
                   AND COALESCE(lane,'F')='F'
                   AND decision_ts >= ?""",
                (lo, hi, today_start),
            ).fetchone()
            today_obs_n = (today_obs[0] or 0) + (today_obs[1] or 0) if today_obs else 0

            traded_n = t_wins + t_losses
            today_n = td_wins + td_losses
            results.append({
                "band": f"T-{hi}-{lo}",
                "traded_n": traded_n,
                "traded_wins": t_wins,
                "traded_losses": t_losses,
                "traded_win_pct": t_wins / traded_n if traded_n > 0 else 0,
                "traded_clip": float(t_clip or 0),
                "obs_n": obs_n,
                "today_n": today_n,
                "today_wins": td_wins,
                "today_clip": float(td_clip or 0),
                "today_obs_n": today_obs_n,
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
    """Return set of KXBTC15M tickers seen live today.
    Excludes MISSED_UNSEEN rows (census markers don't count as coverage)."""
    today_start = et_midnight_ts()
    with _lock:
        rows = _conn.execute(
            """SELECT DISTINCT market_ticker FROM surface
               WHERE market_ticker LIKE 'KXBTC15M%' AND decision_ts >= ?
               AND COALESCE(why_tag, '') != 'MISSED_UNSEEN'""",
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
               FROM surface WHERE lane='H8' AND action='ENTER'
               AND resolution IN ('win','loss')"""
        ).fetchone()
    wins = row[0] if row else 0
    losses = row[1] if row else 0
    return {"wins": wins, "losses": losses}


# ── Boot reconcile helpers (Part A) ───────────────────────────

def count_rows_total() -> int:
    """Total surface rows (used to detect fresh/wiped store)."""
    with _lock:
        row = _conn.execute("SELECT COUNT(*) FROM surface").fetchone()
    return row[0] if row else 0


def has_row_for_ticker(ticker: str) -> bool:
    """Check if any surface row exists for a ticker."""
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM surface WHERE market_ticker=? LIMIT 1",
            (ticker,),
        ).fetchone()
    return row is not None


def insert_orphan_row(ticker: str, position_data: dict) -> int:
    """Insert a surface row for an orphan position found at boot."""
    side = None
    for k in ("yes_position", "position", "net_position"):
        v = position_data.get(k)
        if v is not None:
            try:
                qty = int(v)
                if qty > 0:
                    side = "yes"
                elif qty < 0:
                    side = "no"
                break
            except (ValueError, TypeError):
                pass
    if side is None and position_data.get("no_position"):
        try:
            if int(position_data["no_position"]) > 0:
                side = "no"
        except (ValueError, TypeError):
            pass
    return insert_row(SurfaceRow(
        market_ticker=ticker,
        decision_ts=time.time(),
        action="ENTER",
        side=side,
        why_tag="ORPHAN_POSITION_BOOT",
        env="live-traded",
    ))


_FLIP_WINDOW_COLS = (
    "ts", "close_ts", "tag", "ticker", "entry_yes", "entry_no", "join_yes", "join_no",
    "post_dt_yes", "post_dt_no", "booksum_yes", "booksum_no", "bundle_cost", "exit_yes",
    "exit_no", "capture_a_cents", "capture_b_cents", "realized_cents", "mtm_open_cents",
    "spread_yes", "spread_no", "sigma_at_gate", "ttff_s", "ttflat_s", "outcome_tag",
    "broker_flat",
)


def insert_flip_window(**fields) -> int:
    """WO-LANE-FLIP-3 §3: append one immutable row per flip window at DONE."""
    cols = [c for c in _FLIP_WINDOW_COLS if c in fields]
    vals = [fields[c] for c in cols]
    placeholders = ",".join(["?"] * len(cols))
    with _lock:
        cur = _conn.execute(
            f"INSERT INTO flip_windows ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        _conn.commit()
        row_id = cur.lastrowid
    log.info(f"[STORE] flip_window {fields.get('tag')} {fields.get('outcome_tag')} "
             f"realized={fields.get('realized_cents')}¢ flat={fields.get('broker_flat')} "
             f"(id={row_id})")
    return row_id


def flip_windows_between(lo_ts: float, hi_ts: float) -> list:
    """Return flip_windows rows with lo_ts <= ts < hi_ts, oldest first, as dicts."""
    with _lock:
        cur = _conn.execute(
            f"""SELECT id,{','.join(_FLIP_WINDOW_COLS)} FROM flip_windows
                WHERE ts >= ? AND ts < ? ORDER BY ts ASC""",
            (lo_ts, hi_ts),
        )
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def insert_markout(fill_ts: float, ticker: str, side: str, entry_cents: int,
                   m10=None, m30=None, m60=None) -> int:
    """WO-VISION §1: the markout curve for one entry fill (mark at +10/+30/+60s)."""
    with _lock:
        cur = _conn.execute(
            """INSERT INTO flip_markouts (fill_ts, ticker, side, entry_cents, m10, m30, m60)
               VALUES (?,?,?,?,?,?,?)""",
            (fill_ts, ticker, side, entry_cents, m10, m30, m60),
        )
        _conn.commit()
        return cur.lastrowid


def insert_trip(ts: float, ticker: str, tag: str, side: str, entry_cents: int,
                outcome: str, realized_cents: int) -> int:
    """WO-VISION §5: one managed position (trip) — the row R6 scaling stats read from."""
    with _lock:
        cur = _conn.execute(
            """INSERT INTO flip_trips (ts, ticker, tag, side, entry_cents, outcome, realized_cents)
               VALUES (?,?,?,?,?,?,?)""",
            (ts, ticker, tag, side, entry_cents, outcome, realized_cents),
        )
        _conn.commit()
    log.info(f"[STORE] flip_trip {tag} {side} entry={entry_cents} {outcome} "
             f"realized={realized_cents}¢")
    return cur.lastrowid


def flip_trips_between(lo_ts: float, hi_ts: float) -> list:
    """Trip rows in [lo_ts, hi_ts), oldest first, as dicts (for the R6 pack line)."""
    cols = ("id", "ts", "ticker", "tag", "side", "entry_cents", "outcome", "realized_cents")
    with _lock:
        cur = _conn.execute(
            f"SELECT {','.join(cols)} FROM flip_trips WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
            (lo_ts, hi_ts),
        )
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def flip_outcome_for_ticker(ticker: str) -> Optional[str]:
    """WO-MORNING §2: the most-recent flip_windows outcome_tag for a ticker (or None)."""
    with _lock:
        row = _conn.execute(
            "SELECT outcome_tag FROM flip_windows WHERE ticker=? ORDER BY id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    return row[0] if row else None


def window_pnl_cents(ticker: str) -> float:
    """WO-MORNING §2: window NET settled P&L in cents — the sum across a ticker's resolved
    ENTER legs. Netted bundles net positive; a declined window nets ≈ 0. This is what the
    tail-loss kill counts, so netted/declined windows don't read as per-leg losses."""
    with _lock:
        row = _conn.execute(
            """SELECT COALESCE(SUM(pnl_net), 0) FROM surface
               WHERE market_ticker=? AND action='ENTER' AND resolution IN ('win','loss')""",
            (ticker,),
        ).fetchone()
    return float(row[0] or 0.0) * 100.0


def lookup_lane(ticker: str) -> Optional[str]:
    """Return the lane from the most recent ENTER row for this ticker, or None."""
    with _lock:
        row = _conn.execute(
            """SELECT lane FROM surface
               WHERE market_ticker=? AND action='ENTER'
               ORDER BY id DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
    return row[0] if row else None


def record_epoch(old_book: float, new_book: float, delta: float,
                 reason: str) -> None:
    """Record a treasury epoch transition."""
    boot_id = f"boot-{int(time.time())}"
    with _lock:
        _conn.execute(
            """INSERT INTO epochs (ts, old_book, new_book, delta, reason, boot_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (time.time(), old_book, new_book, delta, reason, boot_id),
        )
        _conn.commit()


def recompute_missing_pnl() -> int:
    """One-time boot fix: recompute pnl_net for resolved ENTER rows where it's 0 or NULL.
    Derives pnl from fill_cost (or cost_per_contract), fee, and resolution."""
    with _lock:
        cur = _conn.execute("""
            UPDATE surface SET pnl_net = CASE
                WHEN resolution = 'win' THEN
                    ((100.0 - COALESCE(fill_cost_cents, cost_per_contract_cents, 0))
                       * COALESCE(contracts, 1)
                     - COALESCE(fee_cents, 0)) / 100.0
                WHEN resolution = 'loss' THEN
                    -(COALESCE(fill_cost_cents, cost_per_contract_cents, 0)
                        * COALESCE(contracts, 1)
                      + COALESCE(fee_cents, 0)) / 100.0
            END
            WHERE resolution IN ('win', 'loss')
            AND action = 'ENTER'
            AND (pnl_net IS NULL OR pnl_net = 0)
        """)
        updated = cur.rowcount
        if updated:
            _conn.commit()
            log.warning(f"[STORE] Recomputed pnl_net for {updated} resolved rows")
    return updated


def migrate_lanes() -> None:
    """Migrate lane column: 'main'->'F', 'h8_probe'->'H8', NULL->'F'."""
    with _lock:
        c1 = _conn.execute(
            "UPDATE surface SET lane='F' WHERE lane IS NULL OR lane='main'"
        ).rowcount
        c2 = _conn.execute(
            "UPDATE surface SET lane='H8' WHERE lane IN ('h8_probe', 'h8')"
        ).rowcount
        if c1 + c2 > 0:
            _conn.commit()
            log.info(f"[STORE] Lane migration: {c1} rows → F, {c2} rows → H8")


def query_lane_losses_recent(lane: str, window_sec: int = 3600) -> int:
    """Count losses for a specific lane in the recent time window."""
    cutoff = time.time() - window_sec
    with _lock:
        row = _conn.execute(
            """SELECT COUNT(*) FROM surface
               WHERE lane=? AND action='ENTER' AND resolution='loss'
               AND settled_ts IS NOT NULL AND settled_ts >= ?""",
            (lane, cutoff),
        ).fetchone()
    return row[0] if row else 0
