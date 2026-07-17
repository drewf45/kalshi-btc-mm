import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_engine.ledger import CashProtocol, Ledger  # noqa: E402
from relay_engine.surface import Surface  # noqa: E402
from relay_engine.gateway import Gateway  # noqa: E402


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
