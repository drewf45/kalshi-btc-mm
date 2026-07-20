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
