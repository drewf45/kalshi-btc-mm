"""WO-2026-07-23-E — THE SIZE TEST. Two constants, no logic change: does size
travel? FLIP_SIZE_CAP 1→3 (Drew ruled explicit 3, not 4 — NET_RISK=3 and the
5% at-risk wall both bind there, so 3 is deterministic regardless of book) and
OPEN_GOUGE_C 17→10 (4 lots resting 10c off the touch is a genuine maker quote).
The read is `requested_count` vs `count` on FLIP EXIT orders, decision rule /3.

HARD RAIL: F byte-identical — this is FLIP-only."""

from relay_engine import config, scoring
from relay_engine.lane_flip import LaneFlip


def test_the_two_dials_are_set():
    assert config.FLIP_SIZE_CAP == 3          # explicit 3 (not 4 — no book drift)
    assert config.OPEN_GOUGE_C == 10          # target = entry + 10


def test_take_geometry_is_entry_plus_ten_capped_ninety():
    assert LaneFlip._take_price(60) == 70     # entry + 10
    assert LaneFlip._take_price(50) == 60     # at the band floor
    assert LaneFlip._take_price(85) == 90     # cap governs (85+10=95 → 90)


def test_flip_cap_does_not_touch_F_sizing():
    """Acceptance #6 — F byte-identical: F sizes by NOTIONAL, a path that never
    reads FLIP_SIZE_CAP. F's lot count is unchanged whatever the FLIP cap is."""
    f_now = scoring.size_order(4162, 97, 10_000, lane="F").contracts
    assert f_now == int(4162 * config.F_NOTIONAL_PCT // 97)   # notional, cap-free
    assert "cap n/a" in scoring.size_order(4162, 97, 10_000, lane="F").reason


def test_flip_sizes_up_to_the_cap_when_kelly_and_depth_allow():
    """FLIP reaches the 3-lot cap when kelly and depth both clear it; the cap —
    not the retired 1-lot leash — is now what bounds the lane."""
    # book $30, 48c: kelly 5, depth ample → min(kelly, depth, NET_RISK=3) = 3
    dec = scoring.size_order(3000, 48, 10_000, lane="FLIP")
    assert min(dec.contracts, config.FLIP_SIZE_CAP) == 3
