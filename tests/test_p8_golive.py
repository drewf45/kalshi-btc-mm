"""WO-P8 (+P7) — window economics from account truth, the two-strike halt,
the single book adapter property test, the replay regression (F prices real
books), reject backoff."""

import json
import random

import pytest

from relay_engine import failures
from relay_engine.book import OrderBook, touch_view
from relay_engine.errors import WallRejection
from relay_engine.lane_fh8 import favorite_side
from relay_engine.replay import replay_frames
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"
TICKER2 = "KXBTC15M-02JAN251015-T99"
TICKER3 = "KXBTC15M-02JAN251030-T99"


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "econ.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


def trade_and_settle(engine, market, pnl_cents, now=1000.0):
    """Synthetic bracket: open at book, settle with a forced pnl by writing the
    settlement into the ledger and closing at book+pnl."""
    open_value = engine.ledger.book_cents()
    engine.econ.open_bracket(market, open_value, now=now)
    engine.ledger.record_fill(market, "F", "yes", "ENTRY", 61, 1, "PROBE")
    engine.ledger.record_settlement(market, "F", pnl_cents, detail="synthetic")
    return engine.econ.close_bracket(
        market, open_value + pnl_cents, pnl_cents,
        lanes_active="F", fills_count=1, now=now + 900)


# ── §1: the bracket ────────────────────────────────────────────────────────
def test_bracket_writes_window_econ_and_pages(engine):
    pnl = trade_and_settle(engine, TICKER, +39)
    assert pnl == 39
    row = engine.ledger.db.execute(
        "SELECT open_value_cents, close_value_cents, window_pnl_cents, fills_pnl_cents"
        " FROM window_econ WHERE market=?", (TICKER,)).fetchone()
    assert row[2] == 39 and row[3] == 39
    surf = engine.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='WINDOW_ECON' AND market=?",
        (TICKER,)).fetchone()[0]
    assert json.loads(surf)["pnl"] == 39
    assert any("📊" in m and "+$0.39" in m and "rate 0/" in m
               for m in engine.telegram_sent)


def test_bracket_adjusts_for_cash_moves_inside(engine):
    open_value = engine.ledger.book_cents()
    engine.econ.open_bracket(TICKER, open_value, now=1000.0)
    engine.ledger.record_fill(TICKER, "F", "yes", "ENTRY", 61, 1, "PROBE")
    engine.ledger.record_settlement(TICKER, "F", 39, detail="win")
    # Drew deposits $5 mid-window (confirmed movement): close value jumps 539c
    engine.ledger.db.execute(
        "INSERT INTO cash_movements (ts, amount_cents, kind, confirmed_by)"
        " VALUES (?,?,?,?)", (1400.0, 500, "CONFIRMED_DEPOSIT", "/confirm_cash"))
    engine.ledger.db.commit()
    pnl = engine.econ.close_bracket(TICKER, open_value + 39 + 500, 39,
                                    lanes_active="F", fills_count=1, now=1900.0)
    assert pnl == 39  # the deposit did not masquerade as trading profit


def test_divergence_pages_with_both_numbers(engine):
    open_value = engine.ledger.book_cents()
    engine.econ.open_bracket(TICKER, open_value, now=1000.0)
    # broker says -10 but fills say +39: the legacy-class lie -> banked + paged
    engine.econ.close_bracket(TICKER, open_value - 10, +39,
                              lanes_active="F", fills_count=1, now=1900.0)
    row = engine.ledger.db.execute(
        "SELECT how_json FROM failures WHERE why_tag='WINDOW_ECON_DIVERGENCE'").fetchone()
    how = json.loads(row[0])
    assert how["broker_pnl"] == -10 and how["fills_pnl"] == 39


def test_untraded_market_writes_no_bracket(engine):
    engine.econ.close_bracket("KXBTC15M-NEVER-TRADED", 999, 0)
    assert engine.ledger.db.execute(
        "SELECT COUNT(*) FROM window_econ").fetchone()[0] == 0


# ── §2: the two-strike halt ────────────────────────────────────────────────
def test_two_negatives_halt_and_page(engine):
    trade_and_settle(engine, TICKER, -20, now=1000.0)
    assert engine.econ.streak == 1 and not engine.econ.halted()
    trade_and_settle(engine, TICKER2, -15, now=3000.0)
    assert engine.econ.streak == 2 and engine.econ.halted()
    assert "RATE_HALT" in engine.gateway.entries_halted_reasons
    page = [m for m in engine.telegram_sent if "RATE HALT" in m]
    assert page and "/reset_halt" in page[0]
    assert TICKER in page[0] and TICKER2 in page[0]


def test_win_resets_streak(engine):
    """A-PLAYER B3 OVERTURNED the forgiving win: the consecutive streak
    still resets for the packs (info), but the HALT is a RATE — a win no
    longer forgives; red-win-red is 2 losses of the last 3 and HALTS
    (two-in-a-row was never the signal; the rate is)."""
    trade_and_settle(engine, TICKER, -20, now=1000.0)
    trade_and_settle(engine, TICKER2, +5, now=3000.0)
    assert engine.econ.streak == 0
    trade_and_settle(engine, TICKER3, -8, now=5000.0)
    assert engine.econ.streak == 1
    assert engine.econ.halted()          # 2 of last 3 <= bound 2-of-4


def test_halt_persists_across_restart(engine, tmp_path):
    trade_and_settle(engine, TICKER, -20, now=1000.0)
    trade_and_settle(engine, TICKER2, -15, now=3000.0)
    assert engine.econ.halted()
    # a redeploy: a NEW engine on the SAME database
    engine2 = ShadowEngine(db_path=str(tmp_path / "econ.db"))
    engine2.boot()
    assert engine2.econ.halted()
    assert "RATE_HALT" in engine2.gateway.entries_halted_reasons
    # ...and risk reduction is still allowed (the halt stops NEW risk only)
    from relay_engine.gateway import Order
    from relay_engine import config
    b = OrderBook(market=TICKER)
    b.apply_snapshot({40: 10}, {1: 10}, ts=1.0)
    engine2.gateway.positions[("EV", TICKER, "F")] = 1
    exit_order = Order(lane="F", event="EV", market=TICKER, side="yes",
                       action="sell", price_cents=40, count=1,
                       size_tier=config.TIER_PROBE, purpose="EXIT")
    assert engine2.gateway.submit(exit_order, b).shadow  # custody continues


def test_reset_halt_is_drews_word(engine):
    trade_and_settle(engine, TICKER, -20, now=1000.0)
    trade_and_settle(engine, TICKER2, -15, now=3000.0)
    assert engine.econ.halted()
    # the Telegram command clears it — entries only, streak reset, row written
    reply = engine.telegram.handle_command("/reset_halt")
    assert "entries re-enabled" in reply and "book $" in reply
    assert not engine.econ.halted() and engine.econ.streak == 0
    assert "RATE_HALT" not in engine.gateway.entries_halted_reasons
    row = engine.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='HALT_RESET'").fetchone()
    assert "confirmed_by=telegram" in row[0]


def test_pack_shows_streak_and_resets(engine):
    from relay_engine.ops import daily_pack
    trade_and_settle(engine, TICKER, -20, now=1000.0)
    pack = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    assert "RATE-HALT: rate=1/1 (bound 2/4) halted=False" in pack


# ── P7 §1: the single book adapter, property-tested ────────────────────────
def test_touch_view_property():
    rng = random.Random(7)
    for _ in range(200):
        ob = OrderBook(market="X")
        yes = {rng.randint(1, 99): rng.randint(1, 500)
               for _ in range(rng.randint(0, 4))}
        no = {rng.randint(1, 99): rng.randint(1, 500)
              for _ in range(rng.randint(0, 4))}
        ob.apply_snapshot(yes, no, ts=1.0)
        tv = touch_view(ob)
        assert tv.yes_bid == ob.best_yes_bid()
        assert tv.no_bid == ob.best_no_bid()
        assert tv.yes_ask == ob.best_yes_ask()
        if tv.yes_bid is not None:
            assert tv.yes_bid_qty == ob.visible_depth("yes", tv.yes_bid)
        # the favorite is always the max-cost bid side, never fabricated
        side, cost, _yq, _fp = favorite_side(tv)
        if tv.yes_bid is None and tv.no_bid is None:
            assert side is None
        else:
            best = max([(tv.yes_bid or 0, "yes"), (tv.no_bid or 0, "no")])
            assert side == best[1] and int(cost) == best[0]


# ── P7: the replay regression — F prices real books ────────────────────────
def test_replay_real_shaped_tape_f_tracks_favorite():
    """A live-shaped tape (fp-dollars snapshot + deltas): F must see the
    favorite on every frame — a blind F can't trade."""
    tape = [
        json.dumps({"type": "orderbook_snapshot",
                    "msg": {"market_ticker": TICKER,
                            "yes": [["0.45", "100.00"], ["0.44", "50.00"]],
                            "no": [["0.30", "80.00"]]}}),
        json.dumps({"type": "orderbook_delta",
                    "msg": {"market_ticker": TICKER, "side": "yes",
                            "price": "0.55", "delta": "20.00"}}),
        json.dumps({"type": "orderbook_delta",
                    "msg": {"market_ticker": TICKER, "side": "no",
                            "price": "0.60", "delta": "10.00"}}),
    ]
    out = replay_frames(tape)
    assert all(side is not None for _, side, _ in out)  # never blind
    assert out[0][1] == "yes" and out[0][2] == 45.0
    assert out[1][1] == "yes" and out[1][2] == 55.0     # tracks the new touch
    assert out[2][1] == "no" and out[2][2] == 60.0      # favorite flips with the book


def test_replay_from_recorder_db(engine):
    engine.recorder.record(TICKER, json.dumps(
        {"type": "orderbook_snapshot",
         "msg": {"market_ticker": TICKER, "yes": [[97, 10]], "no": [[2, 10]]}}))
    engine.recorder.flush()
    from relay_engine.replay import replay_from_db
    out = replay_from_db(engine.ledger)
    assert out and out[0][1] == "yes" and out[0][2] == 97.0


# ── P7: reject backoff ─────────────────────────────────────────────────────
def test_reject_backoff_rests_the_market(gateway):
    b = OrderBook(market="M1")
    b.apply_snapshot({45: 100}, {1: 80}, ts=1.0)
    gateway.note_backoff("M1", seconds=30)
    from relay_engine.gateway import Order
    from relay_engine import config
    with pytest.raises(WallRejection) as e:
        gateway.submit(Order(lane="F", event="EV1", market="M1", side="yes",
                             action="buy", price_cents=45, count=1,
                             size_tier=config.TIER_PROBE, purpose="ENTRY"), b)
    assert e.value.wall == "REJECT_BACKOFF"
    # risk reduction ignores backoff
    gateway.positions[("EV1", "M1", "F")] = 1
    exit_order = Order(lane="F", event="EV1", market="M1", side="yes",
                       action="sell", price_cents=50, count=1,
                       size_tier=config.TIER_PROBE, purpose="EXIT")
    assert gateway.submit(exit_order, b).shadow
    # backoff expires
    gateway.backoff_until["M1"] = 0.0
    assert not gateway.in_backoff("M1")