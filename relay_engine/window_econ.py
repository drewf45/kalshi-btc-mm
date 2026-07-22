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

THE RATE HALT (A-PLAYER B3, DREW-RULED 2026-07-19 — OVERTURNS the
two-consecutive-strike law): a single losing market is NOISE and carries
no information; only a loss-RATE is the signature of a bug or regime
break. The halt fires when RATE_HALT_LOSSES of the last RATE_HALT_WINDOW
settled TRADED markets are negative (per-market BROKER P&L is the unit —
a market that wins net while a lane bled inside it is a WIN, with the
composition logged so a win-that-hid-a-loss stays visible). Everything
else stands from the two-strike era: entries halted engine-wide
(RATE_HALT), open positions custodied to conclusion, the halt PERSISTS in
the DB across boots and redeploys, resume is Drew's word alone
(/reset_halt — entries only; single-gateway law). The consecutive streak
still COUNTS for the packs; it halts nothing.

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
HALT_KEY = "two_strike_halt"      # key name kept: persisted halts survive the rename
STREAK_KEY = "two_strike_streak"  # consecutive streak — reporting only (B3)
STRIKES_KEY = "two_strike_markets"
OUTCOMES_KEY = "rate_halt_outcomes"   # B3: rolling last-M window outcomes
HALT_REASON = "RATE_HALT"
# KAL-50/50 Stage 0.1: the rate halt is now PER-LANE. Each lane keeps its own
# rolling window of fills-truth outcomes ("rate_halt_outcomes:FLIP") and trips
# a scoped gateway reason ("RATE_HALT:FLIP") that halts ONLY that lane —
# FLIP's losing streak no longer halts F, the earner. The account-value
# window_pnl (broker truth) stays the cash-integrity unit and the summary line;
# only the HALT DECISION moves to per-lane fills P&L (the sole source that can
# attribute a window to a lane at all).
LANES_HALTED_KEY = "rate_halt_lanes"  # JSON list of currently lane-halted lanes


def _lane_outcomes_key(lane: str) -> str:
    return f"{OUTCOMES_KEY}:{lane}"

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
    deferred TEXT NOT NULL DEFAULT '',      -- P9 §2: deferral stamps (open+Ns / close+Ns)
    transport TEXT NOT NULL DEFAULT ''      -- P11.1-c: REST|WS — evidence separable
);
"""

# P9 §2 / P11.1-c: columns added after the table shipped — migrate in place.
ECON_MIGRATIONS = (
    ("source", "TEXT NOT NULL DEFAULT 'paper'"),
    ("deferred", "TEXT NOT NULL DEFAULT ''"),
    ("transport", "TEXT NOT NULL DEFAULT ''"),  # REST|WS — evidence separable
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

    def halted_lanes(self) -> set:
        """KAL-50/50 Stage 0.1: lanes currently under their own scoped rate halt."""
        return set(json.loads(self.ledger.get_state(LANES_HALTED_KEY) or "[]"))

    def restore_halt_on_boot(self) -> bool:
        """P8 §2.3: restarts and redeploys do NOT clear the halt. Both the
        legacy global halt and every per-lane scoped halt survive the boot."""
        restored = False
        if self.halted():
            self.gateway.halt_entries(HALT_REASON)
            restored = True
        for lane in sorted(self.halted_lanes()):
            self.gateway.halt_entries(f"{HALT_REASON}:{lane}")
            restored = True
        if restored:
            log.warning("RATE HALT restored from DB — /reset_halt is the only key")
        return restored

    def restore_open_brackets_on_boot(self) -> int:
        """P17 §1.3: an open bracket (close_value NULL) survives a restart —
        reloaded so the settle sweep retries it. This is how the stuck 0930
        window HEALS on the first retry after deploy."""
        rows = self.ledger.db.execute(
            "SELECT market, open_value_cents, ts FROM window_econ"
            " WHERE close_value_cents IS NULL").fetchall()
        restored = 0
        for market, open_val, ts in rows:
            if market not in self.open_brackets:
                self.open_brackets[market] = Bracket(market, int(open_val), ts)
                restored += 1
        if restored:
            log.warning("restored %d open bracket(s) from DB — settle sweep "
                        "will retry them", restored)
        return restored

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
                     deferred: str = "", transport: str = "") -> None:
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
            "INSERT INTO window_econ (ts, market, open_value_cents, source,"
            " deferred, transport) VALUES (?,?,?,?,?,?)",
            (now, market, account_value_cents, source, deferred, transport))
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
                      deferred: str = "", late: bool = False,
                      per_lane: Optional[dict] = None) -> Optional[int]:
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
                "lanes": lanes_active, "fills": fills_count, "t0": now,
                "per_lane": per_lane}
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
            # P-CASH-FATAL-1 §4.6 (stopgap until E1): the window's settlement
            # value is DISPUTED — quarantine it from the book and re-book at
            # fills-truth. The 190945 phantom (+103c the fills said was +4c)
            # entered the book here, then armed the deny-reboot breach; a
            # divergent number never again silently inflates the book that
            # cash reconciles against.
            phantom = self.ledger.quarantine_divergent_settlements(
                market, fills_pnl_cents)
            if phantom:
                self.telegram.alert(
                    f"🧾 DIVERGENT settlement {market}: {phantom:+d}c "
                    f"quarantined from the book — fills-truth "
                    f"{fills_pnl_cents}c booked instead (E1 traces the source)")

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
        self._apply_streak(market, window_pnl, account_value_cents, late=late,
                           per_lane=per_lane)
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
                               deferred=f"close+{now - p['t0']:.0f}s",
                               per_lane=p.get("per_lane"))
            done += 1
        return done

    # ── §2: THE RATE HALT (A-PLAYER B3; P17 §2.1: late truths count) ───
    def _apply_streak(self, market: str, window_pnl: int, book_cents: int,
                      late: bool = False, per_lane: Optional[dict] = None) -> None:
        # KAL-50/50 Stage 0.1: with per-lane fills P&L available, the halt is
        # decided PER LANE so one lane's losses never freeze another. The
        # legacy global path (no attribution) is preserved unchanged below.
        if per_lane:
            self._apply_streak_per_lane(market, window_pnl, book_cents,
                                        per_lane, late)
            return
        # the rolling window of settled traded markets — per-market BROKER
        # P&L is the unit (Engineer: settled-only; never in-flight marks)
        outcomes = json.loads(self.ledger.get_state(OUTCOMES_KEY) or "[]")
        outcomes.append({"market": market, "pnl": window_pnl})
        outcomes = outcomes[-config.RATE_HALT_WINDOW:]
        self.ledger.set_state(OUTCOMES_KEY, json.dumps(outcomes))
        losses = [o for o in outcomes if o["pnl"] < 0]
        # the consecutive streak still REPORTS (packs); it halts nothing
        streak = self.streak + 1 if window_pnl < 0 else 0
        self._set_streak(streak)
        self.ledger.set_state(STRIKES_KEY, json.dumps(losses[-2:]))

        sign = "+" if window_pnl >= 0 else ""
        late_s = " (settled late — books healed)" if late else ""
        self.telegram.alert(
            f"📊 {market} {sign}${window_pnl / 100:.2f} · "
            f"book ${book_cents / 100:.2f} · "
            f"rate {len(losses)}/{len(outcomes)}{late_s}")

        if len(losses) >= config.RATE_HALT_LOSSES and not self.halted():
            self.ledger.set_state(HALT_KEY, "1")
            self.gateway.halt_entries(HALT_REASON)
            named = ", ".join(f"{o['market']} {o['pnl']}c" for o in losses)
            retro = (f"⛔ RATE HALT (retroactive: {market} settled late): "
                     if late else "⛔ RATE HALT: ")
            self.telegram.alert(
                f"{retro}{len(losses)} of last {len(outcomes)} markets "
                f"negative — {named} · book ${book_cents / 100:.2f} · "
                f"reply /reset_halt to resume")
            failures.fail("RATE_HALT",
                          f"{len(losses)} of last {len(outcomes)} settled "
                          f"markets negative: {named}",
                          outcomes=outcomes, book_cents=book_cents)

    def _apply_streak_per_lane(self, market: str, window_pnl: int,
                               book_cents: int, per_lane: dict,
                               late: bool = False) -> None:
        """KAL-50/50 Stage 0.1: each lane runs its own N-of-M rate halt on its
        own fills-truth outcomes. A lane that trips halts ONLY itself; the
        others (F above all) trade on. The per-market broker window_pnl still
        prints as the honest summary line."""
        sign = "+" if window_pnl >= 0 else ""
        late_s = " (settled late — books healed)" if late else ""
        self.telegram.alert(
            f"📊 {market} {sign}${window_pnl / 100:.2f} · "
            f"book ${book_cents / 100:.2f} · "
            f"lanes {','.join(sorted(per_lane))}{late_s}")
        halted = self.halted_lanes()
        for lane in sorted(per_lane):
            pnl = int(per_lane[lane])
            key = _lane_outcomes_key(lane)
            outcomes = json.loads(self.ledger.get_state(key) or "[]")
            outcomes.append({"market": market, "pnl": pnl})
            outcomes = outcomes[-config.RATE_HALT_WINDOW:]
            self.ledger.set_state(key, json.dumps(outcomes))
            losses = [o for o in outcomes if o["pnl"] < 0]
            if len(losses) >= config.RATE_HALT_LOSSES and lane not in halted:
                halted.add(lane)
                self.ledger.set_state(LANES_HALTED_KEY,
                                      json.dumps(sorted(halted)))
                self.gateway.halt_entries(f"{HALT_REASON}:{lane}")
                named = ", ".join(f"{o['market']} {o['pnl']}c" for o in losses)
                retro = (f"⛔ {lane} RATE HALT (retroactive: {market} settled "
                         f"late): " if late else f"⛔ {lane} RATE HALT: ")
                self.telegram.alert(
                    f"{retro}{len(losses)} of last {len(outcomes)} {lane} "
                    f"markets negative — {named} · other lanes trade on · "
                    f"reply /reset_halt to resume")
                failures.fail("RATE_HALT",
                              f"{lane}: {len(losses)} of last {len(outcomes)} "
                              f"settled {lane} markets negative: {named}",
                              lane=lane, outcomes=outcomes,
                              book_cents=book_cents)

    def reset_halt(self, confirmed_by: str = "telegram") -> str:
        """Drew's word alone. Re-enables ENTRIES only — it cannot place, amend,
        or cancel anything (single-gateway law).

        WO-HALT-ORPHAN §1.1/§1.2: the TRUE halt state is the gateway's
        reason SET, not the rate DB flag alone — a status that reads only
        self.halted() lied "no halt active" while ORIENTATION_DIVERGENCE
        froze the desk. The key now clears the whole set (except reasons
        with their own key: cash-fatal/prompt clear via /clear_cash_fatal,
        DEGRADE_LADDER auto-resumes on WS_LIVE) and reports which reasons
        lifted."""
        from .ledger import CASH_FATAL_REASON, CASH_PROMPT_REASON
        keep = (CASH_FATAL_REASON, CASH_PROMPT_REASON, "DEGRADE_LADDER")
        halted_reasons = self.gateway.entries_halted_reasons
        clearable = [r for r in halted_reasons if r not in keep]
        lane_halts = self.halted_lanes()
        if not self.halted() and not lane_halts and not clearable:
            return "no halt active"
        # rate-halt DB flag: cleared only when the rate reason is present
        self.ledger.set_state(HALT_KEY, "0")
        self._set_streak(0)
        self.ledger.set_state(STRIKES_KEY, "[]")
        self.ledger.set_state(OUTCOMES_KEY, "[]")   # B3: the window restarts clean
        # KAL-50/50 Stage 0.1: clear every per-lane window too (the scoped
        # RATE_HALT:<lane> gateway reasons lift with resume_entries_all below).
        for lane in lane_halts:
            self.ledger.set_state(_lane_outcomes_key(lane), "[]")
        self.ledger.set_state(LANES_HALTED_KEY, "[]")
        cleared = self.gateway.resume_entries_all(keep=keep)
        self.surface.write_row("ECON", "ENGINE", f"halt-{int(time.time())}",
                               "HALT_RESET",
                               detail=f"confirmed_by={confirmed_by} "
                                      f"cleared={','.join(cleared) or 'none'}")
        book = self.ledger.book_cents()
        names = ", ".join(cleared) or "none"
        held = ", ".join(sorted(r for r in halted_reasons if r in keep))
        held_s = f" · still held (own key): {held}" if held else ""
        return (f"halt cleared ({names}) — entries re-enabled · "
                f"book ${book / 100:.2f}{held_s} · next window considered "
                f"on the next cycle")

    # ── §2.4: the pack lines ───────────────────────────────────────────
    def pack_lines(self) -> list:
        outcomes = json.loads(self.ledger.get_state(OUTCOMES_KEY) or "[]")
        losses = sum(1 for o in outcomes if o["pnl"] < 0)
        lines = [f"RATE-HALT: rate={losses}/{len(outcomes)} "
                 f"(bound {config.RATE_HALT_LOSSES}/{config.RATE_HALT_WINDOW})"
                 f" halted={self.halted()} · streak={self.streak} (info)"]
        day_ago = time.time() - 86400
        halts = self.ledger.db.execute(
            "SELECT COUNT(*) FROM failures WHERE why_tag IN"
            " ('RATE_HALT','TWO_STRIKE_HALT') AND ts>?",
            (day_ago,)).fetchone()[0]
        resets = self.ledger.db.execute(
            "SELECT ts FROM surface_rows WHERE state='HALT_RESET' AND ts>?",
            (day_ago,)).fetchall()
        lines.append(f"  halts today: {halts} · resets: "
                     + (", ".join(time.strftime("%H:%M", time.gmtime(t)) + " UTC"
                                  for (t,) in resets) or "none"))
        return lines
