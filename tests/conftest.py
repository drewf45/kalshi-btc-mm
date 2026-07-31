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
