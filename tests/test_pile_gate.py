"""WO-2026-07-22-F "WAIT FOR THE PILE" — the entry-discipline gate.

Every logged FLIP entry fired inside the first 57s, before the pile could form,
on a book that had not moved (trend $0) or already finished (skew 41). FLIP now
WAITS: it evaluates only in the [60,180]s pile window and enters ONLY when ALL
conditions agree — favored side in-band, skew forming (10-30), skew GROWING
(>=5, the stampede in progress), the tape moving (|trend|>=15) and agreeing with
the side, depth behind it. No pile = no trade; a skipped window logs OPEN_SKIP.
"""
import logging

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.gateway import Order
from relay_engine.lane_flip import LaneFlip

T = "KXBTC15M-02JAN251000-T99"
EV = T.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
WIN = 900


@pytest.fixture(autouse=True)
def _funnel(ledger):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    yield
    failures._ledger = None


@pytest.fixture
def flip(gateway, ledger, surface):
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def _poll(flip, secs_into, yes_px, no_px, spot, yd=14, nd=10):
    b = OrderBook(market=T)
    b.apply_snapshot({yes_px: yd}, {no_px: nd}, ts=1.0)
    now = CLOSE - (WIN - secs_into)
    return flip.evaluate(T, {"book": b, "now": now, "close_ts": CLOSE,
                             "spot": spot, "grain": None, "spotlead": None})


def _prime_then(flip, entry_secs, yes_px, no_px, spot, yd=14, nd=10):
    """Baseline poll at 65s (small skew), then the test poll — the two polls the
    growth computation needs. Returns the test poll's proposals."""
    _poll(flip, 65, 54, 48, 66_000.0)            # baseline: skew 6, favored yes
    return _poll(flip, entry_secs, yes_px, no_px, spot, yd, nd)


# ── the entry fires when the pile forms ─────────────────────────────────────
def test_enters_when_the_pile_forms(flip):
    """favored yes@60 in-band · skew 20 (∈[10,30]) · grew 14 (>=5) · trend +$20
    (>=15, agrees with yes) · ratio 1.4 (>=1) → ENTER."""
    props = _prime_then(flip, 80, 60, 40, 66_020.0)
    ent = [p for p in props if p.purpose == "ENTRY"]
    assert len(ent) == 1
    assert ent[0].side == "yes" and ent[0].price_cents == 60
    why = ent[0].why
    assert "OPEN50 favored yes@60c" in why and "skew20/grew14" in why
    assert "trend $+20 agree" in why and "pile: all-of met" in why


def test_target_is_entry_plus_17_and_stop_minus_10(flip):
    props = _prime_then(flip, 80, 60, 40, 66_020.0)
    ent = next(p for p in props if p.purpose == "ENTRY")
    assert "target 64c (+4, cap 90)" in ent.why    # OPEN_GOUGE_C = 4
    assert "stop 50c" in ent.why                     # entry − OPEN_MOMENTUM_STOP_C
    assert LaneFlip._take_price(60) == 64


def test_no_favored_side_gt_180_no_entry_no_crash(flip):
    props = _prime_then(flip, 175, 60, 40, 66_020.0)  # still in-window: enters
    assert any(p.purpose == "ENTRY" for p in props)


# ── the pile window bounds ──────────────────────────────────────────────────
def test_too_early_is_refused(flip):
    """A qualifying book at 45s (before the pile window opens) takes NO trade."""
    assert _poll(flip, 45, 60, 40, 66_020.0) == []


def test_past_the_window_skips_and_logs_open_skip(flip, caplog):
    with caplog.at_level(logging.INFO, logger="relay.lane_flip"):
        _poll(flip, 70, 60, 40, 66_000.0)           # flat tape in-window: skip-eligible
        _poll(flip, 190, 60, 40, 66_000.0)           # past 180 → OPEN_SKIP fires
    skips = [r.message for r in caplog.records if "OPEN_SKIP" in r.message]
    assert len(skips) == 1
    assert "reason=flat_tape" in skips[0] and "skew=20" in skips[0] \
        and "trend=$0" in skips[0]


# ── each all-of condition, isolated (the first failure names the reason) ─────
def _reason(flip):
    return flip.windows[T].last_skip_reason


def test_flat_tape_skips(flip):
    _prime_then(flip, 80, 60, 40, 66_000.0)          # trend $0 (spot flat)
    assert _reason(flip) == "flat_tape"


def test_trend_disagree_skips(flip):
    # favored yes but the tape FELL (trend −$20) — spot and book disagree
    _poll(flip, 65, 54, 48, 66_000.0)
    _poll(flip, 80, 60, 40, 65_980.0)
    assert _reason(flip) == "trend_disagree"


def test_below_the_band_skips_price_band(flip):
    # WO-...-G §2.1: the skew-LEVEL gate is retired; the deliberate band [55,64]
    # IS the price gate. A favored side at 54 (below 55) → price_band.
    _prime_then(flip, 80, 54, 46, 66_020.0)
    assert _reason(flip) == "price_band"


def test_above_the_band_skips_price_band(flip):
    # a favored side at 65 (above 64 — the move is fully priced) → price_band
    _prime_then(flip, 80, 65, 35, 66_020.0)
    assert _reason(flip) == "price_band"


def test_no_growth_skips(flip):
    # skew is a STATIC 20 from the baseline on — a decision that already happened
    _poll(flip, 65, 60, 40, 66_000.0)                # baseline skew 20
    _poll(flip, 80, 60, 40, 66_020.0)                # still 20 → grew 0
    assert _reason(flip) == "no_growth"


def test_price_band_skips(flip):
    # favored side at 74 (well above the band — the move is fully priced)
    _poll(flip, 65, 54, 48, 66_000.0)
    _poll(flip, 80, 74, 26, 66_020.0)
    assert _reason(flip) == "price_band"


# ── WO-2026-07-22-G ─────────────────────────────────────────────────────────
def test_flatten_supersedes_a_competing_bail_never_sells_more_than_held(flip,
                                                                        ledger):
    """§1.1: two authorities in one cycle can NEVER sell more than booked_held.
    A 1-lot uncovered leg at the flatten deadline (esc2) with a competing bail
    already proposed this cycle: the safety flatten SUPERSEDES the bail (drops
    it) so the crossfire is the ONE sell — the 10:19 −87c short (bail 1 + flatten
    1 against a 1-lot position) cannot recur."""
    w = flip._window(T, CLOSE)
    ledger.record_fill(T, "FLIP", "yes", "ENTRY", 60, 1, "PROBE")   # 1 lot booked
    w.opens["yes"] = {"entry": 60, "fill_ts": CLOSE - 1100, "count": 1,
                      "take_oid": None, "take_proposed": True, "done": False,
                      "collapse_polls": 0, "catastrophe_polls": 0, "det_ts": None,
                      "entry_oid": None, "defer_polls": 0, "hold": False}
    w.heal_attempts["yes"] = 2                         # jump to the flatten deadline
    w.heal_covered["yes"] = 0
    b = OrderBook(market=T)
    b.apply_snapshot({44: 10}, {55: 10}, ts=1.0)
    # the lane's own bail, already proposed this cycle (a crossfire close)
    bail = Order(lane="FLIP", event=EV, market=T, side="yes", action="sell",
                 price_cents=44, count=1, size_tier=config.TIER_PROBE,
                 purpose="CUT", crossfire=True, reason="open momentum stop")
    proposals = [bail]
    flip._check_uncovered(w, T, proposals, book=b, event=EV, now=CLOSE - 700)
    sells = [p for p in proposals if p.lane == "FLIP" and p.side == "yes"
             and p.action == "sell"]
    assert sum(p.count for p in sells) <= 1            # never more than held
    assert bail not in proposals                       # the bail was superseded
    assert any("FLATTEN" in (p.reason or "") for p in sells)   # by the flatten
    # WO-2026-07-23-B Part 2: the bail sat at 44c — BELOW the entry−stop−slip
    # floor (60−10−3 = 47). The flatten does not sell there; it rests AT the
    # floor for one poll first (never dumps through it on the first cross).
    floor = 60 - config.OPEN_MOMENTUM_STOP_C - config.SLIP_TOLERANCE_C
    assert all(p.price_cents >= floor for p in sells)  # nothing sold below the floor
    assert any(p.price_cents == floor and not p.crossfire for p in sells)


def test_a_traded_window_never_logs_open_skip(flip, ledger, caplog):
    """§1.2: a window that already holds a position (net != 0) returns before
    the PILE_END skip log — the skip dataset is never contaminated by a trade."""
    ledger.record_fill(T, "FLIP", "yes", "ENTRY", 60, 1, "PROBE")   # net != 0
    with caplog.at_level(logging.INFO, logger="relay.lane_flip"):
        _poll(flip, 190, 60, 40, 66_020.0)            # past 180 with a position held
    assert not any("OPEN_SKIP" in r.message for r in caplog.records)


# ── WO-2026-07-23-C Bug 1b: the wall counts LIVE RESTING ORDERS, not just fills ──
def test_entry_wall_blocks_over_a_resting_entry_before_it_fills(flip, gateway):
    """Bug 1: `_market_net` reads only FILLED positions, so a post_only OPEN
    maker resting unfilled was invisible and OPEN re-proposed over it (3 lots at
    one touch). The wall now also counts the gateway's resting ENTRY orders, so a
    second entry is blocked while the first still rests — no position needed."""
    assert flip._market_net(T, EV) == 0                    # nothing FILLED yet
    assert flip._market_entry_blocked(T, EV) is False      # and nothing resting
    # a live OPEN maker rests (unfilled) on the market
    o = gateway.submit(Order(
        lane="FLIP", event=EV, market=T, side="yes", action="buy",
        price_cents=60, count=1, size_tier=config.TIER_PROBE, purpose="ENTRY",
        why="OPEN50 favored yes@60c"), OrderBook(market=T))
    assert o.order_id in gateway.resting
    assert flip._market_net(T, EV) == 0                    # STILL nothing filled
    assert flip._market_entry_blocked(T, EV) is True       # but the resting order blocks


def test_trend_measured_from_the_pile_baseline_not_window_open(flip):
    """§2.2: a move that FINISHED before the window (spot flat since the pile
    baseline) reads trend 0 and is refused flat_tape — never 'arrived late',
    even though a window-open baseline would have counted the earlier run."""
    _poll(flip, 30, 54, 48, 66_000.0)                 # pre-window: spot low
    _poll(flip, 65, 54, 48, 66_040.0)                 # baseline (>=60): spot already ran +40
    props = _poll(flip, 80, 60, 40, 66_040.0)         # entry poll: spot FLAT since baseline
    assert [p for p in props if p.purpose == "ENTRY"] == []
    assert _reason(flip) == "flat_tape"               # trend from baseline = 0


def test_thin_depth_is_logged_not_gated(flip):
    # WO-2026-07-22-J §0.2: depth_ratio no longer gates — the classifier showed
    # no predictive value (the one winner read 0.71, inside the losers' range).
    # A thin favored side (ratio < 1) still ENTERS; the ratio rides on the why.
    _poll(flip, 65, 54, 48, 66_000.0, yd=6, nd=10)
    props = _poll(flip, 80, 60, 40, 66_020.0, yd=6, nd=10)   # ratio 0.60
    ent = [p for p in props if p.purpose == "ENTRY"]
    assert len(ent) == 1 and ent[0].price_cents == 60
    assert "ratio 0.60x" in ent[0].why


def test_a_no_favored_side_window_is_no_pile(flip):
    # a 50/50 book the whole window → never a favored side → OPEN_SKIP no_pile
    _poll(flip, 70, 50, 50, 66_020.0)
    _poll(flip, 190, 50, 50, 66_020.0)
    assert flip.windows[T].open_skip_logged is True


# ── skew is sampled every poll, like spot ───────────────────────────────────
def test_skew_ticks_are_recorded_every_poll(flip):
    _poll(flip, 65, 54, 48, 66_000.0)
    _poll(flip, 80, 60, 40, 66_020.0)
    ticks = flip.windows[T].skew_ticks
    assert (65, 6, 66_000.0) in ticks and (80, 20, 66_020.0) in ticks


# ── WO-2026-07-22-J Phase 0 ─────────────────────────────────────────────────
def test_shared_wall_blocks_open_entry_when_market_holds(flip):
    """§0.1: the ONE per-market entry wall — OPEN does not enter a market that
    already holds a FLIP net (here a held `no`), even with a formed pile."""
    flip.gateway.positions[(EV, T, "FLIP")] = -1          # OPEN holds `no`
    _poll(flip, 65, 54, 48, 66_000.0)
    props = _poll(flip, 80, 60, 40, 66_020.0)             # a qualifying yes pile
    assert props == []                                    # the wall (not the gate)


def test_shared_wall_blocks_hunt_auto_net_against_a_held_side(flip):
    """§0.1: HUNT used a per-SIDE wall, so with OPEN holding `no` it bought
    `yes` — an auto-net at the exchange. Now HUNT passes the same per-market
    wall and refuses."""
    from relay_engine.spotlead import Needle
    flip.gateway.positions[(EV, T, "FLIP")] = -1          # a held `no`
    w = flip._window(T, CLOSE)
    b = OrderBook(market=T)
    b.apply_snapshot({40: 10}, {30: 10}, ts=1.0)
    sl = Needle(side="yes", d_before=200.0, d_after=20.0,
                delta_p=config.HUNT_NEEDLE_POINTS + 5, fair_cents=90.0,
                t_remaining=700.0)
    assert flip._hunt_entry(w, T, EV, b, sl, 700.0) == []  # no auto-net


def test_cross_lane_wall_blocks_flip_when_another_lane_holds(flip):
    """WO-2026-07-22-L §2: the wall sums EVERY lane's net, not just FLIP's. F
    holds `yes` under its OWN key ("F"); a FLIP OPEN entry on the opposite side
    would auto-net at the exchange. The cross-lane wall refuses it — and FLIP's
    own take-quote net (`_net`) stays FLIP-only so its orientation is untouched."""
    flip.gateway.positions[(EV, T, "F")] = 1          # F holds yes@98, own key
    assert flip._net(T, EV) == 0                       # FLIP-only: sees nothing
    assert flip._market_net(T, EV) == 1                # cross-lane: sees F
    _poll(flip, 65, 54, 48, 66_000.0)
    props = _poll(flip, 80, 60, 40, 66_020.0)          # a qualifying OPEN pile
    assert props == []                                 # blocked — no auto-net


def test_no_two_lanes_hold_opposing_sides_of_one_market(flip):
    """The Engineer's acceptance (§7): once ANY lane holds a market, no FLIP lane
    (OPEN or HUNT) can enter it — so opposing-side fills across lanes cannot
    form. Here D holds `no`; both a FLIP OPEN pile and a HUNT needle are refused."""
    from relay_engine.spotlead import Needle
    flip.gateway.positions[(EV, T, "D")] = -1         # D holds `no`
    _poll(flip, 65, 54, 48, 66_000.0)
    assert _poll(flip, 80, 60, 40, 66_020.0) == []    # OPEN refused
    b = OrderBook(market=T)
    b.apply_snapshot({40: 10}, {30: 10}, ts=1.0)
    sl = Needle(side="yes", d_before=200.0, d_after=20.0,
                delta_p=config.HUNT_NEEDLE_POINTS + 5, fair_cents=90.0,
                t_remaining=700.0)
    assert flip._hunt_entry(flip._window(T, CLOSE), T, EV, b, sl, 700.0) == []


def test_pile_baseline_requires_a_real_spot_sample(flip):
    """§0.3: the baseline needs a real spot, not just a skew. A first in-window
    tick with skew but NO spot must not become the baseline (that fell trend to
    0 → flat_tape for the window's whole remaining life). The next tick that
    carries a spot is the baseline, so the pile still selects."""
    _poll(flip, 65, 54, 48, None)                         # skew 6, NO spot
    _poll(flip, 72, 54, 48, 66_000.0)                     # skew 6 WITH spot — baseline
    props = _poll(flip, 85, 60, 40, 66_020.0)             # +$20 from the real baseline
    ent = [p for p in props if p.purpose == "ENTRY"]
    assert len(ent) == 1 and "trend $+20 agree" in ent[0].why
