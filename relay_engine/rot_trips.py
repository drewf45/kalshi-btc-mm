"""WO-ROT-1 Chunk 7 — rot_trips: one row per closed Rotation Desk trip.

Every row carries its why (Why Law) and the table registers its question
(registry: ROT_TRIPS). The daily bundle's ROT sheet is computed FROM this
table, per band and per series: trips, h, W, L, h* = L / (W + L), net $, fees $.

MONEY. Fractional contracts make sub-cent nets real (0.70 x 8c = 5.6c), so
net_cents / fees_cents are stored exactly (REAL) and the sheet rounds ONCE, at
the dollar column — its sums match this table to the cent.

PRICES. Exit proceeds are each sell order's filled count x its price as the
gateway holds it (shadow: the price the pessimistic model filled at; live: our
limit — an IOC at the bid fills at the bid or better, so live net is
conservative by at most the price improvement: UNPROVEN until reconciled
against the venue's fill prices in the settlement audit).

FEES. Live: the venue's own fee_cents on every ROT fill in the trip's span
(fills table). Shadow: makers pay 0 (CSV: 7,588 maker fills, $0.00 fees);
the hard stop's IOC is a taker and pays the published schedule,
ceil(0.07 x C x P x (1 - P)) dollars.

SETTLEMENT TRUTH. A trip held to the bell is written with exit_path SETTLE
and settled=0; the settle sweep books the venue's result
(venue.get_settlement_result) into it — the activity CSV export has no
settlement rows, so h is never missing the settle leg.
"""

import math
import time
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS rot_trips (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,               -- trip closed
    opened_ts REAL NOT NULL,        -- entry submitted
    series TEXT NOT NULL,
    market TEXT NOT NULL,
    grp TEXT NOT NULL,              -- underlying group (the group cap's key)
    side TEXT NOT NULL,
    entry_cost INTEGER NOT NULL,    -- side-terms cents
    size REAL NOT NULL,             -- contracts filled
    target INTEGER NOT NULL,        -- cash-out price
    stop INTEGER NOT NULL,          -- soft-stop line
    hard_stop INTEGER NOT NULL,     -- hard-stop line (the walk-down floor)
    band TEXT NOT NULL,
    fill_ts REAL,                   -- first entry fill
    exit_path TEXT NOT NULL,        -- TARGET | SOFT_STOP | HARD_STOP | TIME | LATE | SETTLE
    trigger_px INTEGER,             -- the bid that fired the stop (NULL for TARGET/SETTLE)
    fill_px REAL,                   -- avg exit price (100/0 once a settle books)
    gap REAL,                       -- trigger_px - fill_px (stop slippage, cents)
    spot_move REAL,                 -- spot against the held side at close (NULL = no spot)
    reads_held INTEGER NOT NULL,
    sold REAL NOT NULL,             -- contracts sold before the bell
    settle_count REAL NOT NULL DEFAULT 0,   -- contracts that rode to settlement
    settle_result TEXT,             -- yes | no once booked
    settled INTEGER NOT NULL DEFAULT 1,     -- 0 = SETTLE leg not yet booked
    net_cents REAL,                 -- NULL until a SETTLE leg is booked
    fees_cents REAL NOT NULL,
    shadow INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    why TEXT NOT NULL               -- the trip's sentence (Why Law)
);
"""


def ensure(db) -> None:
    db.executescript(SCHEMA)


def taker_fee_cents(count, price_cents: int) -> Decimal:
    """Kalshi's published schedule: ceil(0.07 x C x P x (1-P)) dollars."""
    p = price_cents / 100.0
    dollars = 0.07 * float(count) * p * (1.0 - p)
    return Decimal(math.ceil(round(dollars * 100, 9)))


def trip_economics(t, gateway, ledger=None, live: bool = False) -> dict:
    """Sold, proceeds, avg exit, fees for a closed trip from the gateway's
    own order state (+ the fills table's venue fees when live)."""
    sold = Decimal(0)
    proceeds = Decimal(0)
    fees = Decimal(0)
    for oid in t.sell_oids:
        o = gateway.order_index.get(oid)
        if o is None:
            continue
        f = Decimal(str(gateway.filled_counts.get(oid, 0)))
        if f <= 0:
            continue
        sold += f
        proceeds += f * o.price_cents
        if not live and o.ioc:
            fees += taker_fee_cents(f, o.price_cents)
    if live and ledger is not None:
        row = ledger.db.execute(
            "SELECT COALESCE(SUM(fee_cents), 0) FROM fills WHERE market=? AND lane='ROT'"
            " AND ts >= ?", (t.market, t.entry_ts - 1)).fetchone()
        fees = Decimal(str(row[0] or 0))
    avg = (proceeds / sold) if sold > 0 else None
    return {"sold": sold, "proceeds": proceeds, "fees": fees, "avg_exit": avg}


def row_for(t, econ: dict, settle_count: Decimal, spot_move: Optional[float],
            shadow: bool, now: float, cfg) -> dict:
    filled = Decimal(str(t.filled))
    cost = filled * t.cost
    settle_pending = settle_count > 0
    net = None if settle_pending else (econ["proceeds"] - cost - econ["fees"])
    fill_px = econ["avg_exit"]
    gap = (Decimal(t.trigger_px) - fill_px) if (t.trigger_px is not None
                                                and fill_px is not None) else None
    why = (f"{t.closed_path}: {t.side}@{t.cost}c x{filled} band {t.band} rank {t.rank} "
           f"score {t.score:.3f} · sold {econ['sold']} avg "
           f"{'n/a' if fill_px is None else f'{fill_px:.2f}c'}"
           + (f" · {settle_count} to settle" if settle_pending else "")
           + (f" · trigger {t.trigger_px}c" if t.trigger_px is not None else "")
           + (" · SHADOW" if shadow else ""))
    return {
        "ts": now, "opened_ts": t.entry_ts, "series": t.series, "market": t.market,
        "grp": t.underlying, "side": t.side, "entry_cost": int(t.cost),
        "size": float(filled), "target": int(t.cost + cfg.ROT_TARGET_CENTS),
        "stop": int(t.cost - cfg.ROT_STOP_CENTS),
        "hard_stop": int(t.cost - cfg.ROT_HARD_STOP_CENTS), "band": t.band,
        "fill_ts": t.fill_ts, "exit_path": t.closed_path, "trigger_px": t.trigger_px,
        "fill_px": None if fill_px is None else float(fill_px),
        "gap": None if gap is None else float(gap),
        "spot_move": spot_move, "reads_held": int(t.reads),
        "sold": float(econ["sold"]), "settle_count": float(settle_count),
        "settle_result": None, "settled": 0 if settle_pending else 1,
        "net_cents": None if net is None else float(net),
        "fees_cents": float(econ["fees"]), "shadow": int(bool(shadow)),
        "rank": int(t.rank), "score": float(t.score), "why": why,
    }


def write(ledger, row: dict) -> int:
    ensure(ledger.db)
    cols = list(row)
    cur = ledger.db.execute(
        f"INSERT INTO rot_trips ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(row[c] for c in cols))
    ledger.db.commit()
    return cur.lastrowid


def pending_settles(ledger) -> list:
    ensure(ledger.db)
    return ledger.db.execute(
        "SELECT id, market, side, entry_cost, size, sold, fill_px, fees_cents,"
        " settle_count FROM rot_trips WHERE settled=0").fetchall()


def book_settlement(ledger, trip_id: int, result: str) -> Optional[float]:
    """Book the venue's settlement into a SETTLE trip: the riding contracts
    pay 100c on a win, 0 on a loss. Returns the trip's net cents."""
    row = ledger.db.execute(
        "SELECT side, entry_cost, size, sold, fill_px, fees_cents, settle_count, why"
        " FROM rot_trips WHERE id=? AND settled=0", (trip_id,)).fetchone()
    if row is None or result not in ("yes", "no"):
        return None
    side, cost, size, sold, fill_px, fees, settle_count, why = row
    won = result == side
    sold_d = Decimal(str(sold))
    proceeds = (sold_d * Decimal(str(fill_px))) if (fill_px is not None and sold_d > 0) else Decimal(0)
    payout = Decimal(str(settle_count)) * (100 if won else 0)
    net = proceeds + payout - Decimal(str(size)) * cost - Decimal(str(fees))
    total_out = sold_d + Decimal(str(settle_count))
    avg = (proceeds + payout) / total_out if total_out > 0 else None
    ledger.db.execute(
        "UPDATE rot_trips SET settled=1, settle_result=?, net_cents=?, fill_px=?,"
        " why=? WHERE id=?",
        (result, float(net), None if avg is None else float(avg),
         f"{why} · settled {result.upper()} ({'won' if won else 'lost'})", trip_id))
    ledger.db.commit()
    return float(net)


# ── the bundle's ROT sheet ───────────────────────────────────────

SHEET_HEADER = ["scope", "key", "trips", "wins", "h", "W_c", "L_c", "h_star",
                "h_minus_h_star", "net_usd", "fees_usd", "net_cents_exact",
                "fees_cents_exact", "pending_settle"]


def _stats(rows) -> dict:
    """rows: [(size, net_cents, fees_cents)] for booked trips."""
    n = len(rows)
    wins = [(net / size) for size, net, _f in rows if net > 0 and size > 0]
    losses = [(-net / size) for size, net, _f in rows if net < 0 and size > 0]
    h = len(wins) / n if n else None
    W = sum(wins) / len(wins) if wins else 0.0
    L = sum(losses) / len(losses) if losses else 0.0
    hstar = (L / (W + L)) if (W + L) > 0 else None
    net = sum(r[1] for r in rows)
    fees = sum(r[2] for r in rows)
    return {"n": n, "wins": len(wins), "h": h, "W": W, "L": L, "hstar": hstar,
            "net": net, "fees": fees}


def sheet_rows(conn, day_start: float, day_end: Optional[float] = None,
               shadow: Optional[int] = None) -> Tuple[List[str], list]:
    """The ROT sheet: per band, per series, TOTAL. Booked trips only in the
    stats (a SETTLE leg not yet booked is counted in pending_settle)."""
    day_end = float("inf") if day_end is None else day_end
    try:
        q = ("SELECT band, series, size, net_cents, fees_cents, settled FROM rot_trips"
             " WHERE ts >= ? AND ts < ?")
        args = [day_start, day_end]
        if shadow is not None:
            q += " AND shadow=?"
            args.append(int(shadow))
        data = conn.execute(q, args).fetchall()
    except Exception:
        return SHEET_HEADER, []
    out = []
    for scope, idx in (("band", 0), ("series", 1)):
        keys = sorted({r[idx] for r in data})
        for k in keys:
            booked = [(r[2], r[3], r[4]) for r in data if r[idx] == k and r[5] == 1]
            pend = sum(1 for r in data if r[idx] == k and r[5] == 0)
            out.append(_row(scope, k, booked, pend))
    booked = [(r[2], r[3], r[4]) for r in data if r[5] == 1]
    out.append(_row("total", "ALL", booked, sum(1 for r in data if r[5] == 0)))
    return SHEET_HEADER, out


def _row(scope, key, booked, pending) -> list:
    s = _stats(booked)
    edge = (s["h"] - s["hstar"]) if (s["h"] is not None and s["hstar"] is not None) else None
    r4 = lambda v: None if v is None else round(v, 4)
    return [scope, key, s["n"], s["wins"], r4(s["h"]), r4(s["W"]), r4(s["L"]),
            r4(s["hstar"]), r4(edge), round(s["net"] / 100.0, 2),
            round(s["fees"] / 100.0, 2), round(s["net"], 4), round(s["fees"], 4),
            pending]


def band_stats(db, band: str, n: int, since_ts: float = 0.0,
               shadow: Optional[int] = None):
    """The band's own stats over its last n BOOKED trips, or None while it has
    fewer than n (the selector then ranks on the CSV seed). Per contract:
    h = wins / n, W / L = mean win / loss, holds from first fill to close."""
    from .lane_rot import BandStats
    try:
        ensure(db)
        q = ("SELECT size, net_cents, fill_ts, ts FROM rot_trips WHERE band=? AND"
             " settled=1 AND ts >= ?")
        args = [band, float(since_ts)]
        if shadow is not None:
            q += " AND shadow=?"
            args.append(int(shadow))
        rows = db.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, int(n))).fetchall()
    except Exception:
        return None
    if len(rows) < n:
        return None
    s = _stats([(r[0], r[1], 0.0) for r in rows])
    win_h = [r[3] - r[2] for r in rows if r[1] > 0 and r[2] is not None]
    loss_h = [r[3] - r[2] for r in rows if r[1] <= 0 and r[2] is not None]
    avg = lambda xs, d: (sum(xs) / len(xs)) if xs else d
    return BandStats(h=s["h"], W=s["W"], L=s["L"], hold_win_s=avg(win_h, 310.0),
                     hold_loss_s=avg(loss_h, 515.0), n=len(rows),
                     source=f"measured: last {len(rows)} trips")
