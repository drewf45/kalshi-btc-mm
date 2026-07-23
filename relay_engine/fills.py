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
        # on_booked(order, action, price_cents, count, now, fee_cents): lane
        # callbacks + the P13 §1 narration (FLIP's pair/trip accounting rides here)
        self.on_booked = on_booked or (lambda *a: None)
        # P19 §2.1: the salvage anchor — anchor_fn(market, side) returns
        # (d_entry, t_entry, p_entry) or None (table/spot absent at entry =
        # salvage disabled for that position, tagged). Wired by the runner.
        self.anchor_fn = lambda market, side: None
        self.ledger.db.executescript(BOOKED_SCHEMA)
        self.ledger.db.commit()
        # Tape 0718: the account's fill history re-scans every sweep — foreign
        # fills are counted ONCE per unique id, or the counter (and the pack's
        # NONZERO-AFTER-CUTOVER alarm) is noise. Bounded in-memory set.
        self.foreign_seen = 0
        self._foreign_ids_seen: set = set()
        self._last_foreign_logged = -1

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
                # Not ours: this account's history until cutover. Counted
                # ONCE per unique fill id (tape 0718: 200/sweep re-count).
                stats["foreign"] += 1
                if fid not in self._foreign_ids_seen:
                    self._foreign_ids_seen.add(fid)
                    self.foreign_seen += 1
                    if len(self._foreign_ids_seen) > 10_000:
                        self._foreign_ids_seen.clear()  # bound memory; ids re-count at worst
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
            # P21 A1 — THE NETTING MODEL: the venue nets one account's sides
            # (Drew, 0718: "Kalshi closed that loophole"). An opposite-side
            # BUY on a held market IS a net-down — booked as an EXIT of the
            # held side at 100−price, so fills-P&L matches broker truth (the
            # 10:45/11:30 WINDOW_ECON_DIVERGENCE was this stale model).
            record_side = order.side
            key = (order.event, order.market, order.lane)
            net = self.gateway.positions.get(key, 0)
            buy_dir = 1 if (order.side == "yes") == (order.action == "buy") else -1
            if action == "ENTRY" and net != 0 and (net > 0) != (buy_dir > 0):
                record_side = "yes" if net > 0 else "no"
                cost = 100 - cost
                action = "EXIT"
                from . import failures
                failures.fail("SELF_NET_BOOKED",
                              f"{order.market}: ENTRY-purpose opposite-side "
                              f"buy netted down the held {record_side} leg — "
                              f"booked as EXIT@{int(round(cost))}c "
                              f"(A2 wall should have refused this entry)",
                              market=order.market, lane=order.lane, alert=False)
            # P22 §1.1: exits close a unit of risk — the ledger banks the
            # cell outcome inside record_fill; FLIP's intent (OPEN/HUNT)
            # rides the exit reason / entry why so the cells split.
            from . import scoring
            # A-PLAYER B1 — HALF-CENT PRECISION: the venue prices in
            # half-cents above 90c; int(round(96.5)) truncated the residue
            # that accumulated into the day's phantom 1-2c cash deltas.
            # The BOOK stores the fill's exact cents (SQLite INTEGER
            # affinity keeps 96.5 as REAL, losslessly); narration and
            # custody may round, the ledger never does.
            self.ledger.record_fill(order.market, order.lane, record_side,
                                    action, cost, count,
                                    order.size_tier, fee_cents=fee_cents,
                                    cell_lane=scoring.cell_lane(
                                        order.lane,
                                        order.reason or order.why),
                                    requested_count=order.count,
                                    requested_price=order.price_cents)
            self.gateway.on_fill(oid, count=count)
            self.ledger.db.execute(
                "INSERT INTO booked_fills (fill_id, ts, order_id, count) VALUES (?,?,?,?)",
                (fid, now, oid, count))
            self.ledger.db.commit()

            window = f"w-{order.market}"
            state = ENTERED if action == "ENTRY" else EXITED
            anchor_note = ""

            if action == "ENTRY" and self.custodian is not None:
                from .custodian import OpenPosition
                # P24 §1 — THE ANCHOR NEVER GOES MISSING. The anchor_fn
                # returns (d, t, p) on a table hit, or a STRING naming which
                # organ missed (§1.2: spot|strike|close|table) — the 1715
                # class gets a named cause instead of a shrug.
                anchor = self.anchor_fn(order.market, order.side)
                miss = anchor if isinstance(anchor, str) else None
                if not isinstance(anchor, tuple):
                    anchor = None
                if anchor is None:
                    from . import failures
                    if cost > 0:
                        # §1.1: price-implied shield — the entry price IS a
                        # probability; salvage never runs shieldless. WARN
                        # class (the INFO-silence class retired).
                        anchor = (None, None, cost / 100.0)
                        failures.fail(
                            "ANCHOR_FROM_PRICE",
                            f"{order.market} {order.lane}: salvage anchor "
                            f"from ENTRY PRICE {int(round(cost))}c — "
                            f"anchor miss: {miss or 'unknown'}",
                            market=order.market, lane=order.lane,
                            cause=miss or "unknown")
                    else:
                        # no table AND no price — should be impossible
                        failures.fail(
                            "SHIELDLESS",
                            f"⚠ SHIELDLESS {order.market} {order.lane}: no "
                            f"anchor and no entry price — impossible class,"
                            f" audit this booking",
                            market=order.market, lane=order.lane)
                if anchor is not None:
                    d_e, t_e, p_e = anchor
                    anchor_note = (" anchor="
                                   + ("table" if d_e is not None else "price"))
                else:
                    d_e, t_e, p_e = (None,) * 3
                self.custodian.adopt(OpenPosition(
                    event=order.event, market=order.market, lane=order.lane,
                    side=order.side, count=count,
                    entry_price_cents=int(round(cost)),
                    # P24 §3: the zero in the reversal dies at the WRITER —
                    # entry_p_win is the anchor's p_entry (table or
                    # price-implied), never a placeholder.
                    entry_p_win=(p_e if p_e is not None else 0.0),
                    size_tier=order.size_tier, entry_time=now,
                    d_entry=d_e, t_entry=t_e, p_entry=p_e))

            elif action != "ENTRY" and self.custodian is not None:
                # P14: the take FILLED — custody of a flat position ends here
                # (the 7:34 race began with a stale custodian pos object).
                if self.gateway.positions.get(
                        (order.event, order.market, order.lane), 0) == 0:
                    pos_done = self.custodian.positions.pop(
                        f"{order.market}:{order.lane}", None)
                    # SALV-1 §2.3: the position concluded by a filled exit —
                    # one summary, with the realized round trip.
                    if pos_done is not None:
                        self.custodian.emit_salvage_summary(
                            pos_done, "EXIT_FILLED",
                            realized_cents=int(round(cost))
                            - pos_done.entry_price_cents)

            self.surface.write_row(order.lane, order.market, window, state,
                                   detail=f"fill={fid} @{int(round(cost))}c "
                                          f"x{count} fee={fee_cents}c"
                                          + anchor_note)
            stats["booked"] += 1
            self.on_booked(order, action, int(round(cost)), count, now,
                           fee_cents)
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
        # Tape 0718: this line printed every 3s forever — log only when the
        # unique-foreign count CHANGES (the counter keeps full fidelity).
        if stats["foreign"] and self.foreign_seen != self._last_foreign_logged:
            self._last_foreign_logged = self.foreign_seen
            log.info("[RECONCILE] %d unique foreign fills (this account's "
                     "history until cutover)", self.foreign_seen)
        return stats
