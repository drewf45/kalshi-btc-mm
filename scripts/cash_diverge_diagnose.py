#!/usr/bin/env python3
"""WO-2026-07-22 — the cash-divergence forensic + quarantine tool.

The overnight halt (2:48–5:50 AM) was the cash rail refusing to trade on a book
it could not reconcile to the venue: the book claimed +$1.83, the venue said
−$0.16, a ~$1.99 gap from a bad settlement row (a gross-as-net booking, or a
win booked as a loss — both ~194-200c). `/clear_cash_fatal` only unlocks the
door; the next reconcile recomputes the same delta and re-halts. `/confirm_cash`
would BAKE the error into the book forever (WO-2026-07-21 §B4). The correct fix
is to QUARANTINE the bad row (book_cents already excludes divergent=1) and
re-book that window at fills-truth — never re-baseline.

USAGE (run on the box that has the live ledger DB):

  # 1. IDENTIFY — list settlements, flag rows whose pnl is outside the net bound
  python scripts/cash_diverge_diagnose.py --db <ledger.db> [--since <epoch>]

  # 2. QUARANTINE — after naming the culprit, re-book that market at fills-truth
  #    (tags the disputed rows divergent=1 and inserts the correct value; the
  #    honest lifetime number is preserved, not papered over)
  python scripts/cash_diverge_diagnose.py --db <ledger.db> \
      --quarantine <MARKET> --correct-pnl <CENTS>

  # then, and ONLY then, /clear_cash_fatal — NEVER /confirm_cash.
"""
import argparse
import sys

sys.path.insert(0, ".")
from relay_engine.ledger import Ledger      # noqa: E402
from relay_engine import config             # noqa: E402


def _entry_cost_count(led, market, lane):
    row = led.db.execute(
        "SELECT COALESCE(SUM(price_cents*count),0), COALESCE(SUM(count),0)"
        " FROM fills WHERE market=? AND lane=? AND action='ENTRY'",
        (market, lane)).fetchone()
    return int(row[0]), int(row[1])


def diagnose(led, since):
    q = ("SELECT id, ts, market, lane, pnl_cents, divergent FROM settlements"
         + (" WHERE ts >= ?" if since else "") + " ORDER BY id")
    rows = led.db.execute(q, (since,) if since else ()).fetchall()
    slip = config.SETTLE_NOTIONAL_SLIP_C
    print(f"book_cents (non-divergent) = {led.book_cents()}c")
    print(f"{'id':>5} {'lane':>5} {'pnl':>7} {'cost':>6} {'lots':>4} "
          f"{'net-bound':>14} {'div':>4}  market")
    print("-" * 88)
    suspects = []
    for sid, ts, market, lane, pnl, div in rows:
        cost, cnt = _entry_cost_count(led, market, lane)
        lo, hi = (-cost - slip, cnt * 100 - cost + slip) if cnt else (None, None)
        bad = cnt > 0 and not (lo <= pnl <= hi)
        flag = " <== OUT OF BOUND" if bad and not div else ""
        print(f"{sid:>5} {lane:>5} {pnl:>+7} {cost:>6} {cnt:>4} "
              f"{f'[{lo},{hi}]' if cnt else 'no-fills':>14} {div:>4}  {market}{flag}")
        if bad and not div:
            suspects.append((sid, market, lane, pnl, cost, cnt))
    print("-" * 88)
    if suspects:
        print(f"\n{len(suspects)} SUSPECT row(s) — pnl outside [−cost, lots·100−cost]:")
        for sid, market, lane, pnl, cost, cnt in suspects:
            print(f"  id {sid}: {market} {lane} booked {pnl:+d}c but a {cnt}-lot "
                  f"position at {cost}c cost can only net "
                  f"[{-cost},{cnt * 100 - cost}]c. Re-book at the TRUE fills value:")
            print(f"    python {sys.argv[0]} --db <db> --quarantine {market} "
                  "--correct-pnl <TRUE_CENTS>")
    else:
        print("\nNo out-of-bound settlement rows. If the delta persists, the "
              "gap is elsewhere (double-book same value, or a cash_movement).")


def quarantine(led, market, correct_pnl):
    before = led.book_cents()
    removed = led.quarantine_divergent_settlements(market, correct_pnl)
    after = led.book_cents()
    print(f"quarantined {market}: re-booked at fills-truth {correct_pnl:+d}c")
    print(f"  book {before}c -> {after}c  (removed {removed}c)")
    print("  disputed rows tagged divergent=1 (excluded from book_cents, "
          "lifetime preserved). Now — and only now — /clear_cash_fatal. "
          "NEVER /confirm_cash.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--since", type=float, default=None,
                    help="epoch seconds; e.g. the 10:16 PM boot")
    ap.add_argument("--quarantine", metavar="MARKET", default=None)
    ap.add_argument("--correct-pnl", type=int, default=None,
                    help="the TRUE net cents for the quarantined window")
    a = ap.parse_args()
    led = Ledger(a.db)
    if a.quarantine is not None:
        if a.correct_pnl is None:
            ap.error("--quarantine requires --correct-pnl (the TRUE net cents)")
        quarantine(led, a.quarantine, a.correct_pnl)
    else:
        diagnose(led, a.since)


if __name__ == "__main__":
    main()
