"""k_worker/flip_mode.py — WO-LANE-FLIP: the flip doctrine as a MODE in the proven engine.

Enabled ONLY when env KW_MODE=FLIP (default OFF — the engine's directional lanes are
untouched and dormant unless opted in). Reuses the proven organs verbatim:
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

KW_MODE = os.environ.get("KW_MODE", "").strip().upper()
ENABLED = KW_MODE == "FLIP"

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


def _record_enter(ticker: str, close_ts: int, held_side: str, fill_cents: int) -> None:
    """Record the entry as the engine's own ENTER row so reconcile/settlement/treasury
    resolve and book it exactly as they do for the directional lanes (no new accounting)."""
    try:
        store.insert_row(store.SurfaceRow(
            market_ticker=ticker, decision_ts=time.time(), action="ENTER",
            side=held_side, cost_per_contract_cents=int(fill_cents),
            fill_cost_cents=int(fill_cents), fill_ts=time.time(), contracts=1,
            order_type="maker", why_tag="FLIP_ENTER", lane="F", env="live",
            seconds_to_expiry=close_ts - time.time(),
        ))
    except Exception as e:
        notify.send(f"⚠ flip enter-row failed {ticker} {held_side}: {e}")


def _bump_stop_streak(stopped: bool) -> None:
    """Two consecutive stopped windows → pause (reuse the engine's halt state)."""
    streak = int(store.get_state(_STOP_STREAK_KEY) or "0")
    streak = streak + 1 if stopped else 0
    store.set_state(_STOP_STREAK_KEY, str(streak))
    if stopped and streak >= FLIP_PAUSE_AFTER_STOPS:
        store.set_state("halted", f"flip: {streak} consecutive stopped windows")
        notify.alert(f"🛑 FLIP PAUSE — {streak} consecutive stopped windows. "
                     f"Run `python -m k_worker.reset` to resume.")


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
    notify.send(f"🔁 W{tag} — quoting {yb}+{nb}={cost} (both @join, exp T-{FLIP_FLAT_AT})")

    # ENTRY PHASE — poll for fills
    deadline = now + FLIP_ENTRY_SEC
    fills = {}   # held_side -> entry cents
    while time.time() < deadline and len(fills) < 2:
        for hs, oid in (("yes", yes_oid), ("no", no_oid)):
            if hs not in fills:
                fp = _order_fill_price(client, ticker, oid, hs)
                if fp is not None:
                    fills[hs] = fp
                    _record_enter(ticker, close_ts, hs, fp)
                    notify.send(f"🌱 W{tag} — filled {hs.upper()}@{fp}")
        if len(fills) < 2:
            time.sleep(FLIP_POLL_SEC)

    # cancel any unfilled entry bids
    if len(fills) < 2:
        kalshi.cancel_all_for_market(client, ticker)

    if not fills:
        notify.send(f"⏭ W{tag} — no fill ≤ line | SAT")
        return "flip_sat_nofill"

    # lone-leg keepability (WALL): a lone leg above the max means the line was misjudged
    if len(fills) == 1:
        (hs, hp), = fills.items()
        if hp > FLIP_LONE_MAX:
            notify.alert(f"⚠ W{tag} lone {hs.upper()}@{hp} > {FLIP_LONE_MAX} — line misjudged "
                         f"(shouldn't occur at 1-lot join)")

    # POST EXITS — flip each held leg at entry+X via the complement buy, exp T-90
    exit_oids = {}
    for hs, hp in fills.items():
        q = hp + FLIP_X
        es, ep = flip_math.exit_args(hs, q)
        oid, _ = kalshi.place_order_maker(client, ticker, es, ep, 1, expiration_ts=exp)
        exit_oids[hs] = [oid, q]
    notify.send(f"🔁 W{tag} — {'+'.join(str(v) for v in fills.values())} held, "
                f"exits posted @+{FLIP_X}")

    flipped = {}
    # MONITOR EXITS until T-90
    while (close_ts - time.time()) > FLIP_FLAT_AT and exit_oids:
        for hs in list(exit_oids):
            fp = _order_fill_price(client, ticker, exit_oids[hs][0], "yes" if hs == "no" else "no")
            if fp is not None:
                flipped[hs] = exit_oids[hs][1]
                del exit_oids[hs]
                notify.send(f"🌱 W{tag} — flipped {hs.upper()} → {flipped[hs]}")
        if exit_oids:
            time.sleep(FLIP_POLL_SEC)

    # T-90 SWEEP — cancel any unfilled exit and re-post the complement at the CURRENT
    # touch (maker join, still T-90 expiry). If it fills → flat; if not, an intact bundle
    # rides its floor and a lone leg rides settlement (both resolved by the engine's
    # settlement backfill). No taker is added.
    if exit_oids:
        kalshi.cancel_all_for_market(client, ticker)
        book2 = kalshi.fetch_orderbook(client, ticker)
        for hs in list(exit_oids):
            comp = "no" if hs == "yes" else "yes"
            comp_bid = book2.no_bid if comp == "no" else book2.yes_bid
            comp_fp = book2.no_bid_fp if comp == "no" else book2.yes_bid_fp
            if comp_bid is not None:
                try:
                    kalshi.place_order_maker(client, ticker, comp, comp_bid, 1,
                                             expiration_ts=exp, v2_price_str=comp_fp)
                    notify.send(f"🔁 W{tag} — T-90 rejoin {hs.upper()} exit @touch")
                except Exception as e:
                    notify.send(f"⚠ W{tag} T-90 rejoin failed: {e}")

    # DONE line + stop tracking (realized flip cents; settlement backfill books the rest)
    realized = sum((flipped[hs] - fills[hs]) for hs in flipped)
    kind = ("flips %d/%d" % (len(flipped), len(fills))) if flipped else "held→floor/settle"
    stopped = realized <= -FLIP_STOP_CENTS
    _bump_stop_streak(stopped)
    entry_str = "+".join(str(v) for v in fills.values())
    flip_str = "/".join(str(flipped[hs]) for hs in flipped) if flipped else "—"
    sign = "+" if realized >= 0 else ""
    notify.send(f"🔁 W{tag} — {entry_str}={cost}, {kind} @{flip_str} → {sign}{realized}¢ "
                f"realized (rest → settlement) | DONE")
    return "flip_done"
