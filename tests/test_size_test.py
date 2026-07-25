"""WO-2026-07-23-E — THE SIZE TEST. Two constants, no logic change: does size
travel? FLIP_SIZE_CAP 1→3 (Drew ruled explicit 3, not 4 — NET_RISK=3 and the
5% at-risk wall both bind there, so 3 is deterministic regardless of book) and
OPEN_GOUGE_C 17→10 (4 lots resting 10c off the touch is a genuine maker quote).
The read is `requested_count` vs `count` on FLIP EXIT orders, decision rule /3.

HARD RAIL: F byte-identical — this is FLIP-only."""

from relay_engine import config, scoring
from relay_engine.lane_flip import LaneFlip


def test_the_two_dials_are_set():
    assert config.OPEN_GOUGE_C == 4           # target = entry + 4
    # WO-2026-07-24-G: the fixed cap is retired as a binder — FLIP scales by
    # notional; its dial sits under the wall.
    assert config.FLIP_NOTIONAL_PCT < config.AT_RISK_PCT["FLIP"]


def test_take_geometry_is_entry_plus_ten_capped_ninety():
    assert LaneFlip._take_price(60) == 64     # entry + 4
    assert LaneFlip._take_price(50) == 54     # at the band floor
    assert LaneFlip._take_price(90) == 90     # cap governs (90+4=94 → 90)


def test_flip_cap_does_not_touch_F_sizing():
    """Acceptance #6 — F byte-identical: F sizes by NOTIONAL, a path that never
    reads FLIP_SIZE_CAP. F's lot count is unchanged whatever the FLIP cap is."""
    f_now = scoring.size_order(4162, 97, 10_000, lane="F").contracts
    assert f_now == int(4162 * config.F_NOTIONAL_PCT // 97)   # notional, cap-free
    assert "cap n/a" in scoring.size_order(4162, 97, 10_000, lane="F").reason


def test_flip_scales_with_the_book_no_fixed_cap():
    """WO-2026-07-24-G Part 2: the fixed cap is RETIRED — FLIP = min(notional,
    depth), scaling with the book like F. On a deep book NOTIONAL is the ceiling
    (8000*0.14//48 = 23), not a frozen 10; a thin book lets DEPTH bind below."""
    # WO-2026-07-25-K: FLIP's default is now TUITION; this asserts the FULL-size
    # scaling mechanism, so it exercises the promoted dial explicitly.
    _full = config.FLIP_FULL_NOTIONAL_PCT
    dec = scoring.size_order(8000, 48, 10_000, lane="FLIP", notional_pct=_full)  # notional 23, depth ample
    assert dec.contracts == 23 and "→ notional bound" in dec.reason
    assert "no cap — scales with book" in dec.reason
    # a thin book: depth binds below the notional and the reason says so
    thin = scoring.size_order(8000, 48, 12, lane="FLIP", notional_pct=_full)     # depth 12·0.25 = 3
    assert thin.contracts == 3 and "→ depth bound" in thin.reason
