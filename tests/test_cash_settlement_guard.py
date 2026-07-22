"""WO-2026-07-22 (build 55) — the settlement-booking guards. The overnight halt
was the cash rail correctly refusing to trade on a book inflated ~$1.99 by a bad
settlement row (gross-as-net, or a win booked as a loss). Two root-cause guards
at record_settlement: IDEMPOTENCY (no double-book on retry/reboot) and a
NET-VS-GROSS bound (caught LOUD at the source, not 6h later at the halt)."""

import pytest

from relay_engine import config, failures


@pytest.fixture
def funnel(ledger):
    alerts = []
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=alerts.append, run_mode="TEST",
                       boot_id=1)
    yield alerts
    failures._ledger = None


def _settlements(ledger, market):
    return ledger.db.execute(
        "SELECT COUNT(*) FROM settlements WHERE market=? AND divergent=0",
        (market,)).fetchone()[0]


def _paged(ledger, tag):
    return ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag=?", (tag,)).fetchone()[0]


# ── IDEMPOTENCY: a retry / reboot mid-settle must not double-book ───────────
def test_settlement_is_idempotent_per_market_lane(ledger, funnel):
    ledger.record_fill("M1", "F", "yes", "ENTRY", 97, 2, "PROBE")
    ledger.record_settlement("M1", "F", 6)          # net +6c (win, 2@97)
    ledger.record_settlement("M1", "F", 6)          # a retry — must be ignored
    assert _settlements(ledger, "M1") == 1          # ONCE, not twice
    # the quarantine re-book (a different lane) is NOT blocked by the guard
    ledger.quarantine_divergent_settlements("M1", 5)
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM settlements WHERE market='M1'").fetchone()[0] == 2


# ── NET-VS-GROSS: the overnight bug, caught at the source ───────────────────
def test_gross_as_net_breaches_loud(ledger, funnel):
    """F 2@97: cost 194, net bound [−194, +6]. Booking the GROSS 200c (the
    overnight shape) is outside the bound → SETTLE_NOTIONAL_BREACH pages."""
    ledger.record_fill("M1", "F", "yes", "ENTRY", 97, 2, "PROBE")
    ledger.record_settlement("M1", "F", 200)        # gross-as-net
    assert _paged(ledger, "SETTLE_NOTIONAL_BREACH") == 1
    assert any("SETTLE_NOTIONAL_BREACH" in a for a in funnel)


def test_below_the_lower_bound_breaches_loud(ledger, funnel):
    """A loss magnitude beyond the position's own cost (−220 on a 194c cost) is
    impossible from these fills → outside the lower bound, paged."""
    ledger.record_fill("M1", "F", "yes", "ENTRY", 97, 2, "PROBE")
    ledger.record_settlement("M1", "F", -220)
    assert _paged(ledger, "SETTLE_NOTIONAL_BREACH") == 1


def test_correct_net_settlement_does_not_page(ledger, funnel):
    """The honest net (+6 win, or −194 total loss) is inside the bound — silent."""
    ledger.record_fill("M1", "F", "yes", "ENTRY", 97, 2, "PROBE")
    ledger.record_settlement("M1", "F", 6)
    ledger.record_fill("M2", "F", "yes", "ENTRY", 97, 2, "PROBE")
    ledger.record_settlement("M2", "F", -194)       # the favorite lost — full cost
    assert _paged(ledger, "SETTLE_NOTIONAL_BREACH") == 0


def test_cheap_flip_win_is_within_bound(ledger, funnel):
    """A cheap FLIP win (entry 30, +70 net) is legitimately large vs cost —
    the bound is [−cost, count·100−cost], not [−cost, +small], so it is fine."""
    ledger.record_fill("M1", "FLIP", "yes", "ENTRY", 30, 1, "PROBE")
    ledger.record_settlement("M1", "FLIP", 70)      # won: 100 − 30
    assert _paged(ledger, "SETTLE_NOTIONAL_BREACH") == 0


# ── QUARANTINE preserves book-truth (the fix path, not re-baseline) ─────────
def test_quarantine_drops_the_inflated_book(ledger, funnel):
    ledger.record_fill("M1", "F", "yes", "ENTRY", 97, 2, "PROBE")
    b0 = ledger.book_cents()
    ledger.record_settlement("M1", "F", 200)        # inflated (breach paged)
    assert ledger.book_cents() == b0 + 200
    cm_before = ledger.db.execute(
        "SELECT COUNT(*) FROM cash_movements").fetchone()[0]
    removed = ledger.quarantine_divergent_settlements("M1", 6)   # re-book at truth
    assert removed == 194 and ledger.book_cents() == b0 + 6
    # NO new cash_movement absorbed the gap — it was excluded, not papered over
    # (the /confirm_cash re-baseline path is exactly what we must not do)
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM cash_movements").fetchone()[0] == cm_before


def test_diagnose_script_flags_the_suspect(ledger, tmp_path, capsys):
    """The forensic tool identifies the out-of-bound row by name."""
    ledger.record_fill("KXBTC15M-X", "F", "yes", "ENTRY", 97, 2, "PROBE")
    ledger.record_settlement("KXBTC15M-X", "F", 200)
    import scripts.cash_diverge_diagnose as diag
    diag.diagnose(ledger, since=None)
    out = capsys.readouterr().out
    assert "OUT OF BOUND" in out and "KXBTC15M-X" in out
