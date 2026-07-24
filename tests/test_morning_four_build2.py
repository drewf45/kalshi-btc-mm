"""WO-2026-07-24-D — THE MORNING FOUR, build 2/2: Part 1, SIZE THE LANE. Kelly
capped FLIP at ~5-6 on a $43 book, so FLIP_SIZE_CAP=10 never bit and the +4×10
test ran at half the ruled size. F alone had a notional Kelly-bypass; FLIP now
shares it — min(notional, depth, cap). This ships LAST (it doubles the size) so
it lands on build-1's floored stop and visible reconcile.

HARD RAIL: F byte-identical — F's own notional path is untouched; FLIP got its
own branch, F never reads FLIP_NOTIONAL_PCT and FLIP never reads F_NOTIONAL_PCT."""

from relay_engine import config, scoring


# ── Part 1 (#5): FLIP reaches 10 at the live book, binding term named ───────
def test_flip_reaches_ten_at_the_live_book():
    """Acceptance #5: at a ~$43 book and a 60c favored entry, notional = 4300 ·
    0.14 // 60 = 10 — FLIP finally sizes to the ruled cap, where Kelly (358//60 =
    5) held it at half. The reason names the binding term."""
    dec = scoring.size_order(4300, 60, 10_000, lane="FLIP")   # depth ample
    assert dec.contracts == 10 == config.FLIP_SIZE_CAP
    assert "bound" in dec.reason and "notional=10" in dec.reason


def test_flip_no_longer_capped_by_kelly():
    """The bug, quantified: the OLD min(kelly, depth, cap) gave 5 here (kelly
    358//60); the NEW notional path gives 10. Kelly is explicitly retired from
    FLIP's bind — it rides the reason as 'n/a' for the audit, nothing more."""
    kelly_would_have_been = int(4300 * config.KELLY_FRACTION_CEILING // 60)  # 5
    dec = scoring.size_order(4300, 60, 10_000, lane="FLIP")
    assert kelly_would_have_been < 10        # Kelly alone would have starved it
    assert dec.contracts == 10               # notional lets it reach the cap
    assert "kelly n/a" in dec.reason


def test_flip_depth_still_binds_below_the_cap():
    """On a thin book DEPTH is the ceiling and the reason says so — the term the
    +4×10 test measures live (notional/cap clear it, the book does not)."""
    dec = scoring.size_order(4300, 60, 12, lane="FLIP")   # depth 12·0.25 = 3
    assert dec.contracts == 3 and "→ depth bound" in dec.reason


def test_flip_cap_binds_on_a_large_book():
    """On a large book notional clears the cap and the 10-lot cap is the ceiling
    — the reason names it. (This is the ruled max size, never exceeded.)"""
    dec = scoring.size_order(80_000, 48, 10_000, lane="FLIP")
    assert dec.contracts == 10 and "→ cap bound" in dec.reason


def test_flip_notional_scales_with_the_book():
    """Below the notional-for-10 book, FLIP scales DOWN with the book (like F) —
    it is no longer a flat Kelly starve. A $20 book at 60c → 20·2000... = 4."""
    small = scoring.size_order(2000, 60, 10_000, lane="FLIP").contracts
    assert small == int(2000 * config.FLIP_NOTIONAL_PCT // 60) == 4
    assert small < config.FLIP_SIZE_CAP       # the book, not the cap, binds here


# ── the wall headroom (ADVERSARY iii): 10 lots at the band top fits the wall ─
def test_ten_lots_at_the_band_top_fits_the_at_risk_wall():
    """ADVERSARY (iii): 10 lots × 64c (the take, entry+OPEN_GOUGE_C capped) =
    640c must fit under the 15% at-risk wall on the live book. At $43 the wall is
    645c — it fits with nothing spare, which is where a backstop belongs. Below
    ~$42.67 the wall binds first (correct — the backstop does its job)."""
    book = 4300
    at_risk_cap = config.at_risk_cap_cents("FLIP", book)     # 15% of book
    assert 10 * 64 <= at_risk_cap                            # 640 <= 645, fits
    # and the wall is book-proportional, so a shrinking book tightens it
    assert config.at_risk_cap_cents("FLIP", 4000) < 10 * 64  # wall binds first


# ── HARD RAIL: F byte-identical, the two notional dials never cross ──────────
def test_f_notional_path_untouched_by_flip():
    """F sizes by F_NOTIONAL_PCT (0.20), a path that never reads FLIP_NOTIONAL_
    PCT; FLIP sizes by FLIP_NOTIONAL_PCT (0.14), never F's. The dials are
    independent — F's lot count is byte-identical to before this WO."""
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason                             # F is uncapped
    # FLIP at the same book/price uses ITS dial, not F's
    flip = scoring.size_order(4162, 97, 10_000, lane="FLIP")
    assert "cap=" in flip.reason and f.contracts != flip.contracts
