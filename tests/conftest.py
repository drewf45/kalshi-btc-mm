import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_engine.ledger import CashProtocol, Ledger  # noqa: E402
from relay_engine.surface import Surface  # noqa: E402
from relay_engine.gateway import Gateway  # noqa: E402


@pytest.fixture(autouse=True)
def _no_table_autobuild(monkeypatch):
    # P26 §1.1: boot provisions the delta table (network build). Tests never
    # fetch — the provisioning paths are exercised with mocked builders.
    from relay_engine import config
    monkeypatch.setattr(config, "TABLE_AUTOBUILD", False)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "legacy_desk: run under DESK_MODE=LEGACY (pre-desk lanes ON)")


@pytest.fixture(autouse=True)
def _heartbeat_isolation(tmp_path, monkeypatch):
    """WO-ROT-1 Chunk 1: every completed cycle writes the heartbeat file — keep
    it in the test's tmp dir, never ~/Library/Application Support."""
    from relay_engine import config
    monkeypatch.setattr(config, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))


@pytest.fixture(autouse=True)
def _desk_mode(request, monkeypatch):
    """WO-ROT-1 Chunk 0 — the suite runs under the branch default
    (DESK_MODE=ROTATION: pre-desk lanes OFF). Modules that prove the pre-desk
    lanes end to end through the runner carry `legacy_desk` and run with those
    lanes ON — the same config lever Drew would pull to turn them back on."""
    from relay_engine import config
    if request.node.get_closest_marker("legacy_desk"):
        monkeypatch.setattr(config, "DESK_MODE", "LEGACY")
        # These pre-desk end-to-end mechanics (watch ladder, H8 static gate) were
        # authored against the ORIGINAL $100 paper book — the H8 order budget and
        # lot geometry need that book size to clear the min-lot gate. The live
        # shadow baseline is now $50 (SHADOW_PAPER_BANKROLL_USD); pin the legacy
        # book so these keep exercising the lane machinery, not the new baseline.
        monkeypatch.setattr(config, "SHADOW_PAPER_BANKROLL_USD", 100.0)


@pytest.fixture(autouse=True)
def _roster_isolation():
    """WO-2026-07-27-W P3 — the roster lives on MODULE globals that boot and
    /series mutate in place (config.SERIES / SERIES_MODE, lane_fh8.F_SERIES_
    ALLOWED). Snapshot and restore them around every test so a full-engine boot
    in one test never leaks its 3-room roster into a later lane-mechanic test —
    keeping the suite order-independent (preflight runs it whole)."""
    from relay_engine import config, lane_fh8
    series = list(config.SERIES)
    modes = dict(config.SERIES_MODE)
    source = config.ROSTER_SOURCE
    fam = set(lane_fh8.F_SERIES_ALLOWED)
    yield
    config.SERIES[:] = series
    config.SERIES_MODE.clear()
    config.SERIES_MODE.update(modes)
    config.ROSTER_SOURCE = source
    lane_fh8.F_SERIES_ALLOWED = fam


@pytest.fixture
def ledger():
    led = Ledger(":memory:")
    led.baseline(10_000, confirmed_by="boot")  # $100 book
    led.snapshot_caps_at_boot()
    return led


@pytest.fixture
def surface(ledger):
    return Surface(ledger)


@pytest.fixture
def gateway(ledger, surface):
    return Gateway(ledger, surface)


@pytest.fixture
def cash(ledger):
    alerts = []
    proto = CashProtocol(ledger, alert_fn=alerts.append)
    proto.test_alerts = alerts
    return proto
