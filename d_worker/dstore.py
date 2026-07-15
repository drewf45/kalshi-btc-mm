"""KAL-D database — kal_d.db. Append-only writers, queries, state table."""

import os
import json
import time
import sqlite3
import logging
import threading
from typing import Optional, List, Dict
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("d_worker.dstore")

DB_PATH = os.environ.get("DW_DB_PATH", "/var/data/kal_d.db")
NY = ZoneInfo("America/New_York")

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def init_db() -> None:
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=15000")
    _conn.executescript(_SCHEMA)
    _conn.commit()
    log.info(f"[DSTORE] Initialized {DB_PATH}")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts REAL NOT NULL,
    finished_ts REAL,
    markets_seen INTEGER DEFAULT 0,
    seeds INTEGER DEFAULT 0,
    skips INTEGER DEFAULT 0,
    errs INTEGER DEFAULT 0,
    req_count INTEGER DEFAULT 0,
    req_budget INTEGER DEFAULT 0,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER,
    ts REAL NOT NULL,
    market_ticker TEXT NOT NULL,
    series_ticker TEXT,
    close_ts REAL,
    dclass TEXT,
    verdict TEXT NOT NULL,
    side TEXT,
    evidence_json TEXT,
    fee_mult_maker REAL,
    fee_mult_taker REAL
);
CREATE TABLE IF NOT EXISTS series_registry (
    series_ticker TEXT PRIMARY KEY,
    drafted_ts REAL,
    approved_ts REAL,
    approved_by TEXT,
    settle_source TEXT,
    station_or_ref TEXT,
    tz TEXT,
    units TEXT,
    rounding TEXT,
    rules_url TEXT,
    version INTEGER DEFAULT 1,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS fee_registry (
    series_ticker TEXT PRIMARY KEY,
    maker_mult REAL DEFAULT 1.0,
    taker_mult REAL DEFAULT 1.0,
    position_limit INTEGER,
    min_tick INTEGER DEFAULT 1,
    fetched_ts REAL,
    source TEXT
);
CREATE TABLE IF NOT EXISTS shadow_seeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER,
    ts REAL NOT NULL,
    market_ticker TEXT NOT NULL,
    dclass TEXT,
    side TEXT,
    book_json TEXT,
    taker_price_cents INTEGER,
    taker_viable INTEGER,
    maker_price_cents INTEGER,
    sizes_json TEXT,
    close_ts REAL,
    settled_ts REAL,
    result TEXT,
    classifier_correct INTEGER,
    maker_filled_est INTEGER,
    net_clip_taker_cents REAL,
    net_clip_maker_cents REAL,
    lockup_capital_days REAL
);
CREATE TABLE IF NOT EXISTS class_stats_daily (
    date TEXT,
    dclass TEXT,
    n_settled INTEGER DEFAULT 0,
    n_correct INTEGER DEFAULT 0,
    wilson_lb_net_taker REAL,
    wilson_lb_net_maker REAL,
    fills_maker_est INTEGER DEFAULT 0,
    fills_taker_viable INTEGER DEFAULT 0,
    PRIMARY KEY (date, dclass)
);
CREATE TABLE IF NOT EXISTS packs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hour_key TEXT NOT NULL,
    ts REAL NOT NULL,
    rendered_text TEXT NOT NULL,
    stats_json TEXT
);
CREATE TABLE IF NOT EXISTS verdict_skip_agg (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    series_ticker TEXT NOT NULL,
    verdict TEXT NOT NULL,
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dstate (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdicts_ticker_ts ON verdicts (market_ticker, ts);
CREATE INDEX IF NOT EXISTS idx_skip_agg_cycle ON verdict_skip_agg (cycle_id);
"""


# ── State helpers ────────────────────────────────────────────────

def get_state(key: str) -> Optional[str]:
    with _lock:
        row = _conn.execute("SELECT value FROM dstate WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_state(key: str, value: str) -> None:
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO dstate (key, value) VALUES (?, ?)",
                      (key, value))
        _conn.commit()


# ── Scan cycles ──────────────────────────────────────────────────

def start_cycle(req_budget: int) -> int:
    with _lock:
        cur = _conn.execute(
            "INSERT INTO scan_cycles (started_ts, req_budget) VALUES (?, ?)",
            (time.time(), req_budget))
        _conn.commit()
        return cur.lastrowid


def finish_cycle(cycle_id: int, markets_seen: int, seeds: int,
                 skips: int, errs: int, req_count: int, notes: str = "") -> None:
    with _lock:
        _conn.execute(
            """UPDATE scan_cycles SET finished_ts=?, markets_seen=?, seeds=?,
               skips=?, errs=?, req_count=?, notes=? WHERE id=?""",
            (time.time(), markets_seen, seeds, skips, errs, req_count, notes, cycle_id))
        _conn.commit()


# ── Verdicts ─────────────────────────────────────────────────────

def insert_verdict(cycle_id: int, market_ticker: str, series_ticker: str,
                   close_ts: float, dclass: str, verdict: str, side: str = None,
                   evidence: dict = None, fee_maker: float = None,
                   fee_taker: float = None) -> int:
    with _lock:
        cur = _conn.execute(
            """INSERT INTO verdicts
               (cycle_id, ts, market_ticker, series_ticker, close_ts,
                dclass, verdict, side, evidence_json, fee_mult_maker, fee_mult_taker)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cycle_id, time.time(), market_ticker, series_ticker, close_ts,
             dclass, verdict, side,
             json.dumps(evidence) if evidence else None,
             fee_maker, fee_taker))
        _conn.commit()
        return cur.lastrowid


def insert_verdicts_batch(rows: List[tuple]) -> None:
    """Batch insert verdict rows. Each tuple matches the verdicts INSERT columns."""
    if not rows:
        return
    with _lock:
        _conn.executemany(
            """INSERT INTO verdicts
               (cycle_id, ts, market_ticker, series_ticker, close_ts,
                dclass, verdict, side, evidence_json, fee_mult_maker, fee_mult_taker)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows)
        _conn.commit()


def insert_skip_aggregates(cycle_id: int, skip_agg: dict) -> None:
    """Insert aggregated skip counts. skip_agg: {(series, verdict): count}."""
    if not skip_agg:
        return
    now = time.time()
    rows = [(cycle_id, now, series, verdict, count)
            for (series, verdict), count in skip_agg.items()]
    with _lock:
        _conn.executemany(
            """INSERT INTO verdict_skip_agg
               (cycle_id, ts, series_ticker, verdict, count)
               VALUES (?, ?, ?, ?, ?)""",
            rows)
        _conn.commit()


def count_verdicts_since(ts: float, verdict: str = None) -> int:
    with _lock:
        if verdict:
            row = _conn.execute(
                "SELECT COUNT(*) FROM verdicts WHERE ts >= ? AND verdict=?",
                (ts, verdict)).fetchone()
        else:
            row = _conn.execute(
                "SELECT COUNT(*) FROM verdicts WHERE ts >= ?", (ts,)).fetchone()
    return row[0] if row else 0


def verdict_counts_since(ts: float) -> Dict[str, int]:
    with _lock:
        rows = _conn.execute(
            "SELECT verdict, COUNT(*) FROM verdicts WHERE ts >= ? GROUP BY verdict",
            (ts,)).fetchall()
    return {r[0]: r[1] for r in rows}


def top_skip_reasons(ts: float, limit: int = 5) -> List[tuple]:
    with _lock:
        rows = _conn.execute(
            """SELECT verdict, SUM(cnt) as c FROM (
                 SELECT verdict, COUNT(*) as cnt FROM verdicts
                   WHERE ts >= ? AND verdict LIKE 'SKIP_%' GROUP BY verdict
                 UNION ALL
                 SELECT verdict, SUM(count) as cnt FROM verdict_skip_agg
                   WHERE ts >= ? AND verdict LIKE 'SKIP_%' GROUP BY verdict
               ) GROUP BY verdict ORDER BY c DESC LIMIT ?""",
            (ts, ts, limit)).fetchall()
    return rows


# ── Series registry ──────────────────────────────────────────────

def get_registry(series: str) -> Optional[dict]:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM series_registry WHERE series_ticker=?", (series,)).fetchone()
    if not row:
        return None
    cols = ["series_ticker", "drafted_ts", "approved_ts", "approved_by",
            "settle_source", "station_or_ref", "tz", "units", "rounding",
            "rules_url", "version", "notes"]
    return dict(zip(cols, row))


def draft_registry(series: str, settle_source: str, station: str,
                   tz: str, units: str, rounding: str, rules_url: str = "",
                   notes: str = "") -> None:
    with _lock:
        existing = _conn.execute(
            "SELECT version FROM series_registry WHERE series_ticker=?",
            (series,)).fetchone()
        version = (existing[0] + 1) if existing else 1
        _conn.execute(
            """INSERT OR REPLACE INTO series_registry
               (series_ticker, drafted_ts, approved_ts, approved_by,
                settle_source, station_or_ref, tz, units, rounding,
                rules_url, version, notes)
               VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (series, time.time(), settle_source, station, tz, units,
             rounding, rules_url, version, notes))
        _conn.commit()


def approve_registry(series: str, approved_by: str) -> bool:
    with _lock:
        row = _conn.execute(
            "SELECT drafted_ts FROM series_registry WHERE series_ticker=?",
            (series,)).fetchone()
        if not row:
            return False
        _conn.execute(
            "UPDATE series_registry SET approved_ts=?, approved_by=? WHERE series_ticker=?",
            (time.time(), approved_by, series))
        _conn.commit()
    return True


def list_registry(approved_only: bool = False) -> List[dict]:
    with _lock:
        if approved_only:
            rows = _conn.execute(
                "SELECT * FROM series_registry WHERE approved_ts IS NOT NULL"
            ).fetchall()
        else:
            rows = _conn.execute("SELECT * FROM series_registry").fetchall()
    cols = ["series_ticker", "drafted_ts", "approved_ts", "approved_by",
            "settle_source", "station_or_ref", "tz", "units", "rounding",
            "rules_url", "version", "notes"]
    return [dict(zip(cols, r)) for r in rows]


# ── Fee registry ─────────────────────────────────────────────────

def get_fee(series: str) -> Optional[dict]:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM fee_registry WHERE series_ticker=?", (series,)).fetchone()
    if not row:
        return None
    cols = ["series_ticker", "maker_mult", "taker_mult",
            "position_limit", "min_tick", "fetched_ts", "source"]
    return dict(zip(cols, row))


def upsert_fee(series: str, maker_mult: float, taker_mult: float,
               position_limit: int = None, min_tick: int = 1,
               source: str = "api") -> Optional[dict]:
    """Upsert fee registry. Returns old values if changed (for alert), else None."""
    old = get_fee(series)
    changed = None
    if old and (old["maker_mult"] != maker_mult or old["taker_mult"] != taker_mult):
        changed = old
    with _lock:
        _conn.execute(
            """INSERT OR REPLACE INTO fee_registry
               (series_ticker, maker_mult, taker_mult, position_limit, min_tick,
                fetched_ts, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (series, maker_mult, taker_mult, position_limit, min_tick,
             time.time(), source))
        _conn.commit()
    return changed


def list_fees() -> List[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM fee_registry").fetchall()
    cols = ["series_ticker", "maker_mult", "taker_mult",
            "position_limit", "min_tick", "fetched_ts", "source"]
    return [dict(zip(cols, r)) for r in rows]


# ── Shadow seeds ─────────────────────────────────────────────────

def insert_seed(verdict_id: int, market_ticker: str, dclass: str, side: str,
                book_json: str, taker_price: int, taker_viable: bool,
                maker_price: int, sizes_json: str, close_ts: float) -> int:
    with _lock:
        cur = _conn.execute(
            """INSERT INTO shadow_seeds
               (verdict_id, ts, market_ticker, dclass, side, book_json,
                taker_price_cents, taker_viable, maker_price_cents, sizes_json, close_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (verdict_id, time.time(), market_ticker, dclass, side, book_json,
             taker_price, 1 if taker_viable else 0, maker_price, sizes_json, close_ts))
        _conn.commit()
        return cur.lastrowid


def get_unsettled_seeds() -> List[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM shadow_seeds WHERE settled_ts IS NULL"
        ).fetchall()
    cols = ["id", "verdict_id", "ts", "market_ticker", "dclass", "side",
            "book_json", "taker_price_cents", "taker_viable", "maker_price_cents",
            "sizes_json", "close_ts", "settled_ts", "result",
            "classifier_correct", "maker_filled_est",
            "net_clip_taker_cents", "net_clip_maker_cents", "lockup_capital_days"]
    return [dict(zip(cols, r)) for r in rows]


def settle_seed(seed_id: int, result: str, classifier_correct: bool,
                maker_filled_est,
                net_clip_taker: float, net_clip_maker: float,
                lockup_days: float) -> None:
    maker_val = None if maker_filled_est is None else (1 if maker_filled_est else 0)
    with _lock:
        _conn.execute(
            """UPDATE shadow_seeds SET settled_ts=?, result=?,
               classifier_correct=?, maker_filled_est=?,
               net_clip_taker_cents=?, net_clip_maker_cents=?,
               lockup_capital_days=? WHERE id=?""",
            (time.time(), result,
             1 if classifier_correct else 0,
             maker_val,
             net_clip_taker, net_clip_maker, lockup_days, seed_id))
        _conn.commit()


def is_already_seeded(market_ticker: str) -> bool:
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM shadow_seeds WHERE market_ticker=?",
            (market_ticker,)).fetchone()
    return row is not None


def seed_stats_since(ts: float) -> dict:
    with _lock:
        new = _conn.execute(
            "SELECT COUNT(*) FROM shadow_seeds WHERE ts >= ?", (ts,)).fetchone()
        open_seeds = _conn.execute(
            "SELECT COUNT(*) FROM shadow_seeds WHERE settled_ts IS NULL"
        ).fetchone()
        settled = _conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN classifier_correct=1 THEN 1 ELSE 0 END) "
            "FROM shadow_seeds WHERE settled_ts IS NOT NULL"
        ).fetchone()
        today_settled = _conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN classifier_correct=1 THEN 1 ELSE 0 END) "
            "FROM shadow_seeds WHERE settled_ts >= ?", (ts,)).fetchone()
    return {
        "new": new[0],
        "open": open_seeds[0],
        "lifetime_settled": settled[0] or 0,
        "lifetime_correct": settled[1] or 0,
        "today_settled": today_settled[0] or 0,
        "today_correct": today_settled[1] or 0,
    }


def seed_fill_stats_since(ts: float) -> dict:
    with _lock:
        row = _conn.execute(
            """SELECT
                SUM(CASE WHEN taker_viable=1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN maker_filled_est=1 THEN 1 ELSE 0 END),
                COUNT(*)
               FROM shadow_seeds WHERE ts >= ?""",
            (ts,)).fetchone()
    return {
        "taker_viable": row[0] or 0,
        "maker_filled": row[1] or 0,
        "total": row[2] or 0,
    }


def sim_capital_used() -> float:
    """Sum of taker_price_cents × contracts for open (unsettled) seeds, in dollars."""
    with _lock:
        row = _conn.execute(
            """SELECT COALESCE(SUM(taker_price_cents), 0) FROM shadow_seeds
               WHERE settled_ts IS NULL"""
        ).fetchone()
    return (row[0] or 0) / 100.0


def invariant_break_seeds() -> List[dict]:
    """Seeds where classifier was wrong."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM shadow_seeds WHERE classifier_correct=0"
        ).fetchall()
    cols = ["id", "verdict_id", "ts", "market_ticker", "dclass", "side",
            "book_json", "taker_price_cents", "taker_viable", "maker_price_cents",
            "sizes_json", "close_ts", "settled_ts", "result",
            "classifier_correct", "maker_filled_est",
            "net_clip_taker_cents", "net_clip_maker_cents", "lockup_capital_days"]
    return [dict(zip(cols, r)) for r in rows]


# ── Stats rollup ─────────────────────────────────────────────────

def upsert_daily_stats(date: str, dclass: str, n_settled: int, n_correct: int,
                       wilson_taker: float, wilson_maker: float,
                       fills_maker: int, fills_taker_v: int) -> None:
    with _lock:
        _conn.execute(
            """INSERT OR REPLACE INTO class_stats_daily
               (date, dclass, n_settled, n_correct,
                wilson_lb_net_taker, wilson_lb_net_maker,
                fills_maker_est, fills_taker_viable)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, dclass, n_settled, n_correct,
             wilson_taker, wilson_maker, fills_maker, fills_taker_v))
        _conn.commit()


def get_daily_stats() -> List[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM class_stats_daily ORDER BY date DESC, dclass"
        ).fetchall()
    cols = ["date", "dclass", "n_settled", "n_correct",
            "wilson_lb_net_taker", "wilson_lb_net_maker",
            "fills_maker_est", "fills_taker_viable"]
    return [dict(zip(cols, r)) for r in rows]


# ── Cycle queries ────────────────────────────────────────────────

def latest_cycles(n: int = 5) -> List[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM scan_cycles ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    cols = ["id", "started_ts", "finished_ts", "markets_seen", "seeds",
            "skips", "errs", "req_count", "req_budget", "notes"]
    return [dict(zip(cols, r)) for r in rows]


def et_midnight_ts() -> float:
    now = datetime.now(NY)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


# ── Packs ───────────────────────────────────────────────────────

def insert_pack(hour_key: str, rendered_text: str, stats_json: str = "") -> int:
    with _lock:
        cur = _conn.execute(
            "INSERT INTO packs (hour_key, ts, rendered_text, stats_json) VALUES (?, ?, ?, ?)",
            (hour_key, time.time(), rendered_text, stats_json))
        _conn.commit()
        return cur.lastrowid


def latest_packs(n: int = 24) -> List[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM packs ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    cols = ["id", "hour_key", "ts", "rendered_text", "stats_json"]
    return [dict(zip(cols, r)) for r in rows]
