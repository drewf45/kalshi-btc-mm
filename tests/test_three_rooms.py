"""WO-2026-07-27-W P3 — THREE ROOMS, LIVE BY DEFAULT (+ roster persistence U1).

Drew: "I should not have to confirm via Telegram; unlock ETH — XRP, BTC, ETH
tonight." The default roster ships in code: BTC, XRP, ETH all LIVE, SOL banked
(OFF). No /series, no env, no confirmation. ETH enters exactly as XRP did —
20% dial, its own halt (series-scoped), counterparty gate, surv n/a. A Drew
/series change persists (U1) and survives a restart. The banner prints each
room with its source.
"""

import json

import pytest

from relay_engine import config, failures


def _engine(tmp_path, name="3rooms.db"):
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / name))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    return e


# ── P3 default roster: three rooms live, SOL banked ─────────────────────────
def test_default_roster_is_three_rooms_live_sol_off():
    assert config.SERIES == ["KXBTC15M", "KXXRP15M", "KXETH15M"]
    for live in ("KXBTC15M", "KXXRP15M", "KXETH15M"):
        assert config.series_mode(live) == "LIVE"
    assert config.series_mode("KXSOL15M") == "OFF"          # banked next
    assert config.f_enabled_series() == ["KXBTC15M", "KXXRP15M", "KXETH15M"]


def test_banner_prints_each_room_with_its_source(tmp_path):
    from relay_engine import boot
    body = "\n".join(boot.boot_tape())
    assert "SERIES ROOMS (WO-S" in body and "source=default" in body
    assert "BTC[LIVE] F@24%" in body
    assert "XRP[LIVE] F@20%" in body
    assert "ETH[LIVE] F@20%" in body           # unlocked tonight, born at 20%
    assert "SOL[OFF]" in body                  # shown, not forgotten


# ── ETH enters exactly as XRP: 20% dial, own halt, surv n/a, gate ───────────
def test_eth_is_born_at_20pct_like_xrp():
    assert config.f_notional_pct_of("KXETH15M") == config.NEW_SERIES_F_DIAL == 0.20
    assert config.f_notional_pct_of("KXETH15M") == config.f_notional_pct_of("KXXRP15M")
    # BTC keeps its earned 24% (byte-identical sizing base)
    assert config.f_notional_pct_of("KXBTC15M") == config.F_NOTIONAL_PCT == 0.24


def test_eth_halt_is_its_own_room_series_scoped():
    # three rooms rostered → the halt keys on (series, lane): ETH parks ETH only
    assert config.halt_scope("KXETH15M", "F") == "KXETH15M:F"
    assert config.halt_scope("KXBTC15M", "F") == "KXBTC15M:F"


def test_eth_card_is_surv_na_the_btc_table_will_not_speak_for_it(monkeypatch):
    # the BTC-trained table answers ONLY BTC; an ETH consultation is honest BLIND
    from relay_engine import delta
    monkeypatch.setattr(delta, "_LOADED", True)
    assert delta.table_series() == "KXBTC15M"
    assert delta.p_cross(50.0, 700.0, series="KXETH15M") is None   # surv n/a
    assert delta.distance_for_p(0.5, 700.0, series="KXETH15M") is None


# ── U1 — roster persistence: a Drew /series change survives a restart ───────
def test_roster_state_round_trips_through_apply(monkeypatch):
    state = config.roster_state()
    assert state["KXBTC15M"] == "LIVE" and state["KXSOL15M"] == "OFF"
    # park ETH, open SOL, and re-apply — the roster reshapes deterministically
    state = dict(state, KXETH15M="OFF", KXSOL15M="LIVE")
    monkeypatch.setattr(config, "SERIES", list(config.SERIES))
    config.apply_roster(state, source="persisted")
    assert "KXETH15M" not in config.SERIES and "KXSOL15M" in config.SERIES
    assert config.ROSTER_SOURCE == "persisted"


def test_series_change_persists_across_a_reboot(tmp_path):
    e = _engine(tmp_path, "persist.db")
    # Drew parks ETH from the phone; the roster is saved
    e._cmd_series("/series eth off")
    assert "KXETH15M" not in config.SERIES
    saved = json.loads(e.ledger.get_state("series_roster"))
    assert saved["KXETH15M"] == "OFF"
    # a fresh engine on the SAME db restores the persisted roster (U1), not the
    # three-room code default — the /series change survived the restart
    e2 = _engine(tmp_path, "persist.db")
    assert "KXETH15M" not in config.SERIES
    assert config.ROSTER_SOURCE == "persisted"
    assert config.series_mode("KXETH15M") == "OFF"


def test_fresh_db_boots_the_three_room_default(tmp_path):
    # no persisted roster → the code default stands (three rooms LIVE, source default)
    _engine(tmp_path, "fresh.db")
    assert config.SERIES == ["KXBTC15M", "KXXRP15M", "KXETH15M"]
    assert config.ROSTER_SOURCE == "default"
