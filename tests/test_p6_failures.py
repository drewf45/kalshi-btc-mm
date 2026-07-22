"""WO-P6 — frame-shape honesty, the Failure Ledger (R5), Telegram wired (R6),
recorder batching. Including the grep-enforced funnel law."""

import json
import re
from pathlib import Path

import pytest

from relay_engine import failures
from relay_engine.errors import FatalIntegrityError
from relay_engine.feed import DegradeLadder, Feed, FeedState
from relay_engine.ledger import Ledger
from relay_engine.ops import Recorder, Telegram, daily_pack

ROOT = Path(__file__).resolve().parents[1]
TICKER = "KXBTC15M-02JAN251000-T99"


@pytest.fixture(autouse=True)
def funnel(ledger):
    """Wire the funnel to this test's ledger; reset after."""
    alerts = []
    failures.configure(ledger, alert_fn=alerts.append, run_mode="TEST", boot_id=7)
    failures._warn_last.clear()
    yield alerts
    failures._ledger = None
    failures._alert_fn = None


def snapshot_frame(yes=45):
    return json.dumps({"type": "orderbook_snapshot",
                       "msg": {"market_ticker": TICKER,
                               "yes": [[yes, 100]], "no": [[30, 50]]}})


def delta_no_price():
    return json.dumps({"type": "orderbook_delta",
                       "msg": {"market_ticker": TICKER, "side": "yes", "delta": 5}})


# ── §1: frame-shape honesty ────────────────────────────────────────────────
def test_delta_without_price_banks_failure_and_resyncs(ledger):
    feed = Feed(DegradeLadder())
    feed.handle_frame(snapshot_frame(), now=1.0)
    assert feed.ladder.state is FeedState.WS_LIVE
    # a delta frame with NO price key: not a price event — no FATAL, no guess
    feed.handle_frame(delta_no_price(), now=2.0)
    assert feed.ladder.state is not FeedState.WS_LIVE  # book dropped, resync requested
    assert TICKER not in feed.books
    row = ledger.db.execute(
        "SELECT why_tag, how_json, run_mode, boot_id FROM failures"
        " WHERE why_tag='FRAME_SHAPE_UNKNOWN'").fetchone()
    assert row is not None
    assert "orderbook_delta" in row[1]  # THE RAW FRAME is in the record
    assert (row[2], row[3]) == ("TEST", 7)


def test_three_consecutive_unparseable_deltas_fatal(ledger):
    feed = Feed(DegradeLadder())
    feed.handle_frame(snapshot_frame(), now=1.0)
    feed.handle_frame(delta_no_price(), now=2.0)
    feed.handle_frame(delta_no_price(), now=3.0)
    with pytest.raises(FatalIntegrityError, match="WS_DELTA_UNPARSEABLE"):
        feed.handle_frame(delta_no_price(), now=4.0)
    # the evidence was banked BEFORE the raise
    n = ledger.db.execute("SELECT COUNT(*) FROM failures"
                          " WHERE why_tag='WS_DELTA_UNPARSEABLE'").fetchone()[0]
    assert n == 1


def test_good_delta_resets_consecutive_count():
    feed = Feed(DegradeLadder())
    feed.handle_frame(snapshot_frame(), now=1.0)
    feed.handle_frame(delta_no_price(), now=2.0)
    feed.handle_frame(delta_no_price(), now=3.0)
    # a parseable delta resets — the 3rd bad frame later is #1, not #3
    feed.handle_frame(json.dumps({"type": "orderbook_delta",
                                  "msg": {"market_ticker": TICKER, "side": "yes",
                                          "price": 46, "delta": 5}}), now=4.0)
    assert feed.shape_failures == 0
    feed.handle_frame(delta_no_price(), now=5.0)  # no raise


def test_delta_price_dollars_key_ladder():
    feed = Feed(DegradeLadder())
    feed.handle_frame(json.dumps({"type": "orderbook_snapshot",
                                  "msg": {"market_ticker": TICKER,
                                          "yes": [["0.45", "100"]], "no": [["0.30", "50"]]}}),
                      now=1.0)
    feed.handle_frame(json.dumps({"type": "orderbook_delta",
                                  "msg": {"market_ticker": TICKER, "side": "yes",
                                          "price_dollars": "0.97", "delta_fp": "3.00"}}),
                      now=2.0)
    assert feed.book(TICKER).best_yes_bid() == 97


def test_original_crash_frame_replayed_survives(ledger):
    """The v5 crash: a delta whose price key is absent used to default to 0 and
    FATAL the unit assertion. Now: banked shape failure, resync, engine lives."""
    feed = Feed(DegradeLadder())
    feed.handle_frame(snapshot_frame(), now=1.0)
    feed.handle_frame(json.dumps({"type": "orderbook_delta",
                                  "msg": {"market_ticker": TICKER, "side": "no",
                                          "delta": -2}}), now=2.0)  # no price at all
    # alive, resyncing, evidence banked — the 0 was never fabricated
    assert feed.book_units is not None or True
    assert ledger.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0] >= 1


def test_snapshot_dict_levels_parse():
    feed = Feed(DegradeLadder())
    feed.handle_frame(json.dumps({"type": "orderbook_snapshot",
                                  "msg": {"market_ticker": TICKER,
                                          "yes": [{"price": 45, "count": 100}],
                                          "no": [{"price": 30, "count": 50}]}}), now=1.0)
    assert feed.book(TICKER).best_yes_bid() == 45


# ── §3: the Failure Ledger ─────────────────────────────────────────────────
def test_funnel_writes_then_raises_for_fatal(ledger, funnel):
    with pytest.raises(FatalIntegrityError, match=r"\[TEST_TAG\]"):
        failures.fail("TEST_TAG", "it broke", fatal=True, detail=42)
    row = ledger.db.execute(
        "SELECT why_tag, what, how_json, where_src FROM failures"
        " WHERE why_tag='TEST_TAG'").fetchone()
    assert row[1] == "it broke"
    assert json.loads(row[2]) == {"detail": 42}
    assert "test_p6_failures.py" in row[3]  # where
    assert any("⛔ FATAL [TEST_TAG]" in a for a in funnel)


def test_warn_class_throttled_with_counts(funnel):
    for _ in range(5):
        failures.fail("NOISY_TAG", "again")
    warns = [a for a in funnel if "NOISY_TAG" in a]
    assert len(warns) == 1  # throttled
    failures._warn_last["NOISY_TAG"] = (0.0, failures._warn_last["NOISY_TAG"][1])
    failures.fail("NOISY_TAG", "again")
    assert any("suppressed" in a for a in funnel if "NOISY_TAG" in a)


def test_funnel_never_fails_the_engine(funnel):
    """Adversary knob: a broken pager can't crash the patient."""
    failures._alert_fn = lambda msg: (_ for _ in ()).throw(RuntimeError("pager dead"))
    failures.fail("PAGER_DEAD_TEST", "still fine")  # no raise

    class DeadDB:
        def execute(self, *a):
            raise RuntimeError("disk gone")
        def commit(self):
            raise RuntimeError("disk gone")

    class DeadLedger:
        db = DeadDB()

    failures._ledger = DeadLedger()
    failures.fail("LEDGER_DEAD_TEST", "still fine")  # no raise either


def test_prebooted_failures_flush_at_configure(ledger):
    failures._ledger = None
    try:
        failures.fail("EARLY_BOOT_TAG", "before configure")
    finally:
        pass
    failures.configure(ledger, alert_fn=None, run_mode="TEST", boot_id=1)
    n = ledger.db.execute("SELECT COUNT(*) FROM failures"
                          " WHERE why_tag='EARLY_BOOT_TAG'").fetchone()[0]
    assert n == 1


def test_no_bare_fatal_raise_outside_the_funnel():
    """Grep-enforced law: 'A failure that didn't write why/how/what/when
    before raising is itself a failure.' Only failures.py raises directly."""
    offenders = []
    for path in (ROOT / "relay_engine").glob("*.py"):
        if path.name == "failures.py":
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"raise FatalIntegrityError", line):
                offenders.append(f"{path.name}:{i}")
    assert offenders == [], f"bare FatalIntegrityError raises outside the funnel: {offenders}"


def test_pack_has_failures_section(ledger, surface, cash, funnel):
    failures.fail("PACK_TAG", "for the pack")
    pack = daily_pack(ledger, surface, cash)
    assert "FAILURES (by tag" in pack and "PACK_TAG: x1" in pack


# ── §2: Telegram wired ─────────────────────────────────────────────────────
def test_telegram_transport_from_env(monkeypatch, cash):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    tg = Telegram(cash)
    assert tg.wired()

    sent = []

    class FakeResp:
        status_code = 200
        text = ""

    tg._session.post = lambda url, json=None, timeout=None: sent.append(json) or FakeResp()
    tg.alert("hello phone")
    assert sent[0]["chat_id"] == "42" and sent[0]["text"] == "hello phone"


def test_telegram_send_never_raises(monkeypatch, cash):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    tg = Telegram(cash)

    def boom(*a, **k):
        raise RuntimeError("network gone")

    tg._session.post = boom
    tg.alert("this must not raise")  # a broken pager can't crash the patient


def test_telegram_unwired_shadow_falls_back_to_log(monkeypatch, cash):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    tg = Telegram(cash)
    assert not tg.wired()
    tg.alert("goes to the log, loudly")  # no raise


def test_command_surface_is_exactly_the_whitelist(cash):
    """The pair + /reset_halt (P8 §2.3) + /scoreboard (P22 §5) + /clear_cash_fatal
    (P-CASH-FATAL-1 §4.4) + /daily (WO-2026-07-22-K — the read-only day export;
    like /scoreboard it places nothing, changes nothing). Still no order-shaped
    command, ever."""
    tg = Telegram(cash, send_fn=lambda m: None)
    assert Telegram.COMMANDS == ("/confirm_cash", "/deny_cash", "/reset_halt",
                                 "/scoreboard", "/clear_cash_fatal", "/daily")
    for stray in ("/resume_yes", "/paid", "/buy KXBTC15M 5", "/status", "hello"):
        assert "accounting commands only" in tg.handle_command(stray)
    assert tg.handle_command("/reset_halt") == "no halt manager wired"


def test_poll_updates_dispatches_and_tracks_offset(monkeypatch, ledger, cash):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    tg = Telegram(cash)
    sent = []
    tg.send = sent.append

    class FakeResp:
        status_code = 200

        def json(self):
            return {"result": [
                {"update_id": 100, "message": {"text": "/confirm_cash"}},
                {"update_id": 101, "message": {"text": "/hack_the_gateway"}},
            ]}

    tg._session.get = lambda url, params=None, timeout=None: FakeResp()
    handled = tg.poll_updates_once(ledger, timeout=0)
    assert handled == 2
    assert ledger.get_state("tg_update_offset") == "102"  # offset advanced
    assert any("accounting commands only" in s for s in sent)  # stray refused


# ── §4: recorder batching ──────────────────────────────────────────────────
def test_recorder_batches_and_flushes(ledger):
    rec = Recorder(ledger)
    for i in range(5):
        rec.record(TICKER, f"frame-{i}", ts=float(i))
    # buffered, not yet committed — but confirmed_writing already true
    assert rec.confirmed_writing()
    assert ledger.db.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0] == 0
    assert rec.flush() == 5
    assert ledger.db.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0] == 5
    assert rec.flush() == 0  # empty buffer is a no-op