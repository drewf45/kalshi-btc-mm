"""Boot tape — printed IN FULL on every boot (gate 2).

Prints: EPOCH 2 header, the ratification assumptions (A3 — printed every boot
until Drew rules), the DREW-DEFAULT constants, the run mode, the rate governor's
enforced numbers (the printed number IS the enforced number), and recorder
confirmation.
"""

from typing import List

from . import config


def sizing_line(book_cents: int) -> str:
    """P14 §3: the BUDGET is the invariant; lots depend on price — say both.
    (The 7:35 confusion: 'max lots 0' printed at a 99¢ reference while the
    engine correctly traded 1 lot at 39¢ — both were true.)"""
    from .sizing import size_order
    budget = int(book_cents * config.KELLY_FRACTION_CEILING)
    l39 = size_order(config.TIER_PROBE, book_cents, 39, 10_000).contracts
    l99 = size_order(config.TIER_PROBE, book_cents, 99, 10_000).contracts
    return (f"SIZING: 1/12-Kelly · book ${book_cents / 100:.2f} · "
            f"budget/window {budget}¢ · max lots: {l39} @39¢ · {l99} @99¢")


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
                         f"two-strike is the only engine stop")
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
    lines.append("  FLIP: pair-formable-or-nothing · take entry+4 · scratch entry-3 "
                 "· sit-out@3 — margin UNPROVEN, mechanism proven (R-1)")
    lines.append("  F: hold-to-settlement · depth floor stands in thin books "
                 "[STOP-AND-REPORT: passthrough CutParams unregistered — "
                 "catastrophic backstop unreachable, awaiting Drew's ruling]")
    lines.append("  H8: delta-gated >=99% survive · hold to settlement")
    lines.append("  D: cheap entry + resting recovery take (the baton)")
    lines.append("  P: displacement fade with take (negative-spec born)")
    lines.append("  ORPHAN: adopted at boot · D-grade custody · own attribution")
    lines.append("  WALLS: gross+net risk<=3/event · $-at-risk cap · two-strike "
                 "halt (/reset_halt) · orientation sentinels · narrated fills · "
                 "graded tape")
    lines.append("==================================================================")
    return lines


def print_boot_tape(recorder=None, boot_caps=None, auth_line=None) -> List[str]:
    lines = boot_tape(recorder=recorder, boot_caps=boot_caps, auth_line=auth_line)
    for line in lines:
        print(line, flush=True)
    return lines
