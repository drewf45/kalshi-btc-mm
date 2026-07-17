"""Gate 7 machinery (the 24h run itself needs the live feed): every lane
evaluates every market every cycle, Pass rows are census-clean, zero orders
placed, daily pack renders per-lane sections."""

from relay_engine import config
from relay_engine.lanes import build_registry
from relay_engine.ops import daily_pack
from relay_engine.shadow_runner import ShadowEngine


def test_all_five_lanes_registered_in_arbitration_order():
    """P3: MM renamed to FLIP (a proven lane, not a stub); registry order IS
    the entry arbitration order F -> H8 -> FLIP -> D -> P."""
    names = [l.name for l in build_registry()]
    assert names == ["F", "H8", "FLIP", "D", "P"]


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
    # F/H8 (ported) pass these metadata-less tickers; stubs with their reasons
    reasons = dict(engine.ledger.db.execute(
        "SELECT lane, detail FROM surface_rows WHERE terminal=1 AND market='KXBTC15M-A'"))
    assert reasons["F"] == "NO_CLOSE_TS"
    assert reasons["H8"] == "NO_CLOSE_TS"
    assert reasons["FLIP"] == "NO_CLOSE_TS"
    assert reasons["D"] == "STUB_AWAITING_P3.3_PENDING"
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
    for lane in ("F", "H8", "FLIP", "D", "P"):
        assert f"[lane {lane}]" in pack
    assert "PASS=1" in pack
    assert "TRUE-UP" in pack


def test_delta_module_laws():
    """B1 landed: delta is the live loader borrowed whole. TABLE_ABSENT is
    boot-safe (gate inputs return None, static gates rule); R1 refuses
    synthetic manifests; the settlement-anchor law stands."""
    import json
    import pytest
    from pathlib import Path
    from relay_engine import delta

    # TABLE_ABSENT: gate inputs are None, verdicts say so, nothing crashes
    delta._TABLE, delta._LOADED = {}, False
    assert delta.p_cross(100, 300) is None
    assert delta.p_survive(100, 300) is None
    assert delta.h8_table_verdict(100, 300)["reason"] == "TABLE_ABSENT"
    assert delta.f_top_rung_verdict(100, 700, cost_cents=99)["qualified"] is None

    # R1: synthetic manifest refused by construction
    tmp = Path("/tmp/claude-0/-home-user-kalshi-btc-mm/d191cf51-af94-51ca-a17b-9fd70307613e/scratchpad/delta_r1")
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "delta_table.csv").write_text("distance_usd,secs_remaining,p_cross,n,effective_n,wilson_ub,session\n")
    (tmp / "candles_manifest.json").write_text(json.dumps({"source": "Synthetic GBM", "csv_sha256": "x"}))
    assert delta.load(str(tmp / "delta_table.csv")) is False
    assert "synthetic" in delta.refusal_reason()

    # settlement anchor law
    with pytest.raises(ValueError):
        delta.settlement_anchor({"spot": 65000.0})  # generic spot refused
    assert delta.settlement_anchor({"floor_strike": 65000.0}) == 65000.0
