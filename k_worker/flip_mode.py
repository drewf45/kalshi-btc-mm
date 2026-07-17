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
from . import flip_math, flip_pricebrain

NY = ZoneInfo("America/New_York")

KW_MODE = os.environ.get("KW_MODE", "FLIP").strip().upper()   # F-2: FLIP is the default
ENABLED = flip_math.mode_enabled(os.environ.get("KW_MODE"))   # KW_MODE=OFF is the kill switch

FLIP_WINDOW_SEC = int(os.environ.get("FLIP_WINDOW_SEC", "900"))
FLIP_ENTRY_SEC = int(os.environ.get("FLIP_ENTRY_SEC", "300"))   # WO-4: watch the first ~5 min
FLIP_LINE = int(os.environ.get("FLIP_LINE", "99"))
FLIP_LONE_MAX = int(os.environ.get("FLIP_LONE_MAX", "49"))
FLIP_SIDE_MAX = int(os.environ.get("FLIP_SIDE_MAX", "49"))      # WO-4: post a side the 1st time ≤ this
FLIP_REQUIRE_PAIR = int(os.environ.get("FLIP_REQUIRE_PAIR", "1"))  # WO-RESUME: 1 = pair-or-nothing
FLIP_X = int(os.environ.get("FLIP_X", "4"))
FLIP_FLAT_AT = int(os.environ.get("FLIP_FLAT_AT", "90"))
FLIP_STOP_CENTS = int(os.environ.get("FLIP_STOP_CENTS", "25"))
FLIP_PAUSE_AFTER_STOPS = int(os.environ.get("FLIP_PAUSE_AFTER_STOPS", "2"))
FLIP_POLL_SEC = float(os.environ.get("FLIP_POLL_SEC", "3"))

# WO-VISION — the ratchet (ships ON and WIDE; =0 reverts to the proven require_pair window).
# WO-PREDATOR PART A — gates re-tuned to the scratch-era math (the cage below does not move).
FLIP_RATCHET = int(os.environ.get("FLIP_RATCHET", "1"))
FLIP_PAIR_GRACE = int(os.environ.get("FLIP_PAIR_GRACE", "30"))    # §1 opposite bid works this long
FLIP_SCRATCH_S = int(os.environ.get("FLIP_SCRATCH_S", "3"))       # §1(a) scratch if mark ≤ entry−S
FLIP_MARKOUT_STOP = int(os.environ.get("FLIP_MARKOUT_STOP", "2")) # §1(c) markout ≤ −this & worsening
FLIP_OFI_TICKS = int(os.environ.get("FLIP_OFI_TICKS", "4"))       # A1: 3-of-4 net of the last N
FLIP_RATCHET_SIDE_MAX = int(os.environ.get("FLIP_RATCHET_SIDE_MAX", "58"))  # A2: OFI-gated cap
FLIP_CURFEW = int(os.environ.get("FLIP_CURFEW", "150"))           # A3: no new entries after T-this
FLIP_MAX_TRIPS = int(os.environ.get("FLIP_MAX_TRIPS", "8"))       # A3 governor
FLIP_SCRATCH_SITOUT = int(os.environ.get("FLIP_SCRATCH_SITOUT", "4"))  # A3: 4 scratches OR loss
FLIP_CLOSE_WINDOW_S = int(os.environ.get("FLIP_CLOSE_WINDOW_S", "60"))  # B3: TWAP close window
FLIP_MAX_MOVE_PER_S = float(os.environ.get("FLIP_MAX_MOVE_PER_S", "6.0"))  # B3: $/s adverse bound
FLIP_LEAD_TICK = float(os.environ.get("FLIP_LEAD_TICK", "1.0"))   # B1: spot move ($) to penny-lead

_STOP_STREAK_KEY = "flip_stop_streak"


def echo_config() -> None:
    mode = "RATCHET" if FLIP_RATCHET else f"require_pair {FLIP_REQUIRE_PAIR}"
    notify.send(
        "🔁 <b>FLIP MODE</b> — watch the open, post each side the first time its join ≤ "
        f"{FLIP_SIDE_MAX}c, flip @entry+{FLIP_X}c\n"
        f"mode {mode} · line {FLIP_LINE}c · side_max {FLIP_SIDE_MAX}c · x {FLIP_X}c · "
        f"ofi net{FLIP_OFI_TICKS // 2 + 1}/{FLIP_OFI_TICKS} · ratchet_cap {FLIP_RATCHET_SIDE_MAX} · "
        f"curfew T-{FLIP_CURFEW} · trips≤{FLIP_MAX_TRIPS} · sitout@{FLIP_SCRATCH_SITOUT}/loss · "
        f"scratch@entry−{FLIP_SCRATCH_S}c · grace {FLIP_PAIR_GRACE}s · "
        f"stop {FLIP_STOP_CENTS}c · pause@{FLIP_PAUSE_AFTER_STOPS}"
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


def _is_cross_400(e) -> bool:
    """WO-CROSSFIRE: the exchange's 'post only cross' rejection (a post_only order that would
    fill immediately). Match narrowly so other 400s aren't mistaken for it."""
    s = str(e).lower()
    return "400" in s and "post only cross" in s


def _post_entry(client, ticker: str, side: str, join: int, fp_str, exp: int, tag: str):
    """WO-CROSSFIRE §2 — a PASSIVE entry join with reject-and-recover. On a post-only cross
    (the book moved between fetch and post) refetch once and re-price at the fresh join; if it
    would STILL cross, skip the entry this trip (never chase). Returns oid or None."""
    try:
        oid, _ = kalshi.place_order_maker(client, ticker, side, join, 1,
                                          expiration_ts=exp, v2_price_str=fp_str)
        return oid
    except Exception as e:
        if not _is_cross_400(e):
            notify.send(f"⚠ W{tag} entry {side.upper()}@{join} rejected: {e}")
            return None
        try:
            b = kalshi.fetch_orderbook(client, ticker)          # book moved — refetch once
            nj = b.yes_bid if side == "yes" else b.no_bid
            nf = b.yes_bid_fp if side == "yes" else b.no_bid_fp
            if nj is None or nj > FLIP_SIDE_MAX:
                notify.send(f"⚠ W{tag} entry {side.upper()} skipped — book moved (would cross)")
                return None
            oid, _ = kalshi.place_order_maker(client, ticker, side, nj, 1,
                                              expiration_ts=exp, v2_price_str=nf)
            return oid
        except Exception as e2:
            notify.send(f"⚠ W{tag} entry {side.upper()} skipped — book moved (would cross)"
                        if _is_cross_400(e2) else f"⚠ W{tag} entry {side.upper()} reprice failed: {e2}")
            return None


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
        notify.alert(f"🛑 FLIP PAUSE — {streak} consecutive stopped windows.\n"
                     f"⛔ halted. Resume? /resume_yes /resume_no")


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
        roid, _ = kalshi.place_order_maker(client, ticker, comp, cb, abs(int(net)),
                                           expiration_ts=int(close_ts - 2), v2_price_str=cfp,
                                           post_only=False)   # WO-CROSSFIRE: fill-now flatten
        # WO-6 §2: story row so settlement resolves the pair and Δ$-explained stays honest
        _record_enter(ticker, close_ts, comp, None, roid, why_tag="FLIP_FLATTEN")
        notify.send(f"🩹 W{tag} — flatten join {comp.upper()}@{cb}×{abs(int(net))} (exp close−2)")
    except Exception as e:
        notify.send(f"⚠ W{tag} flatten attempt failed: {e}")


def _flat_proof(client, ticker: str, close_ts: int, tag: str,
                expected_flat: bool, ride_side: Optional[str], ride_px: Optional[int]):
    """§1 THE NO-INVENTORY PROOF — flatness proven by the broker, not asserted by the code.
    Returns (broker_flat: Optional[bool], suffix: str, net: Optional[int]) for the DONE line.

    WO-6 §4 polarity: the 🚨 siren means DANGER only. A mismatch in the SAFE direction —
    flatter than modeled (e.g. the salvage filled so net=0 when a ±1 ride was expected) —
    renders as an ℹ️ note, not an alarm. Only a RISK-direction net (more exposure than
    expected, or the wrong sign) pages Drew and is flattened."""
    try:
        net = kalshi.position_for_market(client, ticker)
    except Exception as e:
        notify.send(f"⚠ W{tag} broker flat-check error: {e}")
        return None, "flat ? (broker unreachable)", None
    if expected_flat:
        if net == 0:
            return True, "flat ✓ (broker)", 0
        _inventory(client, ticker, close_ts, tag, net, "expected flat")
        return False, f"🚨 INVENTORY net={net}", net
    want = 1 if ride_side == "yes" else -1     # lone ride: exactly the ridden ±1 leg
    if net == want:
        return True, f"riding {ride_side.upper()}@{ride_px} (±1 accepted)", net
    if net is not None and abs(net) < abs(want) and net * want >= 0:
        # flatter than modeled — the safe direction. Info, not a siren.
        return True, (f"ℹ️ position reconciled: flatter than modeled "
                      f"(net={net}, wanted {want}) — salvage filled"), net
    _inventory(client, ticker, close_ts, tag, net, f"lone ride risk (want {want})")
    return False, f"🚨 INVENTORY net={net} (want {want})", net


def _decline_lone(client, ticker: str, close_ts: int, tag: str, lhs: str, lhp: int):
    """WO-RESUME §1 PAIR-OR-NOTHING: a lone filled leg is not kept — flatten it at once via
    the complement touch (maker, exp close−2). One re-join if unfilled after ~30s; the leg
    was just bought at the touch, so a flatten at the touch fills in practice. Returns
    (flatten_price_or_None, filled_bool). Books a FLIP_DECLINE story row per attempt."""
    comp = "no" if lhs == "yes" else "yes"

    def _touch():
        b = kalshi.fetch_orderbook(client, ticker)
        cb = b.no_bid if comp == "no" else b.yes_bid
        cfp = b.no_bid_fp if comp == "no" else b.yes_bid_fp
        return cb, cfp

    # WO-CROSSFIRE §1: a flatten is a CROSSING intent — it must be allowed to fill NOW, so
    # post_only=False (a maker rest is fine when it doesn't cross; a cross fills as a taker).
    def _flatten(price, fp_str):
        return kalshi.place_order_maker(client, ticker, comp, price, 1,
                                        expiration_ts=int(close_ts - 2),
                                        v2_price_str=fp_str, post_only=False)

    cb, cfp = _touch()
    if cb is None:
        notify.alert(f"⚠ W{tag} decline: no {comp.upper()} touch to flatten lone "
                     f"{lhs.upper()}@{lhp} — rides bounded")
        return None, False
    try:
        roid, _ = _flatten(cb, cfp)
    except Exception as e:
        notify.alert(f"🚨 scratch unfillable W{tag} {lhs.upper()}@{lhp}: {e} — T-90 backstop")
        return None, False
    _record_enter(ticker, close_ts, comp, None, roid, why_tag="FLIP_DECLINE")
    notify.send(f"🚪 W{tag} — declining lone {lhs.upper()}@{lhp}: flatten {comp.upper()}@{cb} "
                f"(exp close−2)")
    used, filled = cb, False
    deadline = time.time() + 30
    while time.time() < deadline and (close_ts - time.time()) > 2:
        engine.heartbeat()
        fp = _order_fill_price(client, ticker, roid, comp)
        if fp is not None:
            used, filled = fp, True
            break
        time.sleep(FLIP_POLL_SEC)
    if not filled:                          # one re-join at the current touch
        kalshi.cancel_all_for_market(client, ticker)
        cb2, cfp2 = _touch()
        if cb2 is not None:
            try:
                roid2, _ = _flatten(cb2, cfp2)
                _record_enter(ticker, close_ts, comp, None, roid2, why_tag="FLIP_DECLINE")
                used = cb2
                notify.send(f"🚪 W{tag} — decline rejoin {comp.upper()}@{cb2} (exp close−2)")
            except Exception as e:
                notify.alert(f"🚨 scratch unfillable W{tag} rejoin {comp.upper()}@{cb2}: {e} "
                             f"— T-90 backstop")
        else:
            notify.alert(f"⚠ W{tag} decline rejoin: no {comp.upper()} touch — lone rides bounded")
    return used, filled


# ── WO-VISION pure signal helpers (crypto-free, unit-tested) ─────────────────
def _tick_direction(ticks, n: int) -> Optional[str]:
    """A1: NET majority of the last n spot deltas (net-3-of-4 at n=4). ≥ majority up →
    'yes' (rising favors above-strike), ≥ majority down → 'no', ties/2-2 → None. Fading
    stays impossible (a tie never returns a side)."""
    if ticks is None or len(ticks) < n + 1:
        return None
    recent = ticks[-(n + 1):]
    deltas = [recent[i + 1] - recent[i] for i in range(n)]
    ups = sum(1 for d in deltas if d > 0)
    downs = sum(1 for d in deltas if d < 0)
    need = n // 2 + 1                                # majority: 3 of 4
    if ups >= need and ups > downs:
        return "yes"
    if downs >= need and downs > ups:
        return "no"
    return None


def _book_lean(book) -> Optional[str]:
    """Which side the book leans by best-bid size, or None if unclear/absent."""
    yq = getattr(book, "yes_bid_qty", None)
    nq = getattr(book, "no_bid_qty", None)
    if yq is None or nq is None:
        return None
    if yq > nq:
        return "yes"
    if nq > yq:
        return "no"
    return None


def _ofi_side(ticks, lean_side: Optional[str]) -> Optional[str]:
    """§2 OFI GATE — fail closed. Returns the side to join iff the last FLIP_OFI_TICKS spot
    deltas ALL agree AND the book lean agrees; otherwise None (no signal / disagreement /
    would-fade → wait). Never fades the tape."""
    d = _tick_direction(ticks, FLIP_OFI_TICKS)
    if d is None or lean_side is None or lean_side != d:
        return None
    return d


def _spot_adverse(side: str, spot: Optional[float], lo, hi) -> bool:
    """§1(b) is spot on the losing side of the strike for the held leg? Conservative: only a
    single clear threshold counts; a two-sided range or missing data → not adverse."""
    if spot is None:
        return False
    strike = None
    if lo is not None and hi is None:
        strike = lo
    elif hi is not None and lo is None:
        strike = hi
    if strike is None:
        return False
    return (spot < strike) if side == "yes" else (spot > strike)


def _scratch_reason(side: str, entry_cents: int, leg_mark, markout_30,
                    prev_markout, spot_adverse_polls: int) -> Optional[str]:
    """§1 scratch signal — cross out NOW when any of (a) leg mark ≤ entry−S, (b) spot through
    the strike sustained ≥2 polls, (c) markout at +30s ≤ −stop and worsening. Else None."""
    if leg_mark is not None and leg_mark <= entry_cents - FLIP_SCRATCH_S:
        return f"mark {leg_mark}≤entry−{FLIP_SCRATCH_S}"                       # (a)
    if spot_adverse_polls >= 2:
        return "spot through strike (2 polls)"                                # (b)
    if (markout_30 is not None and markout_30 <= -FLIP_MARKOUT_STOP
            and prev_markout is not None and markout_30 < prev_markout):
        return f"markout {markout_30}¢ worsening"                             # (c)
    return None


def _spot():
    try:
        return kalshi.get_btc_spot()
    except Exception:
        return None


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

    # WO-VISION — the ratchet manages from the fill with a multi-trip loop. FLIP_RATCHET=0
    # reverts to the proven require_pair single-shot window below (the one-env kill switch).
    # WO-CROSSFIRE §2: no order rejection escapes as a stack trace — the window aborts cleanly
    # and the trip slot is released (the R1 wall re-checks flat next window).
    if FLIP_RATCHET:
        try:
            return _run_ratchet_window(client, ticker, close_ts, tag, exp, market_obj)
        except Exception as e:
            notify.alert(f"⚠ W{tag} ratchet cycle error: {e} — window aborted, slot released")
            return "flip_error"

    # OPPORTUNISTIC ENTRY (WO-4) — WATCH the early window instead of quoting both at the
    # bell. Each side posts its bid the FIRST time its join ≤ FLIP_SIDE_MAX, at most once,
    # no re-peg, expiry T-90. The second side may post only if it keeps the combined bundle
    # ≤ FLIP_LINE against the RESTING first bid (not a stale book). Everything downstream —
    # rung A netting, rung B, sweep, broker flat proof, tags — is unchanged.
    quote_ts = now                          # §3: window-open anchor for post/fill timings
    sp_yes = sp_no = None                   # §3: entry spreads, captured at first book read
    spreads_done = False
    sigma_raw = None                        # §3: no sigma sampler in flip mode (gate is cost)
    deadline = now + FLIP_ENTRY_SEC
    posted = {}                             # side -> {"oid","price","dt"} (dt = secs from open)
    observed = set()
    fills = {}                              # side -> entry cents
    first_fill_ts = None
    flat_ts = None

    while time.time() < deadline and len(fills) < 2:
        book = kalshi.fetch_orderbook(client, ticker)
        if not spreads_done:
            sp_yes, sp_no = _entry_spreads(book)
            spreads_done = True
        for side in ("yes", "no"):
            if side in posted or side in fills:
                continue
            join = book.yes_bid if side == "yes" else book.no_bid
            fp_str = book.yes_bid_fp if side == "yes" else book.no_bid_fp
            if join is None or join > FLIP_SIDE_MAX:
                continue
            # combined wall: a SECOND side posts only if the bundle stays ≤ line against
            # the RESTING first bid's price.
            if posted:
                first = next(iter(posted.values()))
                if first["price"] + join > FLIP_LINE:
                    continue
            if _is_observe():
                if side not in observed:
                    observed.add(side)
                    notify.send(f"🔁 W{tag} — would post {side.upper()}@{join} (observe)")
                continue
            ss, pp = flip_math.entry_args(side, join)
            oid = _post_entry(client, ticker, ss, pp, fp_str, exp, tag)   # passive, recovers
            if oid is None:
                continue
            far = book.no_bid if side == "yes" else book.yes_bid   # §3: far-side bid at post
            booksum = join + far if far is not None else join      # ≪100 or ≈join = lone shape
            posted[side] = {"oid": oid, "price": join, "dt": time.time() - quote_ts,
                            "booksum": booksum}
            notify.send(f"🔁 W{tag} — posted {side.upper()}@{join} "
                        f"(t+{int(posted[side]['dt'])}s, exp T-{FLIP_FLAT_AT})")
        for side in list(posted):          # poll fills for whatever is resting
            if side not in fills:
                fp = _order_fill_price(client, ticker, posted[side]["oid"], side)
                if fp is not None:
                    fills[side] = fp
                    if first_fill_ts is None:
                        first_fill_ts = time.time()
                    _record_enter(ticker, close_ts, side, fp)
                    notify.send(f"🌱 W{tag} — filled {side.upper()}@{fp}")
        if len(fills) == 2 and flat_ts is None:
            flat_ts = time.time()          # rung A nets flat at the second entry fill
        if len(fills) < 2:
            engine.heartbeat()             # F-4: prove liveness through the watch phase
            time.sleep(FLIP_POLL_SEC)

    if _is_observe():
        notify.send(f"🔁 W{tag} — observe: sighted {sorted(observed) or 'none'} "
                    f"≤ {FLIP_SIDE_MAX}c in {FLIP_ENTRY_SEC}s")
        return "flip_observe"

    if not posted:
        notify.send(f"⏭ W{tag} — no side ≤ {FLIP_SIDE_MAX} in {FLIP_ENTRY_SEC}s | SAT")
        return "flip_sat_no_side"

    if len(fills) < len(posted):           # cancel any resting bid that didn't fill
        kalshi.cancel_all_for_market(client, ticker)

    if not fills:
        notify.send(f"⏭ W{tag} — posted but no fill | SAT")
        return "flip_sat_nofill"

    n_entry = len(fills)
    ey, en = fills.get("yes"), fills.get("no")
    cost = (ey + en) if n_entry == 2 else None   # WO-4: bundle cost from actual entries
    captures = []                          # [(rung_label, cents)] — each netted/flipped pair

    # RUNG A — both entry legs filled ⇒ the entry PAIR nets flat and the exchange banks
    # +(100−cost)¢ at the second fill (WO-LANE-FLIP-3 §2: this is the first capture). We do
    # NOT stop here — rung B still posts (the two-rung ladder, formalized from the tape).
    if n_entry == 2:
        cap_a = 100 - cost
        captures.append(("A", cap_a))
        notify.send(f"💰 W{tag} — rung A netted {ey}+{en}={cost} → +{cap_a}¢")

    # WO-RESUME §1 PAIR-OR-NOTHING — a lone filled leg is DECLINED (flattened at the touch),
    # not kept. This closes the loss shape (cheap lone into a decided book) while leaving the
    # profit shape (netted bundles) untouched. Lone-keeping returns only with REQUIRE_PAIR=0.
    declined = False
    decline_px = None
    decline_filled = False
    if n_entry == 1 and FLIP_REQUIRE_PAIR:
        (lhs, lhp), = fills.items()
        decline_px, decline_filled = _decline_lone(client, ticker, close_ts, tag, lhs, lhp)
        declined = True

    # lone-leg keepability (WALL): only relevant when we KEEP a lone leg (REQUIRE_PAIR=0)
    if n_entry == 1 and not declined:
        (lhs, lhp), = fills.items()
        if lhp > FLIP_LONE_MAX:
            notify.alert(f"⚠ W{tag} lone {lhs.upper()}@{lhp} > {FLIP_LONE_MAX} — line misjudged "
                         f"(shouldn't occur at 1-lot join)")

    # RUNG B — post one exit per filled leg at entry+X via the complement buy (exp T-90).
    # For a double fill the two exits form a SECOND netted pair; for a lone leg its exit IS
    # rung B. No rung C is ever posted (the ladder is a pre-committed two-rung sequence).
    #
    # WO-4 GUARD (the cheap-bundle trap): the double-fill exit PAIR costs (200 − cost − 2·X);
    # the juicier rung A is, the more that pair overpays. Post rung B only if the pair keeps
    # to the line — otherwise bank rung A and let the bundle ride its floor. A lone-leg exit
    # is a single complement buy and is exempt.
    post_rung_b = not declined             # a declined lone leg posts no rung B
    if n_entry == 2:
        pair = 200 - cost - 2 * FLIP_X
        if pair > FLIP_LINE:
            post_rung_b = False
            notify.send(f"🔒 W{tag} — rung B skipped (pair {pair} > line {FLIP_LINE}); "
                        f"rung A banked, bundle rides floor")

    exit_oids = {}                         # held_side -> {oid, target, side}
    if post_rung_b:
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
                    _record_enter(ticker, close_ts, comp, None, roid, why_tag="FLIP_SALVAGE")
                    notify.send(f"🔁 W{tag} — T-90 rejoin {hs.upper()} exit @{comp_bid} (exp close−2)")
                except Exception as e:
                    notify.send(f"⚠ W{tag} T-90 rejoin failed: {e}")

    # ── resolve captures + outcome tag (WO-LANE-FLIP-3 §3) ───────────
    exit_yes = exit_fill.get("no")         # yes-terms exit fill (flattened a held NO)
    exit_no = exit_fill.get("yes")         # no-terms exit fill (flattened a held YES)
    ride_side = ride_px = None
    lone_open = False                      # a lone leg with no target flip (salvage/ride/flatten)
    lhs = lhp = None
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
    elif declined:
        # WO-RESUME §1: the lone leg was flattened at the touch. If the flatten filled we
        # intend flat; if not, the leg rides bounded (with an alert) — not a full inventory
        # cascade. Realized is the flatten outcome (booked below).
        (lhs, lhp), = fills.items()
        outcome = "LONE_DECLINED"
        if decline_filled:
            expected_flat = True
        else:
            expected_flat = False
            ride_side, ride_px = lhs, lhp
            notify.alert(f"⚠ W{tag} declined but flatten unfilled — lone {lhs.upper()}@{lhp} "
                         f"rides bounded (maker resting to close−2)")
    else:
        (lhs, lhp), = fills.items()
        if lhs in exit_fill:
            captures.append(("B", 100 - (lhp + exit_fill[lhs])))
            outcome, expected_flat = "LONE_FLIP", True
        else:
            outcome, expected_flat = "LONE_RIDE", False    # refined below from the broker net
            ride_side, ride_px = lhs, lhp
            lone_open = True

    # §1 THE NO-INVENTORY PROOF — the counterparty confirms flatness (WO-6 §4 polarity).
    broker_flat, flat_suffix, net = _flat_proof(client, ticker, close_ts, tag,
                                                expected_flat, ride_side, ride_px)

    # WO-6 §1 — the SALVAGE-AWARE STOP: `realized` includes leg outcomes at DONE, not just
    # flip captures, so a salvaged/flattened/ridden lone leg feeds the stop the truth.
    lone_extra = 0
    inclusive = False
    if declined:                           # WO-RESUME §1: realized = the flatten outcome
        if decline_filled and decline_px is not None:
            lone_extra = (100 - decline_px) - lhp
        else:                              # unfilled → provisional −entry (bounded ride)
            lone_extra = -lhp
        inclusive = True
    elif lone_open:
        want = 1 if lhs == "yes" else -1
        if net == 0:                       # the complement filled → flat (safe direction)
            cb = swept.get(lhs)            # dumped at the touch we rejoined at
            lone_extra = (100 - cb - lhp) if cb is not None else -lhp
            outcome = "LONE_SALVAGE"
        elif net == want:                  # still holding the leg → riding to settlement
            lone_extra = -lhp              # provisional −entry (settlement books truth later)
            outcome = "LONE_RIDE"
        elif net is None:                  # broker unreadable → treat as a ride, provisionally
            lone_extra = -lhp
            outcome = "LONE_RIDE"
        else:                              # risk-direction residual → forced flatten (§1)
            lone_extra = -lhp
            outcome = "LONE_FLATTEN"
        inclusive = True

    realized = sum(c for _, c in captures) + lone_extra
    stopped = realized <= -FLIP_STOP_CENTS
    if stopped:
        outcome = "STOPPED"

    # mark any STILL-HELD leg at the current bid (a salvaged/flattened leg is flat → mtm 0)
    mtm_open = 0
    still_holding = lone_open and net is not None and net == (1 if lhs == "yes" else -1)
    if still_holding and ride_px is not None and book2 is not None:
        cur_bid = book2.yes_bid if lhs == "yes" else book2.no_bid
        if cur_bid is not None:
            mtm_open = int(cur_bid - ride_px)

    ttff = (first_fill_ts - quote_ts) if first_fill_ts else None
    ttflat = (flat_ts - quote_ts) if flat_ts else None
    cap_a_c = next((c for L, c in captures if L == "A"), None)
    cap_b_c = next((c for L, c in captures if L == "B"), None)

    # §3 TAG EVERY WINDOW — one immutable row, the desk's own crossing study.
    try:
        store.insert_flip_window(
            ts=time.time(), close_ts=int(close_ts), tag=tag, ticker=ticker,
            entry_yes=ey, entry_no=en,
            join_yes=(posted.get("yes") or {}).get("price"),
            join_no=(posted.get("no") or {}).get("price"),
            post_dt_yes=(posted.get("yes") or {}).get("dt"),
            post_dt_no=(posted.get("no") or {}).get("dt"),
            booksum_yes=(posted.get("yes") or {}).get("booksum"),
            booksum_no=(posted.get("no") or {}).get("booksum"),
            bundle_cost=(cost if n_entry == 2 else None),
            exit_yes=exit_yes, exit_no=exit_no, capture_a_cents=cap_a_c,
            capture_b_cents=cap_b_c, realized_cents=int(realized), mtm_open_cents=mtm_open,
            spread_yes=sp_yes, spread_no=sp_no, sigma_at_gate=sigma_raw,
            ttff_s=ttff, ttflat_s=ttflat, outcome_tag=outcome,
            broker_flat=(None if broker_flat is None else (1 if broker_flat else 0)),
        )
    except Exception as e:
        notify.send(f"⚠ W{tag} flip_window row failed: {e}")

    _bump_stop_streak(stopped)             # now fed the inclusive number (WO-6 §1)

    # DONE line — a complete sentence: money and why, flatness proven by the broker.
    entry_str = f"{ey}+{en}={cost}" if n_entry == 2 else f"{lhs.upper()}@{lhp}"
    rung_str = " · ".join(f"rung{L} +{c}¢" for L, c in captures) if captures else "no capture"
    incl = (" (incl. decline)" if declined else " (incl. salvage)") if inclusive else ""
    sign = "+" if realized >= 0 else ""
    notify.send(f"🔁 W{tag} — {entry_str} | {rung_str} → {sign}{realized}¢ realized{incl} "
                f"[{outcome}] | {flat_suffix} | DONE")
    return "flip_done"


# ── WO-VISION — the ratchet: fill-clock management + caged re-entry ──────────
def _r1_flat(client, ticker: str, tag: str, silent: bool = False) -> bool:
    """R1 WALL (§2, the wall of walls) — one position at a time. True iff the market reads
    flat (net 0) before a new trip opens. A non-flat state is a violation and pages Drew.
    Structurally, the single-trip-at-a-time loop never opens a second managed position."""
    try:
        net = kalshi.position_for_market(client, ticker)
    except Exception as e:
        if not silent:
            notify.send(f"⚠ W{tag} R1 check error: {e}")
        return False
    if net == 0:
        return True
    if not silent:
        notify.alert(f"🚫 R1 WALL W{tag}: net={net} before a new trip — not flat, standing down")
    return False


def _run_trip(client, ticker: str, close_ts: int, tag: str, exp: int, market_obj: dict,
              trip_no: int, spot_ticks: list, side_cap: Optional[int] = None,
              restrict_side: Optional[str] = None, signal_side: Optional[str] = None,
              snapshot_spot: Optional[float] = None):
    """§1 fill-clock — one managed position. WO-PREDATOR: side_cap governs the join ceiling
    (A2); a gated re-entry restricts to restrict_side; penny-lead (B1) posts join+1 when spot
    has led the stale book in the signal direction (maker; falls back to join on a cross); the
    counterfactual fraction take is logged (B2); and a locked-margin leg vetoes a trigger-(b)
    scratch in the final ≤60s (B3). Returns (outcome, realized, side, entry_cents, meta)."""
    if side_cap is None:
        side_cap = FLIP_SIDE_MAX
    now = time.time()
    try:
        lo, hi = kalshi.extract_boundaries(market_obj or {})
    except Exception:
        lo, hi = None, None
    strike = lo if (lo is not None and hi is None) else (hi if (hi is not None and lo is None) else None)
    meta = {"led": 0, "cf_frac_exit_cents": None, "cf_frac_exit_at": None, "locked_margin": 0}

    posted = {}          # side -> {"oid","price"}
    fills = {}           # side -> entry cents
    first_fill_ts = None
    entry_deadline = min(now + FLIP_ENTRY_SEC, close_ts - FLIP_CURFEW)
    while time.time() < entry_deadline and not fills:
        book = kalshi.fetch_orderbook(client, ticker)
        cur_spot = _spot()
        spot_ticks.append(cur_spot)
        for side in ("yes", "no"):
            if restrict_side and side != restrict_side:
                continue                                 # A2: a gated re-entry joins ONE side
            if side in posted:
                continue
            join = book.yes_bid if side == "yes" else book.no_bid
            fp_str = book.yes_bid_fp if side == "yes" else book.no_bid_fp
            if join is None or join > side_cap:
                continue
            if posted and next(iter(posted.values()))["price"] + join > FLIP_LINE:
                continue
            if _is_observe():
                notify.send(f"🔁 W{tag} T{trip_no} — would post {side.upper()}@{join} (observe)")
                continue
            # B1 penny-lead — spot led the stale book in the signal direction → post join+1
            led = False
            if (signal_side == side and snapshot_spot is not None and cur_spot is not None
                    and join + 1 <= side_cap):
                moved = (cur_spot - snapshot_spot) if side == "yes" else (snapshot_spot - cur_spot)
                led = moved >= FLIP_LEAD_TICK
            if led:
                try:
                    ss, pp = flip_math.entry_args(side, join + 1)
                    oid, _ = kalshi.place_order_maker(client, ticker, ss, pp, 1, expiration_ts=exp)
                    meta["led"] = 1
                    posted[side] = {"oid": oid, "price": join + 1}
                    notify.send(f"🎯 W{tag} T{trip_no} — led {side.upper()}@{join + 1} (spot ahead)")
                    continue
                except Exception as e:
                    if not _is_cross_400(e):             # non-cross error → give up this side
                        notify.send(f"⚠ W{tag} T{trip_no} lead {side.upper()} rejected: {e}")
                        continue
                    # join+1 would cross → fall back to the plain join (led stays 0)
            oid = _post_entry(client, ticker, side, join, fp_str, exp, tag)
            if oid is None:
                continue
            posted[side] = {"oid": oid, "price": join}
        for side in list(posted):
            if side not in fills:
                fp = _order_fill_price(client, ticker, posted[side]["oid"], side)
                if fp is not None:
                    fills[side] = fp
                    first_fill_ts = time.time()
                    _record_enter(ticker, close_ts, side, fp)
                    notify.send(f"🌱 W{tag} T{trip_no} — filled {side.upper()}@{fp}")
        if not fills:
            engine.heartbeat()
            time.sleep(FLIP_POLL_SEC)

    if _is_observe():
        return "observe", 0, None, None, meta
    if not fills:
        if posted:
            kalshi.cancel_all_for_market(client, ticker)
        return None, 0, None, None, meta

    if len(fills) == 2:                                   # both legs filled same tick → netted
        y, n = fills["yes"], fills["no"]
        cap = 100 - (y + n)
        notify.send(f"💰 W{tag} T{trip_no} — both filled {y}+{n} → netted rung A +{cap}¢")
        return "netted", int(cap), "yes", y, meta

    held_side, entry = next(iter(fills.items()))
    other = "no" if held_side == "yes" else "yes"

    # THE TAKE — a passive resting exit at entry+X, posted AT the fill (with expiry)
    q = entry + FLIP_X
    tes, tep = flip_math.exit_args(held_side, q)
    try:
        take_oid, _ = kalshi.place_order_maker(client, ticker, tes, tep, 1, expiration_ts=exp)
        notify.send(f"🎯 W{tag} T{trip_no} — {held_side.upper()}@{entry}, take @{q} posted")
    except Exception as e:                               # never let a take rejection escape
        take_oid = None
        notify.send(f"⚠ W{tag} T{trip_no} take post failed: {e} — managing via scratch/T-90")

    grace_end = first_fill_ts + FLIP_PAIR_GRACE
    mo_rec = {"m10": None, "m30": None, "m60": None}
    prev_mo = None
    adverse_polls = 0
    close_samples = []                                   # B3: spot samples approaching the close
    riding_locked = False                                # B3: a proven winner riding to settlement

    def _cancel(*oids):
        for oid in oids:
            if oid:
                try:
                    kalshi.cancel_order(client, oid)
                except Exception:
                    pass

    def _log_cf(mark, age):
        # B2: the fraction rule (exit at profit>15% of cost OR gainFraction>50% of max gain) —
        # a pure LOGGER, zero behavior change. Record the first moment it would have exited.
        if meta["cf_frac_exit_cents"] is not None or mark is None or entry <= 0:
            return
        gain = mark - entry
        if (gain / entry) > 0.15 or (gain / max(1, 100 - entry)) > 0.50:
            meta["cf_frac_exit_cents"] = int(gain)
            meta["cf_frac_exit_at"] = round(age, 1)

    while True:
        secs_left = close_ts - time.time()
        if not riding_locked and secs_left <= FLIP_FLAT_AT:
            # B3 at the T-90 backstop: a PROVABLY-locked winner rides to settlement (flattening
            # a guaranteed win for breakeven is strictly worse); every other leg flattens now —
            # the T-90 backstop is unchanged for the normal case.
            if (strike is not None and close_samples and flip_pricebrain.locked_margin(
                    held_side, strike, close_samples, secs_left, FLIP_MAX_MOVE_PER_S, FLIP_POLL_SEC)):
                riding_locked = True
                meta["locked_margin"] = 1
                _cancel(posted.get(other, {}).get("oid"), take_oid)
                notify.send(f"🔒 W{tag} T{trip_no} — locked at T-{int(secs_left)}s; "
                            f"riding {held_side.upper()} to settlement")
            else:
                break
        if riding_locked and secs_left <= 2:
            break
        engine.heartbeat()
        book = kalshi.fetch_orderbook(client, ticker)
        spot = _spot()
        spot_ticks.append(spot)
        age = time.time() - first_fill_ts
        leg_mark = book.yes_bid if held_side == "yes" else book.no_bid
        mo = (leg_mark - entry) if leg_mark is not None else None
        if mo_rec["m10"] is None and age >= 10:
            mo_rec["m10"] = mo
        if mo_rec["m30"] is None and age >= 30:
            mo_rec["m30"] = mo
        if mo_rec["m60"] is None and age >= 60:
            mo_rec["m60"] = mo
        _log_cf(leg_mark, age)
        if secs_left <= FLIP_CLOSE_WINDOW_S + FLIP_FLAT_AT and spot is not None:
            close_samples.append(spot)                   # B3: accrue the close-window estimate

        if not riding_locked:
            # PAIR GRACE — the opposite entry bid keeps working; a second fill nets rung A
            if other in posted and other not in fills and time.time() <= grace_end:
                fp2 = _order_fill_price(client, ticker, posted[other]["oid"], other)
                if fp2 is not None:
                    fills[other] = fp2
                    _record_enter(ticker, close_ts, other, fp2)
                    _cancel(take_oid)                 # netted flat → the take would un-flatten us
                    cap = 100 - (entry + fp2)
                    notify.send(f"💰 W{tag} T{trip_no} — pair grace fill {other.upper()}@{fp2} "
                                f"→ netted rung A +{cap}¢")
                    store.insert_markout(first_fill_ts, ticker, held_side, entry, **mo_rec)
                    return "netted", int(cap), held_side, entry, meta
            elif other in posted and other not in fills and time.time() > grace_end:
                _cancel(posted[other]["oid"])         # grace over → drop the opposite entry only
                posted.pop(other, None)

            # THE TAKE filled → +X
            tfp = _order_fill_price(client, ticker, take_oid, tes)
            if tfp is not None:
                _record_enter(ticker, close_ts, tes, tfp, why_tag="FLIP_EXIT")
                notify.send(f"🎯 W{tag} T{trip_no} — take filled {held_side.upper()} → +{FLIP_X}¢")
                store.insert_markout(first_fill_ts, ticker, held_side, entry, **mo_rec)
                return "take", FLIP_X, held_side, entry, meta

        # SCRATCH — cross out NOW on any adverse signal
        adverse_polls = adverse_polls + 1 if _spot_adverse(held_side, spot, lo, hi) else 0
        reason = _scratch_reason(held_side, entry, leg_mark, mo_rec["m30"], prev_mo, adverse_polls)
        prev_mo = mo
        if reason and riding_locked:
            # B3 veto — a locked leg ignores last-seconds spot noise; only announced in the
            # final ≤60s (the veto never fires before T-60).
            if "strike" in reason and secs_left <= FLIP_CLOSE_WINDOW_S:
                notify.send(f"🔒 W{tag} T{trip_no} — scratch vetoed (locked, T-{int(secs_left)}s "
                            f"— avg cannot flip)")
            adverse_polls = 0
        elif reason:
            _cancel(posted.get(other, {}).get("oid"), take_oid)   # opposite entry FIRST, then take
            posted.pop(other, None)
            notify.send(f"✂️ W{tag} T{trip_no} — scratch {held_side.upper()}@{entry}: {reason}")
            flat_px, _f = _decline_lone(client, ticker, close_ts, tag, held_side, entry)
            realized = (100 - flat_px - entry) if flat_px is not None else -entry
            store.insert_markout(first_fill_ts, ticker, held_side, entry, **mo_rec)
            return "scratched", int(realized), held_side, entry, meta

        time.sleep(FLIP_POLL_SEC)

    store.insert_markout(first_fill_ts, ticker, held_side, entry, **mo_rec)
    if riding_locked:                                    # settled a proven winner (backfill books it)
        notify.send(f"🔒 W{tag} T{trip_no} — {held_side.upper()}@{entry} rode locked to settlement")
        return "rode_locked", int(100 - entry), held_side, entry, meta
    # T-90 reached with the take resting — flatten to end the trip bounded (reuse executor)
    _cancel(posted.get(other, {}).get("oid"), take_oid)
    posted.pop(other, None)
    notify.send(f"⏱ W{tag} T{trip_no} — T-90, take unfilled → flatten {held_side.upper()}@{entry}")
    flat_px, _f = _decline_lone(client, ticker, close_ts, tag, held_side, entry)
    realized = (100 - flat_px - entry) if flat_px is not None else -entry
    return "scratched", int(realized), held_side, entry, meta


def _run_ratchet_window(client, ticker: str, close_ts: int, tag: str, exp: int,
                        market_obj: dict) -> Optional[str]:
    """§2 the ratchet — up to FLIP_MAX_TRIPS managed positions per window, ONE at a time (R1
    wall), re-entering only on the OFI gate before the curfew. Governors: sit out at
    FLIP_SCRATCH_SITOUT scratches, window stop (salvage-aware). The magnitude-aware tail-kill
    and hourly budget stand elsewhere."""
    trips, scratches, window_realized = 0, 0, 0
    outcomes, spot_ticks = [], []

    # A3 sit-out: 4 scratches OR a window loss ≥ stop, whichever first (loss budget primary).
    while (trips < FLIP_MAX_TRIPS and scratches < FLIP_SCRATCH_SITOUT
           and window_realized > -FLIP_STOP_CENTS
           and (close_ts - time.time()) > FLIP_CURFEW):
        if not _r1_flat(client, ticker, tag):          # R1 wall — must be flat to open a trip
            break

        secs = close_ts - time.time()
        band = "open" if secs > 600 else ("mid" if secs > 300 else "late")
        # first trip = the blind open watch (side_max 49, both sides); re-entries are OFI-gated
        # (ratchet cap 58, single side), with the gate's spot snapshot for the penny-lead.
        side_cap, restrict_side, signal_side, snap, ofi_mode = FLIP_SIDE_MAX, None, None, None, "open"
        if trips > 0:                                  # re-entry needs the OFI gate's blessing
            book = kalshi.fetch_orderbook(client, ticker)
            snap = _spot()
            spot_ticks.append(snap)
            gated = _ofi_side(spot_ticks, _book_lean(book))
            if gated is None:
                engine.heartbeat()
                time.sleep(FLIP_POLL_SEC)
                continue                               # no signal → wait (never fade the tape)
            side_cap, restrict_side, signal_side, ofi_mode = (
                FLIP_RATCHET_SIDE_MAX, gated, gated, f"net{FLIP_OFI_TICKS // 2 + 1}{FLIP_OFI_TICKS}")
            notify.send(f"🎯 W{tag} — tape agrees → re-enter {gated.upper()} side (cap {side_cap})")

        outcome, realized, side, entry, meta = _run_trip(
            client, ticker, close_ts, tag, exp, market_obj, trips + 1, spot_ticks,
            side_cap=side_cap, restrict_side=restrict_side, signal_side=signal_side,
            snapshot_spot=snap)
        if outcome == "observe":
            return "flip_observe"
        if outcome is None:                            # no fill this trip → window is done
            break
        trips += 1
        window_realized += realized
        outcomes.append(outcome)
        try:
            store.insert_trip(time.time(), ticker, tag, side or "", int(entry or 0),
                              outcome, int(realized), led=meta.get("led"), ofi_mode=ofi_mode,
                              side_cap=side_cap, curfew_band=band,
                              cf_frac_exit_cents=meta.get("cf_frac_exit_cents"),
                              cf_frac_exit_at=meta.get("cf_frac_exit_at"),
                              locked_margin=meta.get("locked_margin"))
        except Exception as e:
            notify.send(f"⚠ W{tag} trip row failed: {e}")
        if outcome == "scratched":
            scratches += 1
        if window_realized <= -FLIP_STOP_CENTS:        # salvage-aware window stop (primary governor)
            notify.send(f"🛑 W{tag} — window stop {window_realized}¢ ≤ −{FLIP_STOP_CENTS} → sit out")
            break

    if scratches >= FLIP_SCRATCH_SITOUT:
        notify.send(f"🪑 W{tag} — {scratches} scratches → sitting the window out")

    stopped = window_realized <= -FLIP_STOP_CENTS
    try:
        store.insert_flip_window(
            ts=time.time(), close_ts=int(close_ts), tag=tag, ticker=ticker,
            realized_cents=int(window_realized),
            outcome_tag=("STOPPED" if stopped else ("RATCHET" if trips else "SAT_no_fill")),
            broker_flat=(1 if _r1_flat(client, ticker, tag, silent=True) else 0),
        )
    except Exception as e:
        notify.send(f"⚠ W{tag} ratchet window row failed: {e}")
    _bump_stop_streak(stopped)

    sign = "+" if window_realized >= 0 else ""
    notify.send(f"🔁 W{tag} — {trips} trips [{' '.join(outcomes) or 'none'}] → "
                f"{sign}{window_realized}¢ | DONE")
    return "flip_done"
