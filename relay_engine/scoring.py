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


def cell_stats(ledger, lane: str, cell: int,
               shadow: bool = False) -> Tuple[int, int, float]:
    """(n, wins, wilson_lower_bound) for one (lane, price_cell). WO-2026-07-25-L:
    LIVE authority reads live cells ONLY (shadow=0) — a shadow (rehearsed) cell
    NEVER drives live sizing/promotion. Pass shadow=True to read the rehearsal
    ledger (the earn-back promotion evidence)."""
    row = ledger.db.execute(
        "SELECT COUNT(*), COALESCE(SUM(won),0) FROM cell_outcomes"
        " WHERE lane=? AND price_cell=? AND shadow=?",
        (lane, cell, int(bool(shadow)))).fetchone()
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


def score(ledger, lane: str, cell: int, shadow: bool = False) -> dict:
    """§2.3 — the full cell verdict: (n, wins, lb, breakeven, margin, tier).
    WO-2026-07-25-L: shadow=True scores the rehearsal ledger (promotion evidence)."""
    n, wins, lb = cell_stats(ledger, lane, cell, shadow=shadow)
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
            "clear_bar": clear,
            # WO-2026-07-25-L §P4: a cell with < CELL_THIN_MIN_N realized outcomes
            # carries NO gate authority — its margin never authorizes or blocks.
            "thin": n < config.CELL_THIN_MIN_N}


def cell_has_authority(ledger, lane: str, cell: int,
                       shadow: bool = False) -> bool:
    """WO-2026-07-25-L §P4 — does this cell hold gate authority? Only when it has
    cleared THIN by REALIZED n (never by modeled numbers). A THIN cell's margin
    is shown greyed and votes on nothing (promotion, sizing tiebreaker)."""
    n, _, _ = cell_stats(ledger, lane, cell, shadow=shadow)
    return n >= config.CELL_THIN_MIN_N


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
    the salvage adjustment is still pending evidence.

    COLD AUDIT build 70 §3: the DISPLAYED break-even and margin are the HONEST
    ones (`breakeven_honest` — the real stop, the realized-loss fallback), the
    number a human reads and acts on. The pack proved the stale model wrong on
    26 of 29 cells; a metric measured-wrong is a live hazard whatever it drives.
    The `tier` column stays the OPERATIVE tier from `score()`/`breakeven()` —
    that one is untouched because it feeds custody cut-scaling (`scaled(size_
    tier)`), and changing it would alter F's cuts (F byte-identical). So the
    edge you read is honest; the tier you see is exactly what the machine uses."""
    book_cents = ledger.book_cents() if book_cents is None else book_cents
    salvage_n, _ = salvage_recapture_cents(ledger)

    def _rows_for(shadow: bool):
        # WO-2026-07-25-L §P1: LIVE cells and SHADOW (rehearsal) cells NEVER share
        # a table row — enumerate each ledger separately, labeled.
        rows = ledger.db.execute(
            "SELECT lane, price_cell FROM cell_outcomes WHERE shadow=?"
            " GROUP BY lane, price_cell", (int(shadow),)).fetchall()
        out = []
        for lane, cell in rows:
            s = score(ledger, lane, cell, shadow=shadow)   # shadow-scoped
            be_honest = breakeven_honest(ledger, lane, cell)   # §3: the honest edge
            margin_honest = s["lb"] - be_honest
            kind = "hold" if lane in HOLD_LANES else "trip"
            mid = cell + config.CELL_WIDTH_CENTS // 2
            lots = size_order(book_cents, mid, 10_000, lane=lane).contracts
            pend = (lane in HOLD_LANES and salvage_n < config.SALVAGE_ADJ_MIN_N)
            out.append((margin_honest, lane, cell, kind, s, be_honest, lots, pend))
        out.sort(key=lambda e: e[0], reverse=True)
        return out

    def _fmt(entries):
        sub = []
        for margin, lane, cell, kind, s, be_honest, lots, pend in entries:
            # WO-2026-07-25-L §P4: a THIN cell (n < CELL_THIN_MIN_N) is greyed —
            # it holds NO gate authority (never a promotion or margin vote) — but
            # the red-margin ⚠ still shows (a warning is a warning); the ·THIN
            # tag says the number carries no authority. Leaves THIN by realized n.
            warn = " ⚠" if margin < 0 else ""
            star = "*" if pend else " "
            tag = f"  ·THIN (n<{config.CELL_THIN_MIN_N}, no authority)" \
                if s.get("thin") else ""
            sub.append(
                f"{lane:<5} {cell_label(cell):<7} ({kind}) "
                f"{s['n']:>3} {s['wins']:>3}  {s['lb']:.2f}  {be_honest:.2f}{star} "
                f"{margin:+.2f}{warn}  {s['tier']:<5} {lots}{tag}")
        return sub

    live = _rows_for(shadow=False)
    lines = ["CELL SCOREBOARD (LIVE)   n   W   LB    BE    MARGIN  TIER  lots@book"]
    lines.extend(_fmt(live))
    covered = {lane for _, lane, *_ in live}
    for lane in _SCOREBOARD_LANES:
        if lane not in covered:
            kind = "hold" if lane in HOLD_LANES else "trip"
            lines.append(f"{lane:<5} {'--':<7} ({kind})   0   -   -     -"
                         f"       -    PROBE 1")
    shadow = _rows_for(shadow=True)
    if shadow:
        lines.append("CELL SCOREBOARD (SHADOW — rehearsal, NOT tradeable capital):")
        lines.extend(_fmt(shadow))
    if salvage_n < config.SALVAGE_ADJ_MIN_N:
        lines.append(f"  *salvage-adj pending (DODGED curve n={salvage_n}"
                     f" < {config.SALVAGE_ADJ_MIN_N} — raw price BE,"
                     f" conservative)")
    return lines


# ── WO-2026-07-23-A: THE SELF-AUDIT — put the truth NEXT TO the model ────────
# READ-ONLY, PACK-SIDE ONLY. These NEVER feed the trading path: the
# tier→size→custody chain still reads `breakeven()`/`SALVAGE_ADJ_MIN_N` above,
# UNTOUCHED, so F and OPEN trade byte-identically (acceptance #9). The pack's
# margin/ranking uses the A1/A2/A3-corrected `breakeven_honest`, and every row
# carries realized MONEY (B1) and the model graded against the actual loss (B2).
SALVAGE_ADJ_MIN_N_HONEST = 8    # A3: reachable — F has 9 losses in 285 trades


def realized_loss_avg(ledger, lane: str,
                      cell: Optional[int] = None,
                      shadow: bool = False) -> Tuple[int, float]:
    """(n_losses, mean |loss| cents) from cell_outcomes — the ACTUAL cost of a
    loss for a lane (and optionally one cell). The truth `loss_modeled`
    approximates; today's bug was `loss_modeled 1c` where this read ~24c.
    WO-2026-07-25-L: LIVE authority reads live cells only (shadow=0)."""
    q = ("SELECT COUNT(*), COALESCE(AVG(-pnl_cents),0) FROM cell_outcomes"
         " WHERE lane=? AND pnl_cents<0 AND shadow=?"
         + (" AND price_cell=?" if cell is not None else ""))
    args = (lane, int(bool(shadow)), cell) if cell is not None \
        else (lane, int(bool(shadow)))
    row = ledger.db.execute(q, args).fetchone()
    return int(row[0]), float(row[1])


def cell_pnl(ledger, lane: str, cell: int,
             day_start: Optional[float] = None) -> Tuple[Optional[int], int]:
    """(pnl_day, pnl_life) realized cents for a cell — MONEY, not a model output
    (B1). A cell can be negative-margin and positive-money; the desk needs both."""
    life = int(ledger.db.execute(
        "SELECT COALESCE(SUM(pnl_cents),0) FROM cell_outcomes"
        " WHERE lane=? AND price_cell=?", (lane, cell)).fetchone()[0])
    if day_start is None:
        return None, life
    day = int(ledger.db.execute(
        "SELECT COALESCE(SUM(pnl_cents),0) FROM cell_outcomes"
        " WHERE lane=? AND price_cell=? AND ts>=?",
        (lane, cell, day_start)).fetchone()[0])
    return day, life


def _take_honest(lane: str) -> float:
    if lane == "OPEN":
        return float(config.OPEN_GOUGE_C)       # A1: the actual target (17), not 20
    return float(config.HUNT_TAKE_CENTS)


def _be_from_loss(lane: str, mid: float, loss: float, take: float,
                  maker: bool = True) -> float:
    """Break-even win-rate for a given loss/take geometry — the SAME shape as
    breakeven(), fed an explicit loss so the model is auditable against it."""
    if lane in HOLD_LANES:
        denom = (100.0 - mid) + loss
        return loss / denom if denom > 0 else 1.0
    fee = 0.0 if maker else taker_fee_cents(max(1.0, loss))
    denom = take + loss + fee
    return (loss + fee) / denom if denom > 0 else 1.0


def loss_modeled_honest(ledger, lane: str, cell: int) -> float:
    """The loss the CORRECTED model assigns. A1: OPEN's stop is the flat
    OPEN_MOMENTUM_STOP_C (10), not the retired band floor (mid−35). A2: a hold
    loss falls back to the realized mean loss when the DODGED curve is short,
    never a total loss (mid) — F salvage-cuts at ~−40, not −97."""
    mid = cell + config.CELL_WIDTH_CENTS // 2
    if lane in HOLD_LANES:
        sn, recapture = salvage_recapture_cents(ledger)
        if sn >= SALVAGE_ADJ_MIN_N_HONEST:       # A3 (reachable) then DODGED
            return max(1.0, mid - recapture)
        rn, ravg = realized_loss_avg(ledger, lane)   # A2 realized fallback
        return min(float(mid), ravg) if rn > 0 else float(mid)
    if lane == "OPEN":
        return float(config.OPEN_MOMENTUM_STOP_C)    # A1: flat 10c everywhere
    if lane == "HUNT":
        return 2.0
    return float(config.HUNT_TAKE_CENTS)


def breakeven_honest(ledger, lane: str, cell: int) -> float:
    """The A1/A2/A3-corrected break-even — the pack's margin/ranking, NEVER the
    trading path."""
    mid = cell + config.CELL_WIDTH_CENTS // 2
    loss = loss_modeled_honest(ledger, lane, cell)
    maker = lane != "HUNT"                        # OPEN exits maker-first; HUNT crosses
    return _be_from_loss(lane, mid, loss, _take_honest(lane), maker=maker)


def scoreboard_rows(ledger, day_start: Optional[float] = None) -> List[dict]:
    """The structured scoreboard: every cell with realized MONEY beside the model
    (B1), and the model GRADED against the actual loss (B2). Sorted by the
    honest margin. Read-only — the instrument grading itself."""
    cells = ledger.db.execute(
        "SELECT lane, price_cell FROM cell_outcomes"
        " GROUP BY lane, price_cell").fetchall()
    out = []
    for lane, cell in cells:
        n, wins, lb = cell_stats(ledger, lane, cell)
        mid = cell + config.CELL_WIDTH_CENTS // 2
        be_mod = breakeven_honest(ledger, lane, cell)
        loss_mod = loss_modeled_honest(ledger, lane, cell)
        rn, loss_act = realized_loss_avg(ledger, lane, cell)
        maker = lane != "HUNT"
        be_impl = (_be_from_loss(lane, mid, loss_act, _take_honest(lane),
                                 maker=maker) if rn > 0 else None)
        pnl_day, pnl_life = cell_pnl(ledger, lane, cell, day_start)
        out.append({
            "lane": lane, "cell": cell_label(cell), "n": n, "W": wins,
            "LB": round(lb, 3), "be_modeled": round(be_mod, 3),
            "margin": round(lb - be_mod, 3),
            "pnl_day_c": pnl_day, "pnl_life_c": pnl_life,
            "loss_modeled_c": round(loss_mod, 1),
            "loss_actual_c": round(loss_act, 1) if rn > 0 else None,
            "be_implied": round(be_impl, 3) if be_impl is not None else None,
            "model_error": (round(be_mod - be_impl, 3)
                            if be_impl is not None else None)})
    out.sort(key=lambda r: r["margin"], reverse=True)
    return out


def lifetime_cell_aggregates(ledger) -> List[dict]:
    """WO-2026-07-23-B §4.4 — THE F BLOCKER. The daily pack is day-scoped, so F's
    9 lifetime losses (the number Part 1's ceiling rests on) are unreadable. This
    is LIFETIME per (lane, cell): n, wins, losses, avg_win, avg_loss, realized
    P&L — the salvage question ('what does an F loss actually cost?') answered
    from the whole record, not one day. Read-only."""
    # WO-2026-07-24-G Part 4: avg_win/avg_loss are PER-CONTRACT now (pnl summed
    # over contracts summed), so an 18-lot era does not blend with the 1-lot era
    # and fake Gate A progress. win_n/loss_n stay ROW counts (windows) for
    # n/wins/losses; the averages divide by CONTRACTS.
    rows = ledger.db.execute(
        "SELECT lane, price_cell, COUNT(*),"
        " COALESCE(SUM(won),0),"
        " COALESCE(SUM(CASE WHEN pnl_cents>0 THEN pnl_cents END),0),"
        " COALESCE(SUM(CASE WHEN pnl_cents>0 THEN contracts END),0),"
        " COALESCE(SUM(CASE WHEN pnl_cents<0 THEN -pnl_cents END),0),"
        " COALESCE(SUM(CASE WHEN pnl_cents<0 THEN contracts END),0),"
        " COALESCE(SUM(pnl_cents),0),"
        " COALESCE(SUM(CASE WHEN pnl_cents<0 THEN 1 ELSE 0 END),0)"
        " FROM cell_outcomes GROUP BY lane, price_cell").fetchall()
    out = []
    for (lane, cell, n, wins, win_sum, win_ct, loss_sum, loss_ct,
         pnl, loss_n) in rows:
        out.append({
            "lane": lane, "cell": cell_label(cell), "n": int(n),
            "wins": int(wins), "losses": int(loss_n),
            "avg_win_c": round(win_sum / win_ct, 1) if win_ct else None,
            "avg_loss_c": round(loss_sum / loss_ct, 1) if loss_ct else None,
            "realized_pnl_c": int(pnl)})
    out.sort(key=lambda r: (r["lane"], r["cell"]))
    return out


def fills_pnl_by_lane(ledger, day_start: Optional[float] = None) -> List[dict]:
    """SUMMARY money (B1/C2): realized fills P&L per lane, day + lifetime — the
    edge number is fills_pnl (strategy), NOT window_pnl (includes accidents)."""
    lanes = [r[0] for r in ledger.db.execute(
        "SELECT DISTINCT lane FROM cell_outcomes ORDER BY lane").fetchall()]
    out = []
    for lane in lanes:
        life = int(ledger.db.execute(
            "SELECT COALESCE(SUM(pnl_cents),0) FROM cell_outcomes WHERE lane=?",
            (lane,)).fetchone()[0])
        day = None
        if day_start is not None:
            day = int(ledger.db.execute(
                "SELECT COALESCE(SUM(pnl_cents),0) FROM cell_outcomes"
                " WHERE lane=? AND ts>=?", (lane, day_start)).fetchone()[0])
        out.append({"lane": lane, "pnl_day_c": day, "pnl_life_c": life})
    return out
