"""WO-P9 "REAL NUMBERS ONLY" — ledger thread-safety, the supervised listener,
no-paper-numbers-in-live (the hole), bracket deferral, standing reconcile.
"""

import asyncio
import json
from pathlib import Path

import pytest

import relay_engine
from relay_engine import config, failures
from relay_engine.errors import FatalIntegrityError
from relay_engine.shadow_runner import ShadowEngine, supervise

TICKER = "KXBTC15M-02JAN251000-T99"
TICKER2 = "KXBTC15M-02JAN251015-T99"


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "p9.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


def fail_rows(engine, tag):
    return engine.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag=?", (tag,)).fetchone()[0]


# ── §1a: ledger thread-safety ──────────────────────────────────────────────
def test_ledger_cross_thread_get_set(ledger):
    """The exact call shape that killed the listener (to_thread → get_state):
    must work from a worker thread now."""
    async def main():
        await asyncio.to_thread(ledger.set_state, "tg_update_offset", "42")
        return await asyncio.to_thread(ledger.get_state, "tg_update_offset")
    assert asyncio.run(main()) == "42"


def test_ledger_cross_thread_writes(ledger):
    async def main():
        await asyncio.to_thread(ledger.record_fill, TICKER, "F", "yes",
                                "ENTRY", 61, 1, "PROBE")
        return await asyncio.to_thread(ledger.book_cents)
    assert asyncio.run(main()) == 10_000


# ── §1b: the supervised listener ───────────────────────────────────────────
def test_supervisor_banks_death_and_restarts(engine):
    stop = asyncio.Event()
    calls = []

    async def flaky_listener():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("sqlite cross-thread boom")
        stop.set()  # second life: run clean, end the test

    asyncio.run(supervise("listener", flaky_listener, stop, engine,
                          backoff_s=0.01))
    assert len(calls) == 2                          # it came BACK
    assert engine.task_restarts["listener"] == 1
    assert fail_rows(engine, "TG_LISTENER_DOWN") == 1   # death is DATA
    assert any("TG_LISTENER_DOWN" in m for m in engine.telegram_sent)
    assert engine.listener_status() == "ok(1 restarts)"


def test_supervisor_down_status_while_in_backoff(engine):
    engine.task_alive["listener"] = False
    engine.task_restarts["listener"] = 3
    assert engine.listener_status() == "down(3 restarts)"


def test_every_create_task_is_supervised():
    """§1b grep-test: no un-supervised task may exist in the tree."""
    root = Path(relay_engine.__file__).parent
    offenders = []
    for py in root.glob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if "create_task(" in line and "supervise(" not in line:
                offenders.append(f"{py.name}:{i}: {line.strip()}")
    assert not offenders, f"unsupervised create_task: {offenders}"


# ── §1d: the /reset_halt round trip + the refusal line ─────────────────────
def test_reset_halt_full_round_trip(engine):
    engine.ledger.set_state("two_strike_halt", "1")
    assert engine.econ.restore_halt_on_boot() is True
    assert "RATE_HALT" in engine.gateway.entries_halted_reasons
    reply = engine.telegram.handle_command("/reset_halt")
    assert "halt cleared" in reply and "book $" in reply
    assert engine.econ.halted() is False
    assert engine.econ.streak == 0
    assert "RATE_HALT" not in engine.gateway.entries_halted_reasons
    row = engine.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='HALT_RESET'").fetchone()
    assert row is not None


def test_garbage_text_gets_the_refusal_line(engine):
    reply = engine.telegram.handle_command("buy 100 yes KXBTC15M")
    assert "unknown command" in reply
    assert "/confirm_cash" in reply and "/reset_halt" in reply


# ── §2: no paper numbers in live, EVER ─────────────────────────────────────
def test_shadow_account_value_is_paper(engine):
    val, src = engine.account_value(now=1000.0)
    assert (val, src) == (10_000, "paper")


def test_live_failed_read_never_returns_ledger_book(engine, monkeypatch):
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    engine.gateway.venue_client = object()
    monkeypatch.setattr(venue, "get_balance", lambda c: (None, None))

    val, src = engine.account_value(now=1000.0)
    assert val is None and src == "venue"       # NEVER 10_000 (the paper book)
    assert fail_rows(engine, "ACCOUNT_VALUE_UNREADABLE") == 1

    # between retries: throttled, no new attempt row
    assert engine.account_value(now=1001.0) == (None, "venue")
    assert fail_rows(engine, "ACCOUNT_VALUE_UNREADABLE") == 1

    # attempts 2 and 3 across the 15s span; attempt 3 pages
    engine.account_value(now=1006.0)
    engine.account_value(now=1011.0)
    assert fail_rows(engine, "ACCOUNT_VALUE_UNREADABLE") == 3
    assert any("ACCOUNT VALUE UNREADABLE x3" in m for m in engine.telegram_sent)

    # the venue answers again: venue truth, streak reset
    monkeypatch.setattr(venue, "get_balance", lambda c: (55.0, 1.0))
    assert engine.account_value(now=1016.0) == (5_600, "venue")
    assert engine._av_fail_streak == 0


def test_live_bracket_rejects_paper_source(engine, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    with pytest.raises(FatalIntegrityError):
        engine.econ.open_bracket(TICKER, 10_000, now=1.0, source="paper")
    assert fail_rows(engine, "BRACKET_PAPER_IN_LIVE") == 1


def test_live_close_rejects_paper_source(engine, monkeypatch):
    engine.econ.open_bracket(TICKER, 10_000, now=1.0, source="paper")  # shadow open
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    with pytest.raises(FatalIntegrityError):
        engine.econ.close_bracket(TICKER, 10_039, 39, now=900.0, source="paper")


def test_deferred_open_completes_on_next_read(engine):
    engine.econ.open_bracket(TICKER, None, now=1000.0)   # live read failed
    assert TICKER in engine.econ.pending_opens
    assert engine.ledger.db.execute(
        "SELECT 1 FROM window_econ WHERE market=?", (TICKER,)).fetchone() is None

    done = engine.econ.flush_deferred(10_000, "venue", now=1012.0)
    assert done == 1 and TICKER in engine.econ.open_brackets
    src, deferred = engine.ledger.db.execute(
        "SELECT source, deferred FROM window_econ WHERE market=?",
        (TICKER,)).fetchone()
    assert src == "venue" and deferred == "open+12s"     # the deferral is stamped


def test_deferred_close_completes_without_resettling(engine):
    engine.econ.open_bracket(TICKER, 10_000, now=1000.0)
    engine.ledger.record_fill(TICKER, "F", "yes", "ENTRY", 61, 1, "PROBE")
    engine.ledger.record_settlement(TICKER, "F", 39, detail="synthetic")

    # close with a failed read: DEFERS — settlement stays booked exactly once
    assert engine.econ.close_bracket(TICKER, None, 39, now=1900.0) is None
    assert TICKER in engine.econ.pending_closes
    assert TICKER not in engine.econ.open_brackets  # sweep won't re-settle it

    engine.econ.flush_deferred(10_039, "venue", now=1945.0)
    row = engine.ledger.db.execute(
        "SELECT window_pnl_cents, source, deferred FROM window_econ"
        " WHERE market=?", (TICKER,)).fetchone()
    assert row[0] == 39 and row[1] == "venue" and "close+45s" in row[2]
    assert engine.ledger.db.execute(
        "SELECT COUNT(*) FROM settlements WHERE market=?",
        (TICKER,)).fetchone()[0] == 1
    assert engine.econ.streak == 0  # positive window applied the streak once


def test_deferred_drawdown_still_counts(engine):
    """A deferral must not launder a lane's loss out of the leash — the per-lane
    money halt (WO-2026-07-24-C) still fires when the deferred close flushes."""
    HALF = config.rate_halt_drawdown_c(10_000) // 2 + 50  # WO-2026-07-24-G: book-derived (~$100 engine book)
    for i, mkt in enumerate((TICKER, TICKER2)):
        engine.econ.open_bracket(mkt, 10_000 - i * HALF, now=1000.0)
        engine.econ.close_bracket(mkt, None, -HALF, now=1900.0,
                                  per_lane={"FLIP": -HALF})
        engine.econ.flush_deferred(10_000 - i * HALF - HALF, "venue", now=1930.0)
    assert "FLIP" in engine.econ.halted_lanes()  # two −70 = −140 < −120: the leash


# ── §3: standing live reconcile ────────────────────────────────────────────
def test_standing_reconcile_routes_drift_to_cash_protocol(engine, monkeypatch):
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    engine.gateway.venue_client = object()
    monkeypatch.setattr(venue, "get_balance", lambda c: (90.0, 0.0))  # book $100

    assert engine.standing_reconcile(now=2000.0) == "PROMPTED"
    assert engine.cash.entries_halted is True
    assert any("CASH DELTA NEGATIVE" in m for m in engine.telegram_sent)


def test_standing_reconcile_defers_under_inflight(engine, monkeypatch):
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    engine.gateway.venue_client = object()
    monkeypatch.setattr(venue, "get_balance", lambda c: (90.0, 0.0))
    engine.gateway.resting["oid"] = object()  # in-flight — quiescence law
    assert engine.standing_reconcile(now=2000.0) == "DEFERRED"
    assert engine.cash.entries_halted is False


def test_standing_reconcile_unreadable_when_venue_fails(engine, monkeypatch):
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    engine.gateway.venue_client = object()
    monkeypatch.setattr(venue, "get_balance", lambda c: (None, None))
    assert engine.standing_reconcile(now=2000.0) == "UNREADABLE"
    assert engine.cash.entries_halted is False  # no paper-number verdicts
