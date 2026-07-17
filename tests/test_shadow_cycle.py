"""Gate 7 machinery (the 24h run itself needs the live feed): every lane
evaluates every market every cycle, Pass rows are census-clean, zero orders
placed, daily pack renders per-lane sections."""

from relay_engine import config
from relay_engine.lanes import build_registry
from relay_engine.ops import daily_pack
from relay_engine.shadow_runner import ShadowEngine


def test_all_five_lanes_registered_in_stable_order():
    names = [l.name for l in build_registry()]
    assert names == ["F", "H8", "D", "MM", "P"]


def test_cycle_census_clean_and_zero_orders():
    engine = ShadowEngine(db_path=":memory:")
    engine.boot()
    markets = ["KXBTC15M-A", "KXBTC15M-B"]
    for _ in range(3):  # repeated cycles must not duplicate terminal rows
        engine.cycle(markets, now=100.0)

    rows = engine.ledger.db.execute(
        "SELECT lane, market, COUNT(*) FROM surface_rows WHERE terminal=1"
        " GROUP BY lane, market").fetchall()
    # census: exactly one terminal row per lane per market — 5 lanes x 2 markets
    assert len(rows) == 10
    assert all(n == 1 for _, _, n in rows)
    # F/H8 pass with the gated reason; stubs with theirs
    reasons = dict(engine.ledger.db.execute(
        "SELECT lane, detail FROM surface_rows WHERE terminal=1 AND market='KXBTC15M-A'"))
    assert reasons["F"] == "GATED_ON_B1_NOT_PORTED"
    assert reasons["H8"] == "GATED_ON_B1_NOT_PORTED"
    assert reasons["D"] == "STUB_AWAITING_CHUNK_6"
    # ZERO orders placed
    assert engine.gateway.shadow_orders == []
    assert engine.gateway.resting == {}


def test_daily_pack_renders_per_lane_sections():
    engine = ShadowEngine(db_path=":memory:")
    engine.boot()
    engine.cycle(["KXBTC15M-A"], now=100.0)
    pack = daily_pack(engine.ledger, engine.surface, engine.cash,
                      venue_statement_cents=0)
    assert f"EPOCH {config.EPOCH}" in pack
    assert "EPOCH BOUNDARY" in pack
    for lane in ("F", "H8", "D", "MM", "P"):
        assert f"[lane {lane}]" in pack
    assert "PASS=1" in pack
    assert "TRUE-UP" in pack


def test_delta_module_is_loudly_gated():
    import pytest
    from relay_engine import delta
    from relay_engine.errors import GatedOnMissingInput
    record = {"floor_strike": 65000.0}
    with pytest.raises(GatedOnMissingInput):
        delta.p_survive(record, 300)
    with pytest.raises(ValueError):
        delta.settlement_anchor({"spot": 65000.0})  # generic spot refused
    assert delta.settlement_anchor(record) == 65000.0
