"""WO-2026-07-24-D — THE MORNING FOUR, build 2/2: Part 1, SIZE THE LANE. Kelly
capped FLIP at ~5-6 on a $43 book, so FLIP_SIZE_CAP=10 never bit and the +4×10
test ran at half the ruled size. F alone had a notional Kelly-bypass; FLIP now
shares it — min(notional, depth, cap). This ships LAST (it doubles the size) so
it lands on build-1's floored stop and visible reconcile.

HARD RAIL: F byte-identical — F's own notional path is untouched; FLIP got its
own branch, F never reads FLIP_NOTIONAL_PCT and FLIP never reads F_NOTIONAL_PCT."""

from relay_engine import config, scoring

# WO-2026-07-25-K §P1/P3 re-anchor: FLIP's DEFAULT dial is now TUITION (0.04) —
# the desk re-earns FULL (0.14) by conversion. These tests assert the FULL-size
# self-scaling MECHANISM (the WO-D/G claim "reaches ten / scales with the book"),
# so they now exercise the PROMOTED path explicitly via notional_pct=FULL. The
# tuition-default sizing is covered in the WO-K suite.
_FULL = config.FLIP_FULL_NOTIONAL_PCT


# ── Part 1 (#5): FLIP reaches 10 at the live book, binding term named ───────
def test_flip_reaches_ten_at_the_live_book():
    """Acceptance #5: at a ~$43 book and a 60c favored entry, notional = 4300 ·
    0.14 // 60 = 10 — the PROMOTED (full) desk sizes to the ruled cap, where
    Kelly (358//60 = 5) held it at half. The reason names the binding term."""
    dec = scoring.size_order(4300, 60, 10_000, lane="FLIP", notional_pct=_FULL)
    assert dec.contracts == 10 == config.FLIP_SIZE_CAP
    assert "bound" in dec.reason and "notional=10" in dec.reason


def test_flip_no_longer_capped_by_kelly():
    """The bug, quantified: the OLD min(kelly, depth, cap) gave 5 here (kelly
    358//60); the full notional path gives 10. Kelly is explicitly retired from
    FLIP's bind — it rides the reason as 'n/a' for the audit, nothing more."""
    kelly_would_have_been = int(4300 * config.KELLY_FRACTION_CEILING // 60)  # 5
    dec = scoring.size_order(4300, 60, 10_000, lane="FLIP", notional_pct=_FULL)
    assert kelly_would_have_been < 10        # Kelly alone would have starved it
    assert dec.contracts == 10               # notional lets it reach the cap
    assert "kelly n/a" in dec.reason


def test_flip_depth_still_binds_below_the_cap():
    """On a thin book DEPTH is the ceiling and the reason says so — the term the
    +4×10 test measures live (notional/cap clear it, the book does not)."""
    dec = scoring.size_order(4300, 60, 12, lane="FLIP", notional_pct=_FULL)   # depth 12·0.25 = 3
    assert dec.contracts == 3 and "→ depth bound" in dec.reason


def test_flip_scales_past_ten_on_a_large_book():
    """WO-2026-07-24-G Part 2: the fixed 10-cap is RETIRED — on a large book
    NOTIONAL is the ceiling (80000*0.14//48 = 233), so FLIP scales with the book
    instead of freezing at 10 (the count-vs-compound governor, killed)."""
    dec = scoring.size_order(80_000, 48, 10_000, lane="FLIP", notional_pct=_FULL)
    assert dec.contracts == int(80_000 * _FULL // 48) == 233
    assert "→ notional bound" in dec.reason and "no cap" in dec.reason


def test_flip_notional_scales_with_the_book():
    """Below the notional-for-10 book, FLIP scales DOWN with the book (like F) —
    it is no longer a flat Kelly starve. A $20 book at 60c → 20·2000... = 4."""
    small = scoring.size_order(2000, 60, 10_000, lane="FLIP",
                               notional_pct=_FULL).contracts
    assert small == int(2000 * _FULL // 60) == 4
    assert small < config.FLIP_SIZE_CAP       # the book, not the cap, binds here


# ── the wall headroom (WO-G): the scaled size fits under the 18% wall ────────
def test_scaled_flip_size_fits_under_the_at_risk_wall():
    """WO-2026-07-24-G: at the post-deposit book (~$90) FLIP sizes to notional
    (9000*0.14//58 = 21 lots), and 21 × the take price must fit under the 18%
    at-risk wall — the dial (14%) sits under the wall (18%) with headroom. The
    wall is book-proportional, so a shrinking book tightens it (the backstop)."""
    book = 9000
    lots = int(book * config.FLIP_NOTIONAL_PCT // 58)        # 21
    take = 58 + config.OPEN_GOUGE_C                          # 62
    cap = config.at_risk_cap_cents("FLIP", book)            # 18% of book = 1620
    assert lots * take <= cap                                # 1302 <= 1620, fits
    assert config.FLIP_NOTIONAL_PCT < config.AT_RISK_PCT["FLIP"]   # dial < wall
    # book-proportional: a much smaller book tightens the wall below the size
    assert config.at_risk_cap_cents("FLIP", 500) < lots * take


# ── HARD RAIL: F byte-identical, the two notional dials never cross ──────────
def test_f_notional_path_untouched_by_flip():
    """F sizes by F_NOTIONAL_PCT (0.20), a path that never reads FLIP_NOTIONAL_
    PCT; FLIP sizes by FLIP_NOTIONAL_PCT (0.14), never F's. The dials are
    independent — F's lot count is byte-identical to before this WO."""
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason                             # F is uncapped
    # FLIP at the same book/price uses ITS dial (notional, no cap), not F's
    flip = scoring.size_order(4162, 97, 10_000, lane="FLIP")
    assert "no cap" in flip.reason and f.contracts != flip.contracts
