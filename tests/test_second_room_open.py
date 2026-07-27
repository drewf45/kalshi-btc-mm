"""WO-2026-07-26-S Stage 3 — opening the XRP room: mode control + /series +
the halt-key migration + the per-series chapter.

The room start command, without a second deploy: `/series xrp on` adds the room
to the roster at its born-LIVE mode, widens F's series family, and migrates the
per-(series,lane) halt keys the moment a second room joins. BTC stays byte-
identical while the second room is built and opened.
"""

import json

import pytest

from relay_engine import config, lane_fh8


@pytest.fixture
def restore_roster():
    """/series mutates config globals — snapshot and restore around each test."""
    series = list(config.SERIES)
    modes = dict(config.SERIES_MODE)
    fam = set(lane_fh8.F_SERIES_ALLOWED)
    yield
    config.SERIES[:] = series
    config.SERIES_MODE.clear()
    config.SERIES_MODE.update(modes)
    lane_fh8.F_SERIES_ALLOWED = fam


def _engine(tmp_path):
    from relay_engine.shadow_runner import ShadowEngine
    from relay_engine import failures
    e = ShadowEngine(db_path=str(tmp_path / "s3.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    return e


# ── the resolver ─────────────────────────────────────────────────────────────
def test_resolve_series_accepts_short_and_full():
    assert config.resolve_series("xrp") == "KXXRP15M"
    assert config.resolve_series("XRP") == "KXXRP15M"
    assert config.resolve_series("KXXRP15M") == "KXXRP15M"
    assert config.resolve_series("doge") is None


# ── /series opens the room (the own start command) ───────────────────────────
def test_series_on_opens_xrp_room(tmp_path, restore_roster):
    e = _engine(tmp_path)
    assert config.SERIES == ["KXBTC15M"]                 # born BTC-only
    out = e._cmd_series("/series xrp on")
    assert "KXXRP15M" in config.SERIES                    # joined the roster
    assert config.series_mode("KXXRP15M") == "LIVE"       # born LIVE (Drew 07-26)
    assert "KXXRP15M" in lane_fh8.F_SERIES_ALLOWED        # F's family widened
    assert "KXXRP15M" in out and "roster" in out


def test_series_shadow_and_off(tmp_path, restore_roster):
    e = _engine(tmp_path)
    e._cmd_series("/series xrp shadow")
    assert config.series_mode("KXXRP15M") == "SHADOW"
    e._cmd_series("/series xrp off")
    assert "KXXRP15M" not in config.SERIES               # parked, off the roster


def test_btc_cannot_be_turned_off(tmp_path, restore_roster):
    e = _engine(tmp_path)
    out = e._cmd_series("/series btc off")
    assert "cannot be turned off" in out
    assert "KXBTC15M" in config.SERIES


def test_series_usage_and_unknown(tmp_path, restore_roster):
    e = _engine(tmp_path)
    assert "usage" in e._cmd_series("/series")
    assert "unknown room" in e._cmd_series("/series doge on")


# ── opening a second room migrates the halt keys (real-money safety) ──────────
def test_opening_second_room_migrates_live_btc_halt(tmp_path, restore_roster):
    e = _engine(tmp_path)
    # a live BTC FLIP rate-halt under the single-room (bare-lane) key
    from relay_engine.window_econ import _lane_outcomes_key, LANES_HALTED_KEY, HALT_REASON
    e.ledger.set_state(_lane_outcomes_key("FLIP"), json.dumps([{"m": "x", "pnl": -99}]))
    e.ledger.set_state(LANES_HALTED_KEY, json.dumps(["FLIP"]))
    e.gateway.halt_entries(f"{HALT_REASON}:FLIP")
    # open XRP → roster grows past one → the keys migrate to series scope
    e._cmd_series("/series xrp on")
    assert e.ledger.get_state(_lane_outcomes_key("KXBTC15M:FLIP")) is not None
    assert e.ledger.get_state(_lane_outcomes_key("FLIP")) is None   # moved, not copied
    assert json.loads(e.ledger.get_state(LANES_HALTED_KEY)) == ["KXBTC15M:FLIP"]
    assert f"{HALT_REASON}:KXBTC15M:FLIP" in e.gateway.entries_halted_reasons
    assert f"{HALT_REASON}:FLIP" not in e.gateway.entries_halted_reasons
    # and BTC-FLIP is still halted after the migration (the halt did not vanish)
    assert e.gateway.entries_halted_for(config.halt_scope("KXBTC15M", "FLIP"))


# ── the per-series chapter renders for every room ────────────────────────────
def test_series_chapter_lines_render(tmp_path, restore_roster):
    from relay_engine.ops import series_chapter_lines
    e = _engine(tmp_path)
    e._cmd_series("/series xrp on")
    lines = series_chapter_lines(e.ledger)
    body = "\n".join(lines)
    assert "SERIES CHAPTERS" in body
    assert "[KXBTC15M]" in body and "[KXXRP15M]" in body
    assert "FIRST-DAY WATCHLIST (KXXRP15M)" in body   # the new room's watchlist
    assert "maker $0" in body


# ── discovery iterates only enabled (non-OFF) rooms ──────────────────────────
def test_discovery_skips_off_rooms(tmp_path, restore_roster, monkeypatch):
    from relay_engine import venue
    e = _engine(tmp_path)
    e._cmd_series("/series xrp on")
    calls = []

    class _Client:
        def request(self, method, path, params=None):
            calls.append(params.get("series_ticker"))
            return {"markets": []}

    venue.list_open_markets(_Client())
    assert calls == ["KXBTC15M", "KXXRP15M"]
    # park XRP → discovery stops polling it
    e._cmd_series("/series xrp off")
    calls.clear()
    venue.list_open_markets(_Client())
    assert calls == ["KXBTC15M"]
