"""Boot tape — printed IN FULL on every boot (gate 2).

Prints: EPOCH 2 header, the ratification assumptions (A3 — printed every boot
until Drew rules), the DREW-DEFAULT constants, the run mode, the rate governor's
enforced numbers (the printed number IS the enforced number), and recorder
confirmation.
"""

import os
from typing import List

from . import config


def sizing_line(book_cents: int) -> str:
    """P14 §3: the BUDGET is the invariant; lots depend on price — say both.
    (The 7:35 confusion: 'max lots 0' printed at a 99¢ reference while the
    engine correctly traded 1 lot at 39¢ — both were true.)

    WO-VERIFY-LOSSTERM-1 B4: the binding constraint stated IN WORDS — the
    one-lot floor at a high-priced favorite is Kelly arithmetic on a small
    book, not a bug; it self-scales as the book compounds. Every number
    printed here is computed from the live constants, never asserted."""
    import math
    from .sizing import size_order
    budget = int(book_cents * config.KELLY_FRACTION_CEILING)
    # P27 §1: full Kelly — the printed lots are min(kelly, depth), no tier
    l39 = size_order(book_cents, 39, 10_000).contracts
    l99 = size_order(book_cents, 99, 10_000).contracts
    l97 = size_order(book_cents, 97, 10_000).contracts
    l98 = size_order(book_cents, 98, 10_000).contracts
    # book needed for n lots at the 97c favorite reference (computed, so it
    # stays honest if the fraction ever moves by Drew's ruling)
    b2 = math.ceil(2 * 97 / config.KELLY_FRACTION_CEILING / 100)
    b3 = math.ceil(3 * 97 / config.KELLY_FRACTION_CEILING / 100)
    # A-PLAYER B4: the fraction is Drew's dial (KELLY_FRACTION env) — the
    # boot states the LIVE value; code never chooses it.
    frac = config.KELLY_FRACTION_CEILING
    return (f"SIZING: Kelly fraction={frac:.4f} (DREW dial: KELLY_FRACTION"
            f" env) · book ${book_cents / 100:.2f} · "
            f"budget/window {budget}¢ · max lots: {l39} @39¢ · {l99} @99¢ · "
            f"kelly-bound: {l97} lot @97¢ ({l98} @98¢) — throttle is book "
            f"size, not a wall; self-scales ~${b2}→2 @97¢, ~${b3}→3")


def boot_tape(recorder=None, boot_caps=None, auth_line=None) -> List[str]:
    lines = [
        "==================================================================",
        f"RELAY ENGINE BOOT — EPOCH {config.EPOCH} (born; tradeable = live balance)",
        "==================================================================",
        f"RUN_MODE={config.RUN_MODE}"
        + ("  [PAPER SHADOW: zero capital, zero orders]" if not config.live_submit_enabled()
           else "  [LIVE]"),
        "RATIFICATIONS ASSUMED (unruled — printed every boot until Drew rules):",
    ]
    for r in config.RATIFICATIONS_ASSUMED:
        lines.append(f"  ASSUMED: {r}")
    lines.append("RULINGS (Drew's, recorded with scope and sunset):")
    for r in config.RULINGS:
        lines.append(f"  RULED: {r}")
    # P11: the transport is named on every boot; the governor's printed
    # numbers ARE the enforced numbers (P11.1-b).
    if config.WS_ENABLED:
        lines.append("TRANSPORT: WS (upgrade path — re-certified separately)")
    else:
        lines.append("TRANSPORT: REST 1s (A3 proven ground) · books 1/s · "
                     "spot 1.5s · fills 3s · discovery 60s")
    lines.append(
        f"REST GOVERNOR: bucket={config.REST_BUCKET_CAPACITY} tokens, "
        f"refill={config.REST_REFILL_PER_SECOND}/s around ALL REST calls "
        f"(printed number IS the enforced number)")
    lines.append("DREW-DEFAULT constants in force:")
    for k, v in config.drew_defaults().items():
        lines.append(f"  DREW-DEFAULT {k} = {v}")
    lines.append(
        f"RATE GOVERNOR: bucket={config.RATE_BUCKET_CAPACITY} tokens, "
        f"refill={config.RATE_REFILL_PER_SECOND}/s (printed number IS the enforced number)")
    if boot_caps is not None:
        # P8 §3 + P14 §3: 1/12-Kelly honestly stated, at BOTH reference prices.
        lines.append(sizing_line(boot_caps.book_cents))
        # P17 §2.3: the rail state in words — never a silently-zero bound.
        floor_c = int(config.DRAWDOWN_ABSOLUTE_FLOOR_USD * 100)
        if boot_caps.book_cents <= floor_c * 2:
            lines.append(f"rail: DISARMED (book < "
                         f"${config.DRAWDOWN_ABSOLUTE_FLOOR_USD * 2:.0f}) — "
                         f"the rate halt is the only engine stop")
        else:
            lines.append(f"rail: ARMED — floor "
                         f"${config.DRAWDOWN_ABSOLUTE_FLOOR_USD:.2f}, headroom "
                         f"${(boot_caps.book_cents - floor_c) / 100:.2f}")
    if boot_caps is not None:
        lines.append(
            f"BOOT CAPS SNAPSHOT: book={boot_caps.book_cents}c "
            f"order_budget={boot_caps.order_budget_cents}c "
            f"({config.PCT_OF_BOOK_CAP:.0%} of book, byte-identical until a confirmed movement)")
    if recorder is not None:
        lines.append(
            "RECORDER: ON — book snapshots -> book_snapshots "
            "(reader: replay harness + shadow verdicts)"
            + (" [confirmed writing]" if recorder.confirmed_writing() else " [awaiting first frame]"))
    if auth_line is not None:
        lines.append(auth_line)  # "AUTH: key id …last4 loaded, PEM parsed" — never more
    db_note = {"RELAY_DB_PATH": "",
               "derived": " — derived from legacy K_WORKER_DB dir; set RELAY_DB_PATH to pin",
               "ephemeral": " — EPHEMERAL (set RELAY_DB_PATH)"}[config.DB_PATH_SOURCE]
    lines.append(f"DB: {config.DB_PATH} (single-writer: this engine's own database)"
                 + db_note)
    # P16 §2: THE SCALP PROFILE — bank small wins, every lane, every market.
    lines.append("PROFILE — bank small wins, every lane, every market:")
    # P21 A4/A5: PAIR retired (the venue nets one account's sides); FLIP is
    # now HUNT (fast, needle-triggered) + OPEN (patient, grain-sided).
    lines.append(f"  FLIP/HUNT: needle ΔP≥{config.HUNT_NEEDLE_POINTS:.0f}pts "
                 "· take entry+"
                 f"{config.HUNT_TAKE_CENTS} · Job-B fast bails (P18)")
    lines.append(f"  FLIP/OPEN: band {config.OPEN_BAND[0]}-"
                 f"{config.OPEN_BAND[1]}¢ + grain≥{config.OPEN_MIN_GRAIN} "
                 f"· join ≤{config.OPEN_MAX_ENTRY_CENTS}¢ · T-15→T-10 · "
                 "one shot/window · margin printed (info — P27 §2b) · "
                 f"exits TAKE(+{config.OPEN_TAKE_CENTS})/DETERMINED "
                 "(post-patience)/T-10 handoff, evacuations cross "
                 "(P26 §3.2; P-FLIP-THESIS-1; PAIR retired)")
    lines.append("  FLIP: every contract flipped — same-side fills merge "
                 "even across partial-fill timing; exits fire only on "
                 "booked-net; UNCOVERED self-heals then FATALs "
                 "(P-FLIP-COUNT-2)")
    lines.append(f"  FLIP EXIT: determined-against is SPOT+TIME, not price — "
                 f"hold through in-band dips for full "
                 f"{config.OPEN_PATIENCE_S // 60}min patience; the "
                 f"CATASTROPHE floor ({config.OPEN_CATASTROPHE_FLOOR}¢ "
                 "fixed, P&L-blind) is the only price backstop "
                 "(WO-FLIP-EXIT-DOCTRINE)")
    lines.append(f"  FLIP THESIS: buy cheap into ~50/50, patient hold "
                 f"({config.OPEN_PATIENCE_S // 60}min floor, no reflexive "
                 f"cut), scalp ~{config.OPEN_TAKE_CENTS}¢ into the swing OR "
                 "hold-to-settle when F agrees (shared inventory, F stands "
                 "down); T-10 book-aware handoff — winners left to F at "
                 "basis, losers cleared; determined-against cuts a genuine "
                 "loser post-window (P-FLIP-THESIS-1)")
    lines.append("  FLIP UNCOVERED: legs cover-or-flatten within one cycle "
                 "— healed only on a CONFIRMED resting exit; self-net "
                 "reconciles against broker truth; never ride bare "
                 "(WO-UNCOVERED-FLATTEN)")
    lines.append("  FLIP HUNT: one direction/window, no averaging down "
                 "(HUNT_REFUSE_LOWER); one loss/window sits out; "
                 "through-floor collapse cuts on 2 sustained polls, any "
                 "minute (WO-BLEED-1/3)")
    lines.append(f"  FLIP CHEAP-LIVE: cheap entry to {config.OPEN_BAND[0]}¢; "
                 "swing + loser-cut logged (FLIP_SWING / FLIP_LOSER_CUT) "
                 "(WO-FLIP-CHEAP-LIVE)")
    lines.append("  FLIP SWING GATE: measures Instrument 1's MEASURED "
                 "took_swing rate per band (the strike-touch proxy asked "
                 "the wrong event, ~0.89 always); §2 two-barrier price "
                 "model shadow-compared; FLIP capped "
                 f"{config.FLIP_SIZE_CAP}-lot until it tracks measurement "
                 "(WO-SWING-GATE-EVENT)")
    lines.append("  FLIP SIDE-ORIENT: exit geometry fully side-relative — "
                 "held-side price (bid for the held contract) is canonical, "
                 "so NO@X and YES@X get mirror-identical decisions (CI gate); "
                 "the §2 shadow carries the held-side sign (NO's implied "
                 "P(cross) is the complement); F stays one-directional "
                 "(WO-FLIP-SIDE-ORIENT)")
    lines.append(f"  FLIP GOAL-TAKE: take is goal-bounded — entry + "
                 f"clamp(book-goal {config.WINDOW_BOOK_GOAL_CENTS}¢ ÷ held, "
                 f"MIN {config.OPEN_TAKE_MIN}¢, MAX {config.OPEN_TAKE_MAX}¢) — "
                 "bank the reliable convergence move, not a rare +20 that "
                 "rode 201430 to the floor; at 1-lot = entry+"
                 f"{config.OPEN_TAKE_MIN}¢, fee-safe; cut unchanged "
                 "(WO-FLIP-GOAL-TAKE)")
    lines.append("  FLIP ENTRY: opening-imbalance IMMEDIATE — buy the cheap "
                 "side (lower bid, the pile-in-abandoned side) the instant "
                 "it's in-band; grain no longer gates (only informs); "
                 "trend-guarded by HUNT seniority (any live needle yields); "
                 "empirical gate is the resting-take fill rate "
                 "(WO-FLIP-IMMEDIATE-ENTRY)")
    lines.append(f"  FLIP LIQUIDITY-HOLD: low price = illiquidity, not loss — "
                 f"resting take entry+{config.OPEN_TAKE_MIN}¢ holds through the "
                 "pile-in (no reactive scalp/band stop); collapse backstop "
                 "stays (SPOT-decided sustained / catastrophe "
                 f"{config.OPEN_CATASTROPHE_FLOOR}¢, a real move not noise); "
                 "endgame T-10 handoff is the primary loss exit; reversion / "
                 "resting-take fill rate measured (Instrument 1) before size "
                 "(WO-FLIP-LIQUIDITY-HOLD)")
    lines.append(f"  FLIP CATASTROPHE: the price floor cuts only on a REAL "
                 f"move — sustained 2 polls + real held-side depth "
                 f"(≥{config.OPEN_CATASTROPHE_MIN_DEPTH}) + past the "
                 f"{config.OPEN_OPENING_WINDOW_S}s opening-illiquidity window, "
                 "or spot-decided; a thin-book low bid on a fresh cheap entry "
                 "is illiquidity, HELD (no more 2-minute dump) "
                 "(WO-FLIP-CATASTROPHE-ILLIQUIDITY)")
    lines.append(f"  FLIP EVERY-MARKET: the liquidity provider — enters EVERY "
                 f"market's cheap side (both-in-band gate RETIRED; filter is "
                 f"buyable [{config.OPEN_ENTRY_FLOOR},{config.OPEN_MAX_ENTRY_CENTS}"
                 f"]¢ + true-50/50 skip + trend-guard), rests toward the "
                 f"{config.OPEN_MIDDLE_TARGET}¢ MIDDLE scaled by entry depth "
                 f"(clamp(middle, entry+{config.OPEN_TAKE_MIN}, 99): cheaper "
                 f"entry = bigger gouge), and ACTIVELY WALKS the exit down late "
                 f"(WALK_START {config.OPEN_WALK_START_S}s → FLAT_BY "
                 f"{config.OPEN_FLAT_BY}s: an unfilled middle take steps toward "
                 "scratch, re-posting — never a catastrophic bell dump, never "
                 "below scratch) (WO-FLIP-EVERY-MARKET-LIQUIDITY)")
    lines.append(f"  FLIP TIME-AWARE: {config.FLIP_NO_SELL_S}s HARD NO-SELL "
                 "from entry — the opening pile-in is NOISE (spot-decided cut "
                 "AND catastrophe floor both suppressed; only a middle-take "
                 "FILL exits); then F's ΔP proof re-arms and the walk-down "
                 "clears inventory toward the DECISION at secs_left<="
                 f"{config.FLIP_DECISION_S} (~minute 11, MOVED from the T-10 "
                 f"600s handoff): a winner is left to F to ride at FLIP's "
                 "basis, a loser is sold — never caught unfilled at the bell "
                 "(WO-BOTH-LANES-MARKET-TRUE)")
    lines.append(f"  F SALVAGE (build 50): a favorite that slips >="
                 f"{config.F_SALVAGE_SLIP_POINTS}pts from entry has lost its "
                 "confidence — an IMMEDIATE table-free price salvage recovers "
                 "(~−40) instead of riding to the −90 backstop; one attempt, "
                 "no re-entry (single-entry wall). F entry rest-back stands "
                 "(build 48, lane-agnostic) (WO-BOTH-LANES-MARKET-TRUE)")
    from .lane_flip import FLIP_WINDOW_SEC as _FWS
    lines.append(f"  FLIP OPENING-ONLY (build 51): entry HARD-CUT at "
                 f"{config.OPEN_OPENING_WINDOW_S}s into the window (secs_into = "
                 f"{_FWS}−secs_left) — FLIP buys the opening pile-in or SKIPS; "
                 "no mid-market entry (the proven −15/−16 fix). Then the 4-min "
                 "hard-hold + active exit stand unchanged")
    lines.append("  INSTRUMENTATION (build 51): every FLIP conclusion writes "
                 "the COMPLETE data point — entry/exit spot + price + spread + "
                 "secs-into + reason tag + book depth both ends — to the DB and "
                 "the tape; the daily pack fires EVERY day (hour>=9 ET, restart-"
                 "safe) with at-a-glance 24h + fill-rate-by-posted-price, the "
                 "money curve (WO-INSTRUMENTATION-AND-FLIP-TIMING)")
    lines.append(f"  FLIP EXIT-CLUSTER (build 52): the SPOT_DECIDED exit no "
                 f"longer market-DUMPS — a real decision (ΔP>="
                 f"{config.OPEN_DETERMINED_K_POINTS:.0f}pts, raised from 15 "
                 "drift) routes through the WALK-DOWN, a maker to scratch, "
                 "never a crossfire at the depressed bid; the CATASTROPHE deep "
                 "backstop keeps its crossfire (genuinely gone). A hard-"
                 f"trending open (>=${config.OPEN_TREND_SKIP_USD:.0f} spot run) "
                 f"SKIPS OPEN; entry ceiling tightened to "
                 f"{config.OPEN_MAX_ENTRY_CENTS}c (real-gouge only). Swing-gate "
                 "telemetry logged every window. F byte-identical "
                 "(WO-FULL-COLD-AUDIT)")
    lines.append(f"  FLIP SELECTION (build 53): the price floor is RELATIVE — "
                 f"max(20c, entry−{config.OPEN_SALVAGE_BUDGET_C}c) — so every "
                 "loss is bounded at the EV table's own budget (was an absolute "
                 "20c under a 25-42c entry = an undeclared size-by-price); the "
                 "relative floor exits as a MAKER (A5-legal), only the absolute "
                 "20c crossfires. Every OPEN entry prints trend_usd + depth_"
                 "ratio (record-only, calibrate Saturday); the routine "
                 "cover-pending leg no longer pages (WO-2026-07-21-FLIP-SELECTION)")
    lines.append("  FLIP REBOOT-HOLD (build 54): an adopted (reboot-orphan) "
                 "position recovers its REAL fill_ts from the DB — the old 0.0 "
                 "default read as ~56yr old and BYPASSED the 4-min hold every "
                 "restart; a missing ts fails SAFE (fresh, full hold) and PAGES "
                 "FLIP_ORPHAN_ADOPTED. FLOOR_BREACH is pinned to the salvage "
                 "budget (no false alarm on a correct bounded loss); the routine "
                 "1-lot cover-pending is FLIP_UNCOVERED_EXPECTED (debug), the "
                 "orphan pages (WO-2026-07-21-B)")
    lines.append("  CASH-RAIL SETTLE-GUARD (build 55): record_settlement is "
                 "IDEMPOTENT (a retry/reboot mid-settle is dropped, SETTLE_DUP_"
                 "IGNORED — matching record_outcome's ON CONFLICT) and BOUNDED "
                 "at the source: a pnl outside [-cost, lots*100-cost] (+slip) is "
                 "a gross-as-net or win-as-loss booking and pages SETTLE_NOTIONAL"
                 "_BREACH LOUD now, not 6h later at the halt. The overnight halt "
                 "was the rail refusing to trade on a ~$1.99-inflated book — "
                 "CORRECT. The fix is QUARANTINE (divergent=1, re-book at fills-"
                 "truth) never /confirm_cash (that bakes the error in forever) "
                 "(WO-2026-07-22)")
    lines.append("  PER-LANE RATE HALT (KAL-50/50 Stage 0.1): the "
                 f"{config.RATE_HALT_LOSSES}-of-{config.RATE_HALT_WINDOW} rate "
                 "halt is decided PER LANE on per-lane fills P&L — a lane that "
                 "trips halts ONLY itself (RATE_HALT:<lane> at the wall) and "
                 "every other lane trades on. FLIP's losing streak no longer "
                 "halts F, the earner. The account-value window pnl stays the "
                 "cash-integrity unit and the summary line; the legacy global "
                 "halt (no attribution) is preserved for callers without "
                 "per-lane truth. LANE_KILL / cash-fatal / orientation stops "
                 "stay global")
    lines.append("  NARRATION LAW: every ENTRY carries a non-empty why — "
                 "the lanes still print their arithmetic, the wall stopped "
                 "grading it (P27 §2c); brain loaded at boot or explained "
                 "hourly")
    # DIAG-1 §2: which mathematical language F speaks — v1 (any-touch
    # survival vs price) or v2 (at-close; buffered until the column ships).
    lines.append(f"F-proof: {config.F_PROOF_MODE} — surv prints as INFO "
                 "on every F/H8 why (P27 §2a: the gate is dead; returns "
                 "only by Drew ruling with at-close units)")
    lines.append("  F: hold-to-settlement · depth floor stands in thin books "
                 "· SALVAGE armed (P19: needle-collapse exits; catastrophic "
                 "backstop reachable — P16 stop-and-report resolved)")
    lines.append("  SALVAGE: reason-taped (P-SALV-1) · anchor survives "
                 "adoption (P-SALV-2: re-anchored from current spot+table "
                 "at every adoption; disabled loudly, never silently; the "
                 "5% backstop reads no anchor)")
    lines.append("  H8: delta-gated >=99% survive · hold to settlement")
    lines.append("  D: cheap entry + resting recovery take (the baton)")
    lines.append("  P: displacement fade with take (negative-spec born)")
    lines.append("  ORPHAN: adopted at boot · D-grade custody · own attribution")
    lines.append(f"  WALLS: gross+net risk<=3/event · $-at-risk cap · rate "
                 f"halt {config.RATE_HALT_LOSSES}-of-"
                 f"{config.RATE_HALT_WINDOW} (/reset_halt) · orientation "
                 "sentinels · narrated fills · "
                 "graded tape · REJECT_SELF_NET (P21 A2)")
    # P27 §1/§4: full Kelly; the ladder reports, never votes; one governor.
    lines.append("  SIZING: FULL KELLY — min(kelly, depth); the Wilson "
                 "ladder reports (scoreboard, pages, custody scaling) and "
                 "never votes (P27 §1); /scoreboard on demand")
    lines.append("HALTS: rate persists (/reset_halt key); orientation "
                 "auto-heals on a fresh recheck; /reset_halt clears ALL "
                 "entry-halt reasons (cash-fatal keeps its own key); status "
                 "reads the gateway set (WO-HALT-ORPHAN)")
    lines.append(f"GOVERNOR: halt-only — walls stop bugs, custody stops "
                 f"losses, the {config.RATE_HALT_LOSSES}-of-"
                 f"{config.RATE_HALT_WINDOW} rate halt stops bad runs "
                 "(A-PLAYER B3: a single loss is NOISE and halts nothing), "
                 "NOTHING else stops trading (P27; era stamped on every "
                 "cell row)")
    lines.append("CASH INTEGRITY: half-cent exact (B1 — the venue's own "
                 f"units above 90¢) · noise deltas ≤"
                 f"{config.CASH_SILENT_REBASE_CENTS}¢ re-baseline SILENTLY "
                 "(B2 — the overnight book runs untouched) · fatal + "
                 "pending prompt persist across boot (P-CASH-FATAL-1); "
                 "deny outranks boot baseline; /clear_cash_fatal is the "
                 "only key")
    lines.append("EXECUTION E1: P&L books from CONFIRMED fills + exchange "
                 "outcome only (window-econ is a check that self-heals to "
                 "fills-truth, never a source); every settlement writes a "
                 "SETTLE_AUDIT provenance row (per-fill contributions, "
                 "outcome, book-after, unmatched-leg flag) and an unexplained "
                 f"book-vs-venue gap >{config.RECON_AUDIT_FLOOR_CENTS}¢ with 0 "
                 "pending is recorded — the phantom names its source "
                 "(WO-INFRA-HARDENING)")
    lines.append("EXECUTION REST-BACK: maker BUY entries re-price at LIVE "
                 "placement to rest at/inside the held-side bid, strictly "
                 "below the derived ask — never post_only into a cross; FLIP "
                 f"rests {config.FLIP_REST_BACK_CENTS}¢ below the cheap side "
                 "(its liquidity doctrine); a rest-back that breaches the lane "
                 "band SKIPS (waits, never chases); deliberate taker only on "
                 "CUT (WO-MAKER-REST-BACK)")
    # P21 B1: the boot cites the doctrine — one page says what the machine
    # believes, why, and what would change its mind. Cited, and verified
    # present (a missing registry is worth a loud boot line, never a crash).
    sem = os.path.join(os.path.dirname(__file__), "..", "docs", "SEMANTICS.md")
    lines.append("DOCTRINE: docs/SEMANTICS.md — every KNOWN carries "
                 "LAW · CODE · TAPE; drift demotes and pages (P21 B1/B2)"
                 + ("" if os.path.exists(sem) else " [MISSING FROM TREE]"))
    lines.append("==================================================================")
    return lines


def print_boot_tape(recorder=None, boot_caps=None, auth_line=None) -> List[str]:
    lines = boot_tape(recorder=recorder, boot_caps=boot_caps, auth_line=auth_line)
    for line in lines:
        print(line, flush=True)
    return lines
