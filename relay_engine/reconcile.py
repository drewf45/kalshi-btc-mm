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
"""

import logging
import time

from . import failures, venue
from .errors import FatalIntegrityError

log = logging.getLogger("relay.reconcile")


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

    if engine.ledger.book_cents() == 0:
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
            engine.custodian.adopt(OpenPosition(
                event=ticker.rsplit("-", 1)[0], market=ticker, lane=lane,
                side=side, count=abs(net), entry_price_cents=price_cents,
                entry_p_win=0.0, size_tier=size_tier, entry_time=time.time()))
            key = (ticker.rsplit("-", 1)[0], ticker, lane)
            engine.gateway.positions[key] = net
            summary["positions_recognized"] += 1
        else:
            summary["positions_quarantined"] += 1
            engine.telegram.alert(
                f"LIVE BOOT: position {net:+d} on {ticker} has NO fill of ours — "
                f"QUARANTINED (not adopted, not traded); resolve by hand")

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
