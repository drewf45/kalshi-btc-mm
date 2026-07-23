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
    lines.append(f"  FLIP THESIS (WO-2026-07-22-E): buy the FAVORED (demanded) "
                 f"side @[{config.OPEN_ENTRY_MIN_C},{config.OPEN_ENTRY_MAX_C}]¢ "
                 f"and sell the +{config.OPEN_GOUGE_C} INTO the pile-in of "
                 "buyers (cap 90) — market making acquires inventory on the "
                 "side with DEMAND, not the abandoned cheap side (that inversion "
                 "was the flip_fill=38% bug, 7-for-7 on tape). NO hold: an "
                 f"adverse move stops at entry−{config.OPEN_MOMENTUM_STOP_C}¢ "
                 "(2-poll, maker-first); the endgame curfew hands winners to F. "
                 "trend/depth LOGGED, not gated (P-FLIP-THESIS-1 superseded)")
    lines.append("  FLIP UNCOVERED: legs cover-or-flatten within one cycle "
                 "— healed only on a CONFIRMED resting exit; self-net "
                 "reconciles against broker truth; never ride bare "
                 "(WO-UNCOVERED-FLATTEN)")
    lines.append("  FLIP HUNT: one direction/window, no averaging down "
                 "(HUNT_REFUSE_LOWER); one loss/window sits out; "
                 "through-floor collapse cuts on 2 sustained polls, any "
                 "minute (WO-BLEED-1/3)")
    lines.append(f"  FLIP ENTRY (WO-2026-07-22-E → -G): buys the FAVORED "
                 f"(higher-priced, demanded) side, deliberate band "
                 f"[{config.OPEN_ENTRY_MIN_C},{config.OPEN_ENTRY_MAX_C}]¢ "
                 f"({config.OPEN_ENTRY_MIN_C} the floor — never below fair value "
                 "+ a real pile). The cheap-side filter, swing gate, and "
                 "trend-skip are RETIRED. One shot/window, maker-only, 1 lot")
    lines.append(f"  FLIP WAIT-FOR-THE-PILE (WO-2026-07-22-F → -G): entry ONLY "
                 f"in [{config.OPEN_PILE_START_S},{config.OPEN_PILE_END_S}]s into "
                 "the window, and ONLY when ALL agree — favored side in the "
                 f"deliberate band [{config.OPEN_ENTRY_MIN_C},"
                 f"{config.OPEN_ENTRY_MAX_C}]¢ (§2.1: the skew-level gate was the "
                 f"band in disguise), skew GROWN ≥{config.OPEN_SKEW_GROWTH_C}¢ "
                 f"across the window (the stampede in progress), "
                 f"|trend|≥${config.OPEN_MIN_TREND_USD:.0f} from the pile-window "
                 "baseline (§2.2, not window-open) AND agreeing with the side, "
                 "depth behind it. No pile = no trade; a skipped window logs "
                 "OPEN_SKIP with its reason (the primary data product) — a traded "
                 "window never does (§1.2). PROBE thresholds, the skip log tunes them")
    lines.append(f"  FLIP EXIT (WO-2026-07-22-E): resting take at entry+"
                 f"{config.OPEN_GOUGE_C}¢ (cap 90) sold INTO the pile-in; ONE "
                 f"momentum stop at entry−{config.OPEN_MOMENTUM_STOP_C}¢, 2-poll "
                 "sustain, NO hold, maker-first (rest at the stop; cross only if "
                 "the book is already through). The 240s hold, F-agrees "
                 "conversion, salvage, catastrophe-split, spot-decided walk, and "
                 "walk-down are RETIRED into that one stop. The endgame curfew at "
                 f"secs_left<={config.FLIP_DECISION_S} hands a winner to F, sells "
                 f"a loser; the dead-floor ({config.OPEN_CATASTROPHE_FLOOR}¢, "
                 "depth, 2-poll) now guards a curfew-HELD winner gone worthless")
    lines.append("  FLIP SIDE-ORIENT: exit geometry fully side-relative — the "
                 "held-side price (bid for the held contract) is canonical, so "
                 "NO@X and YES@X get mirror-identical momentum-stop decisions "
                 "(CI gate); F stays one-directional (WO-FLIP-SIDE-ORIENT)")
    lines.append("  FLIP EXIT OWNERSHIP (WO-2026-07-22-G §1.1): two closing "
                 "authorities in one cycle can never sell more than held — the "
                 "safety FLATTEN SUPERSEDES every other FLIP sell for the side "
                 "(a bail, a stale take), so the crossfire is the ONE close (the "
                 "10:19 −87c short, bail 1 + flatten 1 on a 1-lot position, "
                 "cannot recur)")
    lines.append(f"  FLIP FLATTEN FLOOR (WO-2026-07-23-B Part 2, build 64): the "
                 f"uncovered-leg flatten no longer dumps at whatever the book "
                 f"shows — it FLOORS at entry−{config.OPEN_MOMENTUM_STOP_C}−"
                 f"{config.SLIP_TOLERANCE_C} (the stop plus slip tolerance), "
                 "rests ONE poll AT the floor when the book is already through "
                 "it, and only then crosses below — as a COUNTED FLIP_FLOOR_"
                 "BREACH with its overshoot, never a silent market dump (the "
                 "222100 −15c-through-stop leg cannot recur unmeasured)")
    lines.append("  FLIP ENTRY WALL (WO-2026-07-22-J §0.1 → -L §2): ONE per-"
                 "market net check, now CROSS-LANE — it sums EVERY lane's net "
                 "(F under its own key too), so a FLIP lane (OPEN/HUNT) can never "
                 "buy the side OPPOSITE a position F or any lane already holds "
                 "(the auto-net: two fills, two fees, zero position). One net "
                 "position per market, first lane there owns it. FLIP's own "
                 "take-quote net stays FLIP-only. depth_ratio LOGGED not gated "
                 "(§J0.2); the pile baseline requires a real spot (§J0.3)")
    lines.append(f"  F SALVAGE (build 50): a favorite that slips >="
                 f"{config.F_SALVAGE_SLIP_POINTS}pts from entry has lost its "
                 "confidence — an IMMEDIATE table-free price salvage recovers "
                 "(~−40) instead of riding to the −90 backstop; one attempt, "
                 "no re-entry (single-entry wall). F entry rest-back stands "
                 "(build 48, lane-agnostic) (WO-BOTH-LANES-MARKET-TRUE)")
    lines.append(f"  FLIP TIMING (build 51 → WO-...-F): the 90s opening-only "
                 f"cutoff and the 4-min hard-hold are RETIRED — entry is now the "
                 f"[{config.OPEN_PILE_START_S},{config.OPEN_PILE_END_S}]s pile "
                 "window (see WAIT-FOR-THE-PILE above), and the loss exit is the "
                 "one momentum stop (see FLIP EXIT), no hold")
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
    lines.append("  DAILY BUNDLE (WO-2026-07-22-K): /daily → one .xlsx to "
                 "Telegram — a sheet per logged table + the computed SCOREBOARD, "
                 "the reasoning ledger (surface_rows.detail) beside the outcomes "
                 "(cell_outcomes), book tape decimated + full-res around trades, "
                 "scoped to the day. READ-ONLY (mode=ro), built in /tmp, sent, "
                 "deleted — never touches the settle path (/daily N = N days back)")
    lines.append("  DAILY TRUTH (WO-2026-07-23-A, build 63): the pack now grades "
                 "itself. A SUMMARY sheet LEADS (money, expectation, model "
                 "health, fees, anomalies, open questions); the SCOREBOARD is a "
                 "STRUCTURED table — realized P&L (day+life) BESIDE each cell's "
                 "margin, and the model graded against the ACTUAL loss "
                 "(loss_modeled vs loss_actual, be_implied, model_error). A1 "
                 "OPEN break-even anchors on OPEN_MOMENTUM_STOP_C (the live stop), "
                 "not the retired band floor; A2 a hold loss falls back to the "
                 "realized average, never a total loss; A3 salvage gate is 8 "
                 "(reachable). PACK-SIDE ONLY — the trading breakeven()/"
                 "SALVAGE_ADJ_MIN_N are UNTOUCHED, so F and OPEN trade "
                 "byte-identically (acceptance #9, a KILL CONDITION)")
    lines.append("  ORDER TRUTH + F BLOCKER (WO-2026-07-23-B §4.1/§4.4, build "
                 "65-66): every fill carries requested_count/price beside filled "
                 "(does size TRAVEL — the scaling unknown); the pack gains a "
                 "LIFETIME_CELLS sheet + a SUMMARY 'F BLOCKER' line — n, wins, "
                 "losses, avg_win, avg_loss per cell across the WHOLE record, so "
                 "F's 9 lifetime losses (the number the ceiling rests on) are "
                 "finally readable, not hidden by the day scope")
    lines.append(f"  SCALE F (WO-2026-07-23-B Part 1, build 65): F earns ~98% "
                 f"of the book and was capped at {config.NET_RISK_CROSS_LANE_CAP} "
                 f"contracts by a fixed count. F now SELF-SIZES to "
                 f"{int(config.F_NOTIONAL_PCT * 100)}% of book / price (DREW "
                 "DIAL), bounded only by real depth — Kelly and the count cap no "
                 "longer touch F. DREW RULING: the flat cross-lane wall is "
                 "retired for a PER-LANE, BOOK-PROPORTIONAL dollar wall "
                 f"(at-risk % F={int(config.AT_RISK_PCT['F'] * 100)} "
                 f"FLIP={int(config.AT_RISK_PCT['FLIP'] * 100)} "
                 f"D={int(config.AT_RISK_PCT['D'] * 100)} "
                 f"P={int(config.AT_RISK_PCT['P'] * 100)}) — keep the guard, "
                 "don't exempt F (its job is catching a sizing bug); NET_RISK/"
                 "DOLLAR_RISK names kept for telemetry. Guards: (a) total "
                 f"deployed ≤ {int(config.PORTFOLIO_DEPLOY_PCT * 100)}% of book "
                 "across lanes; (b) any single F loss > "
                 f"{config.F_EVENT_TRIPWIRE_C}c/contract suppresses F for the "
                 "day (the rate halt can't protect a 97%-win lane); (d) "
                 "kelly/depth/notional/at-risk terms + the binding one logged")
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
