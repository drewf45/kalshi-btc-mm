"""Fills loop (P3.1) — the single point where money-truth enters the ledger.

The Adversary's wall: five lanes writing fills through an unproven booking
path is the one way the training run lies to its own curriculum — so this
module ships WITH its booking tests before any lane beyond F/H8 lands.

Booking law:
  - every fill books EXACTLY ONCE (fill ids persisted in booked_fills)
  - attribution comes from the gateway's order_index (lane recorded at
    submit, never inferred from price bands)
  - partial fills book their own counts; position effects go through
    gateway.on_fill(order_id, count)
  - FOREIGN fills (order ids we never placed) are counted and logged, not
    booked: until cutover the live Kal shares this account and its fills are
    not ours to claim. A NONZERO foreign count after cutover is an integrity
    alarm the pack must surface.
  - a fill whose price cannot be parsed books at the order's rest price and
    ALERTS (the venue module logs the raw record once, self-diagnosing).

Win/loss path symmetry: entries and exits book through the same sweep with
the same dedup and the same attribution — no path is privileged.
"""

import logging
import time
from typing import Dict, List, Optional

from . import venue
from .surface import ENTERED, EXITED

log = logging.getLogger("relay.fills")

BOOKED_SCHEMA = """
CREATE TABLE IF NOT EXISTS booked_fills (
    fill_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    order_id TEXT NOT NULL,
    count INTEGER NOT NULL
);
"""

PURPOSE_TO_ACTION = {"ENTRY": "ENTRY", "EXIT": "EXIT", "CUT": "CUSTODIAN_EXIT"}


def _fill_id(record: dict) -> Optional[str]:
    for k in ("fill_id", "trade_id", "id"):
        v = record.get(k)
        if v:
            return str(v)
    # deterministic fallback: order id + created time + count
    oid = record.get("order_id")
    if oid:
        return f"{oid}:{record.get('created_time', record.get('ts', ''))}:{record.get('count', '')}"
    return None


class FillBooker:
    def __init__(self, gateway, ledger, surface, custodian=None, alert_fn=None,
                 on_booked=None):
        self.gateway = gateway
        self.ledger = ledger
        self.surface = surface
        self.custodian = custodian
        self.alert = alert_fn or (lambda msg: None)
        # on_booked(order, action, price_cents, count, now): lane callbacks
        # (FLIP's pair/trip accounting rides here)
        self.on_booked = on_booked or (lambda *a: None)
        self.ledger.db.executescript(BOOKED_SCHEMA)
        self.ledger.db.commit()
        self.foreign_seen = 0

    def _already_booked(self, fill_id: str) -> bool:
        return self.ledger.db.execute(
            "SELECT 1 FROM booked_fills WHERE fill_id=?", (fill_id,)).fetchone() is not None

    def sweep(self, fill_records: List[dict], now: Optional[float] = None) -> Dict[str, int]:
        """Book a batch of venue fill records. Returns counters
        {booked, duplicate, foreign, unparsed_price}."""
        now = time.time() if now is None else now
        stats = {"booked": 0, "duplicate": 0, "foreign": 0, "unparsed_price": 0}
        for record in fill_records:
            fid = _fill_id(record)
            oid = str(record.get("order_id", ""))
            if fid is None or not oid:
                stats["foreign"] += 1
                continue
            order = self.gateway.order_index.get(oid)
            if order is None:
                # Not ours: live Kal shares the account until cutover.
                stats["foreign"] += 1
                self.foreign_seen += 1
                continue
            if self._already_booked(fid):
                stats["duplicate"] += 1
                continue

            cost, fee_cents, count = venue.parse_fill(record, order.side)
            if cost is None:
                cost = float(order.price_cents)
                stats["unparsed_price"] += 1
                self.alert(f"fill {fid} on {order.market}: price unparsed — "
                           f"booked at rest price {order.price_cents}c")
            count = max(1, count)

            action = PURPOSE_TO_ACTION.get(order.purpose, order.purpose)
            self.ledger.record_fill(order.market, order.lane, order.side, action,
                                    int(round(cost)), count, order.size_tier)
            self.gateway.on_fill(oid, count=count)
            self.ledger.db.execute(
                "INSERT INTO booked_fills (fill_id, ts, order_id, count) VALUES (?,?,?,?)",
                (fid, now, oid, count))
            self.ledger.db.commit()

            window = f"w-{order.market}"
            state = ENTERED if action == "ENTRY" else EXITED
            self.surface.write_row(order.lane, order.market, window, state,
                                   detail=f"fill={fid} @{int(round(cost))}c x{count} fee={fee_cents}c")

            if action == "ENTRY" and self.custodian is not None:
                from .custodian import OpenPosition
                self.custodian.adopt(OpenPosition(
                    event=order.event, market=order.market, lane=order.lane,
                    side=order.side, count=count,
                    entry_price_cents=int(round(cost)), entry_p_win=0.0,
                    size_tier=order.size_tier, entry_time=now))
            stats["booked"] += 1
            self.on_booked(order, action, int(round(cost)), count, now)
            log.warning("FILL BOOKED %s %s %s %s %d@%dc fee=%dc",
                        order.lane, order.market, order.side, action,
                        count, int(round(cost)), fee_cents)
        return stats

    def reconcile_sweep(self, client) -> Dict[str, int]:
        """The periodic account-wide sweep: pulls recent fills for ALL tickers
        and books anything ours the per-order paths missed. This is the seatbelt
        under the seatbelt — it must run before trusting any daily pack."""
        records = venue.get_all_recent_fills(client)
        stats = self.sweep(records)
        if stats["foreign"]:
            log.info("[RECONCILE] %d foreign fills (live Kal's until cutover)",
                     stats["foreign"])
        return stats
