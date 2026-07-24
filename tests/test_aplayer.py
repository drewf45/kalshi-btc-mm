"""THE A-PLAYER DOCUMENT (build 33) — the machine that does not need to be
watched. B1 half-cent precision (the venue's own units above 90¢); B2
silent small re-baseline (the overnight book runs untouched); B3 the rate
halt (a single loss is NOISE; only N-of-M stops trading); B4 the Kelly
fraction is Drew's dial; B5 the P&L-blind cut (thresholds never read the
position's P&L). HARD RAIL: the genuine-dispute cash-FATAL, salvage
loss-term, and flip-cover integrity paths stay exactly as proven."""

import json

import pytest

from relay_engine import config, failures
from relay_engine.gateway import Gateway
from relay_engine.ledger import CASH_PROMPT_REASON, CashProtocol

BOOK = 10_000
TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]


# ── B1: HALF-CENT PRECISION ────────────────────────────────────────────────
def test_965_fill_books_at_exactly_965(ledger):
    """Acceptance: write 96.5 → read 96.5 (round-trip, Engineer's flag).
    SQLite INTEGER affinity stores the half-cent losslessly as REAL."""
    ledger.record_fill(TICKER, "F", "yes", "ENTRY", 96.5, 1, "PROBE")
    row = ledger.db.execute(
        "SELECT price_cents FROM fills WHERE market=?", (TICKER,)).fetchone()
    assert row[0] == 96.5


def test_end_to_end_parse_books_half_cent(gateway, ledger, surface):
    """The venue record '0.9650' flows through parse → sweep → ledger with
    the half-cent intact — the truncation site (int(round(cost))) is dead."""
    from relay_engine.book import OrderBook
    from relay_engine.fills import FillBooker
    from relay_engine.gateway import Order
    b = OrderBook(market=TICKER)
    b.apply_snapshot({96: 10}, {2: 10}, ts=1.0)
    booker = FillBooker(gateway, ledger, surface)
    r = gateway.submit(Order(lane="F", event=EVENT, market=TICKER,
                             side="yes", action="buy", price_cents=96,
                             count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY", why="F tier96 · surv~price"),
                       b)
    booker.sweep([{"fill_id": "hc", "order_id": r.order_id,
                   "yes_price_dollars": "0.9650", "count": 1}], now=1000.0)
    assert ledger.db.execute(
        "SELECT price_cents FROM fills").fetchone()[0] == 96.5


def test_half_cent_residue_no_longer_reads_as_cash_delta(ledger):
    """Acceptance: reconcile after high-price fills shows delta=0 where the
    truncating book showed 1-2c. Two 96.5c settlement legs sum to a whole
    cent; the venue's balance moved the same whole cent — CLEAN."""
    ledger.record_settlement(TICKER, "F", 3.5, "half-cent leg")
    ledger.record_settlement(TICKER + "b", "F", 3.5, "half-cent leg")
    assert ledger.book_cents() == BOOK + 7        # summed exact, rounded once
    cash = CashProtocol(ledger, alert_fn=lambda m: None)
    assert cash.reconcile(BOOK + 7, 0, 0, now=1000.0) == "CLEAN"


# ── B2: SILENT SMALL RE-BASELINE ───────────────────────────────────────────
def test_noise_delta_rebaselines_silently_never_prompts(ledger, surface):
    """Acceptance: a 2c overnight delta → silent re-baseline, entries never
    halt, no prompt, no page — the machine runs untouched."""
    alerts = []
    cash = CashProtocol(ledger, alert_fn=alerts.append)
    cash.gateway = Gateway(ledger, surface)
    assert cash.reconcile(BOOK - 2, 0, 0, now=1000.0) == "SILENT_REBASED"
    assert alerts == []                              # zero pages
    assert cash.pending is None and not cash.entries_halted
    assert CASH_PROMPT_REASON not in cash.gateway.entries_halted_reasons
    row = ledger.db.execute(
        "SELECT amount_cents, kind, confirmed_by FROM cash_movements"
        " WHERE kind='SILENT_REBASE'").fetchone()
    assert row == (-2, "SILENT_REBASE", "auto_noise")
    assert ledger.book_cents() == BOOK - 2           # re-baselined, logged
    assert cash.reconcile(BOOK - 2, 0, 0, now=1010.0) == "CLEAN"


def test_large_delta_still_prompts_and_can_go_fatal(ledger, surface):
    """HARD RAIL: above the noise bound the WO-CASH-FATAL-1 machinery is
    byte-identical — prompt, halt, deny, durable FATAL."""
    cash = CashProtocol(ledger, alert_fn=lambda m: None)
    cash.gateway = Gateway(ledger, surface)
    over = config.CASH_SILENT_REBASE_CENTS + 1
    assert cash.reconcile(BOOK - over, 0, 0, now=1000.0) == "PROMPTED"
    assert cash.entries_halted
    cash.deny_cash()
    assert cash.fatal
    assert ledger.get_state("cash_fatal") is not None


# ── B3: THE RATE HALT ──────────────────────────────────────────────────────
class _TG:
    def __init__(self):
        self.alerts = []

    def alert(self, m):
        self.alerts.append(m)


@pytest.fixture
def econ(ledger, gateway, surface):
    from relay_engine.window_econ import WindowEcon
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    e = WindowEcon(ledger, gateway, surface, _TG())
    yield e
    failures._ledger = None


def test_one_loss_never_halts(econ):
    """WO-2026-07-24-C: the halt counts MONEY — a single loss under the drawdown
    threshold (120c at FLIP_SIZE_CAP=3) is NOISE and halts nothing."""
    econ._apply_streak("M0", -20, BOOK, per_lane={"FLIP": -20})
    assert "FLIP" not in econ.halted_lanes()
    assert not econ.halted()


def test_profitable_asymmetric_sequence_does_not_halt(econ):
    """Acceptance #4: −8, −7, +17 nets +2c — a PROFITABLE sequence the old
    2-of-4 count-halt suppressed. Summing money, it never halts."""
    for i, pnl in enumerate((-8, -7, +17)):
        econ._apply_streak(f"M{i}", 0, BOOK, per_lane={"FLIP": pnl})
    assert "FLIP" not in econ.halted_lanes()


def test_lane_drawdown_past_threshold_halts_and_persists(econ, ledger, gateway,
                                                         surface):
    """WO-2026-07-24-C: a lane whose summed drawdown crosses
    RATE_HALT_DRAWDOWN_C halts ONLY itself, pages, and persists across boot;
    /reset_halt is the only key. The global (all-lane) halt is retired."""
    HALF = config.RATE_HALT_DRAWDOWN_C // 2 + 50
    for i, pnl in enumerate((-HALF, +5, -HALF)):       # two crossings < -threshold
        econ._apply_streak(f"M{i}", 0, BOOK, per_lane={"FLIP": pnl})
    assert "FLIP" in econ.halted_lanes()
    assert "RATE_HALT:FLIP" in gateway.entries_halted_reasons
    assert not econ.halted()                         # global untouched
    page = next(a for a in econ.telegram.alerts if "RATE HALT" in a)
    assert "drew down" in page and "/reset_halt" in page
    # the reboot: fresh econ over the same DB — the per-lane halt survives
    from relay_engine.window_econ import WindowEcon
    gw2 = Gateway(ledger, surface)
    econ2 = WindowEcon(ledger, gw2, surface, _TG())
    assert econ2.restore_halt_on_boot() is True
    assert "RATE_HALT:FLIP" in gw2.entries_halted_reasons
    assert econ2.reset_halt().startswith("halt cleared")
    assert json.loads(ledger.get_state("rate_halt_outcomes:FLIP")) == []


def test_per_lane_pnl_is_the_unit(econ):
    """Acceptance: a lane that nets small positive over its windows is a WIN —
    the unit is each lane's own fills-P&L, never the aggregate window."""
    for i, pnl in enumerate((-20, +1, +1, +1)):
        econ._apply_streak(f"M{i}", 0, BOOK, per_lane={"FLIP": pnl})
    assert "FLIP" not in econ.halted_lanes()          # net −17 > −120


# ── B4: THE FRACTION IS DREW'S DIAL ────────────────────────────────────────
def test_fraction_dial_admits_multi_lot_when_ruled(monkeypatch):
    """Acceptance: the ruled fraction admits >1 lot at the live book; the
    Kelly/depth/net-risk MATH is untouched — only the dial turns."""
    from relay_engine.sizing import size_order
    monkeypatch.setattr(config, "KELLY_FRACTION_CEILING", 0.5)
    dec = size_order(1174, 97, 10_000)
    assert dec.contracts == 3          # 587c budget // 97 = 6 → risk cap 3
    monkeypatch.setattr(config, "KELLY_FRACTION_CEILING", 1.0 / 12.0)
    assert size_order(1174, 97, 10_000).contracts == 1   # math unchanged


def test_boot_sizing_line_states_the_dial():
    from relay_engine.boot import sizing_line
    line = sizing_line(1174)
    assert "DREW dial: KELLY_FRACTION env" in line
    assert f"fraction={config.KELLY_FRACTION_CEILING:.4f}" in line


# ── B5: THE P&L-BLIND CUT ──────────────────────────────────────────────────
def test_exit_decision_is_provably_independent_of_pnl(ledger, gateway,
                                                      surface):
    """Acceptance: a position UP 5c and one DOWN 5c, identical book/table/
    time state, get the SAME exit decision — the trigger is the P&L-BLIND
    catastrophe floor (a fixed price, never the entry basis). A human moves
    the bar when up (greed) or down (hope); the machine cannot. (WO-FLIP-
    LIQUIDITY-HOLD retired the band-floor cut — a low mark is illiquidity,
    held; the catastrophe backstop is the remaining P&L-blind price cut and
    is equally basis-independent.)"""
    from relay_engine.book import OrderBook
    from relay_engine.custodian import Custodian
    from relay_engine.feed import DegradeLadder
    from relay_engine.lane_flip import LaneFlip
    CLOSE = 1_000_000.0
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    decisions = {}
    for name, entry in (("down5", 53), ("up5", 43)):   # both mark 48 below
        mkt = f"KXBTC15M-02JAN25100{1 if entry == 53 else 2}-T99"
        flip = LaneFlip(gateway, custodian=Custodian(
            gateway, ledger, surface, ladder=DegradeLadder()))
        w = flip._window(mkt, CLOSE)
        w.opens["yes"] = {"entry": entry, "fill_ts": CLOSE - 1100,
                          "count": 1, "take_oid": "OID-T",
                          "take_proposed": True, "collapse_polls": 0,
                          "det_ts": None, "entry_oid": None,
                          "defer_polls": 0}
        for mark in (48, 20):                          # identical states
            b = OrderBook(market=mkt)
            b.apply_snapshot({mark: 10}, {40: 10}, ts=1.0)
            ctx = {"book": b, "now": CLOSE - 700 - (48 - mark),
                   "close_ts": CLOSE, "spot": None,
                   "grain": None, "spotlead": None}
            # WO-FLIP-CATASTROPHE-ILLIQUIDITY: the catastrophe floor is a REAL
            # move — sustained 2 polls (fill_ts CLOSE-1100 is past the opening
            # window; depth 10 is real). Poll twice; the 2nd is the decision.
            flip.evaluate(mkt, ctx)
            props = flip.evaluate(mkt, ctx)
            decisions.setdefault(name, []).append(
                bool([p for p in props if p.purpose == "CUT"]))
    # same table/time state -> same decision, up or down: hold at 48, cut at
    # the P&L-blind catastrophe floor (20), identical for the +5 and −5 basis
    assert decisions["down5"] == decisions["up5"] == [False, True]
    failures._ledger = None
