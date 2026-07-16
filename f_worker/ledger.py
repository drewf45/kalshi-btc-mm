# f_worker/ledger.py
# flipdesk.db — NEW file, APPEND-ONLY throughout (BUILD ORDER §2).
#
# Nothing here ever UPDATEs or DELETEs a row. State is derived by reading the LATEST
# append: current window state = latest window_events row; current halt = latest halt
# row; current rung = latest size_ladder row. Every transition carries evidence
# (prices, book, clock) so the tape can be reconstructed after the fact.
#
# The ledger is "ledger-truth". The broker is "broker-truth". The hourly pack prints
# them side by side; divergence is an alert (§5), never a silent correction.

import json
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# window P&L "kind" values
KIND_BUNDLE = "bundle"
KIND_LONE = "lone"
KIND_SAT_OUT = "sat_out"
KIND_FLOOR = "floor"
KIND_SALVAGE = "salvage"

# order "kind" values
OK_ENTRY_BID = "entry_bid"
OK_FLIP_ASK = "flip_ask"
OK_SALVAGE = "salvage"
OK_MARKET_OUT = "market_out"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    window_id     TEXT PRIMARY KEY,
    market_ticker TEXT NOT NULL,
    event_ticker  TEXT,
    open_ts       INTEGER,
    close_ts      INTEGER,
    rung          INTEGER,
    created_ts    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS window_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id  TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    evidence   TEXT,
    ts         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    side       TEXT,
    action     TEXT,
    price_cents INTEGER,
    count      INTEGER,
    order_id   TEXT,
    detail     TEXT,
    ts         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id  TEXT NOT NULL,
    side       TEXT,
    price_cents INTEGER,
    count      INTEGER,
    fee_cents  INTEGER DEFAULT 0,
    is_taker   INTEGER DEFAULT 0,
    ts         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS window_pnl (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    gross_cents INTEGER NOT NULL,
    fees_cents INTEGER NOT NULL,
    net_cents  INTEGER NOT NULL,
    stopped    INTEGER NOT NULL,
    rung       INTEGER,
    regime     TEXT,
    settled    INTEGER DEFAULT 0,
    detail     TEXT,
    ts         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS size_ladder (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    rung   INTEGER NOT NULL,
    lots   INTEGER NOT NULL,
    note   TEXT,
    ts     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS halt (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    flip_halt INTEGER NOT NULL,
    reason    TEXT,
    ts        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id     TEXT NOT NULL,
    result        TEXT,
    booked_net    INTEGER,
    broker_net    INTEGER,
    mismatch      INTEGER DEFAULT 0,
    detail        TEXT,
    ts            REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fee_fingerprint (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    detail      TEXT,
    ts          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_reasons (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    reason  TEXT NOT NULL,
    ts      REAL NOT NULL
);
"""

# Columns added after the first schema shipped. Applied idempotently at open so an
# existing flipdesk.db on the box forward-migrates instead of erroring.
_MIGRATIONS = [
    ("fills", "fee_cents", "INTEGER DEFAULT 0"),
    ("fills", "is_taker", "INTEGER DEFAULT 0"),
    ("window_pnl", "regime", "TEXT"),
    ("window_pnl", "settled", "INTEGER DEFAULT 0"),
]


class Ledger:
    def __init__(self, db_path: str = "flipdesk.db", clock: Optional[Callable[[], float]] = None):
        self.db_path = db_path
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()
            # Seed rung 1 / halt-clear once, if empty (these seeds are appends).
            if self._conn.execute("SELECT COUNT(*) c FROM size_ladder").fetchone()["c"] == 0:
                self._conn.execute(
                    "INSERT INTO size_ladder(rung, lots, note, ts) VALUES (1,1,'boot rung',?)",
                    (self._clock(),))
            if self._conn.execute("SELECT COUNT(*) c FROM halt").fetchone()["c"] == 0:
                self._conn.execute(
                    "INSERT INTO halt(flip_halt, reason, ts) VALUES (0,'boot',?)",
                    (self._clock(),))
            self._conn.commit()

    def _migrate(self) -> None:
        """Idempotent forward-migration for dbs created by an earlier schema."""
        for table, col, decl in _MIGRATIONS:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError:
                    pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- windows ----------------
    def record_window(self, window_id: str, market_ticker: str, event_ticker: Optional[str],
                      open_ts: Optional[int], close_ts: Optional[int], rung: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO windows(window_id, market_ticker, event_ticker, "
                "open_ts, close_ts, rung, created_ts) VALUES (?,?,?,?,?,?,?)",
                (window_id, market_ticker, event_ticker, open_ts, close_ts, rung, self._clock()))
            self._conn.commit()

    def record_transition(self, window_id: str, from_state: Optional[str], to_state: str,
                          evidence: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO window_events(window_id, from_state, to_state, evidence, ts) "
                "VALUES (?,?,?,?,?)",
                (window_id, from_state, to_state,
                 json.dumps(evidence or {}, default=str), self._clock()))
            self._conn.commit()

    def current_state(self, window_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT to_state FROM window_events WHERE window_id=? ORDER BY id DESC LIMIT 1",
                (window_id,)).fetchone()
            return row["to_state"] if row else None

    # ---------------- orders / fills ----------------
    def record_order(self, window_id: str, kind: str, side: Optional[str], action: Optional[str],
                     price_cents: Optional[int], count: Optional[int], order_id: Optional[str],
                     detail: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO orders(window_id, kind, side, action, price_cents, count, "
                "order_id, detail, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (window_id, kind, side, action, price_cents, count, order_id,
                 json.dumps(detail or {}, default=str), self._clock()))
            self._conn.commit()

    def record_fill(self, window_id: str, side: str, price_cents: int, count: int,
                    fee_cents: int = 0, is_taker: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO fills(window_id, side, price_cents, count, fee_cents, is_taker, ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (window_id, side, price_cents, count, int(fee_cents),
                 1 if is_taker else 0, self._clock()))
            self._conn.commit()

    # ---------------- window P&L ----------------
    def record_pnl(self, window_id: str, kind: str, gross_cents: int, fees_cents: int,
                   net_cents: int, stopped: bool, rung: int, regime: Optional[str] = None,
                   detail: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO window_pnl(window_id, kind, gross_cents, fees_cents, net_cents, "
                "stopped, rung, regime, settled, detail, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (window_id, kind, int(gross_cents), int(fees_cents), int(net_cents),
                 1 if stopped else 0, int(rung), regime, 0,
                 json.dumps(detail or {}, default=str), self._clock()))
            self._conn.commit()

    def mark_settled(self, window_id: str, result: str, booked_net: int, broker_net: int,
                     mismatch: bool, detail: Optional[Dict[str, Any]] = None) -> None:
        """Append a broker-truth settlement record (F1.2). Append-only: the model P&L row
        is left intact; this is the reconciliation beside it."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO settlements(window_id, result, booked_net, broker_net, mismatch, "
                "detail, ts) VALUES (?,?,?,?,?,?,?)",
                (window_id, result, int(booked_net), int(broker_net), 1 if mismatch else 0,
                 json.dumps(detail or {}, default=str), self._clock()))
            self._conn.commit()

    def record_fee_fingerprint(self, fingerprint: str, detail: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO fee_fingerprint(fingerprint, detail, ts) VALUES (?,?,?)",
                (fingerprint, json.dumps(detail or {}, default=str), self._clock()))
            self._conn.commit()

    def last_fee_fingerprint(self) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint FROM fee_fingerprint ORDER BY id DESC LIMIT 1").fetchone()
            return row["fingerprint"] if row else None

    # ---------------- stuck-gauge tripwire (F2.4) ----------------
    def record_gate_reason(self, reason: str) -> int:
        """Append a window's gate reason and return the trailing CONSECUTIVE count of the
        same reason. A gauge that repeats is a gauge that's stuck — a market can be
        boring, but a sensor reading that never changes is broken."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO gate_reasons(reason, ts) VALUES (?,?)", (reason, self._clock()))
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT reason FROM gate_reasons ORDER BY id DESC LIMIT 200").fetchall()
        streak = 0
        for r in rows:
            if r["reason"] == reason:
                streak += 1
            else:
                break
        return streak

    def realized_flip_stats(self, since_ts: float) -> Dict[str, Any]:
        """Lived flip performance over a trailing window (F1.5): entered windows and how
        they resolved, plus the both-legs-flip rate. Replaces the synthetic priors with
        what the tape actually did."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) c FROM window_pnl WHERE ts>=? GROUP BY kind",
                (since_ts,)).fetchall()
        counts = {r["kind"]: r["c"] for r in rows}
        entered = sum(v for k, v in counts.items() if k != "sat_out")
        bundle = counts.get("bundle", 0)
        flip_rate = round(bundle / entered, 3) if entered else 0.0
        return {"entered": entered, "bundle": bundle, "sat_out": counts.get("sat_out", 0),
                "flip_rate": flip_rate, "by_kind": counts}

    def day_net_cents(self, since_ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(net_cents),0) s FROM window_pnl WHERE ts>=?",
                (since_ts,)).fetchone()
            return int(row["s"])

    def state_counts(self, since_ts: float) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) c FROM window_pnl WHERE ts>=? GROUP BY kind",
                (since_ts,)).fetchall()
            return {r["kind"]: r["c"] for r in rows}

    # ---------------- two-stop halt (W7) ----------------
    def consecutive_stops(self) -> int:
        """Count trailing consecutive STOPPED windows, ignoring sat-outs (no risk taken).
        Resets to 0 the moment a non-stopped, risk-taking window appears."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, stopped FROM window_pnl ORDER BY id DESC LIMIT 50").fetchall()
        streak = 0
        for r in rows:
            if r["kind"] == KIND_SAT_OUT:
                continue  # a sat-out took no risk; it neither stops nor clears the streak
            if r["stopped"]:
                streak += 1
            else:
                break
        return streak

    # ---------------- halt state ----------------
    def set_halt(self, flip_halt: bool, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO halt(flip_halt, reason, ts) VALUES (?,?,?)",
                (1 if flip_halt else 0, reason, self._clock()))
            self._conn.commit()

    def halt_state(self) -> Tuple[bool, str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT flip_halt, reason FROM halt ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return False, ""
            return bool(row["flip_halt"]), row["reason"] or ""

    # ---------------- size ladder (§7) ----------------
    def set_rung(self, rung: int, lots: int, note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO size_ladder(rung, lots, note, ts) VALUES (?,?,?,?)",
                (int(rung), int(lots), note, self._clock()))
            self._conn.commit()

    def current_rung(self) -> Tuple[int, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT rung, lots FROM size_ladder ORDER BY id DESC LIMIT 1").fetchone()
            return (int(row["rung"]), int(row["lots"])) if row else (1, 1)

    def rung_window_history(self, rung: int) -> List[int]:
        """Net cents per window recorded at the given rung (for Wilson LB review)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT net_cents FROM window_pnl WHERE rung=? AND kind!=? ORDER BY id",
                (int(rung), KIND_SAT_OUT)).fetchall()
            return [int(r["net_cents"]) for r in rows]
