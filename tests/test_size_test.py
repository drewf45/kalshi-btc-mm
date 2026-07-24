"""WO-2026-07-23-E — THE SIZE TEST. Two constants, no logic change: does size
travel? FLIP_SIZE_CAP 1→3 (Drew ruled explicit 3, not 4 — NET_RISK=3 and the
5% at-risk wall both bind there, so 3 is deterministic regardless of book) and
OPEN_GOUGE_C 17→10 (4 lots resting 10c off the touch is a genuine maker quote).
The read is `requested_count` vs `count` on FLIP EXIT orders, decision rule /3.

HARD RAIL: F byte-identical — this is FLIP-only."""

from relay_engine import config, scoring
from relay_engine.lane_flip import LaneFlip


def test_the_two_dials_are_set():
    assert config.FLIP_SIZE_CAP == 10          # +4×10 size test
    assert config.OPEN_GOUGE_C == 4           # target = entry + 4


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


def test_flip_sizes_up_to_the_cap_when_kelly_and_depth_allow():
    """WO-2026-07-24-C: FLIP is lane-aware now — min(kelly, depth, FLIP_SIZE_CAP),
    NOT the retired NET_RISK count cap. On a book where kelly and depth both
    clear it, the 10-lot cap is the binding term (depth becomes the ceiling in
    a live thin book — the thing the +4×10 test measures)."""
    dec = scoring.size_order(8000, 48, 10_000, lane="FLIP")  # kelly 13, depth ample
    assert dec.contracts == config.FLIP_SIZE_CAP == 10
    assert "→ cap bound" in dec.reason
    # a thin book: depth binds below the cap and the reason says so
    thin = scoring.size_order(8000, 48, 12, lane="FLIP")     # depth 12·0.25 = 3
    assert thin.contracts == 3 and "→ depth bound" in thin.reason
