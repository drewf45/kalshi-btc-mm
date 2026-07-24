"""KAL-50/50 Stage 0.1 — THE PER-LANE RATE HALT.

FLIP's losing streak used to halt the whole gateway — and F, the only earner,
with it. The rate halt is now decided PER LANE on per-lane fills P&L: a lane
that trips a 2-of-4 streak halts ONLY itself ("RATE_HALT:FLIP"); every other
lane trades on. The legacy global halt (no lane attribution) is preserved
byte-for-byte for callers that don't pass per_lane, and LANE_KILL / cash-fatal
/ orientation reasons stay global — only the rate halt learns lanes."""

import json

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.errors import WallRejection
from relay_engine.gateway import Gateway, Order
from relay_engine.window_econ import HALT_REASON, LANES_HALTED_KEY, WindowEcon

BOOK = 10_000
# WO-2026-07-24-C: a per-window FLIP loss such that TWO cross the size-derived
# drawdown threshold (relative, so the test survives cap/threshold changes).
HALF = config.RATE_HALT_DRAWDOWN_C // 2 + 50
PROOF_WHYS = {
    "F": "F tier61 · surv~price",
    "FLIP": "OPEN grain yesx2 · join 48c · PROBE n=0 · geometry=v2",
}


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


def make_book(market="M1", no_bid=1):
    b = OrderBook(market=market)
    b.apply_snapshot({45: 100, 44: 50}, {no_bid: 80}, ts=1.0)
    return b


def entry(lane, market="M1", event="EV1", price=48, count=1):
    return Order(lane=lane, event=event, market=market, side="yes", action="buy",
                 price_cents=price, count=count, size_tier=config.TIER_PROBE,
                 purpose="ENTRY", why=PROOF_WHYS[lane])


# ── the wall is lane-aware ──────────────────────────────────────────────────
def test_entries_halted_for_scopes_the_rate_halt(gateway):
    """A scoped RATE_HALT:FLIP blocks FLIP alone; a bare RATE_HALT and every
    non-rate reason block all lanes."""
    gateway.halt_entries("RATE_HALT:FLIP")
    assert gateway.entries_halted_for("FLIP") == {"RATE_HALT:FLIP"}
    assert gateway.entries_halted_for("F") == set()      # F is free
    gateway.halt_entries("RATE_HALT")                    # legacy global
    assert gateway.entries_halted_for("F") == {"RATE_HALT"}


def test_lane_kill_and_orientation_stay_global(gateway):
    """The colon in LANE_KILL:FLIP must NOT be read as a rate-halt scope — a
    lane kill (and any non-rate reason) still stops every lane."""
    gateway.halt_entries("LANE_KILL:FLIP")
    gateway.halt_entries("ORIENTATION_DIVERGENCE")
    assert gateway.entries_halted_for("F") == {"LANE_KILL:FLIP",
                                               "ORIENTATION_DIVERGENCE"}


def test_wall_blocks_flip_but_lets_f_through(gateway):
    """End-to-end at the submit wall: with FLIP rate-halted, an F entry never
    raises ENTRIES_HALTED; a FLIP entry does."""
    b = make_book()
    gateway.halt_entries("RATE_HALT:FLIP")
    # F is not stopped by the FLIP halt (may pass or hit an unrelated wall,
    # but NEVER ENTRIES_HALTED)
    try:
        assert gateway.submit(entry("F", price=97), b) is not None
    except WallRejection as e:
        assert e.wall != "ENTRIES_HALTED"
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry("FLIP"), b)
    assert e.value.wall == "ENTRIES_HALTED" and "RATE_HALT:FLIP" in str(e.value)


# ── the halt DECISION is per-lane and MONEY-based (WO-2026-07-24-C) ─────────
def test_flip_drawdown_halts_flip_only_f_untouched(econ, gateway):
    """FLIP draws down past the money threshold while F wins every time — FLIP
    halts, F does not, and the pages name the lane. A profitable FLIP win in the
    window reduces the drawdown; only the SUM crossing the bound halts."""
    windows = [("M0", {"F": +6, "FLIP": -HALF}),
               ("M1", {"F": +5, "FLIP": +4}),   # FLIP win between reduces drawdown
               ("M2", {"F": +6, "FLIP": -HALF})]  # FLIP sum < −threshold
    for market, per_lane in windows:
        net = sum(per_lane.values())
        econ._apply_streak(market, net, BOOK, per_lane=per_lane)
    assert econ.halted_lanes() == {"FLIP"}
    assert "RATE_HALT:FLIP" in gateway.entries_halted_reasons
    assert "RATE_HALT:F" not in gateway.entries_halted_reasons
    assert not econ.halted()                     # the legacy global flag never set
    page = next(a for a in econ.telegram.alerts if "RATE HALT" in a)
    assert "FLIP" in page and "other lanes trade on" in page


def test_f_never_arms_from_its_own_wins(econ):
    """F wins every window — its per-lane drawdown stays positive regardless of
    how badly FLIP does beside it."""
    for i in range(4):
        econ._apply_streak(f"M{i}", 0, BOOK, per_lane={"F": +6, "FLIP": -HALF})
    assert econ.halted_lanes() == {"FLIP"}       # FLIP crosses the threshold; F never


def test_per_lane_halt_persists_across_boot(econ, ledger, gateway, surface):
    """A per-lane halt survives a redeploy: a fresh econ over the same DB
    restores the scoped reason."""
    for market in ("M0", "M1"):
        econ._apply_streak(market, 0, BOOK, per_lane={"FLIP": -HALF})
    assert econ.halted_lanes() == {"FLIP"}
    gw2 = Gateway(ledger, surface)
    econ2 = WindowEcon(ledger, gw2, surface, _TG())
    assert econ2.restore_halt_on_boot() is True
    assert "RATE_HALT:FLIP" in gw2.entries_halted_reasons
    assert gw2.entries_halted_for("F") == set()  # F still free after the reboot


def test_reset_clears_lane_halt_and_its_window(econ, ledger, gateway):
    """/reset_halt lifts the scoped reason AND wipes the lane's rolling window
    so it restarts clean."""
    for market in ("M0", "M1"):
        econ._apply_streak(market, 0, BOOK, per_lane={"FLIP": -HALF})
    assert econ.reset_halt().startswith("halt cleared")
    assert econ.halted_lanes() == set()
    assert "RATE_HALT:FLIP" not in gateway.entries_halted_reasons
    assert json.loads(ledger.get_state("rate_halt_outcomes:FLIP")) == []


def test_no_path_sets_the_global_halt_key_but_a_legacy_one_still_clears(econ,
                                                                        ledger,
                                                                        gateway):
    """Acceptance #1: nothing SETS HALT_KEY going forward (a per-lane halt leaves
    it clear), but a persisted legacy global halt stays READABLE and /reset_halt
    still lifts it."""
    from relay_engine.window_econ import HALT_KEY
    # a per-lane halt never sets the global key
    for m in ("M0", "M1"):
        econ._apply_streak(m, 0, BOOK, per_lane={"FLIP": -70})
    assert ledger.get_state(HALT_KEY) != "1"        # global flag untouched
    # a persisted legacy global halt (from an old build) is still clearable
    ledger.set_state(HALT_KEY, "1")
    gateway.halt_entries("RATE_HALT")
    assert econ.halted()
    reply = econ.reset_halt()
    assert "RATE_HALT" in reply and not econ.halted()
    assert "RATE_HALT" not in gateway.entries_halted_reasons


def test_global_fallback_is_retired_halts_nothing(econ, gateway):
    """WO-2026-07-24-C Part 1: with NO per_lane attribution the global fallback
    is RETIRED — it can no longer halt every lane (F included) on an aggregate
    loss. It logs and returns; nothing is set."""
    for i, pnl in enumerate((-200, -200, -200)):   # would have tripped the old 2-of-4
        econ._apply_streak(f"M{i}", pnl, BOOK)     # no per_lane
    assert not econ.halted()                       # global flag never set
    assert "RATE_HALT" not in gateway.entries_halted_reasons
    assert gateway.entries_halted_for("F") == set()   # F is free — no lane's loss stops it
