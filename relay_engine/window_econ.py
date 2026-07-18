"""Window economics — WO-P8 §1/§2: account truth around every traded market,
and the two-strike leash in Drew's hand.

THE BRACKET: at the first order submit on a market, snapshot live account
value (venue balance + position value; paper book in shadow) — that is
window_open_value. After settlement is confirmed and fills are booked,
snapshot again — window_close_value. window_pnl = close − open, adjusted for
confirmed cash movements timestamped inside the bracket. BROKER TRUTH, not
our arithmetic, is the verdict: the fills-based P&L is computed alongside and
any divergence > 2c writes WINDOW_ECON_DIVERGENCE with both numbers — that
divergence is exactly the class of lie the legacy era died of; now it pages
the phone the moment it exists.

THE TWO-STRIKE HALT: consecutive-negative streak over TRADED markets only,
account-level (all lanes share one account). streak==2 → entries halted
engine-wide (TWO_STRIKE_HALT); open positions and resting exits stay
custodied to conclusion — the halt stops NEW risk, never the management of
existing risk. The halt PERSISTS in the DB across boots and redeploys.
Resume is Drew's word alone: /reset_halt from the configured chat. It
re-enables ENTRIES only; it cannot place, amend, or cancel anything
(single-gateway law).

Win/loss path symmetry: every traded market writes a bracket, green or red;
the streak reads the sign of broker truth, nothing else.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, Optional

from . import config, failures

log = logging.getLogger("relay.window_econ")

DIVERGENCE_TOLERANCE_CENTS = 2
HALT_KEY = "two_strike_halt"
STREAK_KEY = "two_strike_streak"
STRIKES_KEY = "two_strike_markets"
HALT_REASON = "TWO_STRIKE_HALT"

ECON_SCHEMA = """
CREATE TABLE IF NOT EXISTS window_econ (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    market TEXT NOT NULL UNIQUE,
    open_value_cents INTEGER NOT NULL,
    close_value_cents INTEGER,
    cash_moves_cents INTEGER NOT NULL DEFAULT 0,
    window_pnl_cents INTEGER,
    fills_pnl_cents INTEGER,
    lanes_active TEXT NOT NULL DEFAULT '',
    fills_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'paper',   -- P9 §2: venue|paper — live rejects paper
    deferred TEXT NOT NULL DEFAULT ''       -- P9 §2: deferral stamps (open+Ns / close+Ns)
);
"""

# P9 §2: columns added after the table shipped — migrate in place.
ECON_MIGRATIONS = (
    ("source", "TEXT NOT NULL DEFAULT 'paper'"),
    ("deferred", "TEXT NOT NULL DEFAULT ''"),
)


@dataclass
class Bracket:
    market: str
    open_value_cents: int
    open_ts: float


class WindowEcon:
    def __init__(self, ledger, gateway, surface, telegram):
        self.ledger = ledger
        self.gateway = gateway
        self.surface = surface
        self.telegram = telegram
        self.ledger.db.executescript(ECON_SCHEMA)
        for col, ddl in ECON_MIGRATIONS:
            try:
                self.ledger.db.execute(
                    f"ALTER TABLE window_econ ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass  # column already present
        self.ledger.db.commit()
        self.open_brackets: Dict[str, Bracket] = {}
        # P9 §2: brackets whose account-value read failed in live — they DEFER,
        # they never consume a paper number. market -> first-deferred ts / close args.
        self.pending_opens: Dict[str, float] = {}
        self.pending_closes: Dict[str, dict] = {}

    # ── persistence-aware halt state ───────────────────────────────────
    @property
    def streak(self) -> int:
        return int(self.ledger.get_state(STREAK_KEY) or 0)

    def _set_streak(self, n: int) -> None:
        self.ledger.set_state(STREAK_KEY, str(n))

    def halted(self) -> bool:
        return self.ledger.get_state(HALT_KEY) == "1"

    def restore_halt_on_boot(self) -> bool:
        """P8 §2.3: restarts and redeploys do NOT clear the halt."""
        if self.halted():
            self.gateway.halt_entries(HALT_REASON)
            log.warning("TWO-STRIKE HALT restored from DB — /reset_halt is the only key")
            return True
        return False

    # ── §1: the bracket ────────────────────────────────────────────────
    def _reject_paper_in_live(self, market: str, source: str) -> None:
        """P9 §2: a bracket/streak/halt decision may only consume venue-sourced
        values in live — a paper number offered in live mode is the exact lie
        this engine exists to forbid."""
        if config.live_submit_enabled() and source != "venue":
            failures.fail("BRACKET_PAPER_IN_LIVE",
                          f"{market}: bracket offered source={source!r} in LIVE — "
                          f"real numbers only; a paper value never substitutes",
                          fatal=True, market=market, source=source)

    def open_bracket(self, market: str, account_value_cents: Optional[int],
                     now: Optional[float] = None, source: str = "paper",
                     deferred: str = "") -> None:
        """At the FIRST order submit on this market. account_value_cents=None
        means the live read failed — the bracket DEFERS (P9 §2): it opens on
        the next successful read, stamped with the deferral."""
        if market in self.open_brackets:
            return
        row = self.ledger.db.execute(
            "SELECT 1 FROM window_econ WHERE market=?", (market,)).fetchone()
        if row is not None:
            return
        now = time.time() if now is None else now
        if account_value_cents is None:
            self.pending_opens.setdefault(market, now)
            log.warning("WINDOW_ECON open %s DEFERRED — account value unreadable", market)
            return
        self._reject_paper_in_live(market, source)
        self.open_brackets[market] = Bracket(market, account_value_cents, now)
        self.ledger.db.execute(
            "INSERT INTO window_econ (ts, market, open_value_cents, source, deferred)"
            " VALUES (?,?,?,?,?)",
            (now, market, account_value_cents, source, deferred))
        self.ledger.db.commit()
        log.info("WINDOW_ECON open %s at %dc (source=%s)", market,
                 account_value_cents, source)

    def cash_moves_inside(self, open_ts: float, close_ts: float) -> int:
        """Confirmed cash movements timestamped inside the bracket."""
        return int(self.ledger.db.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM cash_movements"
            " WHERE ts>? AND ts<=? AND confirmed_by != 'boot'",
            (open_ts, close_ts)).fetchone()[0])

    def close_bracket(self, market: str, account_value_cents: Optional[int],
                      fills_pnl_cents: int, lanes_active: str = "",
                      fills_count: int = 0,
                      now: Optional[float] = None, source: str = "paper",
                      deferred: str = "") -> Optional[int]:
        """After settlement confirmed + fills booked. Returns window_pnl_cents.
        account_value_cents=None = live read failed — the close DEFERS (the
        settlement is already booked once; the bracket completes on the next
        successful read, never re-settling)."""
        br = self.open_brackets.pop(market, None)
        if br is None:
            return None
        now = time.time() if now is None else now
        if account_value_cents is None:
            self.pending_closes[market] = {
                "bracket": br, "fills_pnl": fills_pnl_cents,
                "lanes": lanes_active, "fills": fills_count, "t0": now}
            log.warning("WINDOW_ECON close %s DEFERRED — account value unreadable",
                        market)
            return None
        self._reject_paper_in_live(market, source)
        cash_moves = self.cash_moves_inside(br.open_ts, now)
        window_pnl = account_value_cents - br.open_value_cents - cash_moves

        # broker truth vs our arithmetic — divergence pages the moment it exists
        if abs(window_pnl - fills_pnl_cents) > DIVERGENCE_TOLERANCE_CENTS:
            failures.fail("WINDOW_ECON_DIVERGENCE",
                          f"{market}: broker window pnl {window_pnl}c vs fills-based "
                          f"{fills_pnl_cents}c (tolerance {DIVERGENCE_TOLERANCE_CENTS}c)",
                          broker_pnl=window_pnl, fills_pnl=fills_pnl_cents,
                          market=market)

        self.ledger.db.execute(
            "UPDATE window_econ SET close_value_cents=?, cash_moves_cents=?,"
            " window_pnl_cents=?, fills_pnl_cents=?, lanes_active=?, fills_count=?,"
            " source=?, deferred=TRIM(deferred || ' ' || ?)"
            " WHERE market=?",
            (account_value_cents, cash_moves, window_pnl, fills_pnl_cents,
             lanes_active, fills_count, source, deferred, market))
        self.ledger.db.commit()
        self.surface.write_row(
            "ECON", market, f"w-{market}", "WINDOW_ECON",
            detail=json.dumps({"open": br.open_value_cents,
                               "close": account_value_cents,
                               "pnl": window_pnl, "lanes": lanes_active,
                               "fills": fills_count}))
        self._apply_streak(market, window_pnl, account_value_cents)
        return window_pnl

    def flush_deferred(self, account_value_cents: int, source: str,
                       now: Optional[float] = None) -> int:
        """P9 §2: a successful account-value read completes every deferred
        bracket, stamped with how long it waited. Returns brackets completed."""
        now = time.time() if now is None else now
        done = 0
        for market, t0 in list(self.pending_opens.items()):
            del self.pending_opens[market]
            self.open_bracket(market, account_value_cents, now=now,
                              source=source, deferred=f"open+{now - t0:.0f}s")
            done += 1
        for market, p in list(self.pending_closes.items()):
            del self.pending_closes[market]
            self.open_brackets[market] = p["bracket"]  # restore, then close for real
            self.close_bracket(market, account_value_cents, p["fills_pnl"],
                               lanes_active=p["lanes"], fills_count=p["fills"],
                               now=now, source=source,
                               deferred=f"close+{now - p['t0']:.0f}s")
            done += 1
        return done

    # ── §2: the two-strike leash ───────────────────────────────────────
    def _apply_streak(self, market: str, window_pnl: int, book_cents: int) -> None:
        strikes = json.loads(self.ledger.get_state(STRIKES_KEY) or "[]")
        if window_pnl < 0:
            streak = self.streak + 1
            strikes.append({"market": market, "pnl": window_pnl})
            strikes = strikes[-2:]
        else:
            streak = 0
            strikes = []
        self._set_streak(streak)
        self.ledger.set_state(STRIKES_KEY, json.dumps(strikes))

        sign = "+" if window_pnl >= 0 else ""
        self.telegram.alert(
            f"📊 {market} {sign}${window_pnl / 100:.2f} · "
            f"book ${book_cents / 100:.2f} · streak {streak}")

        if streak >= 2 and not self.halted():
            self.ledger.set_state(HALT_KEY, "1")
            self.gateway.halt_entries(HALT_REASON)
            s1, s2 = strikes[0], strikes[1]
            self.telegram.alert(
                f"⛔ TWO-STRIKE HALT: {s1['market']} {s1['pnl']}c, "
                f"{s2['market']} {s2['pnl']}c · book ${book_cents / 100:.2f} · "
                f"reply /reset_halt to resume")
            failures.fail("TWO_STRIKE_HALT",
                          f"two consecutive negative windows: "
                          f"{s1['market']} {s1['pnl']}c, {s2['market']} {s2['pnl']}c",
                          strikes=strikes, book_cents=book_cents)

    def reset_halt(self, confirmed_by: str = "telegram") -> str:
        """Drew's word alone. Re-enables ENTRIES only — it cannot place, amend,
        or cancel anything (single-gateway law)."""
        if not self.halted():
            return "no halt active"
        self.ledger.set_state(HALT_KEY, "0")
        self._set_streak(0)
        self.ledger.set_state(STRIKES_KEY, "[]")
        self.gateway.resume_entries(HALT_REASON)
        self.surface.write_row("ECON", "ENGINE", f"halt-{int(time.time())}",
                               "HALT_RESET", detail=f"confirmed_by={confirmed_by}")
        book = self.ledger.book_cents()
        return (f"halt cleared — entries re-enabled · book ${book / 100:.2f} · "
                f"next window considered on the next cycle")

    # ── §2.4: the pack lines ───────────────────────────────────────────
    def pack_lines(self) -> list:
        lines = [f"TWO-STRIKE: streak={self.streak} halted={self.halted()}"]
        day_ago = time.time() - 86400
        halts = self.ledger.db.execute(
            "SELECT COUNT(*) FROM failures WHERE why_tag='TWO_STRIKE_HALT' AND ts>?",
            (day_ago,)).fetchone()[0]
        resets = self.ledger.db.execute(
            "SELECT ts FROM surface_rows WHERE state='HALT_RESET' AND ts>?",
            (day_ago,)).fetchall()
        lines.append(f"  halts today: {halts} · resets: "
                     + (", ".join(time.strftime("%H:%M", time.gmtime(t)) + " UTC"
                                  for (t,) in resets) or "none"))
        return lines
