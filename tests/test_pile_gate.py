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

from relay_engine import config
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip

T = "KXBTC15M-02JAN251000-T99"
CLOSE = 1_000_000.0
WIN = 900


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
    assert "target 77c (+17, cap 90)" in ent.why    # OPEN_GOUGE_C = 17
    assert "stop 50c" in ent.why                     # entry − OPEN_MOMENTUM_STOP_C
    assert LaneFlip._take_price(60) == 77


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


def test_skew_low_skips(flip):
    _prime_then(flip, 80, 55, 48, 66_020.0)          # skew 7 (<10)
    assert _reason(flip) == "skew_low"


def test_skew_high_skips(flip):
    _prime_then(flip, 80, 68, 33, 66_020.0)          # skew 35 (>30)
    assert _reason(flip) == "skew_high"


def test_no_growth_skips(flip):
    # skew is a STATIC 20 from the baseline on — a decision that already happened
    _poll(flip, 65, 60, 40, 66_000.0)                # baseline skew 20
    _poll(flip, 80, 60, 40, 66_020.0)                # still 20 → grew 0
    assert _reason(flip) == "no_growth"


def test_price_band_skips(flip):
    # favored side at 74 (>70 — the move is fully priced)
    _poll(flip, 65, 54, 48, 66_000.0)
    _poll(flip, 80, 74, 26, 66_020.0)
    assert _reason(flip) == "price_band"


def test_ratio_low_skips(flip):
    # every condition passes except depth: the favored side is THINNER (6 < 10)
    _poll(flip, 65, 54, 48, 66_000.0, yd=6, nd=10)
    _poll(flip, 80, 60, 40, 66_020.0, yd=6, nd=10)
    assert _reason(flip) == "ratio_low"


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
    assert (65, 6) in ticks and (80, 20) in ticks
