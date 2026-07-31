"""CHUNK 4 verify: the recovered DUMP decision order (2biFE mechanics,
parameters set here in tests, not shipped), baton lifecycle, kill semantics,
F passthrough, size-tier discipline."""

import pytest

from relay_engine import config
from relay_engine.book import OrderBook
from relay_engine.custodian import CutParams, Custodian, OpenPosition, spot_is_safe
from relay_engine.errors import FatalIntegrityError
from relay_engine.feed import DegradeLadder
from relay_engine.gateway import Order

# Test-only parameter set (the engine ships NO defaults — per-lane values are
# Drew's to rule; §F forbids borrowing the daily bots' numbers as gates).
BASE = CutParams(
    hard_stop_usd=1.00, soft_stop_usd=0.75,
    min_time_remaining_s=15,
    hold_to_settle_s=30, spot_danger_buffer_usd=75,
    grace_period_s=10,
    early_exit_window_s=300, early_exit_loss_fraction=0.30,
    max_loss_fraction_of_balance=0.05, max_loss_fraction_of_cost=0.60,
    catastrophic_loss_cents=60,
    spot_safe_buffer_early_usd=100, spot_safe_buffer_late_usd=50,
    spot_safe_cutoff_s=60,
    rapid_drop_threshold=0.05, rapid_drop_window_s=10,
    max_loss_cents_per_contract=40,
    reversal_threshold=0.12, reversal_threshold_settling=0.20,
    reversal_threshold_profit=0.08, profit_tighten_above_entry=0.05,
    peak_window_s=30, proactive_after_s=30,
    prob_floor=0.50,
)

NOW = 10_000.0
# spot geometry: YES wins above lo=64_000
FAR_SAFE = dict(spot=65_000.0, boundary_lo=64_000.0, boundary_hi=None)
NEAR_DANGER = dict(spot=64_040.0, boundary_lo=64_000.0, boundary_hi=None)


def make_book():
    # wide derived ask (no_bid=1 -> yes ask 99) so 50-61c entries rest below it
    b = OrderBook(market="M1")
    b.apply_snapshot({45: 100}, {1: 80}, ts=1.0)
    return b


def pos(lane="D", tier=config.TIER_PROBE, entry=61, qty=1, entry_p=0.80,
        entry_age=400.0, exit_id=None):
    # default age 400s: past the early-exit window and the settling phase
    return OpenPosition(event="EV1", market="M1", lane=lane, side="yes",
                        count=qty, entry_price_cents=entry, entry_p_win=entry_p,
                        size_tier=tier, entry_time=NOW - entry_age,
                        resting_exit_id=exit_id)


@pytest.fixture
def custodian(gateway, ledger, surface):
    c = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    c.set_lane_params("D", BASE)
    c.set_lane_params("F", CutParams(**{**BASE.__dict__, "salvage_enabled": True}))
    return c


def cut(c, p, p_win, secs=300, exit_bid=None, balance=100.0, now=NOW, **spotkw):
    kw = {**FAR_SAFE, **spotkw}
    return c.should_cut(p, now=now, secs_remaining=secs, p_win=p_win,
                        exit_bid_cents=exit_bid, balance_usd=balance,
                        spot=kw["spot"], boundary_lo=kw["boundary_lo"],
                        boundary_hi=kw["boundary_hi"])


# ── the recovered decision order ──────────────────────────────────────────
def test_dollar_stops_use_worst_of_bid_and_prob(custodian):
    p = pos(qty=2)
    # prob says fine (0.55 -> $0.12 loss) but the BID says $1.12 — bid wins, hard stop
    assert cut(custodian, p, p_win=0.55, exit_bid=5) == "STOP_LOSS_HARD"
    # stops fire even inside grace (order: stops first, no exceptions)
    p2 = pos(qty=2, entry_age=2.0)
    assert cut(custodian, p2, p_win=0.55, exit_bid=5) == "STOP_LOSS_HARD"
    # soft stop band
    p3 = pos(qty=2)
    assert cut(custodian, p3, p_win=0.55, exit_bid=20) == "STOP_LOSS_SOFT"  # $0.82


def test_endgame_guard_holds_small_losses(custodian):
    assert cut(custodian, pos(), p_win=0.58, secs=10, **NEAR_DANGER) is None


def test_spot_safe_master_override_holds_through_book_panic(custodian):
    # prob crashed to 0.30 (below the floor) but spot is $1000 on our side:
    # the book is lying — HOLD
    assert cut(custodian, pos(), p_win=0.30) is None


def test_prob_floor_fires_only_when_spot_not_safe(custodian):
    assert cut(custodian, pos(), p_win=0.30, **NEAR_DANGER) == "PROB_FLOOR"


def test_reversal_on_windowed_peak(custodian):
    p = pos()
    assert cut(custodian, p, p_win=0.90, **NEAR_DANGER) is None
    assert cut(custodian, p, p_win=0.75, now=NOW + 5, **NEAR_DANGER) == "REVERSAL"


def test_rapid_drop_bail(custodian):
    p = pos()
    for i, pw in enumerate([0.95, 0.95, 0.95]):
        assert cut(custodian, p, p_win=pw, now=NOW + i * 2, **NEAR_DANGER) is None
    assert cut(custodian, p, p_win=0.88, now=NOW + 8, **NEAR_DANGER) == "RAPID_DROP"


def test_grace_period_holds_small_wobble(custodian):
    assert cut(custodian, pos(entry_age=5.0), p_win=0.55, **NEAR_DANGER) is None


def test_early_exit_underwater_beats_grace(custodian):
    # 31c down on a 61c entry inside the entry window = 51% of max loss >= 30%
    assert cut(custodian, pos(entry_age=5.0), p_win=0.55,
               exit_bid=30, **NEAR_DANGER) == "EARLY_EXIT_UNDERWATER"


def test_late_hold_to_settle_and_danger_override(custodian):
    # last 20s, spot far on our side: hold even at floor-level prob
    assert cut(custodian, pos(), p_win=0.30, secs=20) is None
    # last 20s but spot within the danger buffer: cut logic runs -> floor fires
    assert cut(custodian, pos(), p_win=0.30, secs=20, **NEAR_DANGER) == "PROB_FLOOR"


def test_bankroll_and_position_caps_precede_spot_safety(custodian):
    # $2 balance: cap $0.10; 31c loss breaches it even though spot is SAFE
    assert cut(custodian, pos(), p_win=0.30, balance=2.0) == "BANKROLL_CAP"


def test_catastrophic_backstop_precedes_spot_safety(custodian):
    # 61c entry, prob exit 0 -> 61c/contract >= 60c catastrophic, spot safe or not
    assert cut(custodian, pos(entry=95, entry_p=0.95), p_win=0.30) in (
        "POSITION_CAP", "CATASTROPHIC", "BANKROLL_CAP")
    r = cut(custodian, pos(entry=95, entry_p=0.95), p_win=0.30, balance=1e9)
    assert r in ("POSITION_CAP", "CATASTROPHIC")


def test_size_buys_discipline(custodian):
    # A 0.10 drop from windowed peak: PROBE threshold 0.12 holds; CLEAR's
    # scaled threshold (0.12/1.3 = 0.092) cuts. entry_p 0.95 keeps the
    # profit-tightened threshold out of play (peak never 0.05 above entry).
    probe = pos(tier=config.TIER_PROBE, entry_p=0.95)
    clear = pos(tier=config.TIER_CLEAR, entry_p=0.95)
    assert cut(custodian, probe, p_win=0.90, **NEAR_DANGER) is None
    assert cut(custodian, clear, p_win=0.90, **NEAR_DANGER) is None
    assert cut(custodian, probe, p_win=0.80, now=NOW + 5, **NEAR_DANGER) is None
    assert cut(custodian, clear, p_win=0.80, now=NOW + 5, **NEAR_DANGER) == "REVERSAL"


def test_lane_f_passthrough_catastrophic_only(custodian):
    p = pos(lane="F")
    # huge dollar loss + floor-level prob: passthrough still holds
    assert cut(custodian, p, p_win=0.20, exit_bid=2, **NEAR_DANGER) is None
    assert cut(custodian, pos(lane="F"), p_win=0.01, **NEAR_DANGER) == "CATASTROPHIC"
    assert cut(custodian, pos(lane="F", entry=95), p_win=0.30, **NEAR_DANGER) == "CATASTROPHIC"


def test_spot_is_safe_geometries():
    # YES above lo; NO below hi (range); NO below lo (up-or-down); unknown = not safe
    assert spot_is_safe("yes", 65_000, 64_000, None, 300, BASE) == (True, 1000)
    assert spot_is_safe("no", 63_000, None, 64_000, 300, BASE) == (True, 1000)
    assert spot_is_safe("no", 63_000, 64_000, None, 300, BASE) == (True, 1000)
    assert spot_is_safe("yes", None, 64_000, None, 300, BASE) == (False, 0.0)
    # late buffer is tighter than early
    assert spot_is_safe("yes", 64_060, 64_000, None, 30, BASE)[0] is True   # 60 >= 50
    assert spot_is_safe("yes", 64_060, 64_000, None, 300, BASE)[0] is False  # 60 < 100


# ── baton, kill, attribution (unchanged laws) ─────────────────────────────
def test_baton_lifecycle_cancel_then_cut(custodian, gateway):
    b = make_book()
    r = gateway.submit(Order(lane="D", event="EV1", market="M1", side="yes", action="buy", why="d-table verdict yes@61¢ · reserved",
                             price_cents=61, count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY"), b)
    gateway.on_fill(r.order_id)
    exit_r = gateway.submit(Order(lane="D", event="EV1", market="M1", side="yes",
                                  action="sell", price_cents=80, count=1,
                                  size_tier=config.TIER_PROBE, purpose="EXIT"), b)
    p = pos(exit_id=exit_r.order_id)
    custodian.adopt(p)
    cut_id = custodian.execute_cut(p, cut_price_cents=44, book=b, trigger="PROB_FLOOR")
    assert exit_r.order_id not in gateway.resting  # exit canceled FIRST
    assert cut_id in gateway.resting               # then the cut went out
    assert p.resting_exit_id is None
    assert "M1:D" not in custodian.positions


def test_baton_gone_exit_is_terminal_not_fatal(custodian):
    """P14 §1 overturned the old law here: an exit id the gateway no longer
    holds means it FILLED or already canceled — the venue's terminal state is
    truth, not a violation. The cut proceeds through §2 re-derivation (no
    fills rows for this adopted pos → the pos object stands, cut goes out).
    The FATAL now lives ONLY on a genuinely unverifiable cancel
    (test_p14_cut_law.py::test_genuinely_stuck_resting_still_fatal)."""
    p = pos(exit_id="FILLED-OR-CANCELED-ALREADY")
    custodian.adopt(p)
    cut_id = custodian.execute_cut(p, cut_price_cents=44, book=make_book(),
                                   trigger="PROB_FLOOR")
    assert cut_id is not None            # the cut went out, race-proof
    assert "M1:D" not in custodian.positions


def test_cut_attributes_to_opening_lane(custodian, gateway, ledger):
    b = make_book()
    r = gateway.submit(Order(lane="P", event="EV1", market="M1", side="yes", action="buy", why="P fade yes · displ +4.0c x2 spot-flat",
                             price_cents=50, count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY"), b)
    gateway.on_fill(r.order_id)
    p = OpenPosition(event="EV1", market="M1", lane="P", side="yes", count=1,
                     entry_price_cents=50, entry_p_win=0.8, size_tier=config.TIER_PROBE)
    custodian.adopt(p)
    custodian.execute_cut(p, cut_price_cents=42, book=b, trigger="REVERSAL")
    lane, action = ledger.db.execute(
        "SELECT lane, action FROM fills WHERE action='CUSTODIAN_EXIT'").fetchone()
    assert lane == "P"  # the OPENING lane's cells, not a custodian bucket


def test_kill_semantics_entries_only(custodian, gateway):
    b = make_book()
    r = gateway.submit(Order(lane="D", event="EV1", market="M1", side="yes", action="buy", why="d-table verdict yes@61¢ · reserved",
                             price_cents=61, count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY"), b)
    gateway.on_fill(r.order_id)
    custodian.adopt(pos())
    custodian.kill_lane("D")
    from relay_engine.errors import WallRejection
    with pytest.raises(WallRejection) as e:
        gateway.submit(Order(lane="D", event="EV2", market="M2", side="yes", action="buy", why="d-table verdict yes@61¢ · reserved",
                             price_cents=61, count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY"), b)
    assert e.value.wall == "ENTRIES_HALTED"
    # ...but the open position is still custodied and can still be cut
    p = custodian.positions["M1:D"]
    custodian.execute_cut(p, cut_price_cents=44, book=b, trigger="PROB_FLOOR")
    assert "M1:D" not in custodian.positions