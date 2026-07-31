"""Boot tape — printed IN FULL on every boot (gate 2).

Prints: EPOCH 2 header, the ratification assumptions (A3 — printed every boot
until Drew rules), the DREW-DEFAULT constants, the run mode, the rate governor's
enforced numbers (the printed number IS the enforced number), and recorder
confirmation.
"""

import os
from typing import List

from . import config


def sizing_line(book_cents: int, owed_cents: int = 0) -> str:
    """P14 §3: the BUDGET is the invariant; lots depend on price — say both.
    WO-2026-07-26-O §O2/O3: sizing works off TRADEABLE = book − owed; the banner
    prints book / owed / tradeable so the scrape is visible and the size honest.
    (The 7:35 confusion: 'max lots 0' printed at a 99¢ reference while the
    engine correctly traded 1 lot at 39¢ — both were true.)

    WO-VERIFY-LOSSTERM-1 B4: the binding constraint stated IN WORDS — the
    one-lot floor at a high-priced favorite is Kelly arithmetic on a small
    book, not a bug; it self-scales as the book compounds. Every number
    printed here is computed from the live constants, never asserted."""
    from .sizing import size_order
    tradeable = max(0, book_cents - owed_cents)   # WO-O §O2: the real sizing base
    budget = int(tradeable * config.KELLY_FRACTION_CEILING)
    # WO-2026-07-24-G Part 4: the boot preview used to call size_order WITHOUT
    # lane= — it printed the generic Kelly path, NOT the notional paths F and
    # FLIP actually trade. Print the REAL per-lane lot counts at the live book so
    # the banner stops lying about size. F @97¢ (its favorite) and FLIP @58¢ (mid
    # band) — the two dials that scale with the book.
    f97 = size_order(tradeable, 97, 10_000, lane="F")
    flip58 = size_order(tradeable, 58, 10_000, lane="FLIP")
    # WO-2026-07-24-G Part 2: the rate-halt drawdown recomputes with the book —
    # print the LIVE value (4 stop-outs at current FLIP size), never a frozen
    # constant. WO-O §O2: off tradeable.
    halt = config.rate_halt_drawdown_c(tradeable)
    frac = config.KELLY_FRACTION_CEILING
    # WO-2026-07-25-K §P1: FLIP starts at TUITION and re-earns FULL by conversion
    # (P3). The boot preview sizes at the tuition floor (a bare size_order reads
    # FLIP_NOTIONAL_PCT); print the earned full target + the ladder bars beside it.
    flip58_full = size_order(tradeable, 58, 10_000, lane="FLIP",
                             notional_pct=config.FLIP_FULL_NOTIONAL_PCT)
    return (f"SIZING: sizing-base ${book_cents / 100:.2f} (internal attribution, "
            f"not the account value — X4; the live account is the venue read on "
            f"the SCRAPE line) · owed ${owed_cents / 100:.2f} "
            f"· tradeable ${tradeable / 100:.2f} (WO-O: sizing off tradeable) · "
            f"Kelly fraction={frac:.4f} · budget/window {budget}¢ · "
            f"F @97¢ → {f97.contracts} lots (dial {config.F_NOTIONAL_PCT:.0%}, "
            f"wall {config.AT_RISK_PCT['F']:.0%}) · "
            f"FLIP @58¢ → {flip58.contracts} lots TUITION (dial "
            f"{config.FLIP_NOTIONAL_PCT:.0%}) / {flip58_full.contracts} lots FULL "
            f"(dial {config.FLIP_FULL_NOTIONAL_PCT:.0%}, wall "
            f"{config.AT_RISK_PCT['FLIP']:.0%}) — earned at trailing-"
            f"{config.FLIP_CONV_WINDOW} conversion ≥{config.FLIP_PROMOTE_CONV:.0%} "
            f"(demote <{config.FLIP_DEMOTE_CONV:.0%}) · no fixed cap — scales "
            f"with book · rate-halt bound −{halt}¢ (4 stop-outs at the tuition "
            "book) — throttle is book size + the at-risk wall, self-scaling")


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
    # WO-2026-07-26-P §A4 — the constant provenance tags. A DERIVED number with
    # no source FAILS LOUD; the constants that CHANGED this deploy print with
    # their tag so a size move is never silent.
    config.assert_constant_tags_sane()
    lines.extend(config.constant_tag_boot_lines())
    # WO-2026-07-26-P §A3 — the data-question registry. Boot asserts every
    # declared writer carries a complete question (WARN 24h, then FATAL).
    from . import registry as _registry
    lines.append(_registry.assert_writers_registered())
    # WO-2026-07-26-S §1 — THE ROOMS. One process, one book; every lane key is
    # (series, lane). Each room prints its mode and its F dial. (The ensemble cap
    # and per-series halts join this banner in Stage 2; XRP joins the roster in
    # Stage 3.)
    # WO-2026-07-27-W P3 — three rooms live by default. Print EVERY known room
    # with its mode + F dial (rostered rooms live; SOL[OFF] shows it is banked,
    # not forgotten), and the roster's SOURCE (persisted /series > env > default).
    def _room_cell(full, short):
        if full in config.SERIES:
            return f"{short}[{config.series_mode(full)}] F@{config.f_notional_pct_of(full):.0%}"
        return f"{short}[OFF]"
    _short = {v: k for k, v in config.KNOWN_SERIES.items()}
    _rooms = " · ".join(_room_cell(full, _short.get(full, full))
                        for full in config.KNOWN_SERIES.values())
    lines.append(f"SERIES ROOMS (WO-S; WO-W P3, source={config.ROSTER_SOURCE}): "
                 f"{_rooms} — three rooms LIVE by default (BTC XRP ETH), SOL "
                 "banked; one book, own dials/records per room; the global kill "
                 "governs all · /series overrides and persists")
    lines.append(
        f"ENSEMBLE (WO-S §2): cap {int(config.ENSEMBLE_AT_RISK_PCT * 100)}% of "
        "tradeable — total SIMULTANEOUS at-risk across ALL rooms, one summed "
        "check ABOVE the lane walls (defers with the why on the row); per-series "
        "halts key on (series, lane) so one room parks itself while the others "
        "print; the correlated tail (≥2 rooms lose one window) is counted once at "
        "combined size — the measured datum that makes the cap derivable")
    lines.append(
        f"RATE GOVERNOR: bucket={config.RATE_BUCKET_CAPACITY} tokens, "
        f"refill={config.RATE_REFILL_PER_SECOND}/s (printed number IS the enforced number)")
    # WO-2026-07-25-L §P1/P2 — the ruling, printed. Per-lane run mode (F LIVE,
    # everything else SHADOW until it earns back), and the single-loss bound the
    # F raise is accepted against (the CEO's stated ceiling).
    live = [ln for ln, m in config.LANE_MODE.items() if m == "LIVE"]
    shadow = [ln for ln, m in config.LANE_MODE.items() if m != "LIVE"]
    lines.append(
        "LANE MODE (WO-L): LIVE=" + (",".join(live) or "none")
        + " · SHADOW=" + (",".join(shadow) or "none")
        + (" · GLOBAL LIVE" if config.live_submit_enabled()
           else " · GLOBAL SHADOW (born state — nothing placed, LANE_MODE"
                " restricts below it)")
        + " — F gets the book; everything else rehearses (👻) and earns it back")
    # WO-2026-07-26-N §P4.2/P4.4/P4.5 — THE OVERNIGHT DOCTRINE, printed.
    lines.append(
        "SALVAGE: " + ("GAGGED (manual kill — telemetry-only, held to the bell)"
        if config.SALVAGE_GAGGED else
        f"LIVE (WO-M S1–S7: sighted {config.SALVAGE_CONFIRM_POLLS}-poll confirm "
        f"[deep+pinned+spot] · worth-floor {config.SALVAGE_WORTH_FLOOR_C}¢ · "
        f"middle-band · maker-first · rarity auto-gag > "
        f"{config.SALVAGE_RARITY_MAX_PER_DAY}/day · all cut paths folded; "
        "catastrophe survives for broker-truth only). The overnight gag ended."))
    lines.append(
        "MONEY: lifetime is RESTATED (WO-N P4.4) — rebuilt from the settlements "
        "ledger alone; the overnight double-booked cuts corrupted cell/window "
        "REPORTING only, never settlements. The daily pack carries the delta.")
    lines.append(
        "CASH-SENTINEL DOCTRINE (WO-N P4.5, banked): a CASH DELTA that fires "
        "within 30 min of ANY anomaly page → /deny_cash + investigate, never "
        "/confirm — a sentinel next to an alarm is EVIDENCE, and confirming it "
        "launders the error into the books. /confirm is for known deposits only.")
    lines.append(
        "DIALS: NO CHANGES this deploy (WO-N P4.6) — F stays "
        f"{config.F_NOTIONAL_PCT:.0%}/{config.AT_RISK_PCT['F']:.0%}; the next size "
        "conversation happens on a restated, trusted lifetime line.")
    if boot_caps is not None:
        # P8 §3 + P14 §3: 1/12-Kelly honestly stated, at BOTH reference prices.
        lines.append(sizing_line(boot_caps.book_cents))
        # WO-L §P2: THE SINGLE-LOSS BOUND — one full unsalvaged F loss ≈ dial ×
        # book. The stated, accepted ceiling of the F raise; further raises gate
        # on salvage shipping. Printed next to the worst-day math (CEO lens).
        slb_pct = config.f_single_loss_bound_pct()
        lines.append(
            f"SINGLE-LOSS BOUND (WO-L P2): one full unsalvaged F loss ≈ "
            f"{slb_pct:.0%} of book (dial × book at a ~97¢ favorite) = "
            f"−${boot_caps.book_cents * slb_pct / 100.0:.2f} — the accepted "
            f"ceiling; further F raises gate on salvage (≈40–50¢ salvaged losses "
            f"support dials past {config.AT_RISK_PCT['F']:.0%})")
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
                 f"trend-skip are RETIRED. One shot/window, maker-only; FLIP now "
                 f"self-scales by notional ({config.FLIP_NOTIONAL_PCT:.0%} of book) "
                 "with NO fixed cap — depth and the at-risk wall bind "
                 "(WO-2026-07-24-G)")
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
                 "across lanes; (b) DELETED (WO-Q) — the day-long F suppression "
                 f"is gone; a single F loss > {config.F_EVENT_TRIPWIRE_C}c/"
                 "contract now PAGES (F_BIG_LOSS) and never refuses F; the money "
                 "rate halt is F's one governor; (d) "
                 "kelly/depth/notional/at-risk terms + the binding one logged")
    lines.append(f"  SCOPE + SIZE THE HALT (WO-2026-07-24-C, build 72): Part 1 "
                 "RETIRES the global fallback — an aggregate loss can no longer "
                 "halt EVERY lane (F included) for losses F did not cause; the "
                 "halt is per-lane, always (HALT_KEY stays readable + /reset_halt "
                 "clears a legacy one, but nothing sets it forward). Part 2 the "
                 "per-lane halt counts MONEY not negative windows (−8,−7,+17 = "
                 "+2¢ must NOT halt), summed over "
                 f"{config.RATE_HALT_WINDOW_N} windows against a threshold "
                 f"DERIVED from size (4·FLIP_SIZE_CAP·OPEN_MOMENTUM_STOP_C = "
                 f"{config.RATE_HALT_DRAWDOWN_C}¢ now) so it scales with the "
                 "position and never strangles the lane it protects. F's guard "
                 "stays the per-event tripwire — F byte-identical")
    lines.append(f"  SIZE THE LANE + COLLECT THE DATA (WO-2026-07-24-C, build "
                 f"73): Part 3 the +4×10 test — OPEN_GOUGE_C {config.OPEN_GOUGE_C} "
                 f"(target = entry+{config.OPEN_GOUGE_C}), FLIP_SIZE_CAP "
                 f"{config.FLIP_SIZE_CAP}, FLIP at-risk wall "
                 f"{int(config.AT_RISK_PCT['FLIP'] * 100)}%; FLIP is now "
                 "LANE-AWARE in sizing — min(kelly, depth, FLIP_SIZE_CAP), the "
                 "retired NET_RISK count no longer silently re-caps it at 3 "
                 "(REVERT together to cap 3 + wall 5% if ≤4 of 10 fill). Part 4 "
                 "the DATA: a FLIP_SIZE log names the binding term (kelly/depth/"
                 "cap) on EVERY entry, and target_touched/target_filled ride "
                 "every FLIP_SWING row — did the middle reach entry+"
                 f"{config.OPEN_GOUGE_C} and did our resting take get the fill. "
                 "F byte-identical (the FLIP_SIZE block is lane-gated)")
    lines.append(f"  THE MORNING FOUR (WO-2026-07-24-D, build 74 build 1/2): four "
                 "sibling-path completions, no exposure change. Part 2 the "
                 "MOMENTUM STOP gets the flatten's price FLOOR — below "
                 f"stop−SLIP_TOLERANCE_C ({config.SLIP_TOLERANCE_C}c) it rests "
                 "one poll at the floor, then crosses at the mark with a COUNTED "
                 "FLIP_FLOOR_BREACH (the unfloored `mark` dump gave back half a "
                 "night). Part 3 ORIENTATION_DIVERGENCE auto-recovers on the "
                 "first LIVE market that reads clean (not the expired one it "
                 f"tripped on) with a {int(config.ORIENTATION_HALT_MAX_S)}s "
                 "ceiling → ORIENTATION_HALT_STUCK (was: 161min of dead time). "
                 "Part 4 the reconcile is INSTRUMENTED — recon_ok=<s> on the "
                 "hourly, RECON_STALLED after "
                 f"{config.RECON_STALL_STREAK} un-cross-checked cycles, and a "
                 "failed venue read INVALIDATES the pv (RECON_NO_PV) instead of "
                 "pinning the gate on a stale number. Part 5 the closed-gate "
                 "reject latches once per (lane, market) until the gate opens. "
                 "F byte-identical")
    lines.append(f"  SIZE THE LANE (WO-2026-07-24-D, build 75 build 2/2): Part 1 "
                 "— FLIP self-scales by NOTIONAL (FLIP_NOTIONAL_PCT "
                 f"{config.FLIP_NOTIONAL_PCT}) like F, NOT Kelly. Kelly capped "
                 "FLIP at ~5-6 on a $43 book (358c/60c), so FLIP_SIZE_CAP=10 "
                 "never bit and the +4×10 test ran at HALF the ruled size — the "
                 "third instance of a fix on one of two siblings (F had the "
                 "bypass, FLIP did not). Now min(notional, depth, cap): 4300·"
                 f"{config.FLIP_NOTIONAL_PCT}//60 = 10, so FLIP reaches the "
                 "cap; depth binds below on a thin book; the 15% at-risk wall "
                 "(10×64c=640c vs 645c) is the gateway backstop. Ships LAST so "
                 "the doubled size lands on build-74's floored stop + visible "
                 "reconcile. F byte-identical (its F_NOTIONAL_PCT path untouched)")
    lines.append(f"  THE SIGHTED STOP (WO-2026-07-24-E Phase 1, build 76): the "
                 "momentum stop is a pure LEVEL test (mark<=stop_px) with no "
                 "trajectory term — it fires whether the book is FALLING to the "
                 "stop or CLIMBING back through it (26JUL0845: sold at 48 into a "
                 "book +28c off its low). Phase 1 (weekday, INSTRUMENTATION only, "
                 "zero behavior change): every poll records low_mark/mark_prev "
                 "(BLIND/MUTE-safe — a None poll fabricates neither), and when "
                 "the live stop fires a SHADOW verdict "
                 f"(off_low>=OPEN_RECOVERY_MIN_C={config.OPEN_RECOVERY_MIN_C}c & "
                 "not falling & above the G1 hard floor stop−SLIP−"
                 f"{config.OPEN_GRACE_HARD_C}c & grace budget "
                 f"{config.OPEN_RECOVERY_MAX_POLLS}) stamps "
                 "would_defer/low_mark/mark_at_cut/off_low_c/grace_polls_shadow "
                 "onto the FLIP_SWING row. The live cut is byte-identical; "
                 "Phase 2 (Saturday, DREW's go) flips the deferral live. F "
                 "byte-identical")
    lines.append(f"  THE FLOODGATES ORDER (WO-2026-07-24-G, build 77): the dials "
                 "to scale with the book already existed; the WALL they pressed "
                 "against measured imaginary risk in the wrong scope. Part 1 the "
                 "at-risk wall is now LANE-SCOPED (FLIP's position no longer "
                 "counts against F's wall — the 40115-class squeeze that halved "
                 "F in shared windows) and a held leg prices at its BASIS not "
                 "99¢ (a 58¢ 6-lot is 348¢ at risk, not 594¢); an event's "
                 f"cross-lane total over {int(config.EVENT_TOTAL_AT_RISK_PCT*100)}"
                 "% of book PAGES (EVENT_TOTAL_AT_RISK). Walls up: F "
                 f"{config.AT_RISK_PCT['F']:.0%}, FLIP {config.AT_RISK_PCT['FLIP']:.0%} "
                 "(dials sit strictly under — boot FATALs on inversion). Part 2 "
                 "FLIP's fixed cap is RETIRED — min(notional, depth) scales with "
                 "the book (~21 lots at $90), and the rate-halt bound recomputes "
                 "with the book (rate_halt_drawdown_c: 4 stop-outs at CURRENT "
                 "size, printed here + hourly). Part 3 the SIGHTED STOP is LIVE "
                 "(OPEN_SIGHTED_STOP): a recovery DEFERS, every cut names its "
                 "trigger [2-poll|G1_HARD|G2_BUDGET|DECISION_SWEEP]. Part 4 the "
                 "boot line prints REAL per-lane sizes; cell stats are "
                 "PER-CONTRACT (era-invariant, protects Gate A). F byte-identical")
    lines.append("  ONE POSITION, ONE STORY (WO-2026-07-24-H, build 78): the "
                 "exchange pieces intents into fills; the engine now pieces them "
                 "back into POSITIONS before it speaks — P&L and 'concluded' "
                 "exist only at count→0. The ↔ line reads the position (gateway "
                 "basis+count), not the last entry fill: PARTIAL x7 (basis, "
                 "riding) while it rides, CLOSED with blended math when flat "
                 "(12:18: +28¢ partial / +68¢ closed x17, not a false '+28 "
                 "concluded' over 10 still riding). The cell outcome books ONCE "
                 "at conclusion with the blended basis + total count (the "
                 "per-exit-fill fiction that contaminated Gate A on pieced "
                 "entries is retired; the pack's scratch/FLIP-R6 read the same "
                 "position rows). A partial exit re-sizes the resting take (the "
                 "merge's cancel+re-propose, reused), and a standing per-cycle "
                 "assert pages EXIT_OVERSIZE if any resting EXIT ever exceeds "
                 "its position — the unguarded twin of the self-net entry wall. "
                 "F byte-identical")
    lines.append("  BLIND IS NOT BARE (WO-2026-07-24-I, build 79): a destructive "
                 "close requires POSITIVE knowledge of bareness from broker "
                 "truth — blindness PAUSES, it never fires. 1345 flattened a "
                 "DOUBLE-covered leg (held 17, resting 34) because two "
                 "instruments read the same position as 0 (in-memory buckets) "
                 "and 34 (gateway registry) and the flatten fired on the zero. "
                 "P1: ONE coverage authority — the broker-truth resting REGISTRY "
                 "(the read EXIT_OVERSIZE uses), buckets a fallback only where "
                 "the registry lacks the order. P2: surplus (covered>held) → "
                 "cancel the duplicate, no flatten/fee; partial (0<cov<held) → "
                 "MAKER top-up at the take, never a market cross; flatten only "
                 "on positively-bare (covered==0); unreadable → HEAL_BLIND, "
                 "hold. P4 (the x34 ROOT): the merge respects cancel_tristate — "
                 "an UNKNOWN (in-flight) cancel keeps the old take resting and "
                 "does NOT repost (no double-cover); only a CONFIRMED cancel "
                 "reposts. One take identity per record. F byte-identical")
    lines.append("  POINT THE HUNTER FORWARD (WO-2026-07-24-J, build 80): the "
                 "banked Divergence Engine goes LIVE as HUNT's first scope. The "
                 "hunt no longer buys the TOUCH lag — it buys the SETTLE edge. "
                 "P1: a SECOND surface from the SAME 180-day corpus — p_end(d,t), "
                 "P the window CLOSES beyond d (the question the market prices), "
                 "with per-cell {p_end, n, wilson_lb}; the physics p_end<=p_cross "
                 "(a close beyond d must have touched d) proves the two answer "
                 "DIFFERENT questions. Absent on a legacy tape → BLIND. P2: the "
                 "FORWARD gate — edge = wilson_LB(p_end)*100 − join >= "
                 f"HUNT_EDGE_MIN_C ({config.HUNT_EDGE_MIN_C:.0f}c, fee-floored); "
                 "the needle DEMOTES to ATTENTION (gate A: when to look, never "
                 "whether to buy); every entry carries the EV tag. P3: the exit "
                 "WATCHES THE THESIS — bail when the settle edge is gone "
                 f"(settle_LB<=cost sustained {config.HUNT_EDGE_GONE_POLLS} "
                 "polls), logging BOTH numbers; the two-tick level bail RETIRES. "
                 "P4: the casefile names every probability's question (settle / "
                 "touch), carries $ distances, calls the gate quantity EDGE, "
                 "marks the info-only touch — 'fair' is banned. P5: the daily "
                 "DIVERGENCE read-back — realized settle vs p_end vs book by "
                 "edge; HUNT stays PROBE until a bucket's Wilson LB clears the "
                 "book. Touch surface untouched (labeled info); F byte-identical")
    lines.append("  THE DESK EARNS ITS SIZE (WO-2026-07-25-K, build 81): the +4 "
                 "OPEN desk was 6-of-11 (55% conversion) against a ~73% "
                 "breakeven and no recorded feature separated its winners from "
                 "its losers at n=11 — the gates measured the crowd (really "
                 "there, piling onto a coin on its edge), not the fragility of "
                 "the open. P1 TUITION SIZE: demote instantly — FLIP dial "
                 f"{config.FLIP_NOTIONAL_PCT:.0%} (was 14%), ~5-6 lots; full "
                 "armor kept, cells still bought, at ~−35¢/day worst instead of "
                 "−$12 days. The rate-halt re-derives from the tuition dial "
                 "(verified). F untouched. P2 THE CONFIDENCE INSTRUMENT: the -J "
                 "settle table gains its desk duty — at OPEN entry the favored "
                 "side's SETTLE-fair (1−p_end/2, the reflection of the "
                 "directionless settle mass) must beat the join by "
                 f"FLIP_CONF_MIN_C ({config.FLIP_CONF_MIN_C:.0f}c); the four "
                 "Saturday losses (spot pinned to strike → settle-fair ≈ 50) "
                 "are REFUSED. BLIND on a legacy tape → tuition bounds it. P3 "
                 "PROMOTION BY CONVERSION: the size ladder is mechanical — "
                 f"trailing-{config.FLIP_CONV_WINDOW} conversion "
                 f"≥{config.FLIP_PROMOTE_CONV:.0%} re-earns full "
                 f"({config.FLIP_FULL_NOTIONAL_PCT:.0%}), "
                 f"<{config.FLIP_DEMOTE_CONV:.0%} demotes instantly (promote "
                 "slowly, demote instantly, no ruling); the entry cell's "
                 "negative Wilson margin blocks a lucky streak from up-sizing a "
                 "losing cell. The tape moves the dial. F byte-identical")
    lines.append("  F GETS THE BOOK; EVERYTHING ELSE EARNS IT (WO-2026-07-25-L, "
                 "build 82): the outside review, made law — one lane earned the "
                 "book, so it gets the book; everything else keeps every rep at "
                 "full speed, with real signals and honest referees, for no "
                 "money at all. P1 PER-LANE RUN MODE: LANE_MODE branches "
                 "gateway.submit — F LIVE, the rest (FLIP/OPEN/HUNT/PAIR/D/P/H8) "
                 "SHADOW even in a live run (SHADOW- oids, full custody sim, cell "
                 "outcomes tagged shadow, ZERO broker traffic, 👻 on every line); "
                 "the global kill still rules, per-lane only restricts below it. "
                 "The PESSIMISTIC fill model (Scientist owns it): a maker fills "
                 "only when the book trades AT/THROUGH its price after rest — no "
                 "fantasy fills buy a fake promotion. P1b TWO LEDGERS: treasury "
                 "(book/lifetime = settlements+cash; deployed = live fills) NEVER "
                 "reads cell_outcomes, and a shadow-lane fill is FATAL-refused — "
                 "simulated P&L can't reach real capital (asserted at boot). P2 F "
                 f"MAXIMUM: dial {config.F_NOTIONAL_PCT:.0%} (80% of the "
                 f"{config.AT_RISK_PCT['F']:.0%} wall); the single-loss bound "
                 f"(≈{config.f_single_loss_bound_pct():.0%} of book on one "
                 "unsalvaged F loss) is the stated ceiling — further raises gate "
                 "on salvage shipping. P3 EARN-BACK: shadow first, golden tape, "
                 "named revert, Drew's sign-off — the pack prints each lane's "
                 "distance to promotion daily (the door, marked with numbers). "
                 "P4 THIN: a cell with <10 realized outcomes holds NO gate "
                 "authority (greyed with its n); it leaves THIN only by realized "
                 "n, never modeled numbers. F byte-identical in logic (size "
                 "params only)")
    lines.append("  THE OVERNIGHT DOCTRINE (WO-2026-07-26-N, build 83): the "
                 "07/25→26 run, read cold — the newest idea (untuned salvage) "
                 "fired TWICE without confirmation and sold two winners at the "
                 "bottom (−$14.30 for $0 dodged); the bookkeeper wrote each cut "
                 "down twice (custodian direct-write + fills-poller); and every "
                 "alarm rang true (sentinel, oversize assert, F rate halt). Both "
                 "root causes were already fixed in build 6 (baton lifecycle "
                 "custodian.execute_cut: tri-state cancel, ledger re-derive, cut "
                 "only proven holdings, FLAT_RACE skip, UNKNOWN=FATAL; unified "
                 "fill dedup fills.py: fill_id primary-keyed, no path privileged). "
                 "The one deploy: P4.1 enforce the ruled shadow modes (FLIP/OPEN/"
                 "HUNT/H8 SHADOW, F LIVE); P4.2 GAG salvage to telemetry-only "
                 "until it re-earns its cut (-M re-arm: confirms-symmetric, "
                 "worth-floor, maker-first, rarity); P4.3 replay both Saturday "
                 "salvages as NO-FIRE + the duplicate-cut/duplicate-fill "
                 "scenarios book ONCE; P4.4 RESTATE lifetime from settlements "
                 "alone; P4.5 the cash-sentinel doctrine (deny+investigate next "
                 "to an alarm, never confirm); P4.6 NO dial changes. FLIP stays "
                 "in shadow (55% vs 73%, lifetime ≈ −$26); the door back is -L "
                 "P3, printed daily. F byte-identical (mode/env/restatement only)")
    lines.append("  SALVAGE EARNS ITS CUT + THE SCRAPE (WO-2026-07-26-M+O, build "
                 "84, one deploy): two promises kept on the restated meter. M — "
                 "salvage goes LIVE as the SIGHTED discipline: a cut fires only "
                 f"after {config.SALVAGE_CONFIRM_POLLS} confirmed polls "
                 "(deep-against + pinned-at-lows via low_mark + spot-confirm), "
                 f"above the {config.SALVAGE_WORTH_FLOOR_C}¢ worth-floor, inside "
                 "the middle band, MAKER-FIRST, rarity auto-gagging over "
                 f"{config.SALVAGE_RARITY_MAX_PER_DAY}/day; ALL cut paths "
                 "(SLIP+K-collapse+CATASTROPHIC) folded under it, catastrophe "
                 "surviving only for broker-truth emergencies; the overnight gag "
                 "ended and F_EVENT_TRIPWIRE's day-long suppression retired (the "
                 "money rate halt governs the run, not a single loss). O — THE "
                 f"SCRAPE: ${config.SCRAPE_PER_MILESTONE_C / 100:.0f} owed per "
                 f"${config.SCRAPE_MILESTONE_C / 100:.0f} of new high-water "
                 "trading equity, seeded at the RESTATED equity; tradeable = "
                 "book − owed substituted at EVERY sizing base (F notional, "
                 "wall, worst-day, rate-halt); deposits never mint, losses never "
                 "un-owe, withdrawals decrement; 💰 milestone + owed on hourly/"
                 "boot/daily; /owed; OWED_UNDERWATER halts if tradeable < one F "
                 "lot. No salvage event can ever increment owed. F entry/hold "
                 "logic byte-identical (exits, accounting, capital arithmetic only)")
    lines.append("  THE WHY BAKE & THE ONE-LOT TRACE (WO-2026-07-26-P, build 85, "
                 "one deploy): the one-lot bug traced to its root and the Why Law "
                 "baked into code. B — the size chokepoint no longer fabricates: "
                 "the book answers TWO questions (joining_depth: the level an "
                 "order joins · band_depth: the wall it trades in front of), a "
                 f"fresh level sizes to max(joining, band×{config.BAND_DEPTH_FRACTION:.0%}) "
                 f"in a ±{config.SIZING_BAND_HALFWIDTH_C}¢ band with BOTH terms + "
                 "the binder printed on the size row; a blind book DEFERS "
                 "(DEPTH_BLIND) and a zero size DEFERS (SIZE_ZERO_DEFER, count=0), "
                 "never a 1 — the two silent fallbacks (`depth or 0`, "
                 "`max(1,contracts)`) are gone and a lint FAILS the build on a "
                 "new one. A — every surface row now REFUSES to write without a "
                 "why (§A1, the shared chokepoint); the data-question registry "
                 "(§A3) makes every surface declare the question it answers, its "
                 "consumer, and its last-read (unread>14d pages DATA_WITHOUT_"
                 "QUESTION); and every capital constant is tagged in-source RULED/"
                 "DERIVED/DREW-DEFAULT (§A4) — a DERIVED with no source fails "
                 "loud. F entry/hold/accounting byte-identical (sizing chokepoint "
                 "+ evidence law only)")
    lines.append("  DELETE GUARD (B): THE LAST SILENT GOVERNOR (WO-2026-07-26-Q, "
                 "build 86): guard (b) — F's day-long per-event suppression — is "
                 "DELETED, not modified. It duplicated the money-based rate halt "
                 "with a cruder rule (one event, calendar-scoped, self-clearing at "
                 "midnight, no resume lever) and was the last governor nobody could "
                 "name in the audit until it fired (today's 1:13 loss refused every "
                 "F window for ~5h). One risk, one governor: the rate halt stays; "
                 "the duplicate dies. Gone: the producer (_trip_f_event), the "
                 "consumer (_f_suppressed_today + the F_SUPPRESSED entry gate), and "
                 "the ledger state (set_f_tripwire/f_suppressed); a live "
                 "f_tripwire_day flag is CLEARED on boot so this deploy resumes F. "
                 f"F_EVENT_TRIPWIRE_C ({config.F_EVENT_TRIPWIRE_C}¢) survives ONLY "
                 "as the F_BIG_LOSS page threshold (information, RULED 2026-07-26). "
                 "F selection/sizing/walls/salvage/scrape byte-identical — this "
                 "order deletes, it does not tune")
    lines.append("  THE WATCH ASKS ITS OWN QUESTION (WO-2026-07-26-R, build 87): "
                 "the post-entry orientation watch halted on ANY >3¢ offset "
                 "between our orderbook read and a fresh market-record read — two "
                 "endpoints on different clocks, so a quiet-book endpoint lag wore "
                 "an orientation alarm (Sunday's two ORIENTATION_DIVERGENCE halts, "
                 "y30-vs-y26, freshness noise). It now halts ONLY on what means our "
                 "read can't be trusted: an INVERSION (the mirror signature the "
                 "tree already owns, _mirror_signature) OR a GROSS non-mirror gap "
                 f"≥{config.ORIENTATION_GROSS_DIVERGENCE_C}¢, each x3. A small sub-"
                 f"gross offset (>{config.BOOK_STALE_OFFSET_C}¢) demotes to "
                 "BOOK_STALE — info + a feed resync request, both values logged, "
                 "counted by hour in the pack (the registry question that makes the "
                 "tolerance derivable) — NEVER a halt. Auto-recovery, the STUCK "
                 "page, and /reset_halt unchanged; both thresholds tagged (WO-P); "
                 "the inversion protection is untouched — this narrows the alarm "
                 "to its disease. F byte-identical")
    lines.append("  THE SECOND ROOM (WO-2026-07-26-S, build 88): F is expanded to "
                 "MORE SERIES, not more features — coverage is an ensemble "
                 "property, caution is a lane property. One repo, one process, one "
                 "book: every lane key is (series, lane), F-BTC and F-XRP siblings "
                 "sharing ALL doctrine and NONE of their records. Per room: mode "
                 "(SERIES_MODE, /series <asset> on|off|live|shadow — the start "
                 "command, no second deploy), F dial (BTC 24% earned, a new room "
                 f"born at {config.NEW_SERIES_F_DIAL:.0%} RULED until its own record "
                 "argues), cell history, scoreboard chapter. Shared: the ENSEMBLE "
                 f"CAP ({int(config.ENSEMBLE_AT_RISK_PCT*100)}% of tradeable, one "
                 "summed check above the lane walls, defers with the why — the "
                 "correlated-tail governor built FRESH, the crypto era never had "
                 "one), per-(series,lane) halts (one room parks itself while the "
                 "others print), the correlated-loss rule (≥2 rooms lose one window "
                 "→ counted once at combined size, the measured datum that derives "
                 "the cap), salvage/scrape/sentinels (one hwm, one owed — the BOOK "
                 "is the unit of stewardship). XRP opens first (KXXRP15M, maker-only, "
                 "1¢ tick, $0 fee OBSERVED not assumed), then SOL, ETH last. BTC's F "
                 "path byte-identical through the refactor (golden tape); the roster "
                 "opened BTC-only, then XRP, and is now three rooms LIVE by default "
                 "(WO-W P3)")
    lines.append("  THE TWO GUARDS (WO-2026-07-26-T, build 89): the two protections "
                 "standing where the ghost phase stood, landed before KXXRP15M's "
                 "first live window. GUARD 1 — the counterparty-liquidity gate into "
                 "F's entry path: never rest a maker buy into a one-sided book (no "
                 "fill, or worse no EXIT — salvage's maker-first rest has nobody to "
                 "rest against and the bound widens from salvageable to total). An "
                 "empty opposite side → NO_COUNTERPARTY, re-eligible next poll; "
                 "existence not a threshold (nothing to tune), all series (free — "
                 "BTC's deep book never triggers it), counted by series/hour as the "
                 "room's liquidity map. GUARD 2 — the table speaks its series or "
                 "says BLIND: every delta consultation carries the market's series; "
                 "the BTC-trained table (Coinbase BTC-USD, 180d) answers ONLY BTC "
                 "and returns None for any other room, so an XRP card prints 'surv "
                 "n/a (no KXXRP15M table)' instead of borrowed BTC physics (Why Law "
                 "Article 1). Sibling sweep: every delta consumer series-passed or "
                 "None-safe. BTC's F path identical (both guards are no-ops on BTC); "
                 "XRP flips LIVE only in the deploy that carries both guards green")
    lines.append("  THE SLEEPING SENTINEL (WO-2026-07-27-V, build 90): a ~$45 "
                 "withdrawal walked past every guard descended from the engine's "
                 "oldest law. ROOT CAUSE (T1): the cash reconcile defers whenever a "
                 "room is busy (resting/unsettled), and a quiescence-defer was "
                 "treated as BENIGN — so a two-room engine that never goes quiet "
                 "silently never reconciled, the book stayed a $97 phantom while "
                 "the venue said $20.70, and BTC's F bounced insufficient-funds "
                 "for hours with no page. FIX: quiescence starvation is not benign "
                 f"— a book unverified against the venue for RECON_MAX_QUIET_S "
                 f"({config.RECON_MAX_QUIET_S}s), for ANY reason, PAGES RECON_"
                 "STARVED; the sentinel's pulse (clean reconciles/hour) rides the "
                 "pack. Two belts, both truth channels the venue offered all "
                 "evening: B1 — an insufficient-funds REJECTION pages BALANCE_"
                 "REJECTED once/window (a lane dying silently is its own alarm; "
                 "keyed on the venue's explicit funds language, never a 500); B2 — "
                 "before submit, an entry's cost over the last CONFIRMED venue CASH "
                 "(not the ledger's belief, age printed) DEFERS CASH_SANITY, and a "
                 f"confirmation older than {config.CASH_CONFIRM_MAX_AGE_S}s defers "
                 "everything + pages (blind is not solvent). Nothing ever sizes off "
                 "money that isn't there. F/XRP/salvage/scrape byte-identical (all "
                 "three LIVE-gated, no-op on a healthy book)")
    lines.append("  THE 24-HOUR RULINGS (WO-2026-07-27-W, build 91): P1 — "
                 "BROKER CASH IS THE ONLY SIZING TRUTH. Every sizing site, wall, "
                 "worst-day and halt reads ONE base — `tradeable_cents` = live "
                 "venue CASH − owed (LIVE), which mechanically SHRINKS as rooms "
                 "deploy; the book demotes to reporting/P&L and the portfolio "
                 "balance (an ~constant estimate that double-counts deployment) "
                 "is read NOWHERE in sizing. Pre-first-read it falls back to book "
                 "so boot has a base; SHADOW stays the paper book (byte-identical). "
                 "P4 — THE CASH RACE: because deployed cash leaves the balance, a "
                 "later proposal in the same window races for what remains — "
                 "self-limiting by arithmetic, no lock needed. The ensemble "
                 f"ceiling reads cash + at-risk (`ensemble_base_cents`, total "
                 f"capital) so its {int(config.ENSEMBLE_AT_RISK_PCT*100)}% cap "
                 "bounds the correlated tail across rooms without double-counting")
    lines.append("  THE SPENT LOSS (WO-2026-07-27-W, build 91): P2 — F's loss is "
                 "ONE large tail per ~25 wins, so its rate-halt bound now speaks "
                 f"F's OWN units: F_HALT_TAIL_MULT ({config.F_HALT_TAIL_MULT}) × "
                 "one full F loss at the dial (≈ F_NOTIONAL_PCT × tradeable). A "
                 "SINGLE ordinary tail never halts F — it pages F_BIG_LOSS; the "
                 "halt fires on a CLUSTER (a second tail inside the trailing "
                 "window). The desk lanes keep their stop-derived "
                 "rate_halt_drawdown_c; each lane's bound speaks its own loss "
                 "language (lane_halt_bound_c). W2b — when ANY halt clears the "
                 "triggering losses are marked SPENT: the trailing window restarts "
                 "clean and the clear row records what was spent, so the same loss "
                 "can never convict twice (before this a tail poisoned the "
                 "trailing-8 sum for hours and re-halted on a loss already "
                 "answered for). Tail-cluster frequency/room is a registry "
                 "question that earns F_HALT_TAIL_MULT a derived number")
    lines.append("  THREE ROOMS, LIVE BY DEFAULT (WO-2026-07-27-W, build 91): P3 "
                 "— the default roster ships in code (BTC XRP ETH all LIVE, SOL "
                 "banked OFF); no /series, no env, no Telegram confirmation opens "
                 "the three rooms. ETH is unlocked tonight and enters exactly as "
                 f"XRP did — born at {config.NEW_SERIES_F_DIAL:.0%} F dial, its own "
                 "series-scoped halt (W2a geometry), counterparty gate live, and "
                 "surv n/a (the BTC table won't speak for it). The roster PERSISTS "
                 "(U1): a Drew /series change is saved to the ledger and restored "
                 "over the code default at the next boot, so the rooms Drew "
                 "opened/parked survive a restart. The banner prints EVERY known "
                 "room with its mode + dial and the roster's source (persisted > "
                 "env > default); the SERIES_LIST env stays the emergency lever, "
                 "/series the override")
    lines.append("  THE VENUE SPEAKS LAST (WO-2026-07-28-X, build 92): X4 — the "
                 "account-truth spine, separated into two jobs so neither number "
                 "competes. ACCOUNT TRUTH (what is the account worth) = VENUE READS "
                 "ONLY: every headline — hourly, settle lines, /owed, pack MONEY, "
                 "the SCRAPE line — prints the venue's own account VALUE (cash + "
                 "portfolio value, the Kalshi-app number), age-stamped, fetched by "
                 "account_value (`last_venue_value_cents`) not derived, via "
                 "`ledger.account_value_display` / `ops.account_headline`. "
                 "`book_cents` is DEMOTED to internal ATTRIBUTION (who earned it — "
                 "lifetime, cell stats, the book-composition arithmetic) and never "
                 "prints as an account value again — Drew's law ('broker data is "
                 "truth 100%') completed at the last surface it hadn't reached. "
                 "SHADOW papers the money; LIVE before the first read falls back to "
                 "book, then the venue value with its age. The phone's number now "
                 "equals the app's, to the cent")
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
    lines.append("  COLD READ (build 67): window-econ brackets and the reported "
                 "book now come from ledger.book_cents() (cash_movements + "
                 "settlements — internally consistent), NOT the venue cash+pv "
                 "read. That read desyncs at ENTRY (cash debited, position not "
                 "yet reflected → LOW) and SETTLEMENT (cash credited, position "
                 "not yet cleared → HIGH); differencing OPEN vs CLOSE put the "
                 "entry notional into window_pnl twice (the +819c-vs-+24c "
                 "phantom, and 'book $X' was that read mislabeled). The ledger "
                 "was never wrong — settlements/book_cents/quarantine were all "
                 "clean. The venue read is kept for standing_reconcile ONLY (P9 "
                 "§3, away from fills/settlements); 'ledger' is a valid live "
                 "bracket source, only 'paper' stays forbidden. SIZING already "
                 "read ledger.book_cents() — the phantom never fed F's size")
    lines.append(f"  COLD AUDIT §2+§3 (build 70): the SOURCE fix for the cash "
                 "family — the venue reads cash and pv on different clocks, so a "
                 "read across a settlement boundary is wrong by the position "
                 "notional (819c/99c/196c/198c — always the position). The cash "
                 "reconcile now DEFERS unless the venue pv agrees with the "
                 f"engine's own deployed_cents within {config.PV_TOLERANCE_C}c — "
                 "it refuses to compute on an inconsistent read instead of "
                 "patching where the bad number lands (4 builds did that). §3: "
                 "the /scoreboard DISPLAY shows breakeven_HONEST (the pack proved "
                 "the stale model wrong on 26/29 cells); score()/tier stay stale "
                 "because tier feeds custody cut-scaling — trading byte-identical")
    lines.append("  TRUNCATION FIX (WO-2026-07-23-F Part 1, build 71): the venue "
                 "ticks in 0.1c and 42% of fills carry a fraction; to_yes_terms "
                 "used int(), which truncated the cost basis toward zero — "
                 "understating cost, OVERSTATING profit, always the same "
                 "direction. It now carries the fraction (float), record_"
                 "settlement stores it at full precision, and book_cents rounds "
                 "ONCE at the sum (its existing rule). Accounting only — no "
                 "trading change; F byte-identical. Expect F's lifetime P&L to "
                 "DROP ~100c as the correction lands (that IS the fix, not a "
                 "regression). Orderbook LEVEL bucketing (book.py:63) is LEFT "
                 "truncating — a stated depth heuristic, not a settlement number")
    lines.append("EXECUTION E1: P&L books from CONFIRMED fills + exchange "
                 "outcome only (window-econ is a check that self-heals to "
                 "fills-truth, never a source); every settlement writes a "
                 "SETTLE_AUDIT provenance row (per-fill contributions, "
                 "outcome, book-after, unmatched-leg flag) and an unexplained "
                 f"book-vs-venue gap >{config.RECON_AUDIT_FLOOR_CENTS}¢ with 0 "
                 "pending is recorded — the phantom names its source "
                 "(WO-INFRA-HARDENING)")
    lines.append("  FOUR BUGS COLD READ (WO-2026-07-23-C, build 68): Bug 2 (the "
                 "root) — EXIT sells now get a SELL-SIDE rest-back mirror: a "
                 "maker exit that would post at/through the bid is lifted to rest "
                 "above it, so it is never refused `post only cross` (the reject "
                 "that failed the cover and summoned the flatten that dumped "
                 "below the stop). Lane F EXCLUDED (byte-identical — its salvage "
                 "keeps maker→crossfire-after-R). Bug 1b — the FLIP entry wall "
                 "now counts LIVE RESTING ORDERS, not just filled positions, so "
                 "OPEN can't stack a second entry over its own resting maker "
                 "(the 3-lots-at-one-touch). BANKED LAW: never gate an action on "
                 "FILLED state when the action can precede the fill — gate on "
                 "INTENT (submitted), clear on fill/cancel/reject (state.traded/"
                 "w.posted/now the order-aware wall)")
    lines.append(f"  THE SIZE TEST (WO-2026-07-23-E, build 69): two dials, no "
                 f"logic change — FLIP_SIZE_CAP 1→{config.FLIP_SIZE_CAP} (DREW: "
                 "explicit 3, not 4 — NET_RISK=3 and the 5% at-risk wall both "
                 f"bind there, so 3 is deterministic; no walls widened for a "
                 f"test) and OPEN_GOUGE_C 17→{config.OPEN_GOUGE_C} (take = entry+"
                 f"{config.OPEN_GOUGE_C}). Does SIZE TRAVEL? Read requested_count "
                 "vs count on FLIP EXITs; decision rule /3 (≥2.6 confirms, ≤1.1 "
                 "reverts). Filter windows that ran <3 lots (the 5% wall binds "
                 "below ~$36 book). F byte-identical — FLIP-only")
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
