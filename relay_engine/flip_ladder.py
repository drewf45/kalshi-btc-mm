"""WO-2026-07-25-K §P3 — PROMOTION BY CONVERSION: the FLIP desk's size ladder,
mechanical. The desk was demoted to tuition (P1) because 55% conversion loses
against a ~73% breakeven; it RE-EARNS full notional the day the tape says it
converts — no ruling at either edge.

  full  ← trailing-N round-trip conversion >= FLIP_PROMOTE_CONV (promote slowly)
  tuition ← conversion < FLIP_DEMOTE_CONV                        (demote instantly)

The two bars are hysteretic (a band between them holds the current tier, so the
dial doesn't chatter). The MARGIN TIEBREAKER rides on top of the streak: even a
promoted desk sizes tuition into a cell whose Wilson margin is negative — a
lucky streak can never up-size a structurally-losing cell (ADVERSARY's gaming
check). Conversion is size-independent, so tuition trades keep earning promotion
(ADVERSARY's deadlock check).

The conversion reads the FLIP_SWING records the pack already keeps; nothing new
is instrumented. State persists in the ledger (survives restart); the tier flip
pages ONCE with the number that moved it.
"""

import logging
from typing import Optional

from . import config

log = logging.getLogger("relay.flip_ladder")

_STATE_KEY = "flip_size_tier"          # "full" | "tuition"
TUITION = "tuition"
FULL = "full"
_MIN_N_TO_DEMOTE = 5                    # don't demote on a single unlucky trip


def trailing_conversion(ledger, window: Optional[int] = None) -> dict:
    """The desk's trailing-N round-trip conversion from FLIP_SWING: a round-trip
    CONVERTS when its net (gross_cents) is positive. Returns {n, wins, rate}
    (rate=None when n==0 — no trips yet, nothing to judge)."""
    import json
    window = config.FLIP_CONV_WINDOW if window is None else window
    rows = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='FLIP_SWING'"
        " ORDER BY id DESC LIMIT ?", (window,)).fetchall()
    n = wins = 0
    for (d,) in rows:
        try:
            gross = float(json.loads(d).get("gross_cents", 0))
        except Exception:
            continue
        n += 1
        if gross > 0:
            wins += 1
    rate = (wins / n) if n else None
    return {"n": n, "wins": wins, "rate": rate}


def current_tier(ledger) -> str:
    """Pure read of the persisted size tier (default tuition — the P1 floor)."""
    return ledger.get_state(_STATE_KEY) or TUITION


def evaluate_size_tier(ledger, now: Optional[float] = None,
                       alert_fn=None) -> str:
    """Move the dial from the tape (hysteretic; promote slowly, demote instantly)
    and page ONCE on a flip with the conversion that moved it. Idempotent — safe
    to call at every entry AND from the hourly; it only writes/pages on a real
    transition. Returns the (possibly new) tier."""
    prev = current_tier(ledger)
    conv = trailing_conversion(ledger)
    rate, n = conv["rate"], conv["n"]
    tier = prev
    if rate is not None:
        if prev != FULL and n >= config.FLIP_CONV_WINDOW \
                and rate >= config.FLIP_PROMOTE_CONV:
            tier = FULL                    # earned a full window over the top bar
        elif prev == FULL and n >= _MIN_N_TO_DEMOTE \
                and rate < config.FLIP_DEMOTE_CONV:
            tier = TUITION                 # instant demotion under the bottom bar
    if tier != prev:
        ledger.set_state(_STATE_KEY, tier)
        up = tier == FULL
        bar = config.FLIP_PROMOTE_CONV if up else config.FLIP_DEMOTE_CONV
        msg = (f"{'📶 DESK PROMOTED' if up else '📉 DESK DEMOTED'} → {tier} "
               f"size: trailing-{config.FLIP_CONV_WINDOW} conversion "
               f"{rate:.0%} ({conv['wins']}/{n}) "
               f"{'≥' if up else '<'} bar {bar:.0%} — FLIP dial now "
               f"{active_pct_for_tier(tier):.0%} of book")
        log.info(msg)
        if alert_fn is not None:
            try:
                alert_fn(msg)
            except Exception:
                pass
        try:
            from . import failures
            failures.fail("DESK_SIZE_CHANGE", msg, alert=False, tier=tier,
                          prev=prev, rate=round(rate, 4), n=n)
        except Exception:
            pass
    return tier


def active_pct_for_tier(tier: str) -> float:
    return config.FLIP_FULL_NOTIONAL_PCT if tier == FULL \
        else config.FLIP_NOTIONAL_PCT


def active_notional_pct(ledger, cell_margin: Optional[float],
                        now: Optional[float] = None, alert_fn=None) -> float:
    """The FLIP notional the entry sizes at: FULL only when the desk is promoted
    AND this entry's cell Wilson margin is non-negative — else tuition. The
    margin is the tiebreaker (P3): a promoted streak still sizes tuition into a
    negative-margin cell, so no lucky streak up-sizes a losing cell."""
    tier = evaluate_size_tier(ledger, now=now, alert_fn=alert_fn)
    if tier == FULL and (cell_margin is not None and cell_margin >= 0):
        return config.FLIP_FULL_NOTIONAL_PCT
    return config.FLIP_NOTIONAL_PCT


def promotion_distance_lines(ledger) -> list:
    """WO-2026-07-25-L §P3 — THE DOOR, MARKED WITH NUMBERS, PRINTED DAILY. Each
    shadow lane's DISTANCE to promotion, so an all-shadow future can never be a
    stuck mood — the thresholds are numbers and the gap to each is on the tape.
    (The protocol still requires a NAMED experiment + Drew's sign-off to actually
    leave shadow; this only reports whether the evidence bar is met.)"""
    from . import config
    out = ["EARN-BACK — distance to promotion (shadow → tuition-live; "
           "named experiment + sign-off still required):"]
    conv = trailing_conversion(ledger)
    if conv["rate"] is None:
        out.append(f"  DESK (FLIP/OPEN): 0/{config.FLIP_CONV_WINDOW} shadow "
                   f"round-trips — need ≥{config.FLIP_PROMOTE_CONV:.0%} over "
                   f"{config.FLIP_CONV_WINDOW}, the -J conf gate live, cell "
                   f"margins ≥ 0")
    else:
        met = (conv["n"] >= config.FLIP_CONV_WINDOW
               and conv["rate"] >= config.FLIP_PROMOTE_CONV)
        gap = max(0.0, config.FLIP_PROMOTE_CONV - conv["rate"]) * 100
        out.append(
            f"  DESK (FLIP/OPEN): shadow conversion {conv['rate']:.0%} "
            f"({conv['wins']}/{conv['n']}) vs bar {config.FLIP_PROMOTE_CONV:.0%}"
            + ("  ✓ evidence MET (awaiting named experiment + sign-off)"
               if met else
               f"  — {gap:.0f}pts short"
               + ("" if conv["n"] >= config.FLIP_CONV_WINDOW
                  else f", {config.FLIP_CONV_WINDOW - conv['n']} more trips")))
    # HUNT: ships only as -J in shadow; live at PROBE after ≥30 shadow entries
    # with realized edge ≥ fee-adjusted threshold (reported from FLIP_SWING/hunt).
    hn = ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE lane='FLIP'"
        " AND state='PROPOSED' AND (detail LIKE 'HUNT ↑%' OR detail LIKE 'HUNT ↓%')"
    ).fetchone()[0]
    out.append(f"  HUNT: {hn} shadow entries — need ≥30 with trailing-30 "
               f"realized edge > 0 (revert: edge ≤ 0)")
    return out


def ladder_line(ledger) -> str:
    """The pack/hourly line: the conversion, the bars, the live tier + dial."""
    conv = trailing_conversion(ledger)
    tier = current_tier(ledger)
    if conv["rate"] is None:
        return (f"DESK SIZE: {tier} (dial {active_pct_for_tier(tier):.0%}) · "
                f"trailing-{config.FLIP_CONV_WINDOW} conversion: no trips yet · "
                f"promote ≥{config.FLIP_PROMOTE_CONV:.0%} / demote "
                f"<{config.FLIP_DEMOTE_CONV:.0%}")
    return (f"DESK SIZE: {tier} (dial {active_pct_for_tier(tier):.0%}) · "
            f"trailing-{config.FLIP_CONV_WINDOW} conversion "
            f"{conv['rate']:.0%} ({conv['wins']}/{conv['n']}) · "
            f"promote ≥{config.FLIP_PROMOTE_CONV:.0%} / demote "
            f"<{config.FLIP_DEMOTE_CONV:.0%}")
