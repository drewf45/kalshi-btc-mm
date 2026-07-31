"""Spot-lead — P18 "THE DETECTIVE" §0/§2: the needle-move equation.

THE SPOT-LEAD LAW (banked): our spot feed is the market's present; the
crowd's screen is its recent past. When BTC moves, the Kalshi book is wrong
for as long as human reaction takes — that lag is the flip's entire habitat.

This module is the ONE trigger function (§6 Engineer). It answers Drew's
first question — DID THE NEEDLE MOVE? — as arithmetic from the delta table
at the CURRENT time-remaining:

    ΔP = p_side(d_after, t) − p_side(d_before, t)

The clock-and-distance law in one line: the same $80 jump is a 20-point
needle at T-9 near the strike and a 0-point nothing at T-1 far away. The
early/mid-window bias falls out FREE — ΔP is structurally largest near the
strike with clock on it and collapses late; no hand-tuned time rules.

Table semantics: p_survive is the shared brain (the same table D and H8
gate on). The favored side's win probability at a spot on ITS side of the
strike is p_survive(d, t); at a spot still on the OTHER side it is the
crossing probability (1 − p_survive). TABLE_ABSENT / CELL_MISSING → None →
no needle → no hunt: evidence-born, exactly like D.
"""

from dataclasses import dataclass
from typing import Optional

from . import config


@dataclass
class Needle:
    side: str            # spot-favored side of the move (always WITH spot)
    d_before: float      # |spot − strike| at the anchor, $
    d_after: float       # |spot − strike| now, $
    delta_p: float       # POINTS (0-100) the favored side gained
    fair_cents: float    # 100 × p_side(d_after, t) — the TOUCH probability
                         # (p_survive/1−p_cross). WO-2026-07-24-J: this is the
                         # TOUCH surface, INFO-only for HUNT (gate B now prices
                         # SETTLE, p_end). "fair" is banned — it names no
                         # question; this is `touch`.
    t_remaining: float

    def casefile(self) -> str:
        """§6 OFF-THE-STREET: the needle's attention page. WO-2026-07-24-J §P4:
        every probability names ITS question — this is the TOUCH probability,
        marked (info) because HUNT's gate now prices SETTLE, not touch. "fair"
        is banned."""
        tmin, tsec = int(self.t_remaining // 60), int(self.t_remaining % 60)
        return (f"needle +{self.delta_p:.0f}pts "
                f"(d {self.d_before:.0f}→{self.d_after:.0f}, T-{tmin}:{tsec:02d})"
                f" · touch {self.fair_cents:.0f}% (info)")


def pick_strike(spot: Optional[float], boundary_lo, boundary_hi) -> Optional[float]:
    """The strike the needle measures against: the nearest boundary to spot."""
    if spot is None:
        return None
    candidates = [b for b in (boundary_lo, boundary_hi) if b is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda b: abs(spot - b))


def needle(anchor_spot: Optional[float], spot_now: Optional[float],
           strike: Optional[float], t_remaining: Optional[float],
           series: Optional[str] = None) -> Optional[Needle]:
    """Gate A's arithmetic. None = no evidence (spot blind, no strike, table
    absent, or no move) — and no evidence means no hunt, never a guess.
    WO-2026-07-26-T Guard 2: `series` scopes the table — a foreign series' needle
    is None (BLIND), never BTC physics on another room's card."""
    from . import delta
    if (anchor_spot is None or spot_now is None or strike is None
            or t_remaining is None or t_remaining <= 0
            or spot_now == anchor_spot):
        return None
    side = "yes" if spot_now > anchor_spot else "no"

    def p_side(spot: float) -> Optional[float]:
        d = abs(spot - strike)
        on_side = "yes" if spot >= strike else "no"
        ps = delta.p_survive(d, t_remaining, series=series)
        if ps is None:
            return None
        return ps if on_side == side else 1.0 - ps

    p_before = p_side(anchor_spot)
    p_after = p_side(spot_now)
    if p_before is None or p_after is None:
        return None
    return Needle(side=side,
                  d_before=abs(anchor_spot - strike),
                  d_after=abs(spot_now - strike),
                  delta_p=(p_after - p_before) * 100.0,
                  fair_cents=p_after * 100.0,
                  t_remaining=t_remaining)


def is_confirmed_needle(sl: Optional[Needle]) -> bool:
    """Gate A passed — the §4.2 P-suppression predicate."""
    return sl is not None and sl.delta_p >= config.HUNT_NEEDLE_POINTS


def settle_fair_favored(spot: Optional[float], strike: Optional[float],
                        side: str, t_remaining: Optional[float],
                        series: Optional[str] = None) -> Optional[float]:
    """WO-2026-07-25-K §P2 — THE CONFIDENCE INSTRUMENT. The favored side's
    SETTLE-fair (cents): the probability that the window CLOSES on the favored
    side of the strike, given spot is `d = |spot − strike|` away on that side
    with `t` left. This is the settle analog of the touch `p_side` — it prices
    fragility FORWARD, the number the market actually settles on.

    Derivation from the -J settle surface: p_end(d, t) is the DIRECTIONLESS
    P(|net close displacement| ≥ d). By the reflection symmetry of a ~zero-drift
    15-min walk, the favored side loses only if the net ADVERSE move ≥ d (the
    close crosses back over the strike), and that is half the directionless
    mass: P(close crosses back) = p_end(d, t) / 2. So

        settle_fair_favored = (1 − p_end(d, t) / 2) × 100

    Pinned to the strike (d → 0, p_end → 1) this tends to 50 — a coin on its
    edge, exactly the four Saturday losses. None = BLIND (spot/strike unknown or
    the settle surface absent) → the desk's conf gate abstains, tuition bounds it.
    """
    from . import delta
    if spot is None or strike is None or t_remaining is None or t_remaining <= 0:
        return None
    d = abs(spot - strike)
    pe = delta.p_end(d, t_remaining, series=series)   # WO-T Guard 2: series-scoped
    if pe is None:                       # legacy touch-only tape / foreign series → BLIND
        return None
    return (1.0 - pe / 2.0) * 100.0
