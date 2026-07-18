"""Ledger — born EPOCH 2 (§A4). Tradeable = live balance.

No prior-era accounting exists in this tree (§A4 — "never born"): book value is
exactly (confirmed cash movements) + (settlement P&L from the settlements ledger).
Honest lifetime comes from the settlements ledger alone; the EPOCH BOUNDARY is
this engine's birth — no prior-era rows exist to count or exclude.

Win/loss path symmetry: every settlement writes through record_settlement
identically whether pnl is positive or negative; the invariant and the cash
protocol treat a surplus and a deficit with the same machinery (a deficit halts
entries pending confirmation, a surplus confirms without halt — both re-baseline
through the same CASH_MOVEMENTS row).

Single-writer law (§A1): this ledger opens the engine's OWN database file.
"""

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from . import config
from .errors import FatalIntegrityError

SCHEMA = """
CREATE TABLE IF NOT EXISTS cash_movements (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    amount_cents INTEGER NOT NULL,
    kind TEXT NOT NULL,             -- BASELINE | CONFIRMED_DEPOSIT | CONFIRMED_WITHDRAWAL
    confirmed_by TEXT NOT NULL,     -- 'boot' | '/confirm_cash' | 'auto_positive'
    breakdown TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    market TEXT NOT NULL,
    lane TEXT NOT NULL,
    pnl_cents INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    market TEXT NOT NULL,
    lane TEXT NOT NULL,             -- attribution: opening lane owns the position (C.2)
    side TEXT NOT NULL,
    action TEXT NOT NULL,           -- ENTRY | EXIT | CUSTODIAN_EXIT
    price_cents INTEGER NOT NULL,   -- cost basis, canonical YES terms
    count INTEGER NOT NULL,
    size_tier TEXT NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0,
    fee_cents INTEGER NOT NULL DEFAULT 0  -- P13 §1: the true cost, per fill
);
CREATE TABLE IF NOT EXISTS surface_rows (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    lane TEXT NOT NULL,
    market TEXT NOT NULL,
    window_id TEXT NOT NULL,
    state TEXT NOT NULL,            -- PASS | PROPOSED | ENTERED | EXITED | CUSTODIED | SETTLED | ...
    terminal INTEGER NOT NULL,      -- 1 = terminal row (one per lane/market/window)
    transport TEXT NOT NULL DEFAULT 'WS',   -- WS | EXPLORATION (feed-parity law)
    detail TEXT NOT NULL DEFAULT '',
    concurrent_lanes TEXT NOT NULL DEFAULT ''  -- Scientist stamp (P3): lanes live on this market at write time
);
CREATE TABLE IF NOT EXISTS engine_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS boots (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL                -- P5 §4: boot-loop detection (reader: BOOT_LOOP alert)
);
CREATE TABLE IF NOT EXISTS window_outcomes (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    market TEXT NOT NULL UNIQUE,
    settled_yes INTEGER NOT NULL    -- P21 A3: the herd's screen, as we saw it
);
CREATE TABLE IF NOT EXISTS cell_outcomes (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    lane TEXT NOT NULL,             -- P22 §1.1: the cell's lane (OPEN/HUNT split from FLIP)
    price_cell INTEGER NOT NULL,    -- entry price bucket lower edge (5c buckets)
    won INTEGER NOT NULL,           -- net pnl (after fees) > 0
    pnl_cents INTEGER NOT NULL,     -- net of fees
    fees_cents INTEGER NOT NULL DEFAULT 0,
    market TEXT NOT NULL,
    kind TEXT NOT NULL,             -- trip | settle | backfill
    UNIQUE(market, lane, kind)      -- idempotent like record_outcome (§1.1)
);
CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    market TEXT NOT NULL,
    snapshot TEXT NOT NULL          -- reader: replay harness + shadow verdicts (§F streams-name-readers)
);
"""


@dataclass
class BootCaps:
    """%-of-book caps snapshotted at boot (C.2). Byte-identical percentages all day;
    only confirmed cash movements re-baseline them."""

    book_cents: int
    order_budget_cents: int


class _LockedConnection:
    """P9 §1a: ONE connection, ONE RLock — every execute/commit path serializes
    through the same lock, so the listener thread, the settle thread, and the
    main loop share the single-writer connection safely. The lock is re-entrant
    (nested calls inside a caller that already holds it are fine); fetches on
    the returned cursor ride sqlite3's serialized threading mode."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def execute(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql, rows):
        with self._lock:
            return self._conn.executemany(sql, rows)

    def executescript(self, sql):
        with self._lock:
            return self._conn.executescript(sql)

    def commit(self):
        with self._lock:
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()


class Ledger:
    def __init__(self, db_path: Optional[str] = None):
        # P9 §1a: cross-thread by design (supervised tasks run ledger calls via
        # asyncio.to_thread); safety comes from the single RLock, not sqlite's
        # same-thread check.
        self.lock = threading.RLock()
        self.db = _LockedConnection(
            sqlite3.connect(db_path or config.DB_PATH, check_same_thread=False),
            self.lock)
        self.db.executescript(SCHEMA)
        # P13 §1: fee per fill (the net line needs the true cost per trade).
        try:
            self.db.execute(
                "ALTER TABLE fills ADD COLUMN fee_cents INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already present
        self.db.commit()
        self.boot_caps: Optional[BootCaps] = None

    # ----- book value (EPOCH 2: cash movements + settlements, nothing else) -----
    def book_cents(self) -> int:
        cash = self.db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM cash_movements").fetchone()[0]
        pnl = self.db.execute("SELECT COALESCE(SUM(pnl_cents),0) FROM settlements").fetchone()[0]
        return int(cash) + int(pnl)

    def lifetime_pnl_cents(self) -> int:
        """Honest lifetime = settlements ledger, full stop."""
        return int(self.db.execute("SELECT COALESCE(SUM(pnl_cents),0) FROM settlements").fetchone()[0])

    # ----- boot -----
    def snapshot_caps_at_boot(self) -> BootCaps:
        book = self.book_cents()
        self.boot_caps = BootCaps(
            book_cents=book,
            order_budget_cents=int(book * config.PCT_OF_BOOK_CAP),
        )
        return self.boot_caps

    def baseline(self, venue_balance_cents: int, confirmed_by: str = "boot") -> None:
        """Set/adjust the cash baseline so book matches the venue. Only boot and the
        cash protocol (confirmation is consent) may call this."""
        delta = venue_balance_cents - self.book_cents()
        if delta != 0:
            self.db.execute(
                "INSERT INTO cash_movements (ts, amount_cents, kind, confirmed_by) VALUES (?,?,?,?)",
                (time.time(), delta, "BASELINE" if confirmed_by == "boot" else
                 ("CONFIRMED_DEPOSIT" if delta > 0 else "CONFIRMED_WITHDRAWAL"), confirmed_by),
            )
            self.db.commit()

    # ----- engine state (key/value) + boot-loop counter (P5 §4) -----
    def get_state(self, key: str):
        row = self.db.execute("SELECT value FROM engine_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO engine_state (key, value) VALUES (?,?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))
        self.db.commit()

    def record_boot(self, now=None) -> int:
        """Insert a boot row; return boots in the last hour (BOOT_LOOP input)."""
        now = time.time() if now is None else now
        self.db.execute("INSERT INTO boots (ts) VALUES (?)", (now,))
        self.db.commit()
        return int(self.db.execute("SELECT COUNT(*) FROM boots WHERE ts>=?",
                                   (now - 3600,)).fetchone()[0])

    # ----- writes -----
    def record_fill(self, market: str, lane: str, side: str, action: str,
                    price_cents: int, count: int, size_tier: str,
                    fee_cents: int = 0, cell_lane: str = None) -> int:
        cur = self.db.execute(
            "INSERT INTO fills (ts, market, lane, side, action, price_cents,"
            " count, size_tier, fee_cents) VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), market, lane, side, action, price_cents, count,
             size_tier, int(fee_cents)),
        )
        self.db.commit()
        # P22 §1.1(a): a non-ENTRY booking CLOSES a unit of risk — the
        # round-trip receipt (the ↔ line's data) persists as a cell outcome
        # at the single point every exit passes (sweep exits AND custodian
        # cuts both book here). cell_lane splits FLIP's intents (OPEN/HUNT);
        # the fills row keeps the attribution lane unchanged.
        if action != "ENTRY":
            row = self.db.execute(
                "SELECT price_cents FROM fills WHERE market=? AND lane=?"
                " AND action='ENTRY' ORDER BY id DESC LIMIT 1",
                (market, lane)).fetchone()
            if row is not None:
                rt = (price_cents - row[0]) * count
                net = rt - int(fee_cents)
                self.record_cell_outcome(
                    cell_lane or lane, row[0], won=net > 0, pnl_cents=net,
                    fees_cents=int(fee_cents), market=market, kind="trip")
        return cur.lastrowid

    def record_cell_outcome(self, lane: str, entry_price_cents: int,
                            won: bool, pnl_cents: int, fees_cents: int,
                            market: str, kind: str, now=None) -> None:
        """P22 §1: one row per CLOSED unit of risk, idempotent by
        (market, lane, kind) — a multi-trip window banks its FIRST trip and
        suppresses re-writes (the same key that makes live+custodian double
        booking and backfill replays safe)."""
        from . import scoring
        self.db.execute(
            "INSERT INTO cell_outcomes (ts, lane, price_cell, won, pnl_cents,"
            " fees_cents, market, kind) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(market, lane, kind) DO NOTHING",
            (time.time() if now is None else now, lane,
             scoring.price_cell(entry_price_cents), int(bool(won)),
             int(pnl_cents), int(fees_cents), market, kind))
        self.db.commit()

    def backfill_cell_outcomes(self) -> int:
        """P22 §1.2: replay existing fills+settlements into cell_outcomes on
        first boot — today's clips and round-trips are not lost history.
        One row per (market, lane), kind=backfill: a lane whose fills fully
        net out banks its round-trip math; a lane that HELD banks the
        settlement pnl (which already contains the whole window's story —
        never both, never double-counted). Guarded + idempotent."""
        if self.get_state("cell_backfill_done"):
            return 0
        wrote = 0
        pairs = self.db.execute(
            "SELECT DISTINCT market, lane FROM fills").fetchall()
        for market, lane in pairs:
            fills = self.db.execute(
                "SELECT side, action, price_cents, count FROM fills"
                " WHERE market=? AND lane=? ORDER BY id",
                (market, lane)).fetchall()
            entries = sum(c for _, a, _, c in fills if a == "ENTRY")
            exits = sum(c for _, a, _, c in fills if a != "ENTRY")
            entry_px = next((p for _, a, p, _ in reversed(fills)
                             if a == "ENTRY"), None)
            if entry_px is None:
                continue
            fees = int(self.db.execute(
                "SELECT COALESCE(SUM(fee_cents),0) FROM fills WHERE market=?"
                " AND lane=?", (market, lane)).fetchone()[0])
            if exits >= entries:
                # flat by round-trips: last exit vs last entry
                exit_px = next((p for _, a, p, _ in reversed(fills)
                                if a != "ENTRY"), None)
                if exit_px is None:
                    continue
                net = (exit_px - entry_px) - fees
            else:
                # held to settlement: the settlements table has the verdict
                row = self.db.execute(
                    "SELECT pnl_cents FROM settlements WHERE market=? AND"
                    " lane=? ORDER BY id DESC LIMIT 1",
                    (market, lane)).fetchone()
                if row is None:
                    continue  # still open — not a closed unit of risk yet
                net = int(row[0]) - fees
            self.record_cell_outcome(lane, entry_px, won=net > 0,
                                     pnl_cents=net, fees_cents=fees,
                                     market=market, kind="backfill")
            wrote += 1
        self.set_state("cell_backfill_done", "1")
        return wrote

    def record_settlement(self, market: str, lane: str, pnl_cents: int, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO settlements (ts, market, lane, pnl_cents, detail) VALUES (?,?,?,?,?)",
            (time.time(), market, lane, pnl_cents, detail),
        )
        self.db.execute("UPDATE fills SET settled=1 WHERE market=? AND lane=?", (market, lane))
        self.db.commit()

    def record_outcome(self, market: str, settled_yes: bool,
                       now=None) -> None:
        """P21 A3: bank the window's outcome for the grain (idempotent)."""
        self.db.execute(
            "INSERT INTO window_outcomes (ts, market, settled_yes)"
            " VALUES (?,?,?) ON CONFLICT(market) DO NOTHING",
            (time.time() if now is None else now, market, int(settled_yes)))
        self.db.commit()

    def unsettled_fill_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM fills WHERE settled=0").fetchone()[0])

    # ----- invariant (C.2): book == balance ± in-flight -----
    def check_invariant(self, venue_balance_cents: int, in_flight_cents: int) -> None:
        book = self.book_cents()
        lo, hi = venue_balance_cents - in_flight_cents, venue_balance_cents + in_flight_cents
        if not (lo <= book <= hi):
            from . import failures
            failures.fail("LEDGER_INVARIANT",
                          f"book={book}c not within venue balance "
                          f"{venue_balance_cents}c ± in-flight {in_flight_cents}c",
                          fatal=True, book=book, venue=venue_balance_cents,
                          in_flight=in_flight_cents)

    # ----- drawdown rail (C.2): absolute floor until book > $50 -----
    def drawdown_floor_cents(self) -> int:
        book = self.book_cents()
        if book <= int(config.DRAWDOWN_FLOOR_UNTIL_BOOK_USD * 100):
            return int(config.DRAWDOWN_ABSOLUTE_FLOOR_USD * 100)
        # Above the threshold the rail may move to percentage semantics; that number
        # is a Chunk 5+ concern — keep the absolute floor as the conservative rail.
        return int(config.DRAWDOWN_ABSOLUTE_FLOOR_USD * 100)

    def drawdown_breached(self) -> bool:
        return self.book_cents() < self.drawdown_floor_cents()


# ---------------------------------------------------------------------------
# Cash-movement protocol (C.2 / BUILD_SEQUENCE 1.2)
# ---------------------------------------------------------------------------

@dataclass
class CashPrompt:
    ts: float
    delta_cents: int
    breakdown: str
    deadline: float


class CashProtocol:
    """Reconciles venue balance against the book under a quiescence window.

    negative delta -> halt entries + Telegram prompt WITH BREAKDOWN;
    /confirm_cash -> re-baseline + CASH_MOVEMENTS row + surface row; resume.
    /deny_cash or 30-min silence -> FATAL, stay stopped.
    positive delta -> confirm without halt (row still written).
    """

    def __init__(self, ledger: Ledger, alert_fn=None):
        self.ledger = ledger
        self.alert = alert_fn or (lambda msg: None)
        self.entries_halted = False
        self.fatal = False
        self.pending: Optional[CashPrompt] = None
        self.last_reconcile_ts: float = 0.0

    def reconcile(self, venue_balance_cents: int, in_flight_orders: int,
                  unsettled_fills: int, now: Optional[float] = None,
                  fills_since_cents: int = 0, fees_since_cents: int = 0) -> str:
        """Returns one of: DEFERRED | CLEAN | PROMPTED | CONFIRMED_POSITIVE | FATAL."""
        now = time.time() if now is None else now
        if self.fatal:
            return "FATAL"
        if self.pending is not None:
            if now >= self.pending.deadline:
                self._go_fatal("cash prompt unanswered for 30 minutes")
                return "FATAL"
            return "PROMPTED"
        # Quiescence window: compute delta only with zero in-flight orders and
        # zero unsettled fills; else defer a cycle.
        if in_flight_orders != 0 or unsettled_fills != 0:
            return "DEFERRED"
        delta = venue_balance_cents - self.ledger.book_cents()
        self.last_reconcile_ts = now
        if delta == 0:
            return "CLEAN"
        breakdown = (
            f"last_reconcile={self.last_reconcile_ts} fills_since={fills_since_cents}c "
            f"fees_since={fees_since_cents}c delta={delta}c"
        )
        if delta < 0:
            self.entries_halted = True
            self.pending = CashPrompt(
                ts=now, delta_cents=delta, breakdown=breakdown,
                deadline=now + config.CASH_DENY_TIMEOUT_SECONDS,
            )
            self.alert(
                f"CASH DELTA NEGATIVE {delta}c — entries HALTED. {breakdown}\n"
                f"Reply /confirm_cash to re-baseline or /deny_cash to stop."
            )
            return "PROMPTED"
        # positive: confirm without halt, row still written
        self.ledger.db.execute(
            "INSERT INTO cash_movements (ts, amount_cents, kind, confirmed_by, breakdown)"
            " VALUES (?,?,?,?,?)",
            (now, delta, "CONFIRMED_DEPOSIT", "auto_positive", breakdown),
        )
        self.ledger.db.commit()
        self.alert(f"CASH DELTA POSITIVE +{delta}c — confirmed without halt. {breakdown}")
        self._rescale_after_movement()
        return "CONFIRMED_POSITIVE"

    def _rescale_after_movement(self) -> None:
        """P16 §3: a CONFIRMED movement re-baselines the caps (C.2) and pages
        the NEW book's sizing — deposit day speaks its own new numbers."""
        self.ledger.snapshot_caps_at_boot()
        try:
            from .boot import sizing_line
            self.alert(sizing_line(self.ledger.book_cents()))
        except Exception:
            pass  # the page is a courtesy; the rescale is the law

    def confirm_cash(self, now: Optional[float] = None) -> bool:
        """/confirm_cash — confirmation is consent: re-baseline + row; entries resume."""
        now = time.time() if now is None else now
        if self.fatal or self.pending is None:
            return False
        p = self.pending
        self.ledger.db.execute(
            "INSERT INTO cash_movements (ts, amount_cents, kind, confirmed_by, breakdown)"
            " VALUES (?,?,?,?,?)",
            (now, p.delta_cents, "CONFIRMED_WITHDRAWAL" if p.delta_cents < 0 else "CONFIRMED_DEPOSIT",
             "/confirm_cash", p.breakdown),
        )
        self.ledger.db.commit()
        self.pending = None
        self.entries_halted = False
        self.alert(f"CASH MOVEMENT CONFIRMED {p.delta_cents}c — re-baselined, entries resumed.")
        self._rescale_after_movement()
        return True

    def deny_cash(self) -> None:
        """/deny_cash — the delta is NOT Drew's doing: integrity violation, FATAL."""
        if self.pending is not None:
            self._go_fatal(f"cash delta {self.pending.delta_cents}c DENIED by Drew")

    def _go_fatal(self, reason: str) -> None:
        self.fatal = True
        self.entries_halted = True
        self.pending = None
        self.alert(f"FATAL: {reason}. Engine stays stopped; manual restart required.")

    def monthly_true_up_line(self, venue_statement_cents: int) -> str:
        """The daily-pack monthly true-up line vs the Kalshi statement (verification)."""
        book = self.ledger.book_cents()
        return (f"TRUE-UP: book={book}c vs statement={venue_statement_cents}c "
                f"diff={book - venue_statement_cents}c")
