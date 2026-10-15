"""WO-ROT-1 Chunk 0 — desk switch, ROT knobs on the boot tape, the count law.

The count rule: size = budget / cost rounded DOWN to the lot step; under one
lot is SKIP_BELOW_ONE_LOT, never bumped to one. The venue boundary validates a
count and never truncates, floors or bumps it.
"""

from decimal import Decimal

import pytest

from relay_engine import boot, config, lanes, sizing, venue
from relay_engine.errors import CountRejected, WallRejection
from relay_engine.shadow_runner import ShadowEngine

ONE = Decimal("1")
CENT = Decimal("0.01")


@pytest.fixture(autouse=True)
def _rotation_desk(monkeypatch):
    """Pin the desk this module proves, whatever DESK_MODE the shell carries."""
    monkeypatch.setattr(config, "DESK_MODE", "ROTATION")
    monkeypatch.setattr(config, "DESK_LEGACY_LANES_ON", frozenset())


# ── the count rule (sizing) ──────────────────────────────────────

def test_whole_lot_step_0_4_contracts_skips():
    # budget 40c at cost 100c = 0.4 contracts: under one whole lot -> skip
    assert sizing.rot_size_contracts(40, 100, ONE) == (None, "SKIP_BELOW_ONE_LOT")


def test_whole_lot_step_2_9_contracts_places_2():
    count, why = sizing.rot_size_contracts(290, 100, ONE)
    assert (count, why) == (Decimal("2"), "")
    assert venue.validated_count_str(count) == "2"


def test_fractional_step_rounds_down_to_hundredths():
    # the ruled step (0.01): 2.9 places 2.90, 0.4 places 0.40 — never bumped to 1
    assert sizing.rot_size_contracts(290, 100, CENT) == (Decimal("2.90"), "")
    assert sizing.rot_size_contracts(40, 100, CENT) == (Decimal("0.40"), "")
    # the WO's worked case: 5% of $6.74 = 33.7c -> 33c budget at 47c = 0.702 -> 0.70
    assert sizing.rot_size_contracts(33, 47, CENT) == (Decimal("0.70"), "")


def test_fractional_step_below_one_lot_skips():
    assert sizing.rot_size_contracts(0, 47, CENT) == (None, "SKIP_BELOW_ONE_LOT")
    # 0.4c at 54c = 0.0074 contracts < 0.01 -> skip (budgets are integer cents,
    # so test the edge with a tiny budget against a large cost)
    assert sizing.rot_size_contracts(1, 101, CENT) == (None, "SKIP_BELOW_ONE_LOT")


def test_default_step_is_the_ruled_hundredth():
    assert config.ROT_LOT_STEP == CENT
    assert sizing.rot_size_contracts(290, 100) == (Decimal("2.90"), "")


@pytest.mark.parametrize("budget,cost", [(100, 0), (100, -5), (100, 50.0),
                                         (-1, 50), (10.5, 50), (100, True)])
def test_bad_sizing_inputs_raise_never_fall_back(budget, cost):
    with pytest.raises(ValueError):
        sizing.rot_size_contracts(budget, cost, CENT)


# ── the venue boundary (validated, never altered) ────────────────

@pytest.mark.parametrize("count,expected", [
    (1, "1"), (2, "2"), ("2", "2"), (Decimal("2.90"), "2.90"),
    (Decimal("0.72"), "0.72"), (Decimal("0.01"), "0.01"), (Decimal("3.00"), "3"),
])
def test_validated_count_str_accepts_grid_counts(count, expected):
    assert venue.validated_count_str(count) == expected


@pytest.mark.parametrize("count", [0, Decimal("0.004"), Decimal("0"), -1, "0.009"])
def test_validated_count_below_one_lot_is_skip(count):
    with pytest.raises(CountRejected) as ei:
        venue.validated_count_str(count)
    assert ei.value.wall == "SKIP_BELOW_ONE_LOT"


@pytest.mark.parametrize("count", [2.9, None, True, "abc", Decimal("2.999"),
                                   Decimal("NaN"), Decimal("Infinity")])
def test_validated_count_invalid_is_refused(count):
    with pytest.raises(CountRejected) as ei:
        venue.validated_count_str(count)
    assert ei.value.wall == "COUNT_INVALID"


def test_count_rejected_is_a_wall_rejection():
    # the gateway already surfaces WallRejection with its why; no new path
    assert issubclass(CountRejected, WallRejection)


class _RecordingClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, json_body=None, **kw):
        self.calls.append((method, path, json_body))
        if method == "POST" and path.endswith("/amend"):
            return {"order_id": "OID-2"}
        if method == "POST":
            return {"order_id": "OID-1"}
        return {"order": {"client_order_id": "CID-0"}}


def test_place_order_maker_sends_the_validated_count():
    c = _RecordingClient()
    oid, _ = venue.place_order_maker(c, "KXBTC15M-X", "yes", 47, count=Decimal("0.70"))
    assert oid == "OID-1"
    assert c.calls[-1][2]["count"] == "0.70"


def test_place_order_maker_zero_count_places_nothing():
    """The old line bumped 0 to 1 and sent it. Now: refused, no request."""
    c = _RecordingClient()
    with pytest.raises(CountRejected) as ei:
        venue.place_order_maker(c, "KXBTC15M-X", "yes", 47, count=0)
    assert ei.value.wall == "SKIP_BELOW_ONE_LOT"
    assert c.calls == []


def test_place_order_maker_never_truncates():
    c = _RecordingClient()
    with pytest.raises(CountRejected):
        venue.place_order_maker(c, "KXBTC15M-X", "yes", 47, count=2.9)  # float
    assert c.calls == []


def test_amend_order_validates_count():
    c = _RecordingClient()
    with pytest.raises(CountRejected):
        venue.amend_order(c, "OID-1", "KXBTC15M-X", "yes", 47, count=0)
    assert not any(m == "POST" for m, _, _ in c.calls)
    new_oid, _ = venue.amend_order(c, "OID-1", "KXBTC15M-X", "yes", 47,
                                   count=Decimal("2.90"))
    assert new_oid == "OID-2"
    assert c.calls[-1][2]["count"] == "2.90"


def test_no_silent_count_bump_left_in_venue():
    import inspect
    src = inspect.getsource(venue)
    assert "max(1, int(count))" not in src


# ── DESK_MODE ────────────────────────────────────────────────────

def test_branch_default_is_rotation():
    """With no DESK_MODE in the environment, the branch boots ROTATION."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = {k: v for k, v in os.environ.items()
           if k not in ("DESK_MODE", "DESK_LEGACY_LANES_ON")}
    out = subprocess.run(
        [sys.executable, "-c",
         "from relay_engine import config; print(config.DESK_MODE)"],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, check=True).stdout.strip()
    assert out == "ROTATION"


def test_unknown_desk_mode_fails_loud():
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = dict(os.environ, DESK_MODE="ROTATON")
    r = subprocess.run(
        [sys.executable, "-c", "import relay_engine.config"],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True)
    assert r.returncode != 0 and "DESK_MODE" in r.stderr


def test_rotation_turns_every_pre_desk_lane_off():
    reg = lanes.build_registry()
    assert [l.name for l in reg] == ["F", "H8", "FLIP", "D", "P"]
    assert all(isinstance(l, lanes.OffLane) for l in reg)
    for l in reg:
        d = l.evaluate("KXBTC15M-26OCT141015-15", {"now": 0.0})
        assert d.proposal is None and not d.multi
        assert d.pass_reason == "LANE_OFF_DESK_ROTATION"
        assert not d.interim  # a first-class terminal Pass
    for ln in config.LEGACY_LANES:
        assert config.lane_off(ln)
    assert not config.lane_off("ROT")


def test_off_lane_keeps_its_inner_object():
    """The runner reaches .shared / .flip / .d / .p by slot — they pass through."""
    reg = lanes.build_registry()
    assert reg[0].shared is reg[1].shared
    assert hasattr(reg[2], "flip") and hasattr(reg[3], "d") and hasattr(reg[4], "p")


def test_legacy_restores_the_build77_registry(monkeypatch):
    monkeypatch.setattr(config, "DESK_MODE", "LEGACY")
    reg = lanes.build_registry()
    assert not any(isinstance(l, lanes.OffLane) for l in reg)
    assert not any(config.lane_off(ln) for ln in config.LEGACY_LANES)


def test_one_old_lane_can_be_turned_back_on(monkeypatch):
    monkeypatch.setattr(config, "DESK_LEGACY_LANES_ON", frozenset({"F"}))
    reg = lanes.build_registry()
    off = {l.name for l in reg if isinstance(l, lanes.OffLane)}
    assert off == {"H8", "FLIP", "D", "P"}


def test_off_gates_proposals_not_custody(monkeypatch):
    """lane_is_live is untouched by DESK_MODE: a venue position under an OFF
    lane's name can still be cut live by custody / shutdown flatten."""
    monkeypatch.setattr(config, "RUN_MODE", "LIVE")
    monkeypatch.setattr(config, "I_UNDERSTAND_LIVE", config.I_UNDERSTAND_LIVE_PHRASE)
    assert config.lane_off("F")
    assert config.lane_is_live("F") == (config.LANE_MODE["F"] == "LIVE")


def test_rotation_cycle_runs_no_old_lane_and_places_zero_orders():
    """In ROTATION the old lanes evaluate nothing: the legacy body runs only
    over markets the desk reads (held, resting, shortlisted) — none here."""
    engine = ShadowEngine(db_path=":memory:")
    engine.boot()
    markets = ["KXBTC15M-A", "KXBTC15M-B"]
    for _ in range(3):
        engine.cycle(markets, now=100.0)
    for m in markets:
        engine.close_window(m)
    rows = engine.ledger.db.execute(
        "SELECT lane FROM surface_rows WHERE lane IN ('F','H8','FLIP','D','P')").fetchall()
    assert rows == []
    assert engine.gateway.shadow_orders == []
    assert engine.gateway.resting == {}


# ── boot tape ────────────────────────────────────────────────────

def test_boot_tape_prints_desk_mode_and_every_rot_knob():
    tape = "\n".join(boot.boot_tape())
    assert "DESK_MODE=ROTATION" in tape
    assert "LANES OFF: F,H8,FLIP,OPEN,HUNT,PAIR,D,P" in tape
    knobs = config.rot_knobs()
    for name in ("ROT_POSITION_PCT", "ROT_DEPLOY_CAP_PCT", "ROT_MARKET_CAP_PCT",
                 "ROT_GROUP_CAP_PCT", "ROT_ENTRY_BAND", "ROT_TARGET_CENTS",
                 "ROT_STOP_CENTS", "ROT_HARD_STOP_CENTS", "ROT_GRACE_S",
                 "ROT_CONFIRM_READS", "ROT_CONFIRM_SPACING_S", "ROT_TIME_STOP_S"):
        assert name in knobs
    for name, value in knobs.items():
        assert f"{name} = {value} [" in tape


def test_rot_knob_values_match_the_work_order():
    assert config.ROT_POSITION_PCT == 0.05
    assert config.ROT_DEPLOY_CAP_PCT == 0.50
    assert config.ROT_MARKET_CAP_PCT == 0.05
    assert config.ROT_GROUP_CAP_PCT == 0.25
    assert config.ROT_ENTRY_BAND == (40, 54)
    assert config.ROT_TARGET_CENTS == 8
    assert config.ROT_STOP_CENTS == 10
    assert config.ROT_HARD_STOP_CENTS == 20
    assert config.ROT_GRACE_S == 60
    assert (config.ROT_CONFIRM_READS, config.ROT_CONFIRM_SPACING_S) == (2, 10)
    assert config.ROT_TIME_STOP_S == 360
    assert config.ROT_KNOB_TAGS["ROT_POSITION_PCT"] == "RULED(2026-10-14)"
