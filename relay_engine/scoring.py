"""Scoring (P22 §2) — one aggregation, three consumers: the sizing path
(tier_for — the orphan finding closed: the Wilson ladder finally has a
caller), the promotion/demotion pages, and the daily scoreboard.

THE number is margin = wilson_lower_bound − breakeven, per (lane, cell):
positive means the cell's measured edge clears its own fee-adjusted bar.
Bars price to each cell's OWN breakeven (§3 — the F-bar blind spot dies:
a 97¢ hold-cell's LEAN bar is the cap .98, not the flat .65), and PROBE
stays a ruling, not a bar — existence is R1/R2's word, only LEAN/CLEAR
are earned. Demotion applies at the NEXT proposal, no grace (§4.2).
"""

import json
from typing import List, Optional, Tuple

from . import config, failures
from .sizing import size_order, wilson_lower_bound

# hold-to-settlement lanes: breakeven is the price itself (salvage-adjusted
# once the DODGED curve has evidence — §3.3 lens note, conservative until).
HOLD_LANES = ("F", "H8", "ORPHAN")

# trip geometry (take_T, bail_L in cents): wins pay +T maker-free; typical
# losses pay −L plus the taker exit fee. OPEN's L is band-shaped (computed).
_TIER_ORDER = [config.TIER_PROBE, config.TIER_LEAN, config.TIER_CLEAR]


def price_cell(price_cents: int) -> int:
    """Entry price -> 5¢ bucket lower edge (35-39 -> 35, ... 95-99 -> 95)."""
    w = config.CELL_WIDTH_CENTS
    return (max(1, min(99, int(price_cents))) // w) * w


def cell_label(cell: int) -> str:
    return f"{cell}-{cell + config.CELL_WIDTH_CENTS - 1}"


def cell_lane(lane: str, why_or_reason: str) -> str:
    """FLIP is two intents with two geometries — the cell splits them.
    Entry whys carry 'OPEN'/'HUNT'; exit reasons carry 'open '/'hunt '."""
    if lane != "FLIP":
        return lane
    tag = (why_or_reason or "").lower()
    if tag.startswith("open"):
        return "OPEN"
    if tag.startswith("hunt"):
        return "HUNT"
    return "FLIP"


def taker_fee_cents(price_cents: float) -> float:
    """Expected taker fee per contract at a price — the venue's curve
    (EXPECTED_FEE_MULTIPLIER · P · (1−P), in cents)."""
    p = max(0.01, min(0.99, price_cents / 100.0))
    return 100.0 * config.EXPECTED_FEE_MULTIPLIER * p * (1.0 - p)


def cell_stats(ledger, lane: str, cell: int) -> Tuple[int, int, float]:
    """(n, wins, wilson_lower_bound) for one (lane, price_cell)."""
    row = ledger.db.execute(
        "SELECT COUNT(*), COALESCE(SUM(won),0) FROM cell_outcomes"
        " WHERE lane=? AND price_cell=?", (lane, cell)).fetchone()
    n, wins = int(row[0]), int(row[1])
    return n, wins, wilson_lower_bound(wins, n)


def salvage_recapture_cents(ledger) -> Tuple[int, float]:
    """(n, mean recovered cents per salvaged loss) from the SALVAGE_VERDICT
    counterfactual rows (P19 §2.6). A DODGED_LOSS row's dodged_cents IS the
    recovered mark; regret rows recapture nothing."""
    rows = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='SALVAGE_VERDICT'"
    ).fetchall()
    vals = []
    for (detail,) in rows:
        try:
            vals.append(max(0.0, float(json.loads(detail)["dodged_cents"])))
        except Exception:
            pass
    if not vals:
        return 0, 0.0
    return len(vals), sum(vals) / len(vals)


def breakeven(ledger, lane: str, cell: int) -> float:
    """§2.2 — the required win-rate for this cell to pay for itself.

    Hold cells (F/H8/ORPHAN): win pays 100−mid, loss pays the effective
    loss L (mid, less salvage recapture once the DODGED curve has
    n ≥ SALVAGE_ADJ_MIN_N — until then raw, conservative). Reduces to
    mid/100 with no recapture: the price IS the bar.

    Trip cells: wins pay take T maker-free; typical losses pay bail L plus
    the taker exit fee (EXPECTED_FEE_MULTIPLIER) — the P18 "≥50% bar"
    generalized per cell instead of printed once.
    """
    mid = cell + config.CELL_WIDTH_CENTS // 2
    if lane in HOLD_LANES:
        loss = float(mid)
        n, recapture = salvage_recapture_cents(ledger)
        if n >= config.SALVAGE_ADJ_MIN_N:
            loss = max(1.0, mid - recapture)
        return loss / ((100.0 - mid) + loss)
    if lane == "HUNT":
        take, bail = float(config.HUNT_TAKE_CENTS), 2.0  # Job-B: entry−2
    elif lane == "OPEN":
        take = float(config.OPEN_TAKE_CENTS)
        bail = max(1.0, mid - config.OPEN_UNDETERMINED_BAND[0])  # band exit
    else:
        # generic trip lane (FLIP legacy, D, P): symmetric take/bail — the
        # old flat bar's honest shape
        take, bail = float(config.HUNT_TAKE_CENTS), float(config.HUNT_TAKE_CENTS)
    fee = taker_fee_cents(max(1.0, mid - bail))  # the loss exit pays taker
    return (bail + fee) / (take + bail + fee)


def bars_for_cell(ledger, lane: str, cell: int) -> Tuple[float, float]:
    """§3.2 — (lean_bar, clear_bar), priced to the cell's own breakeven and
    capped so the math never demands the impossible, only the honest."""
    be = breakeven(ledger, lane, cell)
    lean = min(max(config.TIER_LOWER_BOUNDS[config.TIER_LEAN],
                   be + config.TIER_BUFFER[config.TIER_LEAN]),
               config.TIER_BAR_CAP[config.TIER_LEAN])
    clear = min(max(config.TIER_LOWER_BOUNDS[config.TIER_CLEAR],
                    be + config.TIER_BUFFER[config.TIER_CLEAR]),
                config.TIER_BAR_CAP[config.TIER_CLEAR])
    return lean, clear


def score(ledger, lane: str, cell: int) -> dict:
    """§2.3 — the full cell verdict: (n, wins, lb, breakeven, margin, tier)."""
    n, wins, lb = cell_stats(ledger, lane, cell)
    be = breakeven(ledger, lane, cell)
    lean, clear = bars_for_cell(ledger, lane, cell)
    if lb >= clear:
        tier = config.TIER_CLEAR
    elif lb >= lean:
        tier = config.TIER_LEAN
    else:
        tier = config.TIER_PROBE  # existence is a ruling, not a bar (R1/R2)
    return {"n": n, "wins": wins, "lb": lb, "breakeven": be,
            "margin": lb - be, "tier": tier, "lean_bar": lean,
            "clear_bar": clear}


def tier_for(ledger, lane: str, price_cents: int,
             alert_fn=None) -> str:
    """§4 — THE caller the ladder was missing. Computed fresh at every
    proposal (demote-instantly: no grace); a tier CHANGE for the (lane,
    cell) pages once with its Wilson math and persists across restarts."""
    cell = price_cell(price_cents)
    s = score(ledger, lane, cell)
    tier = s["tier"]
    key = f"tier_{lane}_{cell}"
    prev = ledger.get_state(key) or config.TIER_PROBE
    if tier != prev:
        ledger.set_state(key, tier)
        up = _TIER_ORDER.index(tier) > _TIER_ORDER.index(prev)
        bar = s["clear_bar"] if config.TIER_CLEAR in (tier, prev) \
            else s["lean_bar"]
        lots = config.TIER_MAX_CONTRACTS[tier]
        msg = (f"{'📶 TIER UP' if up else '📉 TIER DOWN'}: {lane} "
               f"{cell_label(cell)}¢ {prev} → {tier} "
               f"(LB {s['lb']:.2f} {'≥' if up else '<'} bar {bar:.2f}, "
               f"n={s['n']}) — {lots} lot{'s' if lots != 1 else ''} "
               f"{'unlocked' if up else 'now the max'}")
        if alert_fn is not None:
            alert_fn(msg)
        try:
            failures.fail("TIER_CHANGE", msg, alert=False, lane=lane,
                          cell=cell, lb=round(s["lb"], 4), bar=round(bar, 4),
                          n=s["n"], tier=tier, prev=prev)
        except Exception:
            pass  # the row is bookkeeping; sizing never blocks on it
    return tier


# ── §5: THE SCOREBOARD (daily pack + /scoreboard — read-only) ──────────────
_SCOREBOARD_LANES = ("F", "H8", "OPEN", "HUNT", "D", "P")


def scoreboard_lines(ledger, book_cents: Optional[int] = None) -> List[str]:
    """The margin table, sorted by margin — the offense map, daily, unasked.
    RED margins render as the warning they are (⚠); hold cells note when
    the salvage adjustment is still pending evidence."""
    book_cents = ledger.book_cents() if book_cents is None else book_cents
    salvage_n, _ = salvage_recapture_cents(ledger)
    rows = ledger.db.execute(
        "SELECT lane, price_cell FROM cell_outcomes"
        " GROUP BY lane, price_cell").fetchall()
    entries = []
    for lane, cell in rows:
        s = score(ledger, lane, cell)
        kind = "hold" if lane in HOLD_LANES else "trip"
        mid = cell + config.CELL_WIDTH_CENTS // 2
        lots = size_order(s["tier"], book_cents, mid, 10_000).contracts
        pend = (lane in HOLD_LANES and salvage_n < config.SALVAGE_ADJ_MIN_N)
        entries.append((s["margin"], lane, cell, kind, s, lots, pend))
    entries.sort(key=lambda e: e[0], reverse=True)
    lines = ["CELL SCOREBOARD          n   W   LB    BE    MARGIN  TIER  lots@book"]
    for margin, lane, cell, kind, s, lots, pend in entries:
        warn = " ⚠" if margin < 0 else ""
        star = "*" if pend else " "
        lines.append(
            f"{lane:<5} {cell_label(cell):<7} ({kind}) "
            f"{s['n']:>3} {s['wins']:>3}  {s['lb']:.2f}  {s['breakeven']:.2f}{star} "
            f"{margin:+.2f}{warn}  {s['tier']:<5} {lots}")
    covered = {lane for _, lane, *_ in entries}
    for lane in _SCOREBOARD_LANES:
        if lane not in covered:
            kind = "hold" if lane in HOLD_LANES else "trip"
            lines.append(f"{lane:<5} {'--':<7} ({kind})   0   -   -     -"
                         f"       -    PROBE 1")
    if salvage_n < config.SALVAGE_ADJ_MIN_N:
        lines.append(f"  *salvage-adj pending (DODGED curve n={salvage_n}"
                     f" < {config.SALVAGE_ADJ_MIN_N} — raw price BE,"
                     f" conservative)")
    return lines
