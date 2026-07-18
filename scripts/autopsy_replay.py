"""P10 §1.1 — replay a window's banked frames through the pipeline.

The banked tape lives in the DEPLOYED worker's database (RELAY_DB_PATH,
e.g. /var/data/relay_shadow.db on Render) — run this THERE (Render shell):

    python -m scripts.autopsy_replay /var/data/relay_shadow.db KXBTC15M-26JUL172130-30

It prints, after every frame, (best_yes_bid, best_no_bid, sum) under TWO
appliers:

  LEGACY — the v7 delta path verbatim (side defaulted to "yes", no snapshot
           foundation required, no seq check): this is the machine that was
           running at 01:25 and is how the corrupting frame is CONVICTED.
  FIXED  — today's Feed.handle_frame: the same frame must be refused
           (banked / resync) and the sum must never exceed 101.

[A1] The replay STARTS at the window's banked orderbook_snapshot. If no
snapshot was banked for the market, the EVIDENCE GAP is stated and the
replay refuses to run mid-stream — an honest gap beats a false conviction.

The first frame where LEGACY's sum exceeds 101 is printed VERBATIM (§1.4) —
paste it into tests/test_p10_book_truth.py as a permanent unit test.
"""

import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_engine.feed import DegradeLadder, Feed  # noqa: E402


def legacy_apply(books: dict, msg: dict) -> None:
    """The v7 delta/snapshot path, byte-faithful to the deployed 01:25 code:
    side DEFAULTED to yes; books auto-created; no foundation/seq checks."""
    mtype = msg.get("type")
    m = msg.get("msg", {})
    market = m.get("market_ticker", "")
    if not market:
        return
    yes, no = books.setdefault(market, ({}, {}))
    if mtype == "orderbook_snapshot":
        yes.clear()
        no.clear()
        for lv in (m.get("yes") or []):
            yes[int(Decimal(str(lv[0])) * (100 if "." in str(lv[0]) else 1))] = 1
        for lv in (m.get("no") or []):
            no[int(Decimal(str(lv[0])) * (100 if "." in str(lv[0]) else 1))] = 1
    elif mtype == "orderbook_delta":
        price = m.get("price")
        delta = m.get("delta")
        if price in (None, 0, "0", "0.0") or delta is None:
            return
        cents = int(Decimal(str(price)) * (100 if "." in str(price) else 1))
        side = m.get("side", "yes")             # <-- THE DEFAULT (the liar's door)
        levels = yes if side == "yes" else no
        q = levels.get(cents, 0) + int(Decimal(str(delta)))
        if q > 0:
            levels[cents] = q
        else:
            levels.pop(cents, None)


def bests(books, market):
    yes, no = books.get(market, ({}, {}))
    yb = max(yes) if yes else None
    nb = max(no) if no else None
    s = (yb + nb) if (yb is not None and nb is not None) else None
    return yb, nb, s


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    db_path, market = sys.argv[1], sys.argv[2]
    rows = sqlite3.connect(db_path).execute(
        "SELECT ts, snapshot FROM book_snapshots WHERE market=? ORDER BY ts, id",
        (market,)).fetchall()
    if not rows:
        print(f"EVIDENCE GAP: no banked frames for {market} in {db_path}")
        return 1
    start = next((i for i, (_, raw) in enumerate(rows)
                  if json.loads(raw).get("type") == "orderbook_snapshot"), None)
    if start is None:
        print(f"EVIDENCE GAP: {len(rows)} banked frames for {market} but NO "
              f"orderbook_snapshot among them — a replay from mid-delta-stream "
              f"would show artifact incoherence, not the bug (A1). Refusing.")
        return 1
    print(f"{len(rows)} banked frames; replay starts at banked snapshot "
          f"(frame index {start}) [A1]")

    legacy_books: dict = {}
    feed = Feed(DegradeLadder())
    corrupting = None
    for i, (ts, raw) in enumerate(rows[start:], start=start):
        msg = json.loads(raw)
        legacy_apply(legacy_books, msg)
        lyb, lnb, ls = bests(legacy_books, market)
        try:
            feed.handle_frame(raw, now=ts)
        except Exception as e:
            print(f"  frame {i}: FIXED pipeline raised {e!r} (banked path)")
        fb = feed.books.get(market)
        fyb = fb.best_yes_bid() if fb else None
        fnb = fb.best_no_bid() if fb else None
        fs = (fyb + fnb) if (fyb is not None and fnb is not None) else None
        flag = ""
        if ls is not None and ls > 101 and corrupting is None:
            corrupting = (i, raw)
            flag = "   <<< FIRST INCOHERENT (LEGACY)"
        print(f"frame {i}: LEGACY y{lyb}/n{lnb} sum={ls} | "
              f"FIXED y{fyb}/n{fnb} sum={fs}"
              f"{' poisoned' if fb is not None and fb.poisoned else ''}{flag}")

    print()
    if corrupting is None:
        print("LEGACY replay never went incoherent on this tape — the 01:25 "
              "corruption did not reproduce from these frames alone. State "
              "this in the fix report (read-rule: an honest gap beats a "
              "false conviction).")
        return 1
    idx, raw = corrupting
    print(f"THE CORRUPTING FRAME (index {idx}) — VERBATIM (§1.4):")
    print(raw)
    fb = feed.books.get(market)
    fixed_ok = fb is None or fb.coherent()
    print(f"FIXED pipeline verdict on the same tape: "
          f"{'coherent / refused corrupting frames' if fixed_ok else 'STILL INCOHERENT — fix incomplete'}")
    return 0 if fixed_ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
