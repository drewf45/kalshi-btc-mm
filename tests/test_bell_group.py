"""WO-2026-07-28-X X5 — BELL-GROUP BRACKETS (fixing X1 honestly, retiring X2).

The account-delta check moved from the market to the BELL (the settlement
instant). At each bell: Σ(all settling markets' fills-math) vs the account delta
across the bell ± tolerance. A shared bell (≥2 rooms settling together) no longer
cries wolf — the old per-market model attributed each room's whole account
movement to that room alone. A genuine break (a phantom, a missing fill) pages
BELL_ECON_DIVERGENCE with the members and marks the bell DISPUTED.
"""

import json

import pytest

from relay_engine import config, failures
from relay_engine.window_econ import WindowEcon

BOOK = 10_000


class _TG:
    def __init__(self):
        self.alerts = []

    def alert(self, m):
        self.alerts.append(m)


@pytest.fixture
def econ(ledger, gateway, surface):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    e = WindowEcon(ledger, gateway, surface, _TG())
    yield e
    failures._ledger = None


# ── X1 fixed: a shared bell of three rooms reconciles, no false divergence ───
def test_shared_bell_of_three_rooms_reconciles_clean(econ):
    """BTC +11, XRP +27, ETH +9 all settle on the same 21:15 bell. Per-market
    (X1) each room's account delta absorbed the OTHERS' settlements → false
    divergence every time. The BELL sums the three fills against the ONE account
    delta across the bell — clean, no page."""
    for m in ("KXBTC15M-A", "KXXRP15M-A", "KXETH15M-A"):
        econ.open_bracket(m, BOOK, now=1000.0)
    # the account climbs cumulatively as each settles within the same bell grace
    econ.close_bracket("KXBTC15M-A", BOOK + 11, 11, now=1500.0)
    econ.close_bracket("KXXRP15M-A", BOOK + 38, 27, now=1501.0)
    econ.close_bracket("KXETH15M-A", BOOK + 47, 9, now=1502.0)
    v = econ.reconcile_bell(econ._bell_id(1500.0))
    assert v["reconciled"] and not v["diverged"]
    assert v["sum_fills"] == 47 and v["acct_delta"] == 47   # Σ fills == account Δ
    assert set(v["members"]) == {"KXBTC15M-A", "KXXRP15M-A", "KXETH15M-A"}
    # NO per-market and NO bell divergence page fired
    for tag in ("WINDOW_ECON_DIVERGENCE", "BELL_ECON_DIVERGENCE"):
        assert econ.ledger.db.execute(
            "SELECT COUNT(*) FROM failures WHERE why_tag=?", (tag,)
        ).fetchone()[0] == 0


def test_a_real_break_at_the_bell_pages_and_disputes(econ):
    """A missing fill: the account moved +50 but the fills only account for +40 —
    a real break. The bell pages BELL_ECON_DIVERGENCE with the members and marks
    every member's settlement DISPUTED (out of book/lifetime/cells)."""
    for m in ("KXBTC15M-B", "KXXRP15M-B"):
        econ.open_bracket(m, BOOK, now=2000.0)
        econ.ledger.record_settlement(m, "F", 25, "settle")   # book +25 each = +50
    econ.close_bracket("KXBTC15M-B", BOOK + 25, 25, now=2500.0)
    econ.close_bracket("KXXRP15M-B", BOOK + 50, 15, now=2501.0)   # fills say +15 (missing)
    v = econ.reconcile_bell(econ._bell_id(2500.0))
    assert v["diverged"] and v["sum_fills"] == 40 and v["acct_delta"] == 50
    # both members disputed, no re-book (X2 retired)
    disputed = econ.ledger.db.execute(
        "SELECT COUNT(*) FROM settlements WHERE divergent=1").fetchone()[0]
    assert disputed == 2
    assert econ.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='BELL_ECON_DIVERGENCE'"
    ).fetchone()[0] == 1


def test_a_straggler_beyond_grace_forms_its_own_bell(econ):
    """A member settling more than BELL_GRACE_S after the bell forms its OWN bell
    (the Adversary's late-XRP case) — it never re-opens a reconciled bell."""
    econ.open_bracket("KXBTC15M-C", BOOK, now=3000.0)
    econ.open_bracket("KXXRP15M-C", BOOK, now=3000.0)
    econ.close_bracket("KXBTC15M-C", BOOK + 5, 5, now=3500.0)
    late = 3500.0 + config.BELL_GRACE_S + 5      # beyond grace
    econ.close_bracket("KXXRP15M-C", BOOK + 12, 7, now=late)
    assert econ._bell_id(3500.0) != econ._bell_id(late)   # two distinct bells
    v1 = econ.reconcile_bell(econ._bell_id(3500.0))
    v2 = econ.reconcile_bell(econ._bell_id(late))
    assert set(v1["members"]) == {"KXBTC15M-C"}
    assert set(v2["members"]) == {"KXXRP15M-C"}


def test_reconcile_is_idempotent_per_bell(econ):
    econ.open_bracket("KXBTC15M-D", BOOK, now=4000.0)
    econ.close_bracket("KXBTC15M-D", BOOK + 8, 8, now=4500.0)
    bid = econ._bell_id(4500.0)
    assert econ.reconcile_bell(bid)["reconciled"] is True
    assert econ.reconcile_bell(bid)["reconciled"] is False   # already done


def test_ready_bells_reconcile_after_grace(econ):
    econ.open_bracket("KXBTC15M-E", BOOK, now=5000.0)
    econ.close_bracket("KXBTC15M-E", BOOK + 3, 3, now=5500.0)
    assert econ.reconcile_ready_bells(now=5500.0 + 1) == 0    # grace not elapsed
    assert econ.reconcile_ready_bells(now=5500.0 + config.BELL_GRACE_S + 1) == 1


def test_bell_econ_divergence_registered_and_grace_tagged():
    from relay_engine import registry
    q = registry.get("BELL_ECON_DIVERGENCE")
    assert q is not None and q.is_complete()
    assert "BELL_ECON_DIVERGENCE" in registry.SEED_SURFACES
    tag = next(t for t in config.constant_tags() if t.name == "BELL_GRACE_S")
    assert tag.kind == config.DREW_DEFAULT
