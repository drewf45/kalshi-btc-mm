"""WO-P17 FINAL "SHOW UP FOR EVERY MARKET" — the terminal-row lattice (§1),
the leash counts late truths (§2), supervision escalates (§3), walls say
their names (§4), rich why-tags (§5), and the window contract end-to-end (§6).
Doctrine: showing up is mandatory; trading is earned."""

import asyncio
import json

import pytest

from relay_engine import config, failures
from relay_engine.shadow_runner import ShadowEngine, supervise

TICKER1 = "KXBTC15M-02JAN251000-T99"
TICKER2 = "KXBTC15M-02JAN251015-T99"
TICKER3 = "KXBTC15M-02JAN251030-T99"


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "p17.db"))
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


def feed_snapshot(engine, ticker, yes, no, now):
    engine.feed.handle_frame(json.dumps(
        {"type": "orderbook_snapshot",
         "msg": {"market_ticker": ticker, "yes": [[yes, 10]], "no": [[no, 10]]}}),
        now=now)


# ── §2: the leash counts late truths ───────────────────────────────────────
def test_late_settlement_marks_receipt_and_retroactive_halt(engine):
    """Two red windows, the second settling LATE: the receipt says so and the
    halt fires retroactively with the named page."""
    from relay_engine.lanes import infer_close_ts_from_ticker
    for i, mkt in enumerate((TICKER1, TICKER2)):
        close = infer_close_ts_from_ticker(mkt)
        engine.market_meta[mkt] = {"close_ts": close}
        engine.econ.open_bracket(mkt, 10_000 - i, now=close - 100)
        engine.ledger.record_fill(mkt, "F", "yes", "ENTRY", 99, 24, "PROBE")
        # held to settlement (no cut) — settle NO 20 min AFTER close (late by the
        # 300s rule): a full-clip −2376/window F tail (24 lots × 99c), so two
        # windows (−4752) are a genuine TAIL CLUSTER that crosses F's W2a bound
        # at the ~$100 book (WO-2026-07-27-W: f_halt_bound_c(10000) = 3600 =
        # 1.5 × one full F loss). One ordinary tail alone would NOT halt.
        engine.settle_traded_market(mkt, settled_yes=False, now=close + 1200)
    # WO-2026-07-24-C: the rate halt is per-lane and MONEY-based — both windows
    # traded only F, and F's own drawdown crosses the tail-cluster bound. The
    # retroactive page names the lane. (F halts only itself.)
    assert engine.econ.halted_lanes() == {"F"}
    late_receipts = [m for m in engine.telegram_sent
                     if "📊" in m and "(settled late — books healed)" in m]
    assert len(late_receipts) == 2
    assert any(m.startswith("⛔ F RATE HALT (retroactive:")
               for m in engine.telegram_sent)


def test_boot_tape_states_the_rail_in_words():
    from relay_engine.boot import boot_tape
    from relay_engine.ledger import BootCaps
    tape_small = "\n".join(boot_tape(boot_caps=BootCaps(3_500, 350)))
    assert "rail: DISARMED" in tape_small
    assert "the rate halt is the only engine stop" in tape_small
    tape_big = "\n".join(boot_tape(boot_caps=BootCaps(10_000, 1_000)))
    assert "rail: ARMED" in tape_big and "headroom $75.00" in tape_big


# ── §3: supervision escalates instead of spinning ──────────────────────────
def test_same_error_x5_pages_once_then_paces(engine, monkeypatch):
    import relay_engine.shadow_runner as sr
    monkeypatch.setattr(sr, "TASK_STUCK_BACKOFF_S", 0.01)
    stop = asyncio.Event()
    calls = []

    async def always_same_error():
        calls.append(1)
        if len(calls) >= 8:
            stop.set()
            return
        raise RuntimeError("same wall every time")

    asyncio.run(supervise("settle", always_same_error, stop, engine,
                          backoff_s=0.01))
    stuck_pages = [m for m in engine.telegram_sent if m.startswith("⚠ TASK_STUCK")]
    assert len(stuck_pages) == 1                      # ONE page, not a storm
    assert "streak & brackets FROZEN" in stuck_pages[0]  # §2.2 consequence
    assert fail_rows(engine, "TASK_STUCK") == 1
    assert len(calls) == 8                            # still retrying, paced


def test_different_error_resets_the_stuck_count(engine):
    stop = asyncio.Event()
    calls = []

    async def alternating():
        calls.append(1)
        if len(calls) >= 8:
            stop.set()
            return
        raise RuntimeError(f"error variant {len(calls) % 2}")

    asyncio.run(supervise("spot", alternating, stop, engine, backoff_s=0.01))
    assert not any(m.startswith("⚠ TASK_STUCK") for m in engine.telegram_sent)


# ── §4: walls say their own names ──────────────────────────────────────────
def test_wall_tags_are_specific():
    import inspect

    from relay_engine import gateway
    src = inspect.getsource(gateway)
    # P27 §1a: "DEPTH" left with the tier wall — the survivors stay specific
    for specific in ("BUDGET", "NET_RISK", "DOLLAR_RISK",
                     "WRONG_WAY", "TAKER_ENTRY", "FLIP_UNPAIRED"):
        assert f'"{specific}"' in src, specific
    for ambiguous in ("PCT_OF_BOOK", "SIZING_TIER", "NET_RISK_CAP",
                      "AT_RISK_CAP", "REJECT_WRONG_WAY_TICK",
                      "REJECT_TAKER_ENTRY", "REJECT_FLIP_UNPAIRED"):
        assert f'"{ambiguous}"' not in src, ambiguous


# ── §5: rich why-tags on the proven lanes ──────────────────────────────────
def test_f_entry_why_carries_the_gate_values(engine):
    from relay_engine.book import touch_view
    from relay_engine.lanes import infer_close_ts_from_ticker
    close = infer_close_ts_from_ticker(TICKER1)
    engine.market_meta[TICKER1] = {"close_ts": close,
                                   "boundary_lo": 117_000.0,
                                   "boundary_hi": 118_000.0}
    feed_snapshot(engine, TICKER1, 97, 2, now=close - 120)
    engine.cycle([TICKER1], now=close - 120, spot=118_500.0)
    row = engine.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE lane='F' AND state='PROPOSED'"
    ).fetchone()
    assert row is not None, "F never proposed"
    detail = row[0]
    for token in ("why=F tier97", "band", "ΔP0.970", "spot118,500", "dist"):
        assert token in detail, (token, detail)


# ── §6: THE WINDOW CONTRACT, END TO END ────────────────────────────────────
def test_window_lifecycle_end_to_end(engine, tmp_path):
    """Three windows: pass-only · pass-early-enter-late · spanning a restart.
    One PASS terminal, one ENTERED→SETTLED story with receipt, one
    GAP_RESTART row, zero FATALs, streak correct, next window always armed."""
    from relay_engine.lanes import infer_close_ts_from_ticker

    # W1 — pass-only: a mid-priced book is not F's territory; the PASS is
    # the discipline working, written as terminal at CLOSE.
    c1 = infer_close_ts_from_ticker(TICKER1)
    engine.market_meta[TICKER1] = {"close_ts": c1}
    feed_snapshot(engine, TICKER1, 45, 54, now=c1 - 120)
    engine.cycle([TICKER1], now=c1 - 120)
    engine.on_market_closed(TICKER1)
    w1_pass = engine.ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE market=? AND lane='F'"
        " AND state='PASS' AND terminal=1", (TICKER1,)).fetchone()[0]
    assert w1_pass == 1

    # W2 — pass early, enter late (F's designed patience): the 9:30 FATAL
    # shape, now the healthy path. PASS provisionally at T-120 on a mid
    # book; the book moves to 97 by T-100; F enters; fill books; settles.
    c2 = infer_close_ts_from_ticker(TICKER2)
    engine.market_meta[TICKER2] = {"close_ts": c2}
    feed_snapshot(engine, TICKER2, 45, 54, now=c2 - 170)
    engine.cycle([TICKER2], now=c2 - 170)          # F passes (provisional)
    feed_snapshot(engine, TICKER2, 97, 2, now=c2 - 100)
    engine.cycle([TICKER2], now=c2 - 100)          # F enters late
    oid = [o for o in engine.gateway.order_index
           if engine.gateway.order_index[o].lane == "F"]
    assert oid, "F never entered in W2"
    engine.gateway.on_fill(oid[-1])
    engine.ledger.record_fill(TICKER2, "F", "yes", "ENTRY", 97, 1, "PROBE")
    engine.econ.open_bracket(TICKER2, 10_000, now=c2 - 100)
    engine.settle_traded_market(TICKER2, settled_yes=True, now=c2 + 20)
    states = {s for (s,) in engine.ledger.db.execute(
        "SELECT state FROM surface_rows WHERE market=? AND lane='F'"
        " AND terminal=1", (TICKER2,)).fetchall()}
    assert states == {"SETTLED"}                   # never a PASS terminal
    assert any("📊" in m and TICKER2 in m for m in engine.telegram_sent)
    assert engine.econ.streak == 0                 # +3¢ window: streak clean

    # W3 — spanning a restart: rows written, no terminal, then a NEW engine
    # on the SAME DB boots and marks the evidence hole.
    c3 = infer_close_ts_from_ticker(TICKER3)
    engine.market_meta[TICKER3] = {"close_ts": c3}
    feed_snapshot(engine, TICKER3, 45, 54, now=c3 - 120)
    engine.cycle([TICKER3], now=c3 - 120)          # interim rows only
    db_path = str(tmp_path / "p17.db")
    e2 = ShadowEngine(db_path=db_path)
    e2.telegram_sent = []
    e2.telegram.send = e2.telegram_sent.append
    e2.boot()                                      # gap_restart_scan runs here
    gaps = e2.ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE market=? AND"
        " state='GAP_RESTART'", (TICKER3,)).fetchone()[0]
    assert gaps >= 1

    # zero FATALs anywhere in the story
    assert fail_rows(engine, "DUPLICATE_TERMINAL_ROW") == 0


def test_silent_window_pages(engine):
    engine._window_of["KXBTC15M-NEVER-EVALUATED"] = "w-silent"
    engine.close_window("KXBTC15M-NEVER-EVALUATED")
    assert fail_rows(engine, "SILENT_WINDOW") == 1


def test_settle_idempotent_and_bracket_restored_across_restart(engine, tmp_path):
    """§1.3: the stuck-0930 shape heals — an open bracket survives restart
    and the retried settlement books exactly once."""
    from relay_engine.lanes import infer_close_ts_from_ticker
    close = infer_close_ts_from_ticker(TICKER1)
    engine.market_meta[TICKER1] = {"close_ts": close}
    engine.econ.open_bracket(TICKER1, 10_000, now=close - 100)
    engine.ledger.record_fill(TICKER1, "F", "yes", "ENTRY", 61, 1, "PROBE")

    e2 = ShadowEngine(db_path=str(tmp_path / "p17.db"))
    e2.telegram_sent = []
    e2.telegram.send = e2.telegram_sent.append
    e2.boot()   # restore_open_brackets_on_boot
    assert TICKER1 in e2.econ.open_brackets
    failures.configure(e2.ledger, alert_fn=e2.telegram.alert,
                       run_mode="TEST", boot_id=2)
    e2.market_meta[TICKER1] = {"close_ts": close}
    e2.settle_traded_market(TICKER1, settled_yes=True, now=close + 1200)
    assert any("(settled late — books healed)" in m for m in e2.telegram_sent)
    assert e2.ledger.db.execute(
        "SELECT COUNT(*) FROM settlements WHERE market=?",
        (TICKER1,)).fetchone()[0] == 1
    # idempotent: a second settle books nothing new
    e2.settle_traded_market(TICKER1, settled_yes=True, now=close + 1300)
    assert e2.ledger.db.execute(
        "SELECT COUNT(*) FROM settlements WHERE market=?",
        (TICKER1,)).fetchone()[0] == 1
    failures._ledger = None
    failures._alert_fn = None


# ── §7: the P17 expected tape grades clean on a clean DB ───────────────────
def test_p17_tape_grade_clean(engine):
    from scripts.tape_grade import CHECKS_P17, grade
    results = grade(engine.ledger.db, checks=CHECKS_P17)
    assert all(ok for _, ok, _ in results), results
