"""k_worker/flip_mode.py — WO-LANE-FLIP: the flip doctrine as a MODE in the proven engine.

On this branch the engine flips BY DEFAULT (F-2: KW_MODE unset ⇒ FLIP); KW_MODE=OFF is
the one-variable kill switch (engine up, directional lanes dormant, flip quiet). Reuses
the proven organs verbatim:
place_order_maker (the four flip intents via flip_math), fetch_orderbook, get_fills/
parse_fill, cancel_all_for_market, discipline halt, notify, and store — so fills flow
into the SAME reconcile / settlement / treasury machinery the engine already runs.
This module adds NO taker path, NO new client, NO new parser (WO constraints).

Doctrine: arm at the open, quote both sides at the join (subpenny), flip at entry+X,
sweep unfilled exits at T-90; an intact bundle rides its guaranteed floor, a lone leg
(≤49¢) rides settlement as the accepted 1-lot residual. One entry phase per window.
"""

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from . import kalshi, gateway, notify, store, discipline, engine
from . import flip_math

NY = ZoneInfo("America/New_York")

KW_MODE = os.environ.get("KW_MODE", "FLIP").strip().upper()   # F-2: FLIP is the default
ENABLED = flip_math.mode_enabled(os.environ.get("KW_MODE"))   # KW_MODE=OFF is the kill switch

FLIP_WINDOW_SEC = int(os.environ.get("FLIP_WINDOW_SEC", "900"))
FLIP_ENTRY_SEC = int(os.environ.get("FLIP_ENTRY_SEC", "120"))
FLIP_LINE = int(os.environ.get("FLIP_LINE", "99"))
FLIP_LONE_MAX = int(os.environ.get("FLIP_LONE_MAX", "49"))
FLIP_X = int(os.environ.get("FLIP_X", "4"))
FLIP_FLAT_AT = int(os.environ.get("FLIP_FLAT_AT", "90"))
FLIP_STOP_CENTS = int(os.environ.get("FLIP_STOP_CENTS", "25"))
FLIP_PAUSE_AFTER_STOPS = int(os.environ.get("FLIP_PAUSE_AFTER_STOPS", "2"))
FLIP_POLL_SEC = float(os.environ.get("FLIP_POLL_SEC", "3"))

_STOP_STREAK_KEY = "flip_stop_streak"


def echo_config() -> None:
    notify.send(
        "🔁 <b>FLIP MODE</b> — arm at open, quote both @join, flip @entry+"
        f"{FLIP_X}c\n"
        f"line {FLIP_LINE}c · lone_max {FLIP_LONE_MAX}c · entry {FLIP_ENTRY_SEC}s · "
        f"flat T-{FLIP_FLAT_AT} · stop {FLIP_STOP_CENTS}c · pause@{FLIP_PAUSE_AFTER_STOPS}"
    )


def _tag(close_ts: int) -> str:
    return datetime.fromtimestamp(int(close_ts), tz=timezone.utc).astimezone(NY).strftime("%H:%M")


def _is_observe() -> bool:
    return bool(getattr(engine, "_observe_mode", False))


# ── fill detection: scan the ticker's fills for OUR order_id ──────────────────
def _order_fill_price(client, ticker: str, order_id: Optional[str], our_side: str) -> Optional[int]:
    if not order_id:
        return None
    try:
        fills = kalshi.get_fills(client, ticker)
    except Exception as e:
        notify.send(f"⚠ flip fills poll error {ticker}: {e}")
        return None
    mine = [f for f in fills if str(f.get("order_id") or "") == str(order_id)]
    if not mine:
        return None
    price, _fee, _cnt = kalshi.parse_fills(mine, our_side)
    return int(round(price)) if price is not None else None


def _record_enter(ticker: str, close_ts: int, side: str,
                  fill_cents: Optional[int] = None, order_id: Optional[str] = None,
                  why_tag: str = "FLIP_ENTER") -> None:
    """Book a filled leg (entry OR rung-B exit) as the engine's own ENTER row so the
    EXISTING reconcile/settlement/treasury machinery resolves it exactly as it does the
    directional lanes — env MUST be 'live-traded' (what the backfill queries). A leg with
    no confirmed fill is recorded unfilled WITH its order_id so settlement backfill can
    verify-or-nofill it against the broker (no new accounting)."""
    filled = fill_cents is not None
    try:
        store.insert_row(store.SurfaceRow(
            market_ticker=ticker, decision_ts=time.time(), action="ENTER", side=side,
            cost_per_contract_cents=int(fill_cents) if filled else None,
            fill_cost_cents=int(fill_cents) if filled else None,
            fill_ts=time.time() if filled else None, contracts=1,
            order_type="maker", why_tag=why_tag, lane="F", env="live-traded",
            order_id=order_id, seconds_to_expiry=close_ts - time.time(),
        ))
    except Exception as e:
        notify.send(f"⚠ flip enter-row failed {ticker} {side}: {e}")


def _bump_stop_streak(stopped: bool) -> None:
    """Two consecutive stopped windows → pause (reuse the engine's halt state)."""
    streak = int(store.get_state(_STOP_STREAK_KEY) or "0")
    streak = streak + 1 if stopped else 0
    store.set_state(_STOP_STREAK_KEY, str(streak))
    if stopped and streak >= FLIP_PAUSE_AFTER_STOPS:
        store.set_state("halted", f"flip: {streak} consecutive stopped windows")
        notify.alert(f"🛑 FLIP PAUSE — {streak} consecutive stopped windows. "
                     f"Run `python -m k_worker.reset` to resume.")


def _entry_spreads(book):
    """(spread_yes, spread_no) in cents at entry, or None where a side's ask is missing."""
    def sp(bid, ask):
        return int(ask - bid) if (bid is not None and ask is not None) else None
    return (sp(book.yes_bid, getattr(book, "yes_ask", None)),
            sp(book.no_bid, getattr(book, "no_ask", None)))


def _inventory(client, ticker: str, close_ts: int, tag: str, net: int, ctx: str) -> None:
    """§1 surprise residual: page Drew BY NAME, hand the position to the EXISTING boot
    reconcile/orphan machinery, and attempt an immediate complement-join to flatten NOW
    (maker touch, exp close−2). No taker is added."""
    notify.alert(f"🚨 INVENTORY W{tag}: net={net} — {ctx}")
    try:
        store.insert_orphan_row(ticker, {"net_position": int(net)})
    except Exception as e:
        notify.send(f"⚠ W{tag} orphan handoff failed: {e}")
    comp = "no" if net > 0 else "yes"          # hold YES→buy NO ; hold NO→buy YES
    try:
        b = kalshi.fetch_orderbook(client, ticker)
        cb = b.no_bid if comp == "no" else b.yes_bid
        cfp = b.no_bid_fp if comp == "no" else b.yes_bid_fp
        if cb is None:
            notify.send(f"⚠ W{tag} flatten: no {comp} touch to join")
            return
        kalshi.place_order_maker(client, ticker, comp, cb, abs(int(net)),
                                 expiration_ts=int(close_ts - 2), v2_price_str=cfp)
        notify.send(f"🩹 W{tag} — flatten join {comp.upper()}@{cb}×{abs(int(net))} (exp close−2)")
    except Exception as e:
        notify.send(f"⚠ W{tag} flatten attempt failed: {e}")


def _flat_proof(client, ticker: str, close_ts: int, tag: str,
                expected_flat: bool, ride_side: Optional[str], ride_px: Optional[int]):
    """§1 THE NO-INVENTORY PROOF — flatness proven by the broker, not asserted by the code.
    Returns (broker_flat: Optional[bool], suffix: str) for the DONE line. Netted/flipped/
    floor-ride windows must read net 0; a lone ride is the accepted ±1 residual. Any other
    net pages Drew and is flattened (§1 via _inventory)."""
    try:
        net = kalshi.position_for_market(client, ticker)
    except Exception as e:
        notify.send(f"⚠ W{tag} broker flat-check error: {e}")
        return None, "flat ? (broker unreachable)"
    if expected_flat:
        if net == 0:
            return True, "flat ✓ (broker)"
        _inventory(client, ticker, close_ts, tag, net, "expected flat")
        return False, f"🚨 INVENTORY net={net}"
    want = 1 if ride_side == "yes" else -1     # lone ride: exactly the ridden ±1 leg
    if net == want:
        return True, f"riding {ride_side.upper()}@{ride_px} (±1 accepted)"
    _inventory(client, ticker, close_ts, tag, net, f"lone ride wanted {want}")
    return False, f"🚨 INVENTORY net={net} (want {want})"


def run_flip_cycle(client, ticker: str, close_ts: int, market_obj: dict) -> Optional[str]:
    """One flip window, blocking from the open through the T-90 sweep. Called by
    engine.run_market_cycle when ENABLED. Returns a short outcome string."""
    now = time.time()
    secs = close_ts - now
    tag = _tag(close_ts)

    if discipline.is_halted():
        return None
    if gateway.is_traded(ticker):          # one entry phase per window (reuse the flag)
        return None
    if secs > FLIP_WINDOW_SEC + 5:         # not open yet
        return None
    if secs < FLIP_WINDOW_SEC - FLIP_ENTRY_SEC:   # booted past the entry phase — skip window
        gateway.mark_traded(ticker, "F")
        notify.send(f"⏭ W{tag} — missed the open (T-{int(secs)}s) | SAT")
        return "flip_sat_missed_open"

    # ARM — one-shot per window (mark BEFORE quoting so a partial failure can't re-arm)
    gateway.mark_traded(ticker, "F")
    exp = int(close_ts - FLIP_FLAT_AT)     # every order dies at T-90 at the exchange

    book = kalshi.fetch_orderbook(client, ticker)
    yb, nb = book.yes_bid, book.no_bid
    if yb is None or nb is None:
        notify.send(f"⏭ W{tag} — book missing a side | SAT")
        return "flip_sat_book"
    cost = flip_math.bundle_cost(yb, nb)
    if cost > FLIP_LINE:                   # WALL: combined ≤ FLIP_LINE
        notify.send(f"⏭ W{tag} — bundle {yb}+{nb}={cost} > {FLIP_LINE} | SAT")
        return "flip_sat_line"
    if _is_observe():
        notify.send(f"🔁 W{tag} — would quote {yb}+{nb}={cost} (observe)")
        return "flip_observe"

    # QUOTE BOTH SIDES at the join, subpenny fp, expiring T-90
    ys, yp = flip_math.entry_args("yes", yb)
    yes_oid, _ = kalshi.place_order_maker(client, ticker, ys, yp, 1,
                                          expiration_ts=exp, v2_price_str=book.yes_bid_fp)
    ns, npc = flip_math.entry_args("no", nb)
    no_oid, _ = kalshi.place_order_maker(client, ticker, ns, npc, 1,
                                         expiration_ts=exp, v2_price_str=book.no_bid_fp)
    quote_ts = time.time()                 # §3: time-to-first-fill / time-to-flat anchor
    sp_yes, sp_no = _entry_spreads(book)   # §3: spread at entry, both sides
    sigma_raw = None                       # §3: no sigma sampler in flip mode (gate is cost)
    notify.send(f"🔁 W{tag} — quoting {yb}+{nb}={cost} (both @join, exp T-{FLIP_FLAT_AT})")

    # ENTRY PHASE — poll for fills. RUNG A = this entry pair.
    deadline = now + FLIP_ENTRY_SEC
    fills = {}   # held_side -> entry cents
    first_fill_ts = None
    flat_ts = None
    while time.time() < deadline and len(fills) < 2:
        for hs, oid in (("yes", yes_oid), ("no", no_oid)):
            if hs not in fills:
                fp = _order_fill_price(client, ticker, oid, hs)
                if fp is not None:
                    fills[hs] = fp
                    if first_fill_ts is None:
                        first_fill_ts = time.time()
                    _record_enter(ticker, close_ts, hs, fp)
                    notify.send(f"🌱 W{tag} — filled {hs.upper()}@{fp}")
        if len(fills) == 2 and flat_ts is None:
            flat_ts = time.time()          # rung A nets flat at the second entry fill
        if len(fills) < 2:
            engine.heartbeat()             # F-4: prove liveness through the blocking phase
            time.sleep(FLIP_POLL_SEC)

    # cancel any unfilled entry bids
    if len(fills) < 2:
        kalshi.cancel_all_for_market(client, ticker)

    if not fills:
        notify.send(f"⏭ W{tag} — no fill ≤ line | SAT")
        return "flip_sat_nofill"

    n_entry = len(fills)
    ey, en = fills.get("yes"), fills.get("no")
    captures = []                          # [(rung_label, cents)] — each netted/flipped pair

    # RUNG A — both entry legs filled ⇒ the entry PAIR nets flat and the exchange banks
    # +(100−cost)¢ at the second fill (WO-LANE-FLIP-3 §2: this is the first capture). We do
    # NOT stop here — rung B still posts (the two-rung ladder, formalized from the tape).
    if n_entry == 2:
        cap_a = 100 - cost
        captures.append(("A", cap_a))
        notify.send(f"💰 W{tag} — rung A netted {ey}+{en}={cost} → +{cap_a}¢")

    # lone-leg keepability (WALL): a lone leg above the max means the line was misjudged
    if n_entry == 1:
        (lhs, lhp), = fills.items()
        if lhp > FLIP_LONE_MAX:
            notify.alert(f"⚠ W{tag} lone {lhs.upper()}@{lhp} > {FLIP_LONE_MAX} — line misjudged "
                         f"(shouldn't occur at 1-lot join)")

    # RUNG B — post one exit per filled leg at entry+X via the complement buy (exp T-90).
    # For a double fill the two exits form a SECOND netted pair; for a lone leg its exit IS
    # rung B. No rung C is ever posted (the ladder is a pre-committed two-rung sequence).
    exit_oids = {}                         # held_side -> {oid, target, side}
    for hs, hp in fills.items():
        q = hp + FLIP_X
        es, ep = flip_math.exit_args(hs, q)
        oid, _ = kalshi.place_order_maker(client, ticker, es, ep, 1, expiration_ts=exp)
        exit_oids[hs] = {"oid": oid, "target": q, "side": es}
    notify.send(f"🔁 W{tag} — rung B posted: exits @entry+{FLIP_X} "
                f"({'+'.join(str(fills[h] + FLIP_X) for h in fills)})")

    # MONITOR RUNG B until T-90. An exit fill flattens its leg and books an ENTER row so the
    # existing settlement machinery accounts the complement leg (no new accounting).
    exit_fill = {}                         # held_side -> exit fill price (exit-side terms)
    while (close_ts - time.time()) > FLIP_FLAT_AT and exit_oids:
        for hs in list(exit_oids):
            eo = exit_oids[hs]
            fp = _order_fill_price(client, ticker, eo["oid"], eo["side"])
            if fp is not None:
                exit_fill[hs] = fp
                del exit_oids[hs]
                _record_enter(ticker, close_ts, eo["side"], fp, why_tag="FLIP_EXIT")
                notify.send(f"🌱 W{tag} — rung B filled {hs.upper()} exit @{fp} ({eo['side'].upper()})")
                if n_entry == 1 and flat_ts is None:
                    flat_ts = time.time()  # lone flip: flat when the single exit fills
        if exit_oids:
            engine.heartbeat()             # F-4: liveness through the exit monitor
            time.sleep(FLIP_POLL_SEC)

    # T-90 SWEEP — cancel unfilled exits, re-join at the CURRENT touch (maker, exp close−2 so
    # it lives to the bell). Record each rejoin as an unfilled ENTER row w/ order_id so the
    # settlement backfill verifies-or-nofills it against the broker. No taker is added.
    swept = {}
    book2 = None
    if exit_oids:
        engine.heartbeat()                 # F-4: liveness across the sweep
        kalshi.cancel_all_for_market(client, ticker)
        book2 = kalshi.fetch_orderbook(client, ticker)
        rejoin_exp = int(close_ts - 2)     # F-3: rejoin lives to the bell, never a past ts
        for hs in list(exit_oids):
            comp = "no" if hs == "yes" else "yes"
            comp_bid = book2.no_bid if comp == "no" else book2.yes_bid
            comp_fp = book2.no_bid_fp if comp == "no" else book2.yes_bid_fp
            if comp_bid is not None:
                try:
                    roid, _ = kalshi.place_order_maker(client, ticker, comp, comp_bid, 1,
                                                       expiration_ts=rejoin_exp, v2_price_str=comp_fp)
                    swept[hs] = comp_bid
                    _record_enter(ticker, close_ts, comp, None, roid, why_tag="FLIP_SWEEP")
                    notify.send(f"🔁 W{tag} — T-90 rejoin {hs.upper()} exit @{comp_bid} (exp close−2)")
                except Exception as e:
                    notify.send(f"⚠ W{tag} T-90 rejoin failed: {e}")

    # ── resolve captures + outcome tag (WO-LANE-FLIP-3 §3) ───────────
    exit_yes = exit_fill.get("no")         # yes-terms exit fill (flattened a held NO)
    exit_no = exit_fill.get("yes")         # no-terms exit fill (flattened a held YES)
    ride_side = ride_px = None
    if n_entry == 2:
        expected_flat = True               # 2 bundles, 1 bundle floor-riding, or a naked leg (flag)
        if len(exit_fill) == 2:
            cap_b = 100 - (exit_fill["yes"] + exit_fill["no"])
            captures.append(("B", cap_b))
            outcome = "NETTED_2R"
        elif len(exit_fill) == 1:
            outcome = "NETTED_1R"          # rung A netted; a single rung-B leg → naked (§1 flags)
        else:
            outcome = "FLOOR_RIDE"         # rung A netted; rung B unfilled → the bundle rides floor
    else:
        (lhs, lhp), = fills.items()
        if lhs in exit_fill:
            captures.append(("B", 100 - (lhp + exit_fill[lhs])))
            outcome, expected_flat = "LONE_FLIP", True
        elif lhs in swept:
            outcome, expected_flat = "LONE_SALVAGE", False
            ride_side, ride_px = lhs, lhp
        else:
            outcome, expected_flat = "LONE_RIDE", False
            ride_side, ride_px = lhs, lhp

    realized = sum(c for _, c in captures)
    stopped = realized <= -FLIP_STOP_CENTS
    if stopped:
        outcome = "STOPPED"

    # mark any open leg at the current bid (bid-marked mtm on the ridden residual)
    mtm_open = 0
    if ride_side is not None and book2 is not None:
        cur_bid = book2.yes_bid if ride_side == "yes" else book2.no_bid
        if cur_bid is not None and ride_px is not None:
            mtm_open = int(cur_bid - ride_px)

    # §1 THE NO-INVENTORY PROOF — the counterparty confirms flatness (or we flatten + page).
    broker_flat, flat_suffix = _flat_proof(client, ticker, close_ts, tag,
                                           expected_flat, ride_side, ride_px)

    ttff = (first_fill_ts - quote_ts) if first_fill_ts else None
    ttflat = (flat_ts - quote_ts) if flat_ts else None
    cap_a_c = next((c for L, c in captures if L == "A"), None)
    cap_b_c = next((c for L, c in captures if L == "B"), None)

    # §3 TAG EVERY WINDOW — one immutable row, the desk's own crossing study.
    try:
        store.insert_flip_window(
            ts=time.time(), close_ts=int(close_ts), tag=tag, ticker=ticker,
            entry_yes=ey, entry_no=en, bundle_cost=(cost if n_entry == 2 else None),
            exit_yes=exit_yes, exit_no=exit_no, capture_a_cents=cap_a_c,
            capture_b_cents=cap_b_c, realized_cents=int(realized), mtm_open_cents=mtm_open,
            spread_yes=sp_yes, spread_no=sp_no, sigma_at_gate=sigma_raw,
            ttff_s=ttff, ttflat_s=ttflat, outcome_tag=outcome,
            broker_flat=(None if broker_flat is None else (1 if broker_flat else 0)),
        )
    except Exception as e:
        notify.send(f"⚠ W{tag} flip_window row failed: {e}")

    _bump_stop_streak(stopped)

    # DONE line — a complete sentence: money and why, flatness proven by the broker.
    entry_str = f"{ey}+{en}={cost}" if n_entry == 2 else f"{list(fills)[0].upper()}@{list(fills.values())[0]}"
    rung_str = " · ".join(f"rung{L} +{c}¢" for L, c in captures) if captures else "no capture"
    sign = "+" if realized >= 0 else ""
    notify.send(f"🔁 W{tag} — {entry_str} | {rung_str} → {sign}{realized}¢ [{outcome}] "
                f"| {flat_suffix} | DONE")
    return "flip_done"
