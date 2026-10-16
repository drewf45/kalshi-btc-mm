"""Edge map from a Kalshi 'Recent Activity' CSV export — UNBIASED.

The CSV export carries explicit Settlement rows with Result + contracts-owned +
average price, so realized outcomes are KNOWN (no API settlement crawl, no
inference). This is the honest edge map: per entry-price bucket, what the tape
ACTUALLY did. Losing buckets are reported as losing. No confirmation bias — we
measure outcomes, never hope.

A Settlement row gives us, per (market, side):
  - contracts owned on that side + their average entry price (cents)
  - Result (yes|no) — which side actually paid $1.00
A YES position wins if Result==yes (payout 100c/contract), else 0. Mirror for NO.
Realized net per contract = payout - avg_entry_price (cents). Fees are small and
live in Trade rows; we fold total fees per series separately.

Usage:
  python -m tools.edge_map_csv <path-to-csv>            # full text report
  python -m tools.edge_map_csv <csv> --series KXBTC15M  # one series
  python -m tools.edge_map_csv <csv> --json             # structured
"""

import csv
import json
import sys
from collections import defaultdict

BUCKETS = [(1, 9), (10, 19), (20, 29), (30, 39), (40, 49), (50, 54),
           (55, 59), (60, 69), (70, 79), (80, 89), (90, 94), (95, 99)]


def bucket_of(cents):
    for lo, hi in BUCKETS:
        if lo <= cents <= hi:
            return f"{lo}-{hi}"
    return "other"


def series_of(ticker):
    # KXBTC15M-26OCT151300-00 -> KXBTC15M
    return ticker.split("-", 1)[0] if ticker else "?"


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def load_positions(path):
    """Each settled position = one (market, side) with contracts + avg entry +
    realized outcome. Returns list of dicts."""
    positions = []
    fees_by_series = defaultdict(float)
    with open(path, newline="", encoding="utf-8-sig") as fh:  # strip Excel BOM
        for r in csv.DictReader(fh):
            typ = r.get("type", "")
            if typ == "Trade":
                fees_by_series[series_of(r.get("Market_Ticker", ""))] += f(r.get("Fee_In_Dollars"))
                continue
            if typ != "Settlement":
                continue
            ticker = r.get("Market_Ticker", "")
            result = (r.get("Result") or "").strip().lower()  # yes|no
            if result not in ("yes", "no"):
                continue
            yes_ct = f(r.get("Yes_Contracts_Owned"))
            yes_px = f(r.get("Yes_Contracts_Average_Price_In_Cents"))
            no_ct = f(r.get("No_Contracts_Owned"))
            no_px = f(r.get("No_Contracts_Average_Price_In_Cents"))
            for side, ct, px in (("yes", yes_ct, yes_px), ("no", no_ct, no_px)):
                if ct <= 0 or px <= 0:
                    continue
                payout = 100.0 if side == result else 0.0
                net_per_ct = payout - px           # realized cents / contract
                positions.append({
                    "series": series_of(ticker), "market": ticker, "side": side,
                    "entry_cents": px, "contracts": ct, "won": side == result,
                    "net_per_ct": net_per_ct, "net_total": net_per_ct * ct,
                })
    return positions, fees_by_series


def summarize(rows):
    """Honest stats for a group of positions."""
    n = len(rows)
    if n == 0:
        return None
    wins = [r for r in rows if r["won"]]
    losses = [r for r in rows if not r["won"]]
    ctr = sum(r["contracts"] for r in rows)
    win_rate = len(wins) / n
    mean_win = sum(r["net_per_ct"] for r in wins) / len(wins) if wins else 0.0
    # loss magnitude is positive cents
    mean_loss = (sum(-r["net_per_ct"] for r in losses) / len(losses)) if losses else 0.0
    be = mean_loss / (mean_win + mean_loss) if (mean_win + mean_loss) > 0 else None
    ev_per_ct = win_rate * mean_win - (1 - win_rate) * mean_loss
    net_total_c = sum(r["net_total"] for r in rows)
    return {
        "n": n, "contracts": round(ctr, 2), "win_rate": win_rate,
        "breakeven": be, "mean_win_c": mean_win, "mean_loss_c": mean_loss,
        "ev_per_ct_c": ev_per_ct, "net_total_usd": net_total_c / 100.0,
        "thin": n < 5,
        "losing": (be is not None and win_rate < be and n >= 5),
    }


def flag(s):
    if s is None:
        return ""
    if s["thin"]:
        return "THIN"
    if s["losing"]:
        return "LOSING"
    return "edge+" if s["ev_per_ct_c"] > 0 else "flat/-"


def row_line(label, s):
    if s is None:
        return f"{label:<12} (no data)"
    be = "  —  " if s["breakeven"] is None else f"{s['breakeven']*100:4.1f}%"
    return (f"{label:<12} {s['n']:>5} {s['contracts']:>9.2f} {s['win_rate']*100:5.1f}% "
            f"{be} {s['mean_win_c']:>6.1f} {s['mean_loss_c']:>7.1f} "
            f"{s['ev_per_ct_c']:>7.2f} {s['net_total_usd']:>9.2f}  {flag(s)}")


def report(path, series_filter=None, as_json=False):
    positions, fees = load_positions(path)
    if series_filter:
        positions = [p for p in positions if p["series"] == series_filter]

    by_bucket = defaultdict(list)
    by_series = defaultdict(list)
    for p in positions:
        by_bucket[bucket_of(p["entry_cents"])].append(p)
        by_series[p["series"]].append(p)

    if as_json:
        out = {
            "total_positions": len(positions),
            "buckets": {b: summarize(by_bucket.get(b, [])) for b, _ in [(f"{lo}-{hi}", 0) for lo, hi in BUCKETS]},
            "series": {s: summarize(rows) for s, rows in by_series.items()},
            "fees_by_series_usd": {k: round(v, 2) for k, v in fees.items()},
        }
        print(json.dumps(out, indent=2, default=str))
        return

    hdr = "=" * 92
    print(hdr)
    print("KALSHI EDGE MAP from activity CSV — realized outcomes (UNBIASED)")
    print(hdr)
    print("MANDATE: settled positions only, Result-confirmed. Losing buckets flagged LOSING.")
    print("N<5 = THIN (no conclusions). EV/net in realized cents per contract.\n")
    print(f"Settled positions: {len(positions)}"
          + (f"   [series={series_filter}]" if series_filter else ""))
    total = summarize(positions)
    if total:
        print(f"TOTAL realized net: ${total['net_total_usd']:,.2f}   "
              f"overall win {total['win_rate']*100:.1f}%  (be {('n/a' if total['breakeven'] is None else f'{total['breakeven']*100:.1f}%')})")
    print()
    cols = f"{'label':<12} {'N':>5} {'ctr':>9} {'win%':>6} {'be%':>5} {'mWin':>6} {'mLoss':>7} {'EV/ct':>7} {'net$':>9}  flag"
    print("BY ENTRY-PRICE BUCKET (entry cents, the side you paid)")
    print("-" * 92); print(cols)
    for lo, hi in BUCKETS:
        b = f"{lo}-{hi}"
        if by_bucket.get(b):
            print(row_line(b, summarize(by_bucket[b])))
    print("\nBY MARKET SERIES")
    print("-" * 92); print(cols)
    for s in sorted(by_series, key=lambda k: -summarize(by_series[k])["net_total_usd"]):
        print(row_line(s, summarize(by_series[s])))
    print("\nFEES PAID BY SERIES ($)")
    for s in sorted(fees, key=lambda k: -fees[k]):
        if fees[s]:
            print(f"  {s:<12} ${fees[s]:,.2f}")
    # honest callouts
    losing = [f"{lo}-{hi}" for lo, hi in BUCKETS
              if by_bucket.get(f"{lo}-{hi}") and summarize(by_bucket[f"{lo}-{hi}"])["losing"]]
    winning = [f"{lo}-{hi}" for lo, hi in BUCKETS
               if by_bucket.get(f"{lo}-{hi}") and not summarize(by_bucket[f"{lo}-{hi}"])["thin"]
               and summarize(by_bucket[f"{lo}-{hi}"])["ev_per_ct_c"] > 0]
    print(f"\nLOSING buckets (realized wr < breakeven, N>=5): {', '.join(losing) or 'none'}")
    print(f"Profitable buckets (EV/ct > 0, N>=5): {', '.join(winning) or 'none'}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("--"):
        print("usage: python -m tools.edge_map_csv <csv> [--series X] [--json]")
        return 2
    path = argv[0]
    series = argv[argv.index("--series") + 1] if "--series" in argv else None
    report(path, series_filter=series, as_json="--json" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
