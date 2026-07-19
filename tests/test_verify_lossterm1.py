"""WO-VERIFY-LOSSTERM-1 — verification pass over the shipped loss-term
fixes + Kelly-throttle legibility. NO trading-behavior change: no sizing
constant, gate, floor, band, or threshold moves in this file's laws (B5 —
if a constant would need to change to pass a test, the test is wrong).

B1: salvage registers the loss-term or FATALs at boot (never silence).
B2: the flip covers every contract — 2-lot lifecycle with ZERO
    FLIP_UNCOVERED_LEG pages; the alarm stays intact on a forced gap.
B3: the cash-fatal survives reboot at the ENGINE level (boot line + wall).
B4: the Kelly throttle is legible — the boot line says the binder in
    words; a favorite sizing to 0 logs SIZE_ZERO_BY_KELLY.
"""

import json
import logging

import pytest

from relay_engine import config, failures
from relay_engine.custodian import Custodian, OpenPosition, salvage_params
from relay_engine.errors import FatalIntegrityError
from relay_engine.feed import DegradeLadder

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0


# ── B1: salvage computes the loss-term, provably, at boot ──────────────────
def test_b1_adoption_armed_with_anchor(ledger, surface, gateway):
    """A held position with an anchor registers SALVAGE_ARMED carrying the
    anchor values — a POSITIVE firing, not an absence of error."""
    c = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    c.set_lane_params("F", salvage_params())
    c.adopt(OpenPosition(event=EVENT, market=TICKER, lane="F", side="yes",
                         count=1, entry_price_cents=95, entry_p_win=0.95,
                         size_tier=config.TIER_PROBE, entry_time=CLOSE - 600,
                         d_entry=200.0, t_entry=500.0, p_entry=0.93))
    row = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='SALVAGE_ARMED'"
        " AND market=?", (TICKER,)).fetchone()
    d = json.loads(row[0])
    assert d["p_entry"] == 0.93 and d["d"] == 200.0 and d["t"] == 500.0


def test_b1_adoption_disabled_tagged_and_backstop_reachable(ledger, surface,
                                                            gateway):
    """Table unavailable -> SALVAGE_DISABLED_TAGGED with its reason (never
    silent), and the 5% catastrophic backstop still fires anchorless."""
    c = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    c.set_lane_params("F", salvage_params())
    pos = OpenPosition(event=EVENT, market=TICKER, lane="F", side="yes",
                       count=1, entry_price_cents=95, entry_p_win=0.95,
                       size_tier=config.TIER_PROBE, entry_time=CLOSE - 600,
                       p_entry=None)
    c.adopt(pos, disabled_reason="table")
    row = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE"
        " state='SALVAGE_DISABLED_TAGGED' AND market=?", (TICKER,)).fetchone()
    assert json.loads(row[0])["reason"] == "table"
    assert c.should_cut(pos, now=1.0, secs_remaining=400, p_win=0.04,
                        exit_bid_cents=4, spot=None, boundary_lo=None,
                        boundary_hi=None, balance_usd=100.0) == "CATASTROPHIC"


def test_b1_boot_selftest_prints_and_passes(tmp_path, capsys):
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "b1.db"))
    e.boot()
    out = capsys.readouterr().out
    assert "SALVAGE SELF-TEST: ARMED fires" in out
    assert "loss-term wired (B1)" in out
    # the throwaway ledger's rows never touch the live surface
    assert e.ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE market LIKE 'SELFTEST%'"
    ).fetchone()[0] == 0
    failures._ledger = None


def test_b1_selftest_fatal_when_registration_unreachable(tmp_path,
                                                         monkeypatch):
    """The Adversary's guard: a verification that cannot fail is a lie.
    Sabotage the registration write -> the boot self-test FATALs loud
    rather than let a held position die silent."""
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "b1f.db"))
    monkeypatch.setattr(Custodian, "adopt",
                        lambda self, pos, disabled_reason=None: None)
    with pytest.raises(FatalIntegrityError, match="SALVAGE_SELFTEST_FAILED"):
        e.salvage_selftest()
    failures._ledger = None
