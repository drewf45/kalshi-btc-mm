"""VERIFY GATE 6: attribution acceptance test — a synthetic STACKED market
(multiple lanes filled on one market) must be reconstructible per-lane from
surface rows ALONE."""

import pytest

from relay_engine import config
from relay_engine.errors import FatalIntegrityError
from relay_engine.surface import CUSTODIED_SETTLED, PASS, SETTLED, Surface


MKT, WIN = "KXBTC15M-STACK", "w1"


def test_stacked_market_reconstructs_per_lane_from_surface_rows_alone(ledger, surface):
    # Lane F: YES 1 @ 61c, held to settlement (YES) -> +39
    ledger.record_fill(MKT, "F", "yes", "ENTRY", 61, 1, config.TIER_PROBE)
    # Lane D: NO 2 @ 35c (no-terms), held to settlement (YES) -> loses basis: -70
    ledger.record_fill(MKT, "D", "no", "ENTRY", 35, 2, config.TIER_LEAN)
    # Lane P: YES 1 @ 50c, CUSTODIAN-exited at 42c before settlement -> -8,
    # attributed to the OPENING lane P (the custodian owns nothing)
    ledger.record_fill(MKT, "P", "yes", "ENTRY", 50, 1, config.TIER_PROBE)
    ledger.record_fill(MKT, "P", "yes", "CUSTODIAN_EXIT", 42, 1, config.TIER_PROBE)
    # Lanes MM, H8: first-class Pass rows
    surface.write_row("MM", MKT, WIN, PASS, detail="STUB_AWAITING_CHUNK_8")
    surface.write_row("H8", MKT, WIN, PASS, detail="GATED_ON_B1_NOT_PORTED")

    per_lane = surface.settle_market(MKT, WIN, settled_yes=True)
    assert per_lane == {"F": 39, "D": -70, "P": -8}

    # P17 §1.1: PASS is provisional until close — finalize writes the
    # terminal PASS rows for the lanes that never entered (MM, H8).
    surface.finalize_window(MKT, WIN)

    # THE ACCEPTANCE READ: surface rows alone, no fills table, no ledger math
    recon = surface.reconstruct_per_lane(MKT, WIN)
    assert recon == {"F": 39, "D": -70, "P": -8, "MM": 0, "H8": 0}

    # ledger agrees (win/loss path symmetry: winners and losers through one path)
    assert ledger.lifetime_pnl_cents() == 39 - 70 - 8

    # custodian exit landed as the OPENING lane's terminal state
    state = ledger.db.execute(
        "SELECT state FROM surface_rows WHERE market=? AND window_id=? AND lane='P'"
        " AND terminal=1", (MKT, WIN)).fetchone()[0]
    assert state == CUSTODIED_SETTLED


def test_per_fill_schema_complete(ledger):
    ledger.record_fill(MKT, "F", "yes", "ENTRY", 61, 1, config.TIER_PROBE)
    lane, market, side, price, count, tier = ledger.db.execute(
        "SELECT lane, market, side, price_cents, count, size_tier FROM fills").fetchone()
    # C.2 schema: per-fill (lane, market, side, cost-basis, size-tier)
    assert (lane, market, side, price, count, tier) == ("F", MKT, "yes", 61, 1, "PROBE")


def test_terminal_lattice_monotonic(surface):
    """P17 §1 overturned the old immutable-terminal law here: PASS is
    provisional (interim) mid-window; a stale PASS terminal meeting SETTLED
    UPGRADES (logged, never fatal); only a REGRESSION stays FATAL."""
    # Why Law §A1: every row carries a sentence — even these mechanics fixtures.
    surface.write_row("F", MKT, WIN, PASS, detail="no edge")     # provisional
    assert surface.write_row("F", MKT, WIN, PASS, detail="no edge") is False  # dedup
    assert surface.write_row("F", MKT, WIN, SETTLED, detail="pnl") is True    # heals
    with pytest.raises(FatalIntegrityError):
        surface.write_row("F", MKT, WIN, PASS, detail="no edge",
                          final=True)                            # regression = bug


def test_stale_pass_terminal_upgrades_logged(surface, ledger):
    from relay_engine import failures
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    try:
        surface.write_row("H8", MKT, WIN, PASS, detail="no edge",
                          final=True)                           # terminal PASS
        assert surface.write_row("H8", MKT, WIN, SETTLED, detail="pnl") is True
        row = ledger.db.execute(
            "SELECT detail FROM surface_rows WHERE lane='H8' AND state='SETTLED'"
        ).fetchone()[0]
        assert "upgrade from PASS" in row
        assert ledger.db.execute(
            "SELECT COUNT(*) FROM failures WHERE why_tag='TERMINAL_UPGRADED'"
        ).fetchone()[0] == 1
    finally:
        failures._ledger = None
        failures._alert_fn = None


def test_interim_rows_on_state_change_only(surface):
    assert surface.write_row("D", MKT, WIN, "PROPOSED", detail="cheap side") is True
    assert surface.write_row("D", MKT, WIN, "PROPOSED",
                             detail="cheap side") is False       # counter, not row
    assert surface.interim_counters[("D", "PROPOSED")] == 1
    assert surface.write_row("D", MKT, WIN, "ENTERED",
                             detail="filled") is True            # state change: full row


def test_settlement_split_no_side(ledger, surface):
    # settled NO: the NO entry wins, the YES entry loses
    ledger.record_fill(MKT, "F", "yes", "ENTRY", 61, 1, config.TIER_PROBE)
    ledger.record_fill(MKT, "D", "no", "ENTRY", 35, 2, config.TIER_LEAN)
    per_lane = surface.settle_market(MKT, WIN, settled_yes=False)
    assert per_lane == {"F": -61, "D": 130}  # (0-61) and 2*(100-35)
