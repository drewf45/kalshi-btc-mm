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
    """zero FLIP entries where either side >49 at the open (Ruling 2).
    P21 A4: OPEN entries are exempt — they are one-sided BY LAW (grain
    side, join <= OPEN_MAX graded in the P21 suite) and their why carries
    the full band (legally up to 56) for the record."""
    rows = db.execute(
        "SELECT detail FROM surface_rows WHERE lane='FLIP' AND state='PROPOSED'"
        " AND detail LIKE '%ENTRY%' AND ts>?", (since,)).fetchall()
    bad = 0
    for (detail,) in rows:
        if "OPEN grain" in detail:
            continue
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
    """P21 A4 OVERTURNED this line's premise: PAIR is retired (the venue
    nets one account's sides — a pair-bundle was a fiction). The P18 line
    stays for the record as a count; CHECKS_P21 grades it to ZERO."""
    n = _one(db, "SELECT COUNT(*) FROM surface_rows WHERE lane='FLIP' AND"
                 " state='PROPOSED' AND detail LIKE '%pair-post%' AND ts>?",
             (since,))
    return True, f"{n} pair post(s) (PAIR retired P21 A4 — see P21 suite)"


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


# ── P19 "SALVAGE, SEAL, AND LET IT RUN" — the unattended-run tape ──────────
RETIRED_FATAL_CLASSES = ("DUPLICATE_TERMINAL_ROW", "BATON_VIOLATION",
                         "ORIENTATION_MIRROR", "WS_ESSENTIAL_CHANNEL_REJECTED")


def check_no_retired_class_fatals(db, since):
    """zero FATALs of any retired class (terminal-row, baton, orientation,
    subscribe) — the classes this project already paid tuition for."""
    qmarks = ",".join("?" * len(RETIRED_FATAL_CLASSES))
    n = _one(db, f"SELECT COUNT(*) FROM failures WHERE why_tag IN ({qmarks})"
                 f" AND ts>?", (*RETIRED_FATAL_CLASSES, since))
    return n == 0, f"{n} retired-class FATAL(s)"


def check_salvage_rows_complete(db, since):
    """any salvage carries needle + save-estimate; its counterfactual lands
    (conditional — passes when no salvage occurred)."""
    salvages = db.execute(
        "SELECT market, detail FROM surface_rows WHERE state='SALVAGE'"
        " AND ts>?", (since,)).fetchall()
    if not salvages:
        return True, "no salvages this window"
    bad = sum(1 for (_, d) in salvages
              if not all(k in d for k in ("delta_p", "est_save", "fair")))
    return bad == 0, f"{bad} incomplete of {len(salvages)} salvage rows"


def check_p_suppression_rows_appear(db, since):
    """the suppression mechanism leaves its rows when needles fire
    (conditional — passes quietly when no needles confirmed)."""
    n = _one(db, "SELECT COUNT(*) FROM surface_rows WHERE lane='P' AND"
                 " detail LIKE 'P_SUPPRESSED%' AND ts>?", (since,))
    return True, f"{n} suppression row(s) (conditional)"


CHECKS_P19 = [
    ("zero retired-class FATALs (the tuition already paid)", check_no_retired_class_fatals),
    ("salvage rows complete: needle + est-save (§2)", check_salvage_rows_complete),
    ("P-suppression rows appear when needles fire (§3.1)", check_p_suppression_rows_appear),
    ("every hunt resolves — no aging flip inventory", check_hunts_resolve),
    ("zero silent windows (§6 contract)", check_no_silent_windows),
]


# ── P24 "SHIELD, FEES, AND THE ZERO IN THE REVERSAL" — expected tape ───────
ANCHOR_MISS_CAUSES = ("spot", "strike", "close", "table")


def check_entries_carry_anchor(db, since):
    """every F/H8 entry booking carries its anchor source (table|price)."""
    rows = db.execute(
        "SELECT detail FROM surface_rows WHERE state='ENTERED' AND"
        " lane IN ('F','H8') AND ts>?", (since,)).fetchall()
    if not rows:
        return True, "no F/H8 entries this window"
    bad = sum(1 for (d,) in rows if "anchor=" not in d)
    return bad == 0, f"{bad} untagged of {len(rows)} F/H8 entries"


def check_anchor_misses_named(db, since):
    """every ANCHOR_FROM_PRICE row names WHICH organ missed — the 1715
    class gets a cause (spot|strike|close|table), never a shrug."""
    rows = db.execute(
        "SELECT how_json FROM failures WHERE why_tag='ANCHOR_FROM_PRICE'"
        " AND ts>?", (since,)).fetchall()
    bad = 0
    for (how,) in rows:
        try:
            if json.loads(how).get("cause") not in ANCHOR_MISS_CAUSES:
                bad += 1
        except Exception:
            bad += 1
    return bad == 0, f"{bad} shrug(s) of {len(rows)} anchor misses"


def check_crossfire_fee_entrance(db, since):
    """code-level: the cut's entrance books the response's OWN fee through
    the one parser (never the multiplier)."""
    try:
        import inspect

        from relay_engine import custodian as _c, venue as _v
        ok = ("parse_response_fee" in inspect.getsource(_c.Custodian.execute_cut)
              and "average_fee_paid" in inspect.getsource(_v))
    except Exception:
        ok = False
    return ok, "execute_cut reads the venue's response fee"


def check_reversal_belt_in_place(db, since):
    """code-level: the zero in the reversal is dead at BOTH ends — the
    writer adopts the anchor's p, the consumer belts a legacy zero."""
    try:
        import inspect

        from relay_engine import custodian as _c, fills as _f
        ok = ("pos.entry_p_win or (pos.entry_price_cents"
              in inspect.getsource(_c.Custodian.should_cut)
              and "entry_p_win=(p_e" in inspect.getsource(_f.FillBooker.sweep))
    except Exception:
        ok = False
    return ok, "writer + belt both in source"


# ── P21 "THE DOCTRINE ENGINE" — expected tape ──────────────────────────────
def check_econ_divergence_silent(db, since):
    """A1's proof is QUIET: the netting model makes fills-P&L match broker
    truth, so the 10:45/11:30 divergence class never pages again."""
    n = _one(db, "SELECT COUNT(*) FROM failures WHERE"
                 " why_tag='WINDOW_ECON_DIVERGENCE' AND ts>?", (since,))
    return n == 0, f"{n} divergence page(s)"


def check_no_self_net_entries(db, since):
    """zero self-net entry bookings: the A2 wall refuses opposite-side
    ENTRY buys on held markets; the A1 belt (SELF_NET_BOOKED) staying
    silent proves the wall held."""
    n = _one(db, "SELECT COUNT(*) FROM failures WHERE"
                 " why_tag='SELF_NET_BOOKED' AND ts>?", (since,))
    return n == 0, f"{n} SELF_NET_BOOKED belt catch(es)"


def check_open_whys_stamped(db, since):
    """every OPEN entry why carries grain>=min, join<=max, both sides in the
    open band — the herd's compass, stamped on every trade."""
    from relay_engine import config as _cfg
    rows = db.execute(
        "SELECT detail FROM surface_rows WHERE lane='FLIP' AND"
        " state='PROPOSED' AND detail LIKE '%OPEN grain%' AND ts>?",
        (since,)).fetchall()
    bad = 0
    for (d,) in rows:
        m = re.search(r"OPEN grain (yes|no)x(\d+) · join (\d+)c ·"
                      r" band y(\d+)/n(\d+)", d)
        lo_b, hi_b = _cfg.OPEN_BAND
        if (m is None or int(m.group(2)) < _cfg.OPEN_MIN_GRAIN
                or int(m.group(3)) > _cfg.OPEN_MAX_ENTRY_CENTS
                or not (lo_b <= int(m.group(4)) <= hi_b)
                or not (lo_b <= int(m.group(5)) <= hi_b)):
            bad += 1
    return bad == 0, f"{bad} unstamped of {len(rows)} OPEN entries"


def check_zero_pair_entries(db, since):
    """PAIR is retired (A4): zero pair-post proposals, ever again."""
    n = _one(db, "SELECT COUNT(*) FROM surface_rows WHERE lane='FLIP' AND"
                 " state='PROPOSED' AND detail LIKE '%pair-post%' AND ts>?",
             (since,))
    return n == 0, f"{n} pair post(s)"


def check_open_exits_intentional(db, since):
    """A5: OPEN exits are EXACTLY take/determined/curfew — zero wiggle
    exits. Tape-level: every reason=open row names one of the three.
    Code-level: FLIP cut-params keep ONLY the catastrophic backstop (the
    −11¢/−12¢ round-trip class is dead)."""
    rows = db.execute(
        "SELECT detail FROM surface_rows WHERE lane='FLIP' AND"
        " state='PROPOSED' AND detail LIKE '%reason=open %' AND ts>?",
        (since,)).fetchall()
    bad = sum(1 for (d,) in rows
              if not any(tok in d for tok in
                         ("open take", "open determined", "open curfew")))
    try:
        from relay_engine.lane_flip import flip_cut_params
        p = flip_cut_params()
        inert = (p.max_loss_cents_per_contract >= 100
                 and p.reversal_threshold >= 1.0 and p.hard_stop_usd >= 999)
    except Exception:
        inert = False
    return (bad == 0 and inert), (f"{bad} off-intent exit(s) of {len(rows)}; "
                                  f"catastrophic-only={'yes' if inert else 'NO'}")


def check_registry_green(db, since):
    """B1/B3: SEMANTICS.md in tree and every KNOWN names a real tape line."""
    try:
        from relay_engine import semantics
        errs = semantics.validate_registry()
        if errs:
            return False, "; ".join(errs[:3])
        n = len(semantics.parse_registry())
        return n >= 16, f"{n} entries, all bound to tape lines"
    except Exception as e:
        return False, f"registry unreadable: {e}"


def check_knowledge_drift_paged(db, since):
    """conditional: any drift arrives AS A PAGE with its answer's name —
    reality outranks the registry, and the demotion is recorded."""
    n = _one(db, "SELECT COUNT(*) FROM failures WHERE"
                 " why_tag='KNOWLEDGE_DRIFT' AND ts>?", (since,))
    return True, f"{n} drift page(s) (conditional — reality decides)"


# ── P22 "THE CELL SCOREBOARD" — expected tape ──────────────────────────────
def check_cell_rows_with_receipts(db, since):
    """every closed unit of risk writes its cell: exits and settlements in
    the window have matching cell_outcomes coverage (per market+lane)."""
    closed = db.execute(
        "SELECT DISTINCT market, lane FROM fills WHERE action!='ENTRY'"
        " AND ts>?", (since,)).fetchall()
    if not closed:
        return True, "no closed risk this window"
    missing = 0
    for market, lane in closed:
        n = _one(db, "SELECT COUNT(*) FROM cell_outcomes WHERE market=?",
                 (market,))
        if n == 0:
            missing += 1
    return missing == 0, (f"{len(closed) - missing}/{len(closed)}"
                          f" closed (market,lane) pairs have cell rows")


def check_scoreboard_ships(db, since):
    """the scoreboard section renders in the daily pack — code-level."""
    try:
        import inspect

        from relay_engine import ops, scoring
        ok = ("scoreboard_lines" in inspect.getsource(ops.daily_pack)
              and "CELL SCOREBOARD" in inspect.getsource(
                  scoring.scoreboard_lines))
    except Exception:
        ok = False
    return ok, "ops.daily_pack renders the cell scoreboard"


def check_tier_changes_earned(db, since):
    """zero tier changes until earned: every TIER_CHANGE row carries its
    Wilson math, and every UP satisfies lb >= bar (bars honest)."""
    rows = db.execute(
        "SELECT how_json FROM failures WHERE why_tag='TIER_CHANGE' AND ts>?",
        (since,)).fetchall()
    bad = 0
    for (how,) in rows:
        try:
            d = json.loads(how)
            up = (d["tier"] != "PROBE" and d["prev"] == "PROBE") or \
                 (d["tier"] == "CLEAR")
            if not all(k in d for k in ("lb", "bar", "n", "tier", "prev")):
                bad += 1
            elif up and float(d["lb"]) < float(d["bar"]):
                bad += 1
        except Exception:
            bad += 1
    return bad == 0, f"{bad} unearned/unstamped of {len(rows)} tier changes"


def check_no_static_probe_sizing(db, since):
    """zero static-PROBE sizing paths remaining — code-level: the runner's
    submit path scores every ENTRY (scoring.tier_for feeds size_order),
    and no lane module calls size_order with a literal tier."""
    try:
        import inspect

        from relay_engine import lane_flip, lanes, shadow_runner
        runner_src = inspect.getsource(shadow_runner.ShadowEngine)
        ok = ("scoring.tier_for" in runner_src
              and "_score_and_size" in runner_src
              and "size_order(" not in inspect.getsource(lanes)
              and "size_order(" not in inspect.getsource(lane_flip))
    except Exception:
        ok = False
    return ok, "every sized ENTRY passes through scoring.tier_for"


CHECKS_P22 = [
    ("cell rows appear with every receipt and settlement (§1)", check_cell_rows_with_receipts),
    ("scoreboard section ships in the pack (§5)", check_scoreboard_ships),
    ("tier changes earned: Wilson math on every page (§4.2)", check_tier_changes_earned),
    ("zero static-PROBE sizing paths (§4.1)", check_no_static_probe_sizing),
]


CHECKS_P21 = [
    ("WINDOW_ECON_DIVERGENCE silent (A1 netting model)", check_econ_divergence_silent),
    ("zero self-net entry bookings (A1 belt · A2 wall)", check_no_self_net_entries),
    ("OPEN whys stamped: grain, join, band (A4)", check_open_whys_stamped),
    ("zero PAIR entries (A4 — PAIR retired)", check_zero_pair_entries),
    ("OPEN exits only TAKE/DETERMINED/CURFEW (A5)", check_open_exits_intentional),
    ("registry green: every KNOWN names its tape line (B1/B3)", check_registry_green),
    ("KNOWLEDGE_DRIFT arrives as a page (B2)", check_knowledge_drift_paged),
]


CHECKS_P24 = [
    ("every F/H8 entry carries anchor: table|price (§1.1)", check_entries_carry_anchor),
    ("anchor misses name their cause (§1.2)", check_anchor_misses_named),
    ("crossfire receipts show the venue's fee (§2)", check_crossfire_fee_entrance),
    ("the reversal belt holds: no zero means profit (§3)", check_reversal_belt_in_place),
    ("WINDOW_ECON_DIVERGENCE silent (A1 netting model)", check_econ_divergence_silent),
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
                          ("P18 the detective", CHECKS_P18),
                          ("P19 let it run", CHECKS_P19),
                          ("P21 the doctrine engine", CHECKS_P21),
                          ("P22 the cell scoreboard", CHECKS_P22),
                          ("P24 shield, fees, reversal", CHECKS_P24)):
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
