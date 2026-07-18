"""P13 Part I — the fills autopsy: our own DB tells the window's story.

    python -m scripts.autopsy_fills <db_path> [market-substring]

Reads the relay ledger's fills + surface rows for the matched market(s) and
prints the entry/exit/fee timeline with per-pair round-trip math — the same
arithmetic the ↔ net line pages live. Run it on the DEPLOYED DB (RELAY_DB_PATH)
for window 0715-15 and commit the output to docs/AUTOPSY_0715.md.

No API keys needed: this reads only what the engine already wrote.
"""

import sqlite3
import sys
import time


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    db_path = sys.argv[1]
    needle = sys.argv[2] if len(sys.argv) > 2 else ""
    db = sqlite3.connect(db_path)

    fills = db.execute(
        "SELECT ts, market, lane, side, action, price_cents, count,"
        " COALESCE(fee_cents, 0), size_tier FROM fills"
        " WHERE market LIKE ? ORDER BY ts, id", (f"%{needle}%",)).fetchall()
    if not fills:
        print(f"EVIDENCE GAP: no fills rows match {needle!r} in {db_path} — "
              f"if this DB was ephemeral (RELAY_DB_PATH unset at the time), "
              f"the window's rows died with the redeploy. State the gap.")
        return 1

    print(f"{len(fills)} fill(s) matching {needle!r}:")
    entries = {}   # (market, lane) -> entry price
    total_net = 0
    total_fees = 0
    for ts, market, lane, side, action, px, cnt, fee, tier in fills:
        stamp = time.strftime("%H:%M:%S", time.gmtime(ts)) + " UTC"
        total_fees += fee
        if action == "ENTRY":
            entries[(market, lane)] = px
            print(f"  {stamp}  ✅ ENTRY {lane} {market} buy {side}@{px}¢ "
                  f"x{cnt} [{tier}]")
        else:
            entry_px = entries.pop((market, lane), None)
            line = (f"  {stamp}  ✂️ {action} {lane} {market} sell "
                    f"{side}@{px}¢ x{cnt} (fee {fee}¢)")
            if entry_px is not None:
                rt = (px - entry_px) * cnt
                net = rt - fee
                total_net += net
                line += f"  ↔ round-trip {rt:+d}¢ + fee {fee}¢ = {net:+d}¢"
            print(line)
    print(f"\nTOTAL: net {total_net:+d}¢ · fees {total_fees}¢")

    rows = db.execute(
        "SELECT ts, lane, state, detail FROM surface_rows"
        " WHERE market LIKE ? ORDER BY ts, id LIMIT 400",
        (f"%{needle}%",)).fetchall()
    if rows:
        print(f"\n{len(rows)} surface row(s):")
        for ts, lane, state, detail in rows:
            stamp = time.strftime("%H:%M:%S", time.gmtime(ts)) + " UTC"
            print(f"  {stamp}  [{lane}] {state} {detail[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
