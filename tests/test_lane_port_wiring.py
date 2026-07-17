"""Wiring test: the ported F/H8 lanes drive the shadow loop end-to-end —
watch ladder confirms on live semantics, proposal passes the relay walls,
rests as a SHADOW order, and the surface census stays honest."""

from datetime import datetime
from zoneinfo import ZoneInfo

from relay_engine.lanes import infer_close_ts_from_ticker
from relay_engine.shadow_runner import ShadowEngine

NY = ZoneInfo("America/New_York")
TICKER = "KXBTC15M-02JAN251000-T99"


def test_infer_close_ts_from_ticker():
    close = infer_close_ts_from_ticker(TICKER)
    assert close == int(datetime(2025, 1, 2, 10, 15, tzinfo=NY).timestamp())
    assert infer_close_ts_from_ticker("KXBTC15M-A") is None


def test_watch_ladder_drives_shadow_proposal():
    engine = ShadowEngine(db_path=":memory:")
    engine.boot()  # paper bankroll books in SHADOW
    assert engine.ledger.book_cents() == 10_000

    close = infer_close_ts_from_ticker(TICKER)
    book = engine.feed.book(TICKER)
    book.apply_snapshot({99: 50}, {1: 40}, ts=0.0)  # 99c YES favorite, counterparty present

    start = close - 850  # inside tier 0 (T-900..600, floor 99, 9 confirms)
    for i in range(12):
        engine.cycle([TICKER], now=start + i * 5)

    # exactly one F proposal, at the touch, flat 1 lot (FLIP may also be
    # working the cheap side of the same book — all lanes live, same market)
    f_orders = [o for o in engine.gateway.shadow_orders if o.lane == "F"]
    assert len(f_orders) == 1
    o = f_orders[0]
    assert (o.lane, o.side, o.price_cents, o.count, o.purpose) == ("F", "yes", 99, 1, "ENTRY")
    payload = engine.gateway._payload(o)
    assert payload["post_only"] is True

    # F surface: WATCHING interim rows then a PROPOSED row
    states = [s for (s,) in engine.ledger.db.execute(
        "SELECT state FROM surface_rows WHERE lane='F' ORDER BY id")]
    assert "WATCHING" in states and "PROPOSED" in states
    assert "PASS" not in states  # F did not pass this window — it proposed

    # H8 verdict on the same market: terminal Pass, cost in F band
    h8 = engine.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE lane='H8' AND terminal=1").fetchone()[0]
    assert h8 == "COST_IN_F_BAND"

    # live submit side-effects mirrored: single entry + hourly exposure
    assert ("F", TICKER) in engine.fh8_shared.state.traded
    assert engine.fh8_shared.state.hourly_exposure_usd() == 0.99

    # further cycles: lane-scoped single entry blocks a second F proposal
    for i in range(12, 24):
        engine.cycle([TICKER], now=start + i * 5)
    assert len([o for o in engine.gateway.shadow_orders if o.lane == "F"]) == 1


def test_h8_band_market_waits_without_spot():
    """An 85c favorite is H8's band; without spot the H8 gate skips (live
    semantics H8_NO_SPOT) — in the final window that's a terminal Pass."""
    engine = ShadowEngine(db_path=":memory:")
    engine.boot()
    close = infer_close_ts_from_ticker(TICKER)
    book = engine.feed.book(TICKER)
    book.apply_snapshot({85: 30}, {15: 30}, ts=0.0)

    engine.cycle([TICKER], now=close - 60)  # final window, H8 time gate territory
    reasons = dict(engine.ledger.db.execute(
        "SELECT lane, detail FROM surface_rows WHERE terminal=1"))
    assert reasons["H8"] == "H8_NO_SPOT"
    assert reasons["F"] == "H8_NO_SPOT"  # shared decision: cost sits in H8's band
