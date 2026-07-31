"""WO-INFRA-HARDENING E1 — the settlement source tracer (build 46).

The booking path was audited SOUND: settlement is gated on the exchange's own
outcome (mkt["result"]), the arithmetic is correct for held and exited legs,
record_settlement marks fills settled=1 (no re-settle double-book), and
window-econ divergence self-heals by quarantining to fills-truth. A
book-vs-exchange phantom is therefore a runtime/data condition, not a code
defect this repo can point to.

E1 makes every booked cent TRACEABLE so the next divergence names its source
instead of being eyeballed dollars later:
  - a per-settlement SETTLE_AUDIT provenance row (pnl, the exchange outcome,
    the per-fill contribution breakdown, net_held per side, the running book
    after the settlement books) with an unmatched-leg flag;
  - a reconcile-side RECON_BOOK_VENUE_DELTA record when the book disagrees
    with the venue and NOTHING is pending (0 unsettled fills, 0 resting) — the
    phantom signature.

Pure instrumentation: no strategy/geometry/Kelly/cash/halt change; the booked
P&L is unchanged."""

import json

import pytest

from relay_engine import config, failures
from relay_engine.shadow_runner import ShadowEngine

MKT, WIN = "KXBTC15M-E1", "w1"


def _audit(surface, market, lane):
    (d,) = surface.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE market=? AND lane=? "
        "AND state='SETTLE_AUDIT' ORDER BY id DESC LIMIT 1",
        (market, lane)).fetchone()
    return json.loads(d)


@pytest.fixture
def funnel_e1(ledger):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    yield
    failures._ledger = None


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "e1.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST",
                       boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


# ── the provenance row: every booked cent is attributed ────────────────────
def test_settle_audit_records_full_provenance(ledger, surface):
    ledger.record_fill(MKT, "F", "yes", "ENTRY", 61, 1, config.TIER_PROBE)
    per_lane = surface.settle_market(MKT, WIN, settled_yes=True)
    assert per_lane["F"] == 39                     # booked pnl UNCHANGED by E1
    a = _audit(surface, MKT, "F")
    assert a["pnl_cents"] == 39 and a["settled_yes"] is True
    assert a["net_held"] == {"yes": 1, "no": 0} and a["unmatched"] is False
    # the per-fill contributions reconstruct the booked pnl exactly
    assert sum(f["contribution_cents"] for f in a["fills"]) == 39
    assert "book_after_cents" in a


def test_contributions_sum_for_an_exited_leg(ledger, surface):
    # enter yes@50, custodian-exit@42 -> -8, net_held zeroes out (matched)
    ledger.record_fill(MKT, "P", "yes", "ENTRY", 50, 1, config.TIER_PROBE)
    ledger.record_fill(MKT, "P", "yes", "CUSTODIAN_EXIT", 42, 1, config.TIER_PROBE)
    per_lane = surface.settle_market(MKT, WIN, settled_yes=True)
    assert per_lane["P"] == -8
    a = _audit(surface, MKT, "P")
    assert sum(f["contribution_cents"] for f in a["fills"]) == -8
    assert a["net_held"] == {"yes": 0, "no": 0} and a["unmatched"] is False


# ── the unmatched-leg detector (a phantom over-exit) ───────────────────────
def test_unmatched_leg_flagged_and_failed(ledger, surface, funnel_e1):
    # an EXIT with no matching ENTRY: net_held goes negative — the signature
    # of a phantom over-exit (exits exceeding the position held)
    ledger.record_fill(MKT, "X", "yes", "EXIT", 49, 1, config.TIER_PROBE)
    surface.settle_market(MKT, WIN, settled_yes=True)
    a = _audit(surface, MKT, "X")
    assert a["net_held"]["yes"] == -1 and a["unmatched"] is True
    # a durable SETTLE_UNMATCHED_LEG row is written (alert=False — no page)
    n = ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='SETTLE_UNMATCHED_LEG'"
    ).fetchone()[0]
    assert n == 1


# ── E1 is pure instrumentation — the booked P&L is unchanged ───────────────
def test_e1_does_not_change_the_booked_pnl(ledger, surface):
    ledger.record_fill(MKT, "F", "yes", "ENTRY", 61, 1, config.TIER_PROBE)
    ledger.record_fill(MKT, "D", "no", "ENTRY", 35, 2, config.TIER_LEAN)
    surface.settle_market(MKT, WIN, settled_yes=True)
    assert ledger.lifetime_pnl_cents() == 39 - 70   # exactly the pre-E1 result


# ── the reconcile-side trail: the phantom signature is recorded ────────────
def test_recon_records_unexplained_book_venue_gap(engine, monkeypatch):
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    engine.gateway.venue_client = object()
    # venue $90 vs book $100, nothing pending -> the phantom signature
    monkeypatch.setattr(venue, "get_balance", lambda c: (90.0, 0.0))
    engine.standing_reconcile(now=2000.0)
    rows = engine.ledger.db.execute(
        "SELECT how_json FROM failures WHERE why_tag='RECON_BOOK_VENUE_DELTA'"
    ).fetchall()
    assert len(rows) == 1
    j = json.loads(rows[0][0])
    assert j["delta_cents"] == j["book_cents"] - j["venue_cents"]
    assert abs(j["delta_cents"]) > config.RECON_AUDIT_FLOOR_CENTS


# ── COLD AUDIT build 70 §2: refuse to reconcile across a settlement boundary ──
def test_recon_defers_when_venue_pv_disagrees_with_deployed(engine, monkeypatch):
    """The source fix: the venue reads cash and pv on different clocks. When the
    venue's pv (a position it still shows) disagrees with the engine's own
    open-position notional (`deployed_cents`, here 0 — the engine marked it
    settled), the read straddles a settlement boundary and its total is wrong by
    the notional. The reconcile REFUSES to compute on it (DEFERRED), rather than
    passing the bad number to `cash.reconcile`."""
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    engine.gateway.venue_client = object()
    # venue still shows a $0.98 position; the engine holds nothing (deployed 0)
    monkeypatch.setattr(venue, "get_balance", lambda c: (100.0, 0.98))
    assert engine.ledger.deployed_cents() == 0
    result = engine.standing_reconcile(now=2000.0)
    assert result == "DEFERRED"
    # and it did NOT record a divergence — we never computed the delta
    n = engine.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='RECON_BOOK_VENUE_DELTA'"
    ).fetchone()[0]
    assert n == 0


def test_recon_runs_when_venue_pv_matches_deployed(engine, monkeypatch):
    """When the venue's pv agrees with the engine's open-position notional, the
    read is internally consistent and the pv-check passes through to
    `cash.reconcile` (stubbed here to isolate the check from cash's own
    quiescence defer)."""
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    engine.gateway.venue_client = object()
    engine.ledger.record_fill(MKT, "F", "yes", "ENTRY", 98, 1, config.TIER_PROBE)
    assert engine.ledger.deployed_cents() == 98        # 1 lot × 98c entry
    monkeypatch.setattr(venue, "get_balance", lambda c: (100.0, 0.98))  # pv 98¢ agrees
    monkeypatch.setattr(engine.cash, "reconcile",
                        lambda **kw: "RECONCILED")      # isolate the pv-check
    result = engine.standing_reconcile(now=2000.0)
    assert result == "RECONCILED"                      # consistent → passed through


def test_recon_silent_when_pending_explains_the_gap(engine, monkeypatch):
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    engine.gateway.venue_client = object()
    monkeypatch.setattr(venue, "get_balance", lambda c: (90.0, 0.0))
    engine.gateway.resting["oid"] = object()        # in-flight explains a gap
    engine.standing_reconcile(now=2000.0)
    n = engine.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='RECON_BOOK_VENUE_DELTA'"
    ).fetchone()[0]
    assert n == 0                                    # pending -> not the phantom


# ── HARD RAIL — E1 is execution-layer instrumentation only ─────────────────
def test_rails_unchanged():
    assert config.RECON_AUDIT_FLOOR_CENTS == 2
    # the booking formula and the doctrine constants are untouched
    assert config.KELLY_FRACTION_CEILING == pytest.approx(1.0 / 12.0)
    assert config.OPEN_CATASTROPHE_FLOOR == 20
    assert not hasattr(config, "OPEN_SCALP_STOP_CENTS")   # build-45 stays
