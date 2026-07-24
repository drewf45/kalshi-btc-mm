"""WO-HALT-ORPHAN — the orphaned orientation halt + overnight noise
(build 38).

Drew found the engine frozen at $35.94 for 25+ min: /reset_halt said "no
halt active" while every proposal was WALL_REJECT[ENTRIES_HALTED]
ORIENTATION_DIVERGENCE. Two halt producers (rate + orientation) share one
gateway reason SET; the operator key reached only the rate reason, and
the status read only the rate DB flag — a durable stop with no door and a
status light lying green.

§1: /reset_halt clears ALL entry-halt reasons (except reasons with their
own key: cash-fatal, DEGRADE_LADDER), reads the gateway set for its guard,
and reports which reasons lifted; orientation auto-recovers on a fresh
recheck. §2: page the real naked leg not its transient; reject the
unfundable favorite once, not 21×; a strike needs a FRESH record.

HARD RAIL: rate-halt still persists across boot and needs /reset_halt;
cash-integrity unchanged; only orientation auto-heals."""

import pytest

from relay_engine import config, failures
from relay_engine.ledger import CASH_FATAL_REASON
from relay_engine.window_econ import HALT_REASON, WindowEcon


class _TG:
    def __init__(self):
        self.alerts = []

    def alert(self, m):
        self.alerts.append(m)


@pytest.fixture
def econ(ledger, gateway, surface):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    e = WindowEcon(ledger, gateway, surface, _TG())
    yield e
    failures._ledger = None


# ── §1.1/§1.2: the key clears ALL reasons; the status tells the truth ──────
def test_reset_halt_clears_orphaned_orientation_and_reports_both(econ,
                                                                 gateway,
                                                                 ledger):
    """A mixed rate+orientation halt: the OLD reset cleared only the rate
    reason and left ORIENTATION_DIVERGENCE orphaned forever. Now the key
    clears BOTH and names them."""
    # a per-lane rate halt trips (FLIP drawdown < −120; WO-2026-07-24-C)
    HALF = config.RATE_HALT_DRAWDOWN_C // 2 + 50
    econ._apply_streak("M0", 0, 3594, per_lane={"FLIP": -HALF})
    econ._apply_streak("M1", 0, 3594, per_lane={"FLIP": -HALF})
    assert "FLIP" in econ.halted_lanes() \
        and "RATE_HALT:FLIP" in gateway.entries_halted_reasons
    # orientation halt trips independently, into the same set
    gateway.halt_entries("ORIENTATION_DIVERGENCE")
    reply = econ.reset_halt()
    assert "ORIENTATION_DIVERGENCE" in reply and "RATE_HALT" in reply
    assert gateway.entries_halted_reasons == set()      # the whole set lifts
    assert econ.halted_lanes() == set()


def test_reset_halt_status_never_lies_orientation_only(econ, gateway):
    """The status LIE: an orientation-only halt (rate flag clear) made the
    OLD reset return 'no halt active' while entries were frozen. Now the
    guard reads the gateway SET — it reports the true halt and clears it."""
    assert not econ.halted()                            # rate flag clear
    gateway.halt_entries("ORIENTATION_DIVERGENCE")      # but the desk IS frozen
    reply = econ.reset_halt()
    assert reply != "no halt active"                    # the lie is dead
    assert "ORIENTATION_DIVERGENCE" in reply
    assert gateway.entries_halted_reasons == set()


def test_reset_halt_keeps_cash_fatal_its_own_key(econ, gateway):
    """The Engineer's persistent-reasons guard: /reset_halt must NOT clear
    a reason with its own key — cash-fatal clears via /clear_cash_fatal."""
    gateway.halt_entries("ORIENTATION_DIVERGENCE")
    gateway.halt_entries(CASH_FATAL_REASON)
    reply = econ.reset_halt()
    assert "ORIENTATION_DIVERGENCE" in reply
    assert CASH_FATAL_REASON in gateway.entries_halted_reasons   # still held
    assert "still held (own key)" in reply and "CASH_FATAL" in reply


def test_reset_halt_truly_clean_is_still_noop(econ, gateway):
    assert gateway.entries_halted_reasons == set()
    assert econ.reset_halt() == "no halt active"


def test_degrade_ladder_survives_reset(econ, gateway):
    """DEGRADE_LADDER auto-resumes on WS_LIVE — the operator key does not
    clear it (a transport-state halt, not an operator stop)."""
    gateway.halt_entries("DEGRADE_LADDER")
    econ.reset_halt()
    assert "DEGRADE_LADDER" in gateway.entries_halted_reasons


# ── §1.3: orientation auto-recovers on a fresh clean recheck ───────────────
@pytest.fixture
def engine(tmp_path, monkeypatch):
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "halt.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST",
                       boot_id=1)
    yield e
    failures._ledger = None


TICKER = "KXBTC15M-02JAN251000-T99"


def test_orientation_auto_recovers_on_fresh_pass(engine, monkeypatch):
    import json
    engine.feed.handle_frame(json.dumps(
        {"type": "orderbook_snapshot",
         "msg": {"market_ticker": TICKER, "yes": [[45, 10]], "no": [[30, 10]]}}),
        now=1000.0)
    # the halt is live, stamped with its market
    engine.gateway.halt_entries("ORIENTATION_DIVERGENCE")
    engine._orientation_halt_market = TICKER
    # a FRESH record now AGREES with ours (45 vs 45, ≤3¢) → auto-resume
    monkeypatch.setattr(engine, "_fresh_record_touches",
                        lambda market: (45, 47))
    engine.process_divergence_watches(object(), now=1010.0)
    assert "ORIENTATION_DIVERGENCE" not in \
        engine.gateway.entries_halted_reasons
    assert engine._orientation_halt_market is None
    assert any("ORIENTATION recovered" in m for m in engine.telegram_sent)


def test_orientation_stays_halted_while_fresh_still_diverges(engine,
                                                            monkeypatch):
    """A REAL inversion keeps failing the fresh recheck → stays halted
    (Adversary: auto-resume requires a PASSING fresh two-witness read)."""
    import json
    engine.feed.handle_frame(json.dumps(
        {"type": "orderbook_snapshot",
         "msg": {"market_ticker": TICKER, "yes": [[45, 10]], "no": [[30, 10]]}}),
        now=1000.0)
    engine.gateway.halt_entries("ORIENTATION_DIVERGENCE")
    engine._orientation_halt_market = TICKER
    monkeypatch.setattr(engine, "_fresh_record_touches",
                        lambda market: (66, 68))       # still 21¢ apart
    engine.process_divergence_watches(object(), now=1010.0)
    assert "ORIENTATION_DIVERGENCE" in \
        engine.gateway.entries_halted_reasons          # a real one holds


# ── §2B: the unfundable favorite is refused once, not 21× ──────────────────
def test_budget_wall_rejected_once_per_window(engine, monkeypatch):
    from relay_engine.errors import WallRejection
    from relay_engine.gateway import Order

    calls = {"n": 0}

    def _submit(order, book, now_mono=None):
        calls["n"] += 1
        raise WallRejection("BUDGET", "unfundable 96c favorite")
    monkeypatch.setattr(engine.gateway, "submit", _submit)
    monkeypatch.setattr(engine, "_score_and_size", lambda p, b: None)

    from relay_engine.book import OrderBook
    book = OrderBook(market=TICKER)
    book.apply_snapshot({96: 10}, {2: 10}, ts=1.0)
    engine._window_of[TICKER] = "w1"

    def _proposal():
        return Order(lane="F", event="EV", market=TICKER, side="yes",
                     action="buy", price_cents=96, count=1,
                     size_tier=config.TIER_PROBE, purpose="ENTRY",
                     why="F tier96 · surv~price")
    # simulate the cycle's submit loop across 5 cycles: after the first
    # BUDGET reject, the same lane/side/price is not re-submitted
    for _ in range(5):
        for p in [_proposal()]:
            br_key = (p.market, p.lane, p.side, p.price_cents)
            if p.purpose == "ENTRY" and br_key in engine._budget_rejected:
                continue
            try:
                engine.gateway.submit(p, book)
            except WallRejection as e:
                if e.wall in ("BUDGET", "DOLLAR_RISK", "NET_RISK"):
                    engine._budget_rejected.add(br_key)
    assert calls["n"] == 1                              # one reject, not 5


# ── HARD RAIL ──────────────────────────────────────────────────────────────
def test_rate_halt_still_persists_and_needs_key(econ, gateway, ledger,
                                                surface):
    """The rate halt is NOT auto-healing: it persists across boot and only
    /reset_halt clears it (unchanged)."""
    HALF = config.RATE_HALT_DRAWDOWN_C // 2 + 50
    econ._apply_streak("M0", 0, 3594, per_lane={"FLIP": -HALF})
    econ._apply_streak("M1", 0, 3594, per_lane={"FLIP": -HALF})
    assert "FLIP" in econ.halted_lanes()
    # a reboot: fresh econ over the same DB — the rate halt survives
    from relay_engine.gateway import Gateway
    gw2 = Gateway(ledger, surface)
    econ2 = WindowEcon(ledger, gw2, surface, _TG())
    assert econ2.restore_halt_on_boot() is True
    assert f"{HALT_REASON}:FLIP" in gw2.entries_halted_reasons  # persisted, not auto-healed
