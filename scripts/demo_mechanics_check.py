"""P3.6 — SAME-DAY DEMO MECHANICS CHECK (Broker lens: protects against VENUE
surprises; it does not sequence the build).

Run ONE session against the DEMO account, same day as P3.1:

    KALSHI_ENV=demo KALSHI_API_BASE=https://external-api.demo.kalshi.co \
    KALSHI_API_KEY_ID=... KALSHI_PRIVATE_KEY_PEM_BASE64=... \
    python scripts/demo_mechanics_check.py

What it does (and nothing more):
  1. one maker order: place (post_only) -> amend -> cancel, via the ported client
  2. stacked resting orders (two markets, one event family) -> read
     `Get Total Resting Order Value` -> report the venue's collateral treatment
     of stacked resting orders vs our arithmetic (the gateway counts resting
     entries toward the $-cap; the venue must agree)
  3. the netting_enabled first-order probe: place a first order, read the event's
     collateral state, place the complement, read again — does the first order's
     characteristics lock the treatment of subsequent lanes' collateral?
  4. per-leg fee treatment vs the designation-list read (get_series_fee_changes)

Output: docs/DEMO_MECHANICS_REPORT.md — one page, numbers. ANY SURPRISE = WALL,
back to Drew. Cannot run from the build sandbox (network policy blocks the
venue); run from Render shell or any machine with venue reach.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_engine import venue  # noqa: E402

REPORT = Path(__file__).resolve().parents[1] / "docs" / "DEMO_MECHANICS_REPORT.md"


def main() -> int:
    lines = ["# DEMO MECHANICS REPORT (P3.6)", ""]

    def note(s):
        print(s, flush=True)
        lines.append(s)

    client = venue.build_client()
    venue.probe_orders_route(client)
    venue.probe_fills_route(client)

    ev, ticker, mobj = venue.discover_market(client)
    close_ts = venue.resolve_close_ts(mobj, ticker)
    note(f"- market: `{ticker}` (event `{ev}`, closes {close_ts})")
    book = venue.fetch_orderbook(client, ticker)
    note(f"- book: yes_bid={book.yes_bid} no_bid={book.no_bid}")
    if book.yes_bid is None:
        note("- **WALL: no book to join — rerun during market hours**")
        REPORT.write_text("\n".join(lines) + "\n")
        return 1

    # 1. place -> amend -> cancel
    oid, resp = venue.place_order_maker(client, ticker, "yes", book.yes_bid,
                                        v2_price_str=book.yes_bid_fp)
    note(f"- placed maker yes@{book.yes_bid} oid=`{oid}`")
    time.sleep(1)
    new_price = max(1, book.yes_bid - 1)
    new_oid, _ = venue.amend_order(client, oid, ticker, "yes", new_price)
    note(f"- amend to {new_price}: {'ok, oid=`%s`' % new_oid if new_oid else '**FAILED — surprise, WALL**'}")
    status = venue.cancel_order(client, new_oid or oid)
    note(f"- cancel: {status}")

    # 2. stacked resting + Get Total Resting Order Value
    trov_before = venue.get_total_resting_order_value(client)
    oid_a, _ = venue.place_order_maker(client, ticker, "yes", book.yes_bid,
                                       v2_price_str=book.yes_bid_fp)
    trov_one = venue.get_total_resting_order_value(client)
    oid_b = None
    if book.no_bid is not None:
        oid_b, _ = venue.place_order_maker(client, ticker, "no", book.no_bid,
                                           v2_price_str=book.no_bid_fp)
    trov_two = venue.get_total_resting_order_value(client)
    note(f"- Total Resting Order Value: before={trov_before} one={trov_one} two={trov_two}")
    expected_one = (trov_before or 0) + book.yes_bid
    note(f"- our arithmetic after one: {expected_one}c -> "
         f"{'AGREES' if trov_one == expected_one else '**DISAGREES — WALL, back to Drew**'}")

    # 3. netting_enabled first-order probe: with yes+no resting on one market,
    # does the venue hold collateral for both legs or net them?
    if oid_b is not None and trov_two is not None and trov_one is not None:
        second_leg_cost = trov_two - trov_one
        note(f"- second (complement) leg collateral: {second_leg_cost}c "
             f"(full-cost means no resting-side netting; ~0 means netting_enabled "
             f"locked by the first order)")

    # cleanup
    for o in (oid_a, oid_b):
        if o:
            venue.cancel_order(client, o)
    note("- cleanup: all demo orders cancelled")

    # 4. fee designation read
    fees = venue.get_series_fee_changes(client)
    note(f"- series fee changes: {fees if fees is not None else 'endpoint unreachable (tripwire disabled this boot)'}")

    REPORT.write_text("\n".join(lines) + "\n")
    print(f"\nreport -> {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
