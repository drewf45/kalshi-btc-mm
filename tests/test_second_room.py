"""WO-2026-07-26-S "THE SECOND ROOM" — Stage 1: series as a dimension.

One repo, one process; every lane key becomes (series, lane). These tests pin the
Stage-1 foundation: the canonical market→series map, per-series mode/dial config,
the F dial threading into sizing (BTC byte-identical), discovery iterating the
roster, and the sibling-sweep guarantees (F's series gate parameterized, D
explicitly BTC-scoped, no schema migration because series is a COMPUTED key).
"""

import pytest

from relay_engine import config, lane_fh8


# ── the canonical market→series map ──────────────────────────────────────────
def test_series_of_is_the_ticker_prefix():
    assert config.series_of("KXBTC15M-25JAN0210-T99000") == "KXBTC15M"
    assert config.series_of("KXXRP15M-25JAN0210-T3") == "KXXRP15M"
    assert config.series_of("") == ""
    # existing rows resolve to their series with no migration — series is COMPUTED
    assert config.series_of("KXBTC15M-anything") == "KXBTC15M"


# ── per-series dial: BTC earned, new rooms born at 20% ───────────────────────
def test_f_dial_is_per_series_btc_earned_new_room_born_at_20():
    assert config.f_notional_pct_of("KXBTC15M") == config.F_NOTIONAL_PCT  # 0.24
    assert config.f_notional_pct_of("KXXRP15M") == config.NEW_SERIES_F_DIAL  # 0.20
    assert config.f_notional_pct_of("KXSOL15M") == 0.20   # any unlisted new room
    assert config.NEW_SERIES_F_DIAL == 0.20


# ── the F dial threads into sizing; BTC path byte-identical ──────────────────
def test_f_sizing_btc_byte_identical_via_room_dial():
    from relay_engine import scoring
    # BTC: passing the room dial (0.24) equals the legacy global-constant path
    legacy = scoring.size_order(4162, 97, 10_000, lane="F")
    roomed = scoring.size_order(4162, 97, 10_000, lane="F",
                                notional_pct=config.f_notional_pct_of("KXBTC15M"))
    assert roomed.contracts == legacy.contracts
    assert roomed.reason == legacy.reason      # identical decision + narration
    # XRP: a smaller dial → a smaller (never larger) notional clip on the same book
    xrp = scoring.size_order(4162, 97, 10_000, lane="F",
                             notional_pct=config.f_notional_pct_of("KXXRP15M"))
    assert xrp.contracts <= legacy.contracts


# ── per-series mode; global kill still governs ───────────────────────────────
def test_series_mode_and_live_gating():
    assert config.series_mode("KXBTC15M") in ("LIVE", "SHADOW", "OFF")
    assert config.series_mode("KXBTC15M") == "LIVE"       # the proven room
    assert config.series_mode("KXXRP15M") == "LIVE"       # born LIVE (Drew 07-26)
    assert config.series_mode("KXDOGE15M") == "OFF"       # unrostered → does nothing
    # a LIVE series is only live-submitting when the GLOBAL kill is off too
    if not config.live_submit_enabled():
        assert config.series_is_live("KXBTC15M") is False  # born-shadow global state


# ── the roster + F's enabled family ──────────────────────────────────────────
def test_roster_is_btc_only_through_stage_1_2():
    assert config.SERIES == ["KXBTC15M"]      # XRP joins the roster in Stage 3
    assert config.f_enabled_series() == ["KXBTC15M"]


# ── the sibling sweep: F's series gate is parameterized (default BTC) ─────────
def test_f_series_gate_defaults_btc_only_byte_identical():
    # the golden tape (BTC tapes) sees the default BTC-only family → identical
    assert lane_fh8.F_SERIES_ALLOWED == {"KXBTC15M"}
    from relay_engine.lane_fh8 import evaluate, FH8State, FH8Stats, TouchBook
    st, stx = FH8State(), FH8Stats()
    # a non-rostered series is refused WRONG_FAMILY (same wall, widened test)
    r = evaluate("KXXRP15M-25JAN0210-T3", TouchBook(), 120.0, 100.0,
                 state=st, stats=stx)
    assert not r.allowed and r.reject_code == "WRONG_FAMILY"
    # BTC still passes wall 1 (proceeds to the real walls, not WRONG_FAMILY)
    r2 = evaluate("KXBTC15M-25JAN0210-T99000", TouchBook(), 120.0, 100.0,
                  state=st, stats=stx)
    assert r2.reject_code != "WRONG_FAMILY"


def test_f_gate_widens_when_engine_sets_allowed(monkeypatch):
    """The engine widens F_SERIES_ALLOWED at boot (Stage 3); once widened, F
    accepts the new room's markets at wall 1."""
    from relay_engine.lane_fh8 import evaluate, FH8State, FH8Stats, TouchBook
    monkeypatch.setattr(lane_fh8, "F_SERIES_ALLOWED", {"KXBTC15M", "KXXRP15M"})
    r = evaluate("KXXRP15M-25JAN0210-T3", TouchBook(), 120.0, 100.0,
                 state=FH8State(), stats=FH8Stats())
    assert r.reject_code != "WRONG_FAMILY"   # now past the family wall


# ── discovery iterates the roster ────────────────────────────────────────────
def test_discovery_iterates_the_series_roster(monkeypatch):
    from relay_engine import venue
    calls = []

    class _Client:
        def request(self, method, path, params=None):
            calls.append(params.get("series_ticker"))
            return {"markets": []}

    monkeypatch.setattr(config, "SERIES", ["KXBTC15M", "KXXRP15M"])
    venue.list_open_markets(_Client())
    assert calls == ["KXBTC15M", "KXXRP15M"]   # one signed call per room


# ── D is explicitly BTC-scoped (honest, not a bug) ───────────────────────────
def test_lane_d_is_explicitly_btc_scoped():
    from relay_engine import lane_d
    v = lane_d.classify("KXXRP15M-25JAN0210-T3", None, 120.0, None, None, None)
    assert v.verdict == "SKIP_WRONG_SERIES"   # D stays BTC-only by design
