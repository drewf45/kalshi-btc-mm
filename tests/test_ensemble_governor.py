"""WO-2026-07-26-S Stage 2 — the ensemble governor for the correlated tail.

Three ruled pieces: the ensemble cap (total simultaneous at-risk ≤ 50% of
tradeable, one summed check above the lane walls, defer-with-why), per-series
halts (the rate halt keys on (series, lane) so one room parks while others
print), and the correlated-loss rule (≥2 rooms losing one wall-clock window
counted ONCE at combined size — the measured tail datum).
"""

import json

import pytest

from relay_engine import config
from relay_engine.window_econ import combined_correlated_loss


# ── the correlated-loss rule (pure) ──────────────────────────────────────────
def test_correlated_loss_counts_once_at_combined_size():
    # ≥2 rooms lose the same window → one event, combined size, never per-summed twice
    ev = combined_correlated_loss({"KXBTC15M": -90, "KXXRP15M": -40, "KXSOL15M": 25})
    assert ev is not None
    assert ev["series"] == ["KXBTC15M", "KXXRP15M"]     # only the LOSERS
    assert ev["combined_cents"] == -130                 # counted ONCE, combined
    assert ev["per_series"] == {"KXBTC15M": -90, "KXXRP15M": -40}


def test_a_single_room_loss_is_not_correlated():
    assert combined_correlated_loss({"KXBTC15M": -90, "KXXRP15M": 30}) is None
    assert combined_correlated_loss({"KXBTC15M": -90}) is None
    assert combined_correlated_loss({}) is None


def test_correlated_window_pages_and_writes_a_row(tmp_path):
    from relay_engine.shadow_runner import ShadowEngine
    from relay_engine import failures
    e = ShadowEngine(db_path=str(tmp_path / "corr.db"))
    e.boot()
    sent = []
    e.telegram.send = sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    try:
        ev = e.econ.record_correlated_window(
            "25JAN0210", {"KXBTC15M": -90, "KXXRP15M": -40})
        assert ev["combined_cents"] == -130
        # a CORRELATED_LOSS surface row is written with the combined datum
        row = e.ledger.db.execute(
            "SELECT detail FROM surface_rows WHERE state='CORRELATED_LOSS'"
        ).fetchone()
        assert row is not None and json.loads(row[0])["combined_cents"] == -130
        # and a page fires (the one shock that reaches every room)
        assert any("CORRELATED LOSS" in m for m in sent)
        # one room only → nothing recorded
        assert e.econ.record_correlated_window("x", {"KXBTC15M": -5}) is None
    finally:
        failures._ledger = None
        failures._alert_fn = None


# ── the ensemble cap defers-with-why (Acceptance 3) ──────────────────────────
def test_ensemble_cap_is_50pct_of_tradeable_and_names_itself(tmp_path):
    from relay_engine.book import OrderBook
    from relay_engine.gateway import Order
    from relay_engine.shadow_runner import ShadowEngine
    from relay_engine import failures
    e = ShadowEngine(db_path=str(tmp_path / "ens.db"))
    e.ledger.baseline(10_000, confirmed_by="boot")   # $100 tradeable (owed 0)
    # pre-deploy ~48% of tradeable so the next entry breaches the 50% ensemble cap
    e.ledger.record_fill("KXBTC15M-A", "F", "yes", "ENTRY", 96, 50, config.TIER_PROBE)
    book = OrderBook(market="KXBTC15M-B")
    book.apply_snapshot({97: 10_000}, {1: 10_000}, ts=1.0)
    p = Order(lane="F", event="KXBTC15M-B", market="KXBTC15M-B", side="yes",
              action="buy", price_cents=97, count=40, size_tier=config.TIER_PROBE,
              purpose="ENTRY", why="F tier97")
    try:
        e._score_and_size(p, book)
        # clamped by the ensemble cap (deployed already near the 50% ceiling)
        assert p.count < 40
        assert "ensemble-cap" in p.why           # the cap names itself on the row
        assert "capital=" in p.why               # WO-W P4: base = cash + at-risk
    finally:
        failures._ledger = None


def test_ensemble_cap_constant_is_ruled_50pct():
    assert config.ENSEMBLE_AT_RISK_PCT == 0.50
    assert config.ENSEMBLE_AT_RISK_PCT == config.PORTFOLIO_DEPLOY_PCT  # same ceiling


# ── per-series halt scope: bare lane while single-room (byte-identical) ───────
def test_halt_scope_is_bare_lane_single_room_then_series_scoped(monkeypatch):
    assert config.SERIES == ["KXBTC15M"]
    assert config.halt_scope("KXBTC15M", "FLIP") == "FLIP"    # byte-identical key
    # with a second room, the scope keys on (series, lane): XRP parks XRP only
    monkeypatch.setattr(config, "SERIES", ["KXBTC15M", "KXXRP15M"])
    assert config.halt_scope("KXXRP15M", "F") == "KXXRP15M:F"
    assert config.halt_scope("KXBTC15M", "F") == "KXBTC15M:F"


def test_correlated_loss_registered_as_a_data_question():
    from relay_engine import registry
    q = registry.get("CORRELATED_LOSS")
    assert q is not None and q.is_complete()
    assert "CORRELATED_LOSS" in registry.SEED_SURFACES
