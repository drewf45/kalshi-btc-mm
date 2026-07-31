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


def combined_correlated_loss(series_pnls: dict) -> Optional[dict]:
    """WO-2026-07-26-S §2 — the correlated-loss test, pure. Given {series:
    pnl_cents} for the rooms that settled a shared wall-clock window, return the
    combined-size event when ≥2 rooms LOST (the correlated tail), else None.
    Counted ONCE at combined size — never per-room-summed twice."""
    losers = {s: int(p) for s, p in series_pnls.items() if p < 0}
    if len(losers) < 2:
        return None
    return {"series": sorted(losers),
            "per_series": losers,
            "combined_cents": int(sum(losers.values()))}

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
    # COLD READ (build 67): the bracket value is the LEDGER book, a REAL
    # reconciled number (cash_movements + settlements), so "ledger" is a valid
    # live source. Only "paper" — a SHADOW-mode fabrication — remains the lie
    # this invariant forbids in live.
    _LIVE_BRACKET_SOURCES = ("venue", "ledger")

    def _reject_paper_in_live(self, market: str, source: str) -> None:
        """P9 §2 (amended by build 67): a bracket/streak/halt decision may only
        consume REAL values in live — the venue read OR the reconciled ledger
        book. A paper (shadow) number offered in live is the exact lie this
        engine exists to forbid."""
        if (config.live_submit_enabled()
                and source not in self._LIVE_BRACKET_SOURCES):
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
                    f"🧾 DIVERGENT settlement {market}: {phantom:+.1f}c "
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
        # WO-2026-07-24-C Part 1: the GLOBAL fallback is RETIRED. It was the one
        # path by which an aggregate loss (FLIP's) could halt EVERY lane —
        # including F, for losses F did not cause. Per-lane attribution now
        # exists on every close (shadow_runner settle path), so the fallback has
        # no job. HALT_KEY stays READABLE and /reset_halt still clears it (a
        # persisted legacy halt can be lifted), but NOTHING sets it going
        # forward — the halt is per-lane, always.
        if per_lane:
            self._apply_streak_per_lane(market, window_pnl, book_cents,
                                        per_lane, late)
            return
        log.info("WINDOW_ECON %s: no lane attribution — nothing to halt "
                 "(global fallback retired, WO-2026-07-24-C)", market)

    def _apply_streak_per_lane(self, market: str, window_pnl: int,
                               book_cents: int, per_lane: dict,
                               late: bool = False) -> None:
        """KAL-50/50 Stage 0.1: each lane runs its own N-of-M rate halt on its
        own fills-truth outcomes. A lane that trips halts ONLY itself; the
        others (F above all) trade on. The per-market broker window_pnl still
        prints as the honest summary line."""
        sign = "+" if window_pnl >= 0 else ""
        late_s = " (settled late — books healed)" if late else ""
        # WO-2026-07-28-X X4: the settle line's account figure is the VENUE's
        # number (age-stamped), never the derived book.
        from .ops import account_headline
        self.telegram.alert(
            f"📊 {market} {sign}${window_pnl / 100:.2f} · "
            f"account {account_headline(self.ledger)} · "
            f"lanes {','.join(sorted(per_lane))}{late_s}")
        halted = self.halted_lanes()
        # WO-2026-07-24-G Part 2: the drawdown threshold recomputes with the BOOK
        # (four stop-outs at CURRENT FLIP size). FLIP scales with the book now, so
        # a frozen threshold is the count-vs-money bug reborn. WO-2026-07-26-O §O2:
        # off TRADEABLE = the passed account value − owed, so the halt tightens as
        # the scrape banks (subtract owed from the value handed in, don't re-read).
        tradeable = max(0, book_cents - self.ledger.owed_cents())
        # WO-2026-07-26-S §2: the rate halt keys on (series, lane). scope is the
        # bare lane while one room is rostered (byte-identical) and '{series}:
        # {lane}' with more — so XRP's drawdown parks XRP, BTC keeps printing.
        series = config.series_of(market)
        for lane in sorted(per_lane):
            # WO-2026-07-24-C Part 2: count MONEY, not negative windows. A
            # profitable asymmetric sequence (−8,−7,+17 = +2¢) must NOT halt; a
            # slow bleed that never trips 2-of-4 must. Sum the last N windows'
            # fills-P&L against a drawdown threshold. WO-2026-07-27-W W2a: the
            # bound is now asked PER LANE in the lane's own loss language — the F
            # family speaks tail units (1.5× one full F loss, a single tail can't
            # halt alone), the desk keeps its stop-out geometry — so an ordinary F
            # tail no longer trips the ~3%-of-book desk bound and poisons the sum.
            scope = config.halt_scope(series, lane)
            bound = config.lane_halt_bound_c(lane, tradeable)
            pnl = float(per_lane[lane])
            key = _lane_outcomes_key(scope)
            outcomes = json.loads(self.ledger.get_state(key) or "[]")
            outcomes.append({"market": market, "pnl": pnl})
            outcomes = outcomes[-config.RATE_HALT_WINDOW_N:]
            self.ledger.set_state(key, json.dumps(outcomes))
            drawdown = sum(o["pnl"] for o in outcomes)
            if drawdown < -bound and scope not in halted:
                halted.add(scope)
                self.ledger.set_state(LANES_HALTED_KEY,
                                      json.dumps(sorted(halted)))
                self.gateway.halt_entries(f"{HALT_REASON}:{scope}")
                named = ", ".join(f"{o['market']} {o['pnl']:+.0f}c"
                                  for o in outcomes)
                # W2a: the geometry the bound speaks — a CLUSTER of F tails, or
                # the desk's stop-outs. The tail-cluster count is a registry datum
                # (SCIENTIST: the pack counts cluster frequency per room).
                if lane in config.TAIL_HALT_LANES:
                    tails = [o for o in outcomes if o["pnl"] < 0]
                    geom = (f"tail cluster: {len(tails)} F-size tails "
                            f"(bound {config.F_HALT_TAIL_MULT}× one full F loss "
                            f"= −{bound}¢)")
                    self.surface.write_row(
                        "ECON", market, f"tailcluster-{int(time.time())}",
                        "TAIL_CLUSTER",
                        detail=json.dumps({"lane": lane, "series": series,
                                           "n_tails": len(tails),
                                           "drawdown_c": round(drawdown, 1),
                                           "bound_c": bound}))
                else:
                    geom = (f"< −{bound}¢ — 4 stop-outs at book "
                            f"${book_cents / 100:.0f}")
                retro = (f"⛔ {lane} RATE HALT (retroactive: {market} settled "
                         f"late): " if late else f"⛔ {lane} RATE HALT: ")
                self.telegram.alert(
                    f"{retro}{lane} drew down {drawdown:+.0f}¢ over the last "
                    f"{len(outcomes)} windows ({geom}) — {named} · other lanes "
                    "trade on · reply /reset_halt to resume")
                failures.fail("RATE_HALT",
                              f"{lane}: drawdown {drawdown:+.0f}c over last "
                              f"{len(outcomes)} {lane} windows < "
                              f"-{bound}c: {named}",
                              lane=lane, drawdown_cents=round(drawdown, 1),
                              outcomes=outcomes, book_cents=book_cents)

    # WO-2026-07-26-S §2: the lanes whose per-(series,lane) halt state migrates
    # when the roster grows past one room (bare-lane keys → series-scoped).
    _HALT_LANES = ("F", "H8", "FLIP", "OPEN", "HUNT", "D", "P")

    def migrate_halt_keys_to_series(self, default_series: str = "KXBTC15M") -> int:
        """WO-2026-07-26-S §2 — when a SECOND room joins the roster, halt_scope
        flips from the bare lane to '{series}:{lane}' for EVERY room (BTC too).
        A live BTC halt persisted under the bare key must move with it, or a real
        drawdown-halt would silently orphan. Rename the bare-lane outcomes state,
        the halted-set entries, and any live gateway RATE_HALT:<lane> reason to
        their {default_series}:<lane> form. Idempotent (a bare key that no longer
        exists is skipped). Returns the count migrated."""
        moved = 0
        for lane in self._HALT_LANES:
            bare = _lane_outcomes_key(lane)
            scoped = _lane_outcomes_key(f"{default_series}:{lane}")
            val = self.ledger.get_state(bare)
            if val is not None and self.ledger.get_state(scoped) is None:
                self.ledger.set_state(scoped, val)
                self.ledger.del_state(bare)
                moved += 1
        # the halted-set: bare lane names → scoped
        halted = set(json.loads(self.ledger.get_state(LANES_HALTED_KEY) or "[]"))
        rescoped = {s if ":" in s else f"{default_series}:{s}" for s in halted}
        if rescoped != halted:
            self.ledger.set_state(LANES_HALTED_KEY, json.dumps(sorted(rescoped)))
        # live gateway reasons: RATE_HALT:<lane> → RATE_HALT:<series>:<lane>
        for r in list(self.gateway.entries_halted_reasons):
            if r.startswith(f"{HALT_REASON}:") and r.count(":") == 1:
                lane = r.split(":", 1)[1]
                if lane in self._HALT_LANES:
                    self.gateway.resume_entries(r)
                    self.gateway.halt_entries(f"{HALT_REASON}:{default_series}:{lane}")
        return moved

    def record_correlated_window(self, window_slot: str,
                                 series_pnls: dict) -> Optional[dict]:
        """WO-2026-07-26-S §2 — THE CORRELATED-LOSS RULE (RULED: "correlated risk
        is all"). Losses in ≥2 series inside the SAME wall-clock window are the
        one shock that reaches every room at once (a cross-crypto air-pocket
        flipping every favorite together). Count it ONCE at COMBINED size and
        name it: it PAGES, writes a CORRELATED_LOSS surface row (the measured
        datum that lets the 50% ensemble cap become DERIVED, not guessed), and is
        scored against the ensemble. The per-series halts are ALREADY fed by each
        window's own per-lane P&L (_apply_streak_per_lane) — this is the ensemble
        marker on top, never a double-count of the per-room drawdown. Additive:
        with one room it can never fire. Returns the event dict, or None."""
        ev = combined_correlated_loss(series_pnls)
        if ev is None:
            return None
        ev["window_slot"] = window_slot
        self.telegram.alert(
            f"🌐 CORRELATED LOSS — {len(ev['series'])} rooms lost the same "
            f"window ({window_slot}): {', '.join(ev['series'])} combined "
            f"{ev['combined_cents']:+d}¢. The one shock that reaches every room "
            "— counted once, at combined size (WO-S §2).")
        self.surface.write_row(
            "ENSEMBLE", "ENGINE", f"corr-{window_slot}", "CORRELATED_LOSS",
            detail=json.dumps(ev))
        failures.fail("CORRELATED_LOSS",
                      f"{len(ev['series'])} rooms lost window {window_slot}: "
                      f"{ev['per_series']} combined {ev['combined_cents']}c",
                      alert=False, series=",".join(ev["series"]),
                      combined_cents=ev["combined_cents"], window_slot=window_slot)
        return ev

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
        # WO-2026-07-27-W W2b — THE SPENT LOSS: the losses that triggered the halt
        # are the punishment SERVED; mark them SPENT (record the total, restart the
        # window clean) so the same loss can never convict twice. Without this a
        # tail sat in the trailing-8 sum for hours and re-halted the room on a loss
        # it had already answered for.
        spent = {}
        for lane in lane_halts:
            outcomes = json.loads(
                self.ledger.get_state(_lane_outcomes_key(lane)) or "[]")
            losses = sum(o["pnl"] for o in outcomes if o["pnl"] < 0)
            if losses < 0:
                spent[lane] = round(losses, 1)
            self.ledger.set_state(_lane_outcomes_key(lane), "[]")
        self.ledger.set_state(LANES_HALTED_KEY, "[]")
        cleared = self.gateway.resume_entries_all(keep=keep)
        spent_s = (" spent=" + ",".join(f"{l} {c:+.0f}c"
                                        for l, c in sorted(spent.items()))
                   if spent else "")
        self.surface.write_row("ECON", "ENGINE", f"halt-{int(time.time())}",
                               "HALT_RESET",
                               detail=f"confirmed_by={confirmed_by} "
                                      f"cleared={','.join(cleared) or 'none'}"
                                      f"{spent_s}")
        from .ops import account_headline    # WO-X X4: venue truth, not book
        names = ", ".join(cleared) or "none"
        held = ", ".join(sorted(r for r in halted_reasons if r in keep))
        held_s = f" · still held (own key): {held}" if held else ""
        spent_msg = (" · losses SPENT (window restarts clean):"
                     + spent_s.replace(" spent=", " ") if spent else "")
        return (f"halt cleared ({names}) — entries re-enabled · "
                f"account {account_headline(self.ledger)}{held_s}{spent_msg} · "
                f"next window considered on the next cycle")

    # ── §2.4: the pack lines ───────────────────────────────────────────
    def pack_lines(self) -> list:
        # WO-2026-07-24-C: the halt is per-lane and MONEY-based — show each
        # lane's summed drawdown over its rolling window against the size-derived
        # threshold, and which lanes are halted. The global count-rate is retired.
        halted = self.halted_lanes()
        # WO-2026-07-24-G Part 2 / WO-O §O2: the bound is TRADEABLE-derived now —
        # show the LIVE value (4 stop-outs at current tradeable), not a constant.
        bound = config.rate_halt_drawdown_c(self.ledger.tradeable_cents())
        lines = []
        rows = self.ledger.db.execute(
            "SELECT key, value FROM engine_state WHERE key LIKE ?",
            (OUTCOMES_KEY + ":%",)).fetchall()
        for key, val in sorted(rows):
            lane = key.split(":", 1)[1]
            outcomes = json.loads(val or "[]")
            if not outcomes:
                continue
            dd = sum(o["pnl"] for o in outcomes)
            lines.append(
                f"RATE-HALT {lane}: drawdown {dd:+.0f}c/{len(outcomes)}w "
                f"(bound -{bound}c) "
                f"halted={lane in halted}")
        if not lines:
            lines.append(
                f"RATE-HALT: per-lane money halt (bound "
                f"-{bound}c/{config.RATE_HALT_WINDOW_N}w) "
                f"· halted={sorted(halted) or 'none'}")
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
