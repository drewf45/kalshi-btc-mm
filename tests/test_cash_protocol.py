"""VERIFY GATE 2 (part 2): synthetic ±$5 delta test passes BOTH branches —
confirm resumes / deny stays FATAL. Plus quiescence and 30-min silence."""

import pytest

from relay_engine import config
from relay_engine.errors import FatalIntegrityError

BOOK = 10_000  # conftest baseline, cents
FIVE = 500


def test_quiescence_defers_with_inflight_orders(cash):
    assert cash.reconcile(BOOK - FIVE, in_flight_orders=1, unsettled_fills=0) == "DEFERRED"
    assert cash.reconcile(BOOK - FIVE, in_flight_orders=0, unsettled_fills=2) == "DEFERRED"
    assert not cash.entries_halted


def test_clean_reconcile(cash):
    assert cash.reconcile(BOOK, 0, 0) == "CLEAN"


def test_negative_five_confirm_branch_resumes(cash, ledger):
    # -$5: halt + prompt with breakdown
    assert cash.reconcile(BOOK - FIVE, 0, 0, now=1000.0) == "PROMPTED"
    assert cash.entries_halted
    assert any("HALTED" in a and "delta=-500c" in a for a in cash.test_alerts)
    # /confirm_cash -> re-baseline + CASH_MOVEMENTS row + resume
    assert cash.confirm_cash(now=1010.0)
    assert not cash.entries_halted
    assert not cash.fatal
    row = ledger.db.execute(
        "SELECT amount_cents, kind, confirmed_by FROM cash_movements"
        " WHERE confirmed_by='/confirm_cash'").fetchone()
    assert row == (-FIVE, "CONFIRMED_WITHDRAWAL", "/confirm_cash")
    assert ledger.book_cents() == BOOK - FIVE
    assert cash.reconcile(BOOK - FIVE, 0, 0, now=1020.0) == "CLEAN"


def test_negative_five_deny_branch_stays_fatal(cash):
    assert cash.reconcile(BOOK - FIVE, 0, 0, now=1000.0) == "PROMPTED"
    cash.deny_cash()
    assert cash.fatal
    assert cash.entries_halted
    assert any("FATAL" in a for a in cash.test_alerts)
    # stays stopped: nothing resumes it
    assert cash.reconcile(BOOK - FIVE, 0, 0, now=1010.0) == "FATAL"
    assert not cash.confirm_cash(now=1020.0)
    assert cash.fatal


def test_thirty_minute_silence_goes_fatal(cash):
    assert cash.reconcile(BOOK - FIVE, 0, 0, now=1000.0) == "PROMPTED"
    # 29 minutes: still prompted
    assert cash.reconcile(BOOK - FIVE, 0, 0,
                          now=1000.0 + config.CASH_DENY_TIMEOUT_SECONDS - 60) == "PROMPTED"
    # 30 minutes: FATAL, stays stopped
    assert cash.reconcile(BOOK - FIVE, 0, 0,
                          now=1000.0 + config.CASH_DENY_TIMEOUT_SECONDS) == "FATAL"
    assert cash.fatal


def test_positive_five_confirms_without_halt(cash, ledger):
    assert cash.reconcile(BOOK + FIVE, 0, 0, now=1000.0) == "CONFIRMED_POSITIVE"
    assert not cash.entries_halted
    row = ledger.db.execute(
        "SELECT amount_cents, kind, confirmed_by FROM cash_movements"
        " WHERE confirmed_by='auto_positive'").fetchone()
    assert row == (FIVE, "CONFIRMED_DEPOSIT", "auto_positive")
    assert ledger.book_cents() == BOOK + FIVE


def test_telegram_commands_route_and_refuse_orders(cash):
    from relay_engine.ops import Telegram
    tg = Telegram(cash, send_fn=lambda m: None)
    cash.reconcile(BOOK - FIVE, 0, 0, now=1000.0)
    assert tg.handle_command("/confirm_cash") == "ok"
    # order-shaped chatter is refused: accounting commands only
    assert "accounting commands only" in tg.handle_command("/buy KXBTC15M 5")
    assert "accounting commands only" in tg.handle_command("/paid")  # waterfall command never existed


def test_ledger_invariant_and_drawdown(ledger):
    ledger.check_invariant(BOOK, 0)
    ledger.check_invariant(BOOK - 100, 100)  # within ± in-flight
    with pytest.raises(FatalIntegrityError):
        ledger.check_invariant(BOOK - 300, 100)
    # drawdown rail: absolute-floor semantics while book <= $50... conftest book is $100
    assert ledger.drawdown_floor_cents() == int(config.DRAWDOWN_ABSOLUTE_FLOOR_USD * 100)
    assert not ledger.drawdown_breached()


def test_monthly_true_up_line(cash, ledger):
    line = cash.monthly_true_up_line(BOOK - 25)
    assert "TRUE-UP" in line and "diff=25c" in line
