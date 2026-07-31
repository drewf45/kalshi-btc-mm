"""WO-CASH-FATAL-1 — THE DENY-CASH REBOOT BREACH (2026-07-19, 09:44-09:56).

The law under test: an operator's integrity STOP is DURABLE. /deny_cash
persists (§4.1) and restores before any baseline (§4.2/§4.3 — the
amnesiac re-baseline was the breach); only /clear_cash_fatal clears it
(§4.4), never a restart. The pending prompt persists too (Adversary: a
reboot mid-prompt resumes PROMPTED, not trading). Both stops reach the
GATEWAY WALL (pre-fix they were narration only — no code path read
cash.entries_halted). The all-stops boot audit (§4.5) FATALs on any
persisted stop the wall doesn't honor. A DIVERGENT settlement is
quarantined from the book (§4.6) — the 99c phantom never inflates the
book cash reconciles against again."""

import pytest

from relay_engine import config, failures
from relay_engine.errors import FatalIntegrityError, WallRejection
from relay_engine.gateway import Gateway, Order
from relay_engine.ledger import (CASH_FATAL_KEY, CASH_FATAL_REASON,
                                 CASH_PENDING_KEY, CASH_PROMPT_REASON,
                                 CashProtocol)

BOOK = 10_000  # conftest baseline, cents
FIVE = 500
TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]


def _protocol(ledger, surface):
    """A boot's-worth of fresh objects over the SAME DB — what a restart
    actually constructs (shadow_runner.py builds a fresh CashProtocol and
    Gateway every boot; the DB is the only survivor)."""
    gw = Gateway(ledger, surface)
    alerts = []
    cash = CashProtocol(ledger, alert_fn=alerts.append)
    cash.gateway = gw
    cash.test_alerts = alerts
    return cash, gw


def _entry(book_px=48):
    return Order(lane="F", event=EVENT, market=TICKER, side="yes",
                 action="buy", price_cents=book_px, count=1,
                 size_tier=config.TIER_PROBE, purpose="ENTRY",
                 why="F tier48 · surv~price")


def _book():
    from relay_engine.book import OrderBook
    b = OrderBook(market=TICKER)
    b.apply_snapshot({48: 10}, {49: 10}, ts=1.0)
    return b


# ── §4.1-§4.4: the deny survives the reboot ────────────────────────────────
def test_deny_persists_reboot_restores_and_refuses_to_trade(ledger, surface):
    cash1, gw1 = _protocol(ledger, surface)
    assert cash1.reconcile(BOOK - FIVE, 0, 0, now=1000.0) == "PROMPTED"
    # the wall ACTUALLY halts (pre-fix "entries HALTED" was narration only)
    assert CASH_PROMPT_REASON in gw1.entries_halted_reasons
    cash1.deny_cash()
    assert ledger.get_state(CASH_FATAL_KEY) is not None      # §4.1 durable
    assert ledger.get_state(CASH_PENDING_KEY) is None
    assert CASH_FATAL_REASON in gw1.entries_halted_reasons

    # ── REBOOT: fresh objects, same DB — the incident's exact move ──
    cash2, gw2 = _protocol(ledger, surface)
    line = cash2.restore_on_boot(now=1100.0)
    assert line is not None and "/clear_cash_fatal" in line  # §4.2 boot line
    assert cash2.fatal and cash2.entries_halted
    assert CASH_FATAL_REASON in gw2.entries_halted_reasons
    assert cash2.reconcile(BOOK - FIVE, 0, 0, now=1200.0) == "FATAL"
    with pytest.raises(WallRejection) as e:
        gw2.submit(_entry(), _book())
    assert e.value.wall == "ENTRIES_HALTED"
    assert "CASH_FATAL" in e.value.detail

    # a SECOND restart is still not a key
    cash3, gw3 = _protocol(ledger, surface)
    assert cash3.restore_on_boot(now=1300.0) is not None
    assert cash3.fatal

    # §4.4: the operator IS the key — and clearing is not accepting
    book_before = ledger.book_cents()
    reply = cash3.clear_cash_fatal(confirmed_by="test")
    assert "cleared" in reply
    assert not cash3.fatal
    assert ledger.get_state(CASH_FATAL_KEY) is None
    assert CASH_FATAL_REASON not in gw3.entries_halted_reasons
    assert ledger.book_cents() == book_before        # NO re-baseline happened
    # the delta still exists -> the next reconcile re-prompts from scratch
    assert cash3.reconcile(BOOK - FIVE, 0, 0, now=1400.0) == "PROMPTED"
    # after the clear, a fresh boot is clean until the new prompt persists
    cash4, _ = _protocol(ledger, surface)
    assert cash4.restore_on_boot(now=1500.0) is not None  # new pending restored


def test_clear_without_fatal_is_noop(ledger, surface):
    cash, _ = _protocol(ledger, surface)
    assert cash.clear_cash_fatal() == "no cash fatal active"


# ── Adversary: the pending prompt is equally durable ───────────────────────
def test_pending_prompt_survives_reboot_still_prompted_not_trading(ledger,
                                                                   surface):
    cash1, _ = _protocol(ledger, surface)
    assert cash1.reconcile(BOOK - FIVE, 0, 0, now=1000.0) == "PROMPTED"
    assert ledger.get_state(CASH_PENDING_KEY) is not None

    cash2, gw2 = _protocol(ledger, surface)
    line = cash2.restore_on_boot(now=1060.0)          # within the deadline
    assert line is not None and "CASH PROMPT restored" in line
    assert cash2.pending is not None and cash2.pending.delta_cents == -FIVE
    assert CASH_PROMPT_REASON in gw2.entries_halted_reasons
    assert cash2.reconcile(BOOK - FIVE, 0, 0, now=1070.0) == "PROMPTED"
    with pytest.raises(WallRejection):
        gw2.submit(_entry(), _book())
    # consent still works after the restore — and clears the durable prompt
    assert cash2.confirm_cash(now=1080.0)
    assert ledger.get_state(CASH_PENDING_KEY) is None
    assert CASH_PROMPT_REASON not in gw2.entries_halted_reasons
    assert cash2.reconcile(BOOK - FIVE, 0, 0, now=1090.0) == "CLEAN"


def test_pending_prompt_expired_across_reboot_goes_fatal(ledger, surface):
    cash1, _ = _protocol(ledger, surface)
    cash1.reconcile(BOOK - FIVE, 0, 0, now=1000.0)
    cash2, gw2 = _protocol(ledger, surface)
    # the 30-min silence law counts wall-clock, reboot or not
    cash2.restore_on_boot(now=1000.0 + config.CASH_DENY_TIMEOUT_SECONDS + 1)
    assert cash2.fatal
    assert ledger.get_state(CASH_FATAL_KEY) is not None
    assert CASH_FATAL_REASON in gw2.entries_halted_reasons


def test_deny_reboot_rebuy_loop_is_dead(ledger, surface):
    """The incident's exact loop, three times: deny -> reboot -> the engine
    must come back FATAL every time; zero entries clear the wall."""
    cash, gw = _protocol(ledger, surface)
    cash.reconcile(BOOK - FIVE, 0, 0, now=1000.0)
    cash.deny_cash()
    for i in range(3):
        cash, gw = _protocol(ledger, surface)      # reboot i+1
        cash.restore_on_boot(now=1100.0 + i)
        assert cash.fatal, f"reboot {i + 1} resurrected the engine"
        with pytest.raises(WallRejection):
            gw.submit(_entry(), _book())


# ── §4.3: deny outranks the boot baseline ──────────────────────────────────
def test_live_boot_reconcile_refuses_baseline_under_fatal(tmp_path,
                                                          monkeypatch):
    from relay_engine import venue
    from relay_engine.reconcile import live_boot_reconcile
    from relay_engine.shadow_runner import ShadowEngine

    class _Client:
        def request(self, *a, **k):
            raise RuntimeError("no resting sweep in tests")

    db = str(tmp_path / "cashfatal.db")
    e1 = ShadowEngine(db_path=db)
    e1.boot()
    assert e1.cash.reconcile(e1.ledger.book_cents() - FIVE, 0, 0,
                             now=1000.0) == "PROMPTED"
    e1.cash.deny_cash()
    book_denied = e1.ledger.book_cents()

    # ── REBOOT: a fresh engine over the same DB file ──
    e2 = ShadowEngine(db_path=db)
    e2.boot()                                   # restore runs BEFORE baseline
    assert e2.cash.fatal
    assert CASH_FATAL_REASON in e2.gateway.entries_halted_reasons
    monkeypatch.setattr(venue, "get_balance",
                        lambda c: ((book_denied - FIVE) / 100.0, 0.0))
    monkeypatch.setattr(venue, "get_positions", lambda c: [])
    moves_before = e2.ledger.db.execute(
        "SELECT COUNT(*) FROM cash_movements").fetchone()[0]
    summary = live_boot_reconcile(e2, _Client())
    assert summary["cash_state"] == "FATAL_RESTORED"
    # the disputed delta was NOT laundered to zero by the restart
    assert e2.ledger.db.execute(
        "SELECT COUNT(*) FROM cash_movements").fetchone()[0] == moves_before
    assert e2.ledger.book_cents() == book_denied
    failures._ledger = None


# ── §4.5: the all-stops boot audit ─────────────────────────────────────────
def test_stop_audit_honors_and_fatals(ledger, surface):
    from relay_engine.window_econ import HALT_KEY

    class _Eng:
        pass
    from relay_engine.shadow_runner import ShadowEngine
    eng = _Eng()
    eng.ledger = ledger
    eng.gateway = Gateway(ledger, surface)
    eng.cash = CashProtocol(ledger, alert_fn=lambda m: None)
    eng.cash.gateway = eng.gateway
    audit = ShadowEngine.audit_durable_stops

    assert "no durable stops" in audit(eng)
    # honored: restore loads the stop onto the wall, audit confirms
    ledger.set_state(CASH_FATAL_KEY, '{"reason": "test", "ts": 0}')
    eng.cash.restore_on_boot(now=1000.0)
    assert "cash-fatal=HONORED" in audit(eng)
    # the breach shape: DB knows the stop, the wall does not -> FATAL loud
    eng.gateway.resume_entries(CASH_FATAL_REASON)
    with pytest.raises(FatalIntegrityError):
        audit(eng)
    # two-strike coverage: same audit, same law
    eng.gateway.halt_entries(CASH_FATAL_REASON)
    ledger.set_state(HALT_KEY, "1")
    with pytest.raises(FatalIntegrityError):
        audit(eng)                       # halted in DB, wall never told
    from relay_engine.window_econ import HALT_REASON
    eng.gateway.halt_entries(HALT_REASON)   # B3: the rate halt's reason
    assert "two-strike=HONORED" in audit(eng)


# ── §4.6: the DIVERGENT settlement is quarantined from the book ────────────
def test_divergent_bell_marks_disputed_no_per_market_rebook(ledger, surface,
                                                            gateway):
    """WO-2026-07-28-X X5 retires the X2 per-market quarantine/re-book. A single
    market's close no longer differences the account per market (X1's bug) and
    never re-books a fills-truth replacement — per-market attribution IS fills-math
    now. The account-delta check moved to the BELL: reconcile_bell sums the
    members' fills vs the account delta and, on a break, marks the bell DISPUTED
    (divergent=1 → out of book/lifetime/cells) WITHOUT overwriting anything.
    Account truth is venue-read (X4) and owed reads venue cash (X7), so the
    disputed number can inflate nothing."""
    from relay_engine.window_econ import WindowEcon

    class _TG:
        def __init__(self):
            self.alerts = []

        def alert(self, m):
            self.alerts.append(m)

    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    econ = WindowEcon(ledger, gateway, surface, _TG())
    # the incident's shape: the settle path booked +103c; the fills say +4c
    ledger.record_settlement(TICKER, "F", 103, "settle")
    econ.open_bracket(TICKER, BOOK, now=1000.0)
    # close no longer re-books: the settlement stays, window_pnl IS the fills-math
    assert econ.close_bracket(TICKER, BOOK + 103, fills_pnl_cents=4,
                              now=1500.0) == 4
    rows = ledger.db.execute(
        "SELECT lane, pnl_cents, divergent FROM settlements"
        " WHERE market=? ORDER BY id", (TICKER,)).fetchall()
    assert rows == [("F", 103, 0)]                    # NOT re-booked, not yet marked
    # the BELL catches it: Σ fills (+4) vs account delta (+103) diverges
    v = econ.reconcile_bell(econ._bell_id(1500.0))
    assert v["diverged"] and v["sum_fills"] == 4 and v["acct_delta"] == 103
    # the member is now DISPUTED (divergent=1 → out of lifetime), no re-book row
    assert ledger.db.execute(
        "SELECT divergent FROM settlements WHERE market=?",
        (TICKER,)).fetchone()[0] == 1
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='BELL_ECON_DIVERGENCE'"
    ).fetchone()[0] == 1
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='WINDOW_ECON_DIVERGENCE'"
    ).fetchone()[0] == 0                               # the per-market page is retired
    failures._ledger = None


def test_settlement_matching_fills_truth_is_not_quarantined(ledger):
    """A clean settlement (sum == fills-truth) is never touched — the
    quarantine only fires on a DISPUTED value."""
    ledger.record_settlement(TICKER, "F", 5, "settle")
    assert ledger.quarantine_divergent_settlements(TICKER, 5) == 0
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM settlements WHERE divergent=1").fetchone()[0] == 0
    assert ledger.book_cents() == BOOK + 5


# ── the operator key routes through Telegram ───────────────────────────────
def test_clear_cash_fatal_command_routes(ledger, surface):
    from relay_engine.ops import Telegram
    cash, gw = _protocol(ledger, surface)
    tg = Telegram(cash, send_fn=lambda m: None)
    assert "/clear_cash_fatal" in Telegram.COMMANDS
    assert tg.handle_command("/clear_cash_fatal") == "no cash fatal active"
    cash.reconcile(BOOK - FIVE, 0, 0, now=1000.0)
    cash.deny_cash()
    assert "cleared" in tg.handle_command("/clear_cash_fatal")
    assert not cash.fatal
