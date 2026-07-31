"""WO-P5 "SPEAK ITS DIALECT" — per-channel subscription with empirical
fallback, code-first error classification, first-frame unit assertion,
BOOT_LOOP tag."""

import json
import logging

import pytest

from relay_engine.errors import FatalIntegrityError
from relay_engine.feed import DegradeLadder, Feed
from relay_engine.shadow_runner import ChannelSubscriber, ShadowEngine, subscribe_cmd

TICKER = "KXBTC15M-02JAN251000-T99"


# ── §1: per-channel subscription ───────────────────────────────────────────
def test_one_cmd_per_channel():
    sub = ChannelSubscriber()
    cmds = sub.cmds_for([TICKER])
    assert len(cmds) == 4
    for cmd in cmds:
        assert len(cmd["params"]["channels"]) == 1  # a stranger can only kill itself
        assert cmd["params"]["market_tickers"] == [TICKER]
        assert "series_tickers" not in cmd["params"]
    assert {c["params"]["channels"][0] for c in cmds} == {
        "orderbook_delta", "ticker_v2", "market_lifecycle_v2", "fill"}
    assert len({c["id"] for c in cmds}) == 4  # unique cmd ids


def test_code8_falls_back_once_then_degrades():
    sub = ChannelSubscriber()
    cmds = {c["params"]["channels"][0]: c for c in sub.cmds_for([TICKER])}
    tick_id = cmds["ticker_v2"]["id"]
    # code 8 on ticker_v2 -> retry cmd with the dialect name "ticker"
    retry = sub.on_reply({"id": tick_id, "type": "error", "msg": {"code": 8}})
    assert retry is not None and retry["params"]["channels"] == ["ticker"]
    # code 8 again on the fallback -> degraded, engine continues
    assert sub.on_reply({"id": retry["id"], "type": "error", "msg": {"code": 8}}) is None
    assert "ticker_v2" in sub.degraded
    # acceptance learns the empirical vocabulary
    life_id = cmds["market_lifecycle_v2"]["id"]
    sub.on_reply({"id": life_id, "type": "subscribed"})
    assert sub.accepted["market_lifecycle_v2"] == "market_lifecycle_v2"


def test_essential_channel_failure_is_fatal():
    sub = ChannelSubscriber()
    cmds = {c["params"]["channels"][0]: c for c in sub.cmds_for([TICKER])}
    ob_id = cmds["orderbook_delta"]["id"]
    # orderbook_delta has no alias: code 8 = no book, no engine
    with pytest.raises(FatalIntegrityError, match="essential channel"):
        sub.on_reply({"id": ob_id, "type": "error", "msg": {"code": 8}})


def test_nonessential_nonvocab_error_degrades_not_fatal():
    sub = ChannelSubscriber()
    cmds = {c["params"]["channels"][0]: c for c in sub.cmds_for([TICKER])}
    fill_id = cmds["fill"]["id"]
    assert sub.on_reply({"id": fill_id, "type": "error", "msg": {"code": 3}}) is None
    assert "fill" in sub.degraded


def test_accepted_vocabulary_logged_once(caplog):
    sub = ChannelSubscriber()
    cmds = sub.cmds_for([TICKER])
    with caplog.at_level(logging.INFO, logger="relay.shadow"):
        for c in cmds:
            sub.on_reply({"id": c["id"], "type": "subscribed"})
    vocab_lines = [r for r in caplog.records if "WS channels accepted" in r.message]
    assert len(vocab_lines) == 1


def test_rollover_reuses_learned_dialect():
    sub = ChannelSubscriber()
    for c in sub.cmds_for([TICKER]):
        if c["params"]["channels"] == ["ticker_v2"]:
            retry = sub.on_reply({"id": c["id"], "type": "error", "msg": {"code": 8}})
            sub.on_reply({"id": retry["id"], "type": "subscribed"})
        else:
            sub.on_reply({"id": c["id"], "type": "subscribed"})
    assert sub.accepted["ticker_v2"] == "ticker"
    # a rollover subscribe speaks the learned dialect directly
    cmds2 = {c["params"]["channels"][0] for c in sub.cmds_for(["KXBTC15M-NEXT"])}
    assert "ticker" in cmds2 and "ticker_v2" not in cmds2


# ── §2: code-first classification ──────────────────────────────────────────
def test_config_error_containing_word_order_still_fatals():
    feed = Feed(DegradeLadder())
    feed.note_sent('{"cmd":"subscribe"}')
    with pytest.raises(FatalIntegrityError):
        feed.handle_frame(json.dumps({
            "type": "error", "msg": {"code": 8, "msg": "subscription order invalid"}}))


def test_per_order_code_never_fatals_regardless_of_text():
    feed = Feed(DegradeLadder())
    feed.handle_frame(json.dumps({
        "type": "error", "msg": {"code": 25, "msg": "anything at all"}}))
    assert feed.order_errors == 1


def test_unknown_code_fatals_codeless_keywords_tiebreak():
    feed = Feed(DegradeLadder())
    with pytest.raises(FatalIntegrityError):
        feed.handle_frame(json.dumps({"type": "error", "msg": {"code": 999}}))
    feed2 = Feed(DegradeLadder())
    feed2.handle_frame(json.dumps({"type": "error", "msg": {"msg": "order rejected"}}))
    assert feed2.order_errors == 1  # codeless + order keyword -> routed


# ── §3: first-frame unit assertion ─────────────────────────────────────────
def snapshot(levels_yes, levels_no):
    return json.dumps({"type": "orderbook_snapshot",
                       "msg": {"market_ticker": TICKER,
                               "yes": levels_yes, "no": levels_no}})


def test_units_cents_ints(caplog):
    feed = Feed(DegradeLadder())
    with caplog.at_level(logging.INFO, logger="relay.feed"):
        feed.handle_frame(snapshot([[45, 100]], [[30, 50]]), now=1.0)
    assert feed.book_units == "cents"
    assert feed.book(TICKER).best_yes_bid() == 45
    assert any("WS book units: cents" in r.message for r in caplog.records)


def test_units_dollar_strings():
    feed = Feed(DegradeLadder())
    feed.handle_frame(snapshot([["0.45", "100.00"]], [["0.30", "50.00"]]), now=1.0)
    assert feed.book_units == "dollars"
    b = feed.book(TICKER)
    assert b.best_yes_bid() == 45 and b.best_no_bid() == 30
    # deltas parse through the SAME learned units
    feed.handle_frame(json.dumps({"type": "orderbook_delta",
                                  "msg": {"market_ticker": TICKER, "side": "yes",
                                          "price": "0.46", "delta": 10}}), now=2.0)
    assert b.best_yes_bid() == 46


def test_units_garbage_is_fatal_with_echo():
    feed = Feed(DegradeLadder())
    with pytest.raises(FatalIntegrityError, match="neither"):
        feed.handle_frame(snapshot([["banana", 1]], []), now=1.0)


def test_units_reassert_per_connection():
    feed = Feed(DegradeLadder())
    feed.handle_frame(snapshot([[45, 100]], [[30, 50]]), now=1.0)
    assert feed.book_units == "cents"
    feed.socket_died("drill")
    assert feed.book_units is None  # next connection re-asserts


# ── §4: BOOT_LOOP tag ──────────────────────────────────────────────────────
def test_boot_loop_alert_with_last_fatal():
    engine = ShadowEngine(db_path=":memory:")
    alerts = []
    engine.telegram.send = alerts.append
    engine.record_fatal("essential channel 'orderbook_delta' rejected")
    for _ in range(10):
        engine.ledger.record_boot()
    engine.boot()  # 11th boot in the hour
    boot_loops = [a for a in alerts if "BOOT_LOOP" in a]
    assert len(boot_loops) == 1
    assert "11 boots" in boot_loops[0]
    assert "orderbook_delta" in boot_loops[0]  # the last FATAL rides the alert