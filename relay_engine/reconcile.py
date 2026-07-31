"""F6 — LIVE boot reconcile (law 7): money truth enters BEFORE the first cycle.

In LIVE mode the ledger baselines from the venue — balance, positions, resting
orders — and every unexplained delta routes through the cash protocol (which
halts entries and pages Drew; his confirmation is consent). Shadow mode never
calls this (the paper baseline path is its own, proven).

Positions found at boot that our own fills cannot explain are QUARANTINED:
alerted, counted, never adopted as tradeable attribution — lane truth comes
from our fills table, never from a cost-band guess (the misattribution marker
died in 3.3). Foreign resting orders are alerted, never cancelled (they may be
the old engine's during transition).

Win/loss path symmetry: surpluses and deficits both route through the same
cash-protocol reconcile; recovered positions are custodied the same whichever
side of profit they sit on.

SALV-2 — THE ANCHOR SURVIVES RESTARTS. Every adoption here computes the
salvage anchor from CURRENT spot + delta table (the entry path's own
derivation, engine._salvage_anchor — reused, never reimplemented); if spot
or table is unavailable the position adopts anchorless but LOUDLY
(SALVAGE_DISABLED_TAGGED via the SALV-1 tape). DOCUMENTED LIMITATION
(Engineer, accepted): an adoption-time anchor measures collapse from
custody start, not original entry — salvage on a re-adopted mid-fall
position triggers later than the original anchor would have. Honest-but-
weaker; strictly better than disabled. BOUNDED BY DESIGN (Adversary): the
catastrophic backstop reads no anchor — a crash-loop re-anchor cannot
postpone the 5% floor.
"""

import logging
import time

from . import failures, venue
from .errors import FatalIntegrityError

log = logging.getLogger("relay.reconcile")


def _adoption_anchor(engine, ticker: str, side: str):
    """SALV-2 §1: the anchor at adoption time — the ENTRY path's exact
    derivation (engine._salvage_anchor: current spot + strike + delta
    table). Returns ((d, t, p) | None, miss_reason | None)."""
    try:
        anchor = engine._salvage_anchor(ticker, side)
    except Exception as e:
        return None, f"anchor derivation raised: {e}"
    if isinstance(anchor, tuple):
        return anchor, None
    return None, (anchor if isinstance(anchor, str) else "unknown")


def _orphan_mark_cents(engine, client, ticker: str, side: str):
    """SALV-2 §3: an ORPHAN's entry price is the venue mark for the held
    side when a book/record is readable — 50c only as a LOGGED fallback
    (real dollars stop being priced at a fiction)."""
    book = engine.feed.books.get(ticker)
    if book is not None:
        bid = book.best_yes_bid() if side == "yes" else book.best_no_bid()
        if bid is not None:
            return int(bid), "book"
    try:
        from decimal import Decimal
        rec = venue.get_market(client, ticker)
        v = rec.get("yes_bid_dollars")
        if v is not None:
            yes_bid = int(Decimal(str(v)) * 100)
            return (yes_bid if side == "yes" else 100 - yes_bid), "record"
    except Exception:
        pass
    log.warning("ORPHAN_MARK_FALLBACK %s: no readable book or record — "
                "entry priced at the 50c fallback", ticker)
    return 50, "fallback_50"


def live_boot_reconcile(engine, client) -> dict:
    """Returns a summary dict; raises FatalIntegrityError on unreadable truth."""
    cash, pv = venue.get_balance(client)
    if cash is None:
        failures.fail("LIVE_BOOT_BALANCE_UNREADABLE",
                      "LIVE boot: venue balance unreadable — cannot baseline, "
                      "refusing to run", fatal=True)
    venue_cents = int(round(cash * 100))

    summary = {"venue_balance_cents": venue_cents, "cash_state": None,
               "positions_recognized": 0, "positions_quarantined": 0,
               "foreign_resting": 0}

    if engine.cash.fatal:
        # P-CASH-FATAL-1 §4.3: a denied delta may NOT be silently
        # re-baselined to the venue — the operator's stop outranks the
        # boot baseline (the deny-reboot breach was exactly this line
        # running without this guard). Entries stay walled; custody of
        # EXISTING risk continues below (the halt stops new risk only).
        summary["cash_state"] = "FATAL_RESTORED"
        log.warning("LIVE boot reconcile: CASH FATAL active — refusing to "
                    "baseline; /clear_cash_fatal is the only key")
    elif engine.ledger.book_cents() == 0:
        # first live boot: the venue balance IS the baseline
        engine.ledger.baseline(venue_cents, confirmed_by="live_boot")
        # CHUNK D (dry-run-caught): boot() snapshotted caps BEFORE this
        # baseline — at book 0 the PCT_OF_BOOK budget is 0 and every entry
        # would be rejected until the next restart. A confirmed baseline
        # re-snapshots the caps (C.2: confirmed movements re-baseline).
        engine.boot_caps = engine.ledger.snapshot_caps_at_boot()
        summary["cash_state"] = "BASELINED"
    else:
        # every later boot: unexplained deltas are the cash protocol's business
        state = engine.cash.reconcile(
            venue_cents,
            in_flight_orders=len(engine.gateway.resting),
            unsettled_fills=engine.ledger.unsettled_fill_count())
        summary["cash_state"] = state

    # positions: recognized (our fills explain them) vs quarantined
    for p in venue.get_positions(client):
        ticker = str(p.get("ticker") or p.get("market_ticker") or "")
        net = 0
        for k in ("position", "net_position", "yes_position", "qty", "count"):
            if k in p:
                try:
                    net = int(p[k])
                    break
                except (ValueError, TypeError):
                    continue
        if not ticker or net == 0:
            continue
        row = engine.ledger.db.execute(
            "SELECT lane, side, price_cents, size_tier FROM fills"
            " WHERE market=? AND action='ENTRY' AND settled=0"
            " ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        if row is not None:
            lane, side, price_cents, size_tier = row
            from .custodian import OpenPosition
            # SALV-2 §1: re-adoption carries an anchor — computed from
            # CURRENT spot+table (custody-start baseline; see module
            # docstring for the accepted limitation). No anchor -> adopt
            # anyway, DISABLED LOUDLY via the SALV-1 tape.
            anchor, miss = _adoption_anchor(engine, ticker, side)
            d_e, t_e, p_e = anchor if anchor else (None, None, None)
            engine.custodian.adopt(OpenPosition(
                event=ticker.rsplit("-", 1)[0], market=ticker, lane=lane,
                side=side, count=abs(net), entry_price_cents=price_cents,
                entry_p_win=(p_e if p_e is not None
                             else price_cents / 100.0),
                size_tier=size_tier, entry_time=time.time(),
                d_entry=d_e, t_entry=t_e, p_entry=p_e),
                disabled_reason=(f"adoption anchor miss: {miss}"
                                 if anchor is None else None))
            key = (ticker.rsplit("-", 1)[0], ticker, lane)
            engine.gateway.positions[key] = net
            summary["positions_recognized"] += 1
        else:
            # RULING 1 (P15, ratified): ORPHAN ADOPTION — every dollar is
            # OWNED. A position our fills cannot explain is adopted under
            # lane ORPHAN: custodied to conclusion (D-grade cut params),
            # NEVER lane evidence (its fills/settlements attribute to ORPHAN
            # alone; no Wilson cell reads it). P14's no-fills-rows fallback
            # means its cuts act on the pos object, by design.
            summary["positions_quarantined"] += 1
            from .custodian import OpenPosition
            from .lane_d import d_cut_params
            side = "yes" if net > 0 else "no"
            engine.custodian.set_lane_params("ORPHAN", d_cut_params())
            # SALV-2 §3: the venue mark, not a fictional 50c; §1: anchored
            # from current spot+table like every other adoption.
            mark_cents, mark_src = _orphan_mark_cents(engine, client,
                                                      ticker, side)
            anchor, miss = _adoption_anchor(engine, ticker, side)
            d_e, t_e, p_e = anchor if anchor else (None, None, None)
            engine.custodian.adopt(OpenPosition(
                event=ticker.rsplit("-", 1)[0], market=ticker, lane="ORPHAN",
                side=side, count=abs(net), entry_price_cents=mark_cents,
                entry_p_win=(p_e if p_e is not None
                             else mark_cents / 100.0),
                size_tier="PROBE", entry_time=time.time(),
                d_entry=d_e, t_entry=t_e, p_entry=p_e),
                disabled_reason=(f"adoption anchor miss: {miss}"
                                 if anchor is None else None))
            log.info("ORPHAN %s adopted at %dc (%s)", ticker, mark_cents,
                     mark_src)
            engine.gateway.positions[(ticker.rsplit("-", 1)[0], ticker,
                                      "ORPHAN")] = net
            engine.surface.write_row("ORPHAN", ticker, f"w-{ticker}",
                                     "ORPHAN_ADOPTED", detail=f"net {net:+d}")
            failures.fail("ORPHAN_FOUND",
                          f"{ticker}: position {net:+d} has no fill of ours — "
                          f"adopted as ORPHAN (Ruling 1), custodied to "
                          f"conclusion, never lane evidence",
                          market=ticker, net=net, alert=False)
            engine.telegram.alert(
                f"🧾 ORPHAN adopted: {net:+d} on {ticker} — custodied to "
                f"conclusion (Ruling 1); never lane evidence")

    # resting orders: ours re-registered is future work at cutover; foreign alerted
    try:
        resp = client.request("GET", venue._orders_route,
                              params={"status": "resting", "limit": 200})
        for o in (resp or {}).get("orders", []):
            oid = str(o.get("order_id") or o.get("id") or "")
            if oid and oid not in engine.gateway.order_index:
                summary["foreign_resting"] += 1
        if summary["foreign_resting"]:
            engine.telegram.alert(
                f"LIVE BOOT: {summary['foreign_resting']} resting order(s) not ours — "
                f"left untouched (the old engine's during transition?)")
    except Exception as e:
        log.warning("LIVE boot: resting-orders sweep failed (non-fatal): %s", e)

    log.warning("LIVE BOOT RECONCILE: %s", summary)
    return summary
