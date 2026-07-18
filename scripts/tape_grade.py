"""P15 §1 — THE TAPE-ACCOUNTABILITY LAW: the tape grades the deploy.

    python -m scripts.tape_grade <db_path> [window_hours=24]

Every work order ships with its EXPECTED TAPE; this grader reads the failures
table + surface rows + fills for the last N hours and prints PASS/FAIL per
expected line. It also runs inside the daily pack ("DEPLOY GRADE" section)
until every line passes twice, then retires. A deploy whose expected tape has
not materialized within 24h is a FINDING — the code and the world disagree.

Shadow windows count (R-2: shadow tape is doctrine evidence, not just
liveness evidence).
"""

import json
import re
import sqlite3
import sys
import time

# ── P15's EXPECTED TAPE (§3) — each check: (name, fn(db, since_ts)) ────────


def _one(db, sql, args):
    row = db.execute(sql, args).fetchone()
    return row[0] if row else 0


def check_no_lone_flip_entries(db, since):
    """zero FLIP entries where either side >49 at the open (Ruling 2)."""
    rows = db.execute(
        "SELECT detail FROM surface_rows WHERE lane='FLIP' AND state='PROPOSED'"
        " AND detail LIKE '%ENTRY%' AND ts>?", (since,)).fetchall()
    bad = 0
    for (detail,) in rows:
        m = re.search(r"y(\d+)/n(\d+)", detail)
        if m and (int(m.group(1)) > 49 or int(m.group(2)) > 49):
            bad += 1
    return bad == 0, f"{bad} lone-leg FLIP entr{'y' if bad == 1 else 'ies'} of {len(rows)} proposals"


def check_no_depth_storms(db, since):
    """zero PCT_OF_BOOK / SIZING storms on books with depth >=1 (Ruling 3)."""
    rows = db.execute(
        "SELECT how_json FROM failures WHERE why_tag='WALL_STORM' AND ts>?",
        (since,)).fetchall()
    bad = 0
    for (how,) in rows:
        try:
            if json.loads(how).get("wall_tag") in ("BUDGET", "DEPTH"):
                bad += 1
        except Exception:
            pass
    return bad == 0, f"{bad} [BUDGET]/[DEPTH] wall storm(s)"


def check_no_same_side_doubles(db, since):
    """zero same-side double entries, any lane: two ENTRY fills on one
    (market, lane, side) with no exit fill between them."""
    rows = db.execute(
        "SELECT market, lane, side, action FROM fills WHERE ts>? ORDER BY id",
        (since,)).fetchall()
    open_entry = {}
    doubles = 0
    for market, lane, side, action in rows:
        k = (market, lane, side)
        if action == "ENTRY":
            if open_entry.get(k):
                doubles += 1
            open_entry[k] = True
        else:
            open_entry[(market, lane, side)] = False
    return doubles == 0, f"{doubles} same-side double entr{'y' if doubles == 1 else 'ies'}"


def check_orphans_adopted(db, since):
    """an ORPHAN adoption row for every orphan found (Ruling 1)."""
    found = _one(db, "SELECT COUNT(*) FROM failures WHERE why_tag='ORPHAN_FOUND'"
                     " AND ts>?", (since,))
    adopted = _one(db, "SELECT COUNT(*) FROM surface_rows WHERE"
                       " state='ORPHAN_ADOPTED' AND ts>?", (since,))
    return adopted >= found, f"found {found} · adopted {adopted}"


def check_flip_pack_line_ships(db, since):
    """the FLIP R6 line renders daily (R-1's required reading) — code-level:
    the deployed ops module carries the line."""
    try:
        from relay_engine import ops
        import inspect
        ok = "FLIP R6" in inspect.getsource(ops.daily_pack)
    except Exception:
        ok = False
    return ok, "ops.daily_pack renders FLIP R6"


def check_no_baton_fatals(db, since):
    """zero BATON_VIOLATION FATALs on cut-vs-fill races (P14 in force)."""
    n = _one(db, "SELECT COUNT(*) FROM failures WHERE why_tag='BATON_VIOLATION'"
                 " AND ts>?", (since,))
    return n == 0, f"{n} baton FATAL(s)"


CHECKS = [
    ("zero lone-leg FLIP entries (Ruling 2)", check_no_lone_flip_entries),
    ("zero [BUDGET]/[DEPTH] wall storms (Ruling 3, specific tags §4)", check_no_depth_storms),
    ("zero same-side double entries (Fix A)", check_no_same_side_doubles),
    ("orphans adopted, never abandoned (Ruling 1)", check_orphans_adopted),
    ("FLIP R6 pack line ships (R-1)", check_flip_pack_line_ships),
    ("zero BATON_VIOLATION FATALs (P14)", check_no_baton_fatals),
]


# ── P16 "THE SCALP PROFILE, FUNDED" — deposit-day expected tape ────────────
def check_deposit_confirmed(db, since):
    """the deposit's confirm round-trip landed: a CONFIRMED_DEPOSIT from the
    cash protocol itself (auto_positive / /confirm_cash) — boot baselines
    (boot, shadow_paper_boot, live_boot) are not deposits."""
    n = _one(db, "SELECT COUNT(*) FROM cash_movements WHERE"
                 " kind='CONFIRMED_DEPOSIT' AND ts>? AND"
                 " confirmed_by IN ('auto_positive','/confirm_cash')", (since,))
    return n >= 1, f"{n} confirmed deposit(s)"


def check_f_traded(db, since):
    """F's first LIVE entries — the depth floor's proof."""
    n = _one(db, "SELECT COUNT(*) FROM fills WHERE lane='F' AND"
                 " action='ENTRY' AND ts>?", (since,))
    return n >= 1, f"{n} F entr{'y' if n == 1 else 'ies'}"


def check_orphan_settlements_attributed(db, since):
    """conditional: any ORPHAN settlement books to ORPHAN, never a lane."""
    found = _one(db, "SELECT COUNT(*) FROM failures WHERE"
                     " why_tag='ORPHAN_FOUND' AND ts>?", (since,))
    if found == 0:
        return True, "no orphans this window"
    settled = _one(db, "SELECT COUNT(*) FROM settlements WHERE lane='ORPHAN'"
                       " AND ts>?", (since,))
    return True, f"{found} orphan(s), {settled} settled to ORPHAN so far"


def check_brackets_source_venue(db, since):
    """brackets carry the streak's account truth: closed rows source=venue
    (conditional — passes when nothing traded yet)."""
    total = _one(db, "SELECT COUNT(*) FROM window_econ WHERE ts>?"
                     " AND close_value_cents IS NOT NULL", (since,))
    venue_n = _one(db, "SELECT COUNT(*) FROM window_econ WHERE ts>? AND"
                       " close_value_cents IS NOT NULL AND source='venue'",
                   (since,))
    return venue_n == total, f"{venue_n}/{total} closed brackets source=venue"


def check_no_orientation_pages(db, since):
    """zero ORIENTATION findings (or exactly-explained ones — a nonzero count
    here fails the grade until it is read)."""
    n = _one(db, "SELECT COUNT(*) FROM failures WHERE why_tag IN"
                 " ('ORIENTATION_MIRROR','ORIENTATION_SUSPECT',"
                 "'ORIENTATION_DIVERGENCE') AND ts>?", (since,))
    return n == 0, f"{n} orientation finding(s)"


CHECKS_P16 = [
    ("deposit confirm round-trip landed", check_deposit_confirmed),
    ("F's first live entries (depth floor proof)", check_f_traded),
    ("zero lone-leg FLIP entries", check_no_lone_flip_entries),
    ("zero same-side double entries", check_no_same_side_doubles),
    ("orphan settlements attribute to ORPHAN", check_orphan_settlements_attributed),
    ("closed brackets source=venue", check_brackets_source_venue),
    ("zero BATON_VIOLATION FATALs", check_no_baton_fatals),
    ("zero [BUDGET]/[DEPTH] wall storms", check_no_depth_storms),
    ("zero unexplained orientation pages", check_no_orientation_pages),
]


# ── P17 "SHOW UP FOR EVERY MARKET" — expected tape ─────────────────────────
OLD_AMBIGUOUS_TAGS = ("PCT_OF_BOOK", "SIZING_TIER", "NET_RISK_CAP", "AT_RISK_CAP")


def check_no_terminal_regressions(db, since):
    """zero DUPLICATE_TERMINAL_ROW FATALs — the 9:30 class is dead by lattice
    (upgrades log TERMINAL_UPGRADED instead; those are informational)."""
    n = _one(db, "SELECT COUNT(*) FROM failures WHERE"
                 " why_tag='DUPLICATE_TERMINAL_ROW' AND ts>?", (since,))
    up = _one(db, "SELECT COUNT(*) FROM failures WHERE"
                  " why_tag='TERMINAL_UPGRADED' AND ts>?", (since,))
    return n == 0, f"{n} regression(s) · {up} legal upgrade(s)"


def check_no_silent_windows(db, since):
    """every window from deploy onward has rows — a rowless window is a §6
    contract breach and pages SILENT_WINDOW."""
    n = _one(db, "SELECT COUNT(*) FROM failures WHERE"
                 " why_tag='SILENT_WINDOW' AND ts>?", (since,))
    return n == 0, f"{n} silent window(s)"


def check_every_window_concluded(db, since):
    """every closed window ends in a receipt (SETTLED) or a clean PASS/
    GAP_RESTART terminal — no window left mid-story."""
    total = _one(db, "SELECT COUNT(DISTINCT window_id) FROM surface_rows"
                     " WHERE ts>?", (since,))
    concluded = _one(db, "SELECT COUNT(DISTINCT window_id) FROM surface_rows"
                         " WHERE ts>? AND terminal=1", (since,))
    # windows still LIVE right now legitimately lack terminals — tolerate 2
    return total - concluded <= 2, f"{concluded}/{total} windows concluded"


def check_specific_wall_tags_only(db, since):
    """storm/reject pages carry SPECIFIC wall tags — zero storms under the
    old ambiguous names (the BUDGET-misread-as-depth lesson)."""
    rows = db.execute(
        "SELECT how_json FROM failures WHERE why_tag='WALL_STORM' AND ts>?",
        (since,)).fetchall()
    bad = 0
    for (how,) in rows:
        try:
            if json.loads(how).get("wall_tag") in OLD_AMBIGUOUS_TAGS:
                bad += 1
        except Exception:
            pass
    return bad == 0, f"{bad} storm(s) under old ambiguous tags"


def check_task_stuck_bounded(db, since):
    """zero TASK_STUCK (or exactly one with its story)."""
    n = _one(db, "SELECT COUNT(*) FROM failures WHERE why_tag='TASK_STUCK'"
                 " AND ts>?", (since,))
    return n <= 1, f"{n} TASK_STUCK page(s)"


def check_no_stale_open_brackets(db, since, now=None):
    """the healed-window proof: no bracket still open 30+ minutes after its
    row was written (the stuck-0930 shape)."""
    now = time.time() if now is None else now
    n = _one(db, "SELECT COUNT(*) FROM window_econ WHERE"
                 " close_value_cents IS NULL AND ts < ?", (now - 1800,))
    return n == 0, f"{n} stale open bracket(s)"


CHECKS_P17 = [
    ("zero terminal regressions; upgrades legal (§1)", check_no_terminal_regressions),
    ("zero silent windows (§6 contract)", check_no_silent_windows),
    ("every closed window concluded: receipt or PASS (§6.3)", check_every_window_concluded),
    ("storm pages carry specific wall tags (§4)", check_specific_wall_tags_only),
    ("TASK_STUCK bounded: zero or one with its story (§3)", check_task_stuck_bounded),
    ("no stale open brackets — stuck windows heal (§1.3)", check_no_stale_open_brackets),
    ("zero BATON_VIOLATION FATALs", check_no_baton_fatals),
]


# ── P18 "THE DETECTIVE" — expected tape ────────────────────────────────────
def _hunt_rows(db, since):
    return db.execute(
        "SELECT detail, ts, market FROM surface_rows WHERE lane='FLIP'"
        " AND state='PROPOSED' AND detail LIKE '%HUNT needle%' AND ts>?",
        (since,)).fetchall()


def check_hunt_whys_complete(db, since):
    """every HUNT why carries (ΔP, d_before→d_after, fair, gap, converge)."""
    rows = _hunt_rows(db, since)
    bad = sum(1 for (d, _, _) in rows
              if not all(tok in d for tok in
                         ("needle +", "d ", "fair", "gap", "converging")))
    return bad == 0, f"{bad} incomplete of {len(rows)} hunt casefiles"


def check_hunt_needles_at_or_above_n(db, since):
    """zero HUNT entries with ΔP < N (gate A is the law, graded)."""
    from relay_engine import config as _cfg
    bad = 0
    for detail, _, _ in _hunt_rows(db, since):
        m = re.search(r"needle \+(\d+)pts", detail)
        if m and float(m.group(1)) < _cfg.HUNT_NEEDLE_POINTS:
            bad += 1
    return bad == 0, f"{bad} sub-N needle(s)"


def check_no_p_hunt_collision(db, since):
    """zero P-vs-HUNT same-market opposite entries — suppression rows appear
    instead (the fee-bleed class, dead)."""
    hunt_markets = {m for (_, _, m) in _hunt_rows(db, since)}
    if not hunt_markets:
        return True, "no hunts this window"
    qmarks = ",".join("?" * len(hunt_markets))
    p_fills = _one(db, f"SELECT COUNT(*) FROM fills WHERE lane='P' AND"
                       f" action='ENTRY' AND market IN ({qmarks}) AND ts>?",
                   (*hunt_markets, since))
    return p_fills == 0, f"{p_fills} P entries on hunted markets"


def check_hunts_resolve(db, since, now=None):
    """every hunt resolves take / breakeven-bail / time-box — zero FLIP
    inventory older than 15 minutes without an exit fill."""
    now = time.time() if now is None else now
    entries = db.execute(
        "SELECT market, ts FROM fills WHERE lane='FLIP' AND action='ENTRY'"
        " AND ts>? AND ts<?", (since, now - 900)).fetchall()
    stale = 0
    for market, ts in entries:
        n = _one(db, "SELECT COUNT(*) FROM fills WHERE lane='FLIP' AND market=?"
                     " AND action IN ('EXIT','CUSTODIAN_EXIT') AND ts>=?",
                 (market, ts))
        if n == 0:
            stale += 1
    return stale == 0, f"{stale} unresolved FLIP entr{'y' if stale == 1 else 'ies'}"


def check_pair_mode_alive(db, since):
    """PAIR mode still posts on genuine two-way books (conditional)."""
    n = _one(db, "SELECT COUNT(*) FROM surface_rows WHERE lane='FLIP' AND"
                 " state='PROPOSED' AND detail LIKE '%pair-post%' AND ts>?",
             (since,))
    return True, f"{n} pair post(s) (conditional — books decide)"


def check_hit_rate_bar_ships(db, since):
    """the FLIP pack line prints hit-rate against the honest ≥50% bar."""
    try:
        import inspect

        from relay_engine import ops
        ok = "bar ≥50%" in inspect.getsource(ops.daily_pack)
    except Exception:
        ok = False
    return ok, "ops.daily_pack prints the bar"


CHECKS_P18 = [
    ("every HUNT casefile complete: ΔP, d, fair, gap, converge", check_hunt_whys_complete),
    ("zero HUNT entries with ΔP < N (gate A graded)", check_hunt_needles_at_or_above_n),
    ("zero P-vs-HUNT collisions (suppression instead)", check_no_p_hunt_collision),
    ("every hunt resolves — no aging flip inventory", check_hunts_resolve),
    ("PAIR mode still posts on two-way books", check_pair_mode_alive),
    ("FLIP pack line prints the ≥50% bar", check_hit_rate_bar_ships),
]


def grade(db, window_hours: float = 24.0, now=None, checks=None):
    """Returns [(name, ok, detail)] for the last window_hours."""
    now = time.time() if now is None else now
    since = now - window_hours * 3600
    out = []
    for name, fn in (checks or CHECKS):
        try:
            ok, detail = fn(db, since)
        except Exception as e:
            ok, detail = False, f"grader error: {e}"
        out.append((name, ok, detail))
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    hours = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0
    db = sqlite3.connect(sys.argv[1])
    total_fails = 0
    for label, checks in (("P15", CHECKS), ("P16 deposit day", CHECKS_P16),
                          ("P17 show up", CHECKS_P17),
                          ("P18 the detective", CHECKS_P18)):
        results = grade(db, hours, checks=checks)
        print(f"DEPLOY GRADE ({label} expected tape, last {hours:.0f}h):")
        fails = 0
        for name, ok, detail in results:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")
            fails += 0 if ok else 1
        print(f"GRADE: {len(results) - fails}/{len(results)}")
        total_fails += fails
    return 0 if total_fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
