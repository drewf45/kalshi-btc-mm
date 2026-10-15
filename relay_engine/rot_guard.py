"""WO-ROT-1 Chunk 8 — the break-even guard, the runaway cap, the go-live gates.

BREAK-EVEN GUARD. Per band, over its last ROT_GUARD_TRIPS booked trips (of
the run's own kind: live trips judge a live desk, shadow trips a shadow desk):
if h < h* = L / (W + L) the band stops taking new entries and Drew is paged
with the numbers. L includes taker fees on hard stops (net is after fees).
Drew reopens with /rot_reopen <band> — an ACCOUNTING-ONLY command: it flips
the flag and restarts the band's evidence window (trips after the reopen are
the ones judged next), it never places an order.

RUNAWAY CAP (LENS). Every live ROT entry rides one Kalshi order group whose
contracts limit is 3x the largest single position. If fills pass it within
15 s, Kalshi itself cancels every resting entry in the group and rejects new
ones until reset. The desk pages Drew and halts entries (ROT_RUNAWAY); only
Drew resets (/rot_group_reset). Order-group API shapes are UNPROVEN until
seen on Kalshi demo.

GO-LIVE GATES. The shadow day's verdict (per band h vs h*) rides the daily
pack with the bundle; live needs the 40-49 band above break-even on it. The
first live day runs at ROT_FIRST_LIVE_DEPLOY_CAP_PCT (25%); the full cap
(50%) opens after one completed live day above break-even.
"""

import json
import logging
import math
import time
from typing import Dict, List, Optional

from . import config, rot_trips

log = logging.getLogger("relay.rot_guard")

PAUSED_KEY = "rot_paused_bands"            # {band: {"ts":..., "why":...}}
REOPENED_KEY = "rot_band_reopened"         # {band: ts} — the evidence window start
PROMOTED_KEY = "rot_live_promoted"         # ts the full deploy cap opened
GROUP_KEY = "rot_order_group_id"
RUNAWAY_HALT = "ROT_RUNAWAY"
BANDS = ("40-49", "50-54")


def _load(ledger, key) -> dict:
    raw = ledger.get_state(key)
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except ValueError:
        log.error("state %s unreadable: %r", key, raw[:200])
        return {}


def paused_bands(ledger) -> set:
    return set(_load(ledger, PAUSED_KEY))


def band_window_stats(ledger, band: str, shadow: Optional[int] = None):
    """The band's stats over its last ROT_GUARD_TRIPS booked trips since its
    last reopen, of the given kind (1 shadow / 0 live / None both); None while
    it has fewer."""
    since = float(_load(ledger, REOPENED_KEY).get(band, 0.0))
    return rot_trips.band_stats(ledger.db, band, config.ROT_GUARD_TRIPS,
                                since_ts=since, shadow=shadow)


def evaluate(ledger, desk, alert, shadow: Optional[int] = None,
             now: Optional[float] = None) -> List[str]:
    """Run the guard. Pauses (persisted) and pages each band that falls under
    h*. Returns the bands newly paused."""
    now = time.time() if now is None else now
    paused = _load(ledger, PAUSED_KEY)
    newly = []
    for band in BANDS:
        if band in paused:
            continue
        st = band_window_stats(ledger, band, shadow)
        if st is None:
            continue
        hs = st.h_star()
        if st.h < hs:
            why = (f"h {st.h:.3f} < h* {hs:.3f} over last {st.n} trips "
                   f"(W {st.W:.2f}c L {st.L:.2f}c incl. fees)")
            paused[band] = {"ts": now, "why": why}
            newly.append(band)
            from . import failures
            failures.fail("ROT_BAND_PAUSED", f"band {band} paused: {why}",
                          alert=False, band=band, h=st.h, h_star=hs, n=st.n)
            alert(f"⛔ ROT band {band} PAUSED — {why}. No new entries in it. "
                  f"Reopen: /rot_reopen {band}")
    if newly:
        ledger.set_state(PAUSED_KEY, json.dumps(paused))
    desk.paused_bands = set(paused)
    return newly


def reopen(ledger, desk, text: str, now: Optional[float] = None) -> str:
    """/rot_reopen <band> — flips the flag and restarts the band's evidence
    window. Accounting only: never places an order."""
    now = time.time() if now is None else now
    parts = text.split()
    if len(parts) < 2 or parts[1] not in BANDS:
        return f"usage: /rot_reopen <band> — bands: {', '.join(BANDS)}"
    band = parts[1]
    paused = _load(ledger, PAUSED_KEY)
    if band not in paused:
        return f"band {band} is not paused"
    paused.pop(band)
    ledger.set_state(PAUSED_KEY, json.dumps(paused))
    reo = _load(ledger, REOPENED_KEY)
    reo[band] = now
    ledger.set_state(REOPENED_KEY, json.dumps(reo))
    desk.paused_bands = set(paused)
    return (f"band {band} REOPENED — its next {config.ROT_GUARD_TRIPS} trips are "
            "judged fresh (no order placed)")


# ── the runaway cap: one Kalshi order group ──────────────────────

def group_limit(desk) -> Optional[int]:
    """3x the largest single position: the size at the cheapest band cost.
    None when not even one lot fits (no entries -> no group needed)."""
    size, _why = desk.size_for(config.ROT_ENTRY_BAND[0])
    if size is None:
        return None
    return int(math.ceil(float(size) * config.ROT_RUNAWAY_MULT))


def ensure_group(ledger, desk, client, alert) -> Optional[str]:
    """LIVE boot: create the order group (limit = 3x the largest position) and
    hand its id to the desk. Failure is loud and halts entries: the runaway
    rail is not optional."""
    from . import venue
    lim = group_limit(desk)
    if lim is None:
        log.warning("ROT order group: no lot fits the book — none created")
        return None
    try:
        gid = venue.create_order_group(client, lim)
    except Exception as e:
        gid = None
        log.warning("create_order_group failed: %s", e)
    if not gid:
        ledger.entry_halts.add(RUNAWAY_HALT)
        alert("⛔ ROT runaway rail: order group NOT created — entries halted "
              "(the rail is not optional). Check the API, then /rot_group_reset.")
        return None
    ledger.set_state(GROUP_KEY, gid)
    desk.order_group_id = gid
    alert(f"🛡 ROT order group {gid}: contracts limit {lim} "
          f"({config.ROT_RUNAWAY_MULT:g}x the largest position) — Kalshi cancels "
          "every resting entry if fills pass it within 15 s")
    return gid


def is_group_trip(err: str) -> bool:
    e = (err or "").lower()
    return "order group" in e or "order_group" in e


def tripped(ledger, alert, detail: str) -> None:
    if RUNAWAY_HALT in ledger.entry_halts:
        return
    ledger.entry_halts.add(RUNAWAY_HALT)
    from . import failures
    failures.fail("ROT_RUNAWAY_TRIPPED", f"order group tripped: {detail}"[:300],
                  alert=False)
    alert("🚨 ROT RUNAWAY CAP TRIPPED — Kalshi cancelled the resting entries and "
          f"refuses new ones ({detail[:120]}). Entries halted. Only Drew resets: "
          "/rot_group_reset")


def group_reset(ledger, desk, client) -> str:
    """/rot_group_reset — Drew's key. Resets the venue's order group (no order
    is placed) and lifts the ROT_RUNAWAY halt."""
    gid = getattr(desk, "order_group_id", None) or ledger.get_state(GROUP_KEY)
    if client is None:
        ledger.entry_halts.discard(RUNAWAY_HALT)
        return "no venue client (SHADOW): runaway halt cleared"
    if not gid:
        return "no order group on record — restart creates one"
    from . import venue
    try:
        ok = venue.reset_order_group(client, gid)
    except Exception as e:
        return f"order group reset FAILED ({e}) — halt stays"
    if not ok:
        return "order group reset not confirmed — halt stays"
    ledger.entry_halts.discard(RUNAWAY_HALT)
    return f"order group {gid} reset — entries resume"


# ── go-live gates ────────────────────────────────────────────────

def day_verdict(ledger, day_start: float, shadow: int) -> Dict[str, dict]:
    """Per band (and ALL) for one day's trips of a kind: n, h, h*, above."""
    out = {}
    _h, rows = rot_trips.sheet_rows(ledger.db, day_start, day_start + 86400,
                                    shadow=shadow)
    for r in rows:
        scope, key, n, _w, h, _W, _L, hs = r[:8]
        if scope in ("band", "total"):
            out[key] = {"n": n, "h": h, "h_star": hs,
                        "above": (h is not None and hs is not None and h >= hs)}
    return out


def verdict_lines(ledger, day_start: float, shadow: int) -> List[str]:
    v = day_verdict(ledger, day_start, shadow)
    kind = "SHADOW" if shadow else "LIVE"
    day = time.strftime("%Y-%m-%d", time.localtime(day_start))
    if not v:
        return [f"ROT {kind} VERDICT {day}: no booked trips"]
    lines = [f"ROT {kind} VERDICT {day} (h vs h* = L/(W+L)):"]
    for k in sorted(v, key=lambda x: (x == "ALL", x)):
        d = v[k]
        h = "n/a" if d["h"] is None else f"{d['h']:.3f}"
        hs = "n/a" if d["h_star"] is None else f"{d['h_star']:.3f}"
        lines.append(f"  {k}: n {d['n']} · h {h} · h* {hs} · "
                     f"{'ABOVE' if d['above'] else 'NOT above'} break-even")
    if shadow:
        ok = v.get("40-49", {}).get("above", False)
        lines.append("  GO-LIVE GATE (40-49 above break-even on the shadow day): "
                     + ("MET — live is Drew's call" if ok else "NOT MET — stay in shadow"))
    return lines


def deploy_cap(ledger) -> float:
    """The live desk's first day runs at ROT_FIRST_LIVE_DEPLOY_CAP_PCT; the full
    ROT_DEPLOY_CAP_PCT opens once a completed live day was above break-even
    (persisted). Shadow always uses the full cap (it rehearses the target)."""
    if not config.live_submit_enabled():
        return config.ROT_DEPLOY_CAP_PCT
    if ledger.get_state(PROMOTED_KEY):
        return config.ROT_DEPLOY_CAP_PCT
    return config.ROT_FIRST_LIVE_DEPLOY_CAP_PCT


def maybe_promote(ledger, day_start: float, alert) -> bool:
    """Called once a day for the completed day: a LIVE day whose ALL row is
    above break-even (and has at least ROT_PROMOTE_MIN_TRIPS) opens the full
    deploy cap. Returns True when it promoted."""
    if ledger.get_state(PROMOTED_KEY):
        return False
    v = day_verdict(ledger, day_start, shadow=0).get("ALL")
    if not v or v["n"] < config.ROT_PROMOTE_MIN_TRIPS or not v["above"]:
        return False
    ledger.set_state(PROMOTED_KEY, str(time.time()))
    alert(f"✅ ROT live day above break-even (n {v['n']}, h {v['h']:.3f} ≥ h* "
          f"{v['h_star']:.3f}) — deploy cap opens "
          f"{config.ROT_FIRST_LIVE_DEPLOY_CAP_PCT:.0%} → {config.ROT_DEPLOY_CAP_PCT:.0%}")
    return True
