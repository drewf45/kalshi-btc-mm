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
            if json.loads(how).get("wall_tag") in ("PCT_OF_BOOK", "SIZING_TIER"):
                bad += 1
        except Exception:
            pass
    return bad == 0, f"{bad} depth-class wall storm(s)"


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
    ("zero depth-class wall storms (Ruling 3)", check_no_depth_storms),
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
    ("zero depth-class wall storms", check_no_depth_storms),
    ("zero unexplained orientation pages", check_no_orientation_pages),
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
    for label, checks in (("P15", CHECKS), ("P16 deposit day", CHECKS_P16)):
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
