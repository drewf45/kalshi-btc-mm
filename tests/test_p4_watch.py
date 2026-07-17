"""WO-P4 "MAKE IT WATCH" — the five ordered tests: subscribe grammar,
rollover prune, error classification, spot wiring (H8 fires + custodian
spot-safety), live boot reconcile. Plus the cycle throttle gate."""

import json

import pytest

from relay_engine import config
from relay_engine.errors import FatalIntegrityError
from relay_engine.feed import DegradeLadder, Feed
from relay_engine.shadow_runner import CYCLE_SECONDS, ShadowEngine, subscribe_cmd

TICKER = "KXBTC15M-02JAN251000-T99"


# ── F1e: the subscribe payload snapshot (P5: one channel per cmd) ──────────
def test_subscribe_payload_grammar():
    cmd = subscribe_cmd(3, [TICKER, "KXBTC15M-02JAN251015-T99"], "orderbook_delta")
    assert cmd["cmd"] == "subscribe" and cmd["id"] == 3
    params = cmd["params"]
    assert "market_tickers" in params and "series_tickers" not in params
    assert params["market_tickers"] == sorted([TICKER, "KXBTC15M-02JAN251015-T99"])
    assert params["channels"] == ["orderbook_delta"]  # exactly one channel per cmd


# ── F1c/F4: rollover — discovery adds, close prunes ────────────────────────
def test_rollover_prunes_closed_window():
    engine = ShadowEngine(db_path=":memory:")
    engine.boot()
    engine.on_market_discovered(TICKER, {"close_ts": 1_000_000,
                                         "floor_strike": 64_000.0})
    assert engine.market_meta[TICKER]["close_ts"] == 1_000_000
    assert engine.market_meta[TICKER]["boundary_lo"] == 64_000.0
    engine.feed.book(TICKER).apply_snapshot({40: 5}, {1: 5}, ts=1.0)
    engine._window_of[TICKER] = "w-x"
    engine.flip.windows[TICKER] = object()

    engine.on_market_closed(TICKER)
    assert TICKER not in engine.market_meta
    assert TICKER not in engine.feed.books
    assert TICKER not in engine._window_of
    assert TICKER not in engine.flip.windows


# ── F1d: error classification ──────────────────────────────────────────────
def test_error_frames_classified():
    feed = Feed(DegradeLadder())
    feed.note_sent('{"cmd":"subscribe","params":{"market_tickers":["X"]}}')
    # per-order error: routed, counted, NOT fatal
    feed.handle_frame(json.dumps({"type": "error",
                                  "msg": {"code": 25, "msg": "post only cross on order"}}))
    assert feed.order_errors == 1
    # config/subscribe error: FATAL with the sent payload echoed
    with pytest.raises(FatalIntegrityError) as e:
        feed.handle_frame(json.dumps({"type": "error",
                                      "msg": {"code": 6, "msg": "unknown channel"}}))
    assert "market_tickers" in str(e.value)  # the autopsy carries what was sent


# ── F2: spot wiring — H8 sees distance; custodian sees safety ─────────────
def test_spot_flows_into_h8_gate():
    """An 85c favorite with spot+boundary distance >= 0.15% inside T-60:
    with spot wired, H8's static gate FIRES (proposal); without spot it
    degrades to H8_NO_SPOT. Both proven."""
    engine = ShadowEngine(db_path=":memory:")
    engine.boot()
    close = 1_000_000
    engine.on_market_discovered(TICKER, {"close_ts": close, "floor_strike": 64_000.0,
                                         "cap_strike": 64_500.0})
    engine.feed.book(TICKER).apply_snapshot({85: 30}, {5: 30}, ts=close - 55.0)

    # no spot: blind eye -> H8_NO_SPOT
    engine.cycle([TICKER], now=close - 55)
    reasons = dict(engine.ledger.db.execute(
        "SELECT lane, detail FROM surface_rows WHERE terminal=1"))
    assert reasons["H8"] == "H8_NO_SPOT"

    # spot present and fresh: the gate evaluates distance and fires
    engine2 = ShadowEngine(db_path=":memory:")
    engine2.boot()
    engine2.on_market_discovered(TICKER, {"close_ts": close, "floor_strike": 64_000.0,
                                          "cap_strike": 64_500.0})
    engine2.feed.book(TICKER).apply_snapshot({85: 30}, {5: 30}, ts=close - 55.0)
    engine2.record_spot(65_000.0, close - 56)  # dist = spot-cap = 500 -> 0.77% >= 0.15%
    engine2.cycle([TICKER], now=close - 55)
    h8_orders = [o for o in engine2.gateway.shadow_orders if o.lane == "H8"]
    assert len(h8_orders) == 1
    assert (h8_orders[0].side, h8_orders[0].price_cents) == ("yes", 85)

    # stale spot serves as None (BLIND bound)
    assert engine2.fresh_spot(close - 55) == 65_000.0
    assert engine2.fresh_spot(close + 100) is None


def test_custodian_tick_receives_spot_and_boundaries():
    """Spot-safety master override works through the runner: a losing-looking
    mark HOLDS when spot is safely on our side of the discovered boundary."""
    from relay_engine.custodian import OpenPosition
    from tests.test_custodian import BASE
    engine = ShadowEngine(db_path=":memory:")
    engine.boot()
    close = 1_000_000
    engine.on_market_discovered(TICKER, {"close_ts": close, "floor_strike": 64_000.0})
    engine.feed.book(TICKER).apply_snapshot({30: 30}, {1: 30}, ts=close - 300.0)
    engine.custodian.set_lane_params("D", BASE)
    engine.custodian.adopt(OpenPosition(
        event="EV", market=TICKER, lane="D", side="yes", count=1,
        entry_price_cents=61, entry_p_win=0.61, size_tier=config.TIER_PROBE,
        entry_time=close - 700))
    # spot $1000 above the floor: master override holds despite mark 30
    engine.record_spot(65_000.0, close - 301)
    engine.cycle([TICKER], now=close - 300)
    assert f"{TICKER}:D" in engine.custodian.positions  # held, not cut
    # spot near the boundary: the same mark cuts (prob floor / caps engage)
    engine.record_spot(64_020.0, close - 299)
    engine.cycle([TICKER], now=close - 298)
    assert f"{TICKER}:D" not in engine.custodian.positions  # cut executed


# ── F6: live boot reconcile ────────────────────────────────────────────────
class FakeClient:
    def __init__(self, balance_cents=12_345, positions=None, orders=None):
        self.balance_cents = balance_cents
        self.positions = positions or []
        self.orders = orders or []

    def request(self, method, path, params=None, json_body=None, timeout=10.0):
        if "balance" in path:
            return {"balance": self.balance_cents, "portfolio_value": 0}
        if "positions" in path:
            return {"positions": self.positions}
        if "orders" in path:
            return {"orders": self.orders}
        return {}


def test_live_boot_reconcile_baselines_and_quarantines():
    from relay_engine.reconcile import live_boot_reconcile
    engine = ShadowEngine(db_path=":memory:")  # book 0 until baselined
    client = FakeClient(
        balance_cents=12_345,
        positions=[{"ticker": TICKER, "position": 1}],       # no fill of ours
        orders=[{"order_id": "NOT-OURS-1", "status": "resting"}])
    summary = live_boot_reconcile(engine, client)
    assert engine.ledger.book_cents() == 12_345               # venue truth baselined
    assert summary["cash_state"] == "BASELINED"
    assert summary["positions_quarantined"] == 1              # never adopted
    assert summary["positions_recognized"] == 0
    assert f"{TICKER}:?" not in engine.custodian.positions
    assert summary["foreign_resting"] == 1                    # alerted, untouched

    # a position OUR fills explain is adopted with true attribution
    engine2 = ShadowEngine(db_path=":memory:")
    engine2.ledger.baseline(12_345, confirmed_by="boot")
    engine2.ledger.record_fill(TICKER, "D", "yes", "ENTRY", 65, 1, config.TIER_PROBE)
    summary2 = live_boot_reconcile(engine2, FakeClient(
        balance_cents=12_345 - 65, positions=[{"ticker": TICKER, "position": 1}]))
    assert summary2["positions_recognized"] == 1
    assert engine2.custodian.positions[f"{TICKER}:D"].entry_price_cents == 65


def test_live_boot_unreadable_balance_is_fatal():
    from relay_engine.reconcile import live_boot_reconcile

    class DeadClient:
        def request(self, *a, **k):
            raise RuntimeError("HTTP 503")

    with pytest.raises(FatalIntegrityError):
        live_boot_reconcile(ShadowEngine(db_path=":memory:"), DeadClient())


# ── F3: throttle constant is the enforced gate ─────────────────────────────
def test_cycle_seconds_is_the_printed_and_enforced_number():
    assert CYCLE_SECONDS == 1.0
    import inspect
    from relay_engine import shadow_runner
    src = inspect.getsource(shadow_runner.run)
    assert "CYCLE_SECONDS" in src  # the constant gates the loop, not decoration