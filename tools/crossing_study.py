#!/usr/bin/env python3
# tools/crossing_study.py
# DELIVERABLE 0 — the offline crossing study (BUILD ORDER §6). A SCRIPT, not a service.
#
# Question it prices before the first window is ever traded: for a KXBTC15M window, how
# often does spot cross its strike, how often does it re-cross, and how often would BOTH
# legs of a bundle flip at a target of X cents/side? Broken out by time-of-day and vol
# regime, so pricebrain's D4 gate is seeded by evidence instead of a guess.
#
# The borrow list credits k_worker/delta_table_* candle data for the spine; that data
# never existed in this repo. So this script takes candle data from one of:
#   --csv PATH        columns: time(epoch|iso), open, high, low, close   (real data)
#   --coinbase        pull recent 1-minute BTC candles live from Coinbase
#   --synthetic N     generate N minutes of GBM candles (offline demo / CI)
#
# Outputs (next to --out DIR, default cwd):
#   crossing_study.csv          one row per 15M window
#   crossing_study_summary.md   one-page summary for Drew
#   flipdesk_gate.json          gate constants pricebrain will load at boot
#
# The strike proxy for a historical window is its OPEN price (KXBTC15M strikes sit at/
# near the window-open spot). The cents<->USD sensitivity near the money is the Gaussian
# local delta:  cents_per_usd = 100 * phi(0) / sd_usd,  sd_usd = sigma * sqrt(window_sec).
# So flipping a leg X cents needs a spot excursion of about X / cents_per_usd dollars;
# a BOTH-legs flip needs that excursion on BOTH sides of the strike within the window.

import argparse
import csv
import json
import math
import os
import sys
from typing import Dict, List, Optional, Tuple

WINDOW_SEC = 900          # 15 minutes
BUCKET_MIN = 60           # candle granularity we assume (1m)
X_TARGETS = [4, 5, 6, 7, 8]
PHI0 = 1.0 / math.sqrt(2.0 * math.pi)   # standard normal pdf at 0 ~ 0.3989


# ----------------------------- data loading -----------------------------
def _parse_time(v: str) -> float:
    v = str(v).strip()
    try:
        return float(v)
    except ValueError:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()


def load_csv(path: str) -> List[Tuple[float, float]]:
    rows: List[Tuple[float, float]] = []
    with open(path) as f:
        rd = csv.DictReader(f)
        for r in rd:
            t = _parse_time(r.get("time") or r.get("timestamp") or r.get("t"))
            c = float(r.get("close") or r.get("c"))
            rows.append((t, c))
    rows.sort(key=lambda x: x[0])
    return rows


def load_coinbase(minutes: int = 300) -> List[Tuple[float, float]]:
    import requests
    r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                     params={"granularity": 60}, timeout=10)
    r.raise_for_status()
    data = r.json()  # [ time, low, high, open, close, volume ]
    rows = [(float(c[0]), float(c[4])) for c in data]
    rows.sort(key=lambda x: x[0])
    return rows[-minutes:]


def load_coinbase_paginated(days: int, granularity: int = 60,
                            sleep_s: float = 0.34) -> Tuple[List[Tuple[float, float]], Dict]:
    """Paginated multi-day fetch (F1.5 — ported doctrine from the old delta_table_builder).

    Coinbase caps candles at ~300 per request, so a real 180-day/1-minute study needs
    ~860 chunked requests walked backward with start/end. We dedupe by timestamp, sleep
    between calls to respect the rate limit, and emit a manifest+validator (requests,
    candles, coverage, gaps) so partial/holey pulls are visible instead of silent.
    """
    import time as _time
    from datetime import datetime, timezone
    import requests

    def _iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    now = _time.time()
    start = now - days * 86400
    chunk = granularity * 300
    rows: Dict[int, float] = {}
    manifest = {"days": days, "granularity": granularity, "requests": 0,
                "empty_requests": 0, "errors": 0}
    t = start
    while t < now:
        e = min(now, t + chunk)
        try:
            r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                             params={"granularity": granularity, "start": _iso(t), "end": _iso(e)},
                             timeout=15)
            r.raise_for_status()
            data = r.json()
            if not data:
                manifest["empty_requests"] += 1
            for c in data:
                rows[int(c[0])] = float(c[4])
        except Exception as ex:
            manifest["errors"] += 1
            print(f"[study] chunk {_iso(t)} failed: {ex}", file=sys.stderr)
        manifest["requests"] += 1
        t = e
        _time.sleep(sleep_s)

    out = sorted(rows.items())
    manifest["candles"] = len(out)
    expected = days * 1440
    manifest["expected_candles"] = expected
    manifest["coverage_pct"] = round(100.0 * len(out) / expected, 2) if expected else 0.0
    # gap validator: count missing-minute runs
    gaps = 0
    ts_sorted = [t for t, _ in out]
    for i in range(1, len(ts_sorted)):
        if ts_sorted[i] - ts_sorted[i - 1] > granularity * 1.5:
            gaps += 1
    manifest["gaps"] = gaps
    return [(float(t), c) for t, c in out], manifest


def gen_synthetic(minutes: int, sigma_per_min: float = 120.0, seed: int = 7,
                  start: float = 100_000.0) -> List[Tuple[float, float]]:
    """Deterministic GBM-ish walk (no Math.random needed): a fixed LCG so CI is stable."""
    rows = []
    price = start
    state = seed
    t0 = 1_700_000_000  # fixed epoch so bucketing is reproducible
    for i in range(minutes):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        u = (state / 0x7FFFFFFF) * 2.0 - 1.0   # ~uniform[-1,1]
        price = max(1.0, price + u * sigma_per_min)
        rows.append((float(t0 + i * 60), round(price, 2)))
    return rows


# ----------------------------- window analysis -----------------------------
def realized_sigma_usd_per_sqrt_sec(closes: List[float]) -> float:
    if len(closes) < 3:
        return 0.0
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / max(1, len(diffs) - 1)
    sd_per_min = math.sqrt(max(0.0, var))
    return sd_per_min / math.sqrt(60.0)


def vol_regime(sigma: float, low: float, high: float) -> str:
    if sigma < low:
        return "low"
    if sigma >= high:
        return "high"
    return "mid"


def analyze_window(rows: List[Tuple[float, float]]) -> Optional[Dict]:
    if len(rows) < 4:
        return None
    closes = [c for _, c in rows]
    strike = closes[0]
    sigma = realized_sigma_usd_per_sqrt_sec(closes)
    sd_usd = max(1e-6, sigma * math.sqrt(WINDOW_SEC))
    cents_per_usd = 100.0 * PHI0 / sd_usd

    # crossings: sign changes of (price - strike)
    crossings = 0
    prev = closes[0] - strike
    for c in closes[1:]:
        d = c - strike
        if d == 0:
            continue
        if prev != 0 and (d > 0) != (prev > 0):
            crossings += 1
        prev = d if d != 0 else prev
    recrosses = max(0, crossings - 1)

    max_up = max((c - strike) for c in closes)
    max_down = max((strike - c) for c in closes)

    flips: Dict[int, int] = {}
    for x in X_TARGETS:
        usd_needed = x / max(1e-9, cents_per_usd)
        both = 1 if (max_up >= usd_needed and max_down >= usd_needed) else 0
        flips[x] = both

    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    hour = datetime.fromtimestamp(rows[0][0], tz=timezone.utc).astimezone(
        ZoneInfo("America/New_York")).hour

    return {
        "start_ts": int(rows[0][0]), "hour_ny": hour, "strike": round(strike, 2),
        "sigma": round(sigma, 4), "sd_usd": round(sd_usd, 1),
        "cents_per_usd": round(cents_per_usd, 4),
        "crossings": crossings, "recrosses": recrosses,
        "max_up": round(max_up, 1), "max_down": round(max_down, 1),
        **{f"flip_x{x}": flips[x] for x in X_TARGETS},
    }


def slice_windows(rows: List[Tuple[float, float]]) -> List[List[Tuple[float, float]]]:
    """Group candles into aligned 15M windows by floor(ts / 900)."""
    buckets: Dict[int, List[Tuple[float, float]]] = {}
    for t, c in rows:
        buckets.setdefault(int(t // WINDOW_SEC), []).append((t, c))
    return [sorted(v, key=lambda x: x[0]) for _, v in sorted(buckets.items())]


# ----------------------------- aggregation / output -----------------------------
def build_gate(windows: List[Dict], low: float, high: float) -> Dict:
    sigmas = sorted(w["sigma"] for w in windows if w["sigma"] > 0)
    if sigmas:
        floor = round(sigmas[max(0, int(0.10 * len(sigmas)) - 1)], 2)
        ceil = round(sigmas[min(len(sigmas) - 1, int(0.95 * len(sigmas)))], 2)
    else:
        floor, ceil = 5.0, 30.0
    # per-regime both-legs-flip rate at the boot target of 6c
    rate: Dict[str, float] = {}
    for reg in ("low", "mid", "high"):
        grp = [w for w in windows if vol_regime(w["sigma"], low, high) == reg]
        rate[reg] = round(sum(w["flip_x6"] for w in grp) / len(grp), 3) if grp else 0.0
    return {
        "sigma_low": low, "sigma_high": high,
        "sigma_floor": floor, "sigma_ceil": ceil,
        "max_build_cost": 99, "min_flip_headroom": 2, "max_leg_spread": 6,
        "flip_rate": rate,
        "_provenance": "seeded by tools/crossing_study.py (Deliverable 0)",
    }


def write_csv(windows: List[Dict], path: str) -> None:
    if not windows:
        return
    cols = list(windows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(windows)


def write_summary(windows: List[Dict], gate: Dict, path: str, source: str) -> None:
    n = len(windows)
    avg_cross = sum(w["crossings"] for w in windows) / n if n else 0
    avg_recross = sum(w["recrosses"] for w in windows) / n if n else 0
    lines = []
    lines.append("# FLIPDESK — Deliverable 0: Crossing Study\n")
    lines.append(f"Source: **{source}** · Windows analyzed: **{n}**\n")
    lines.append(f"- Avg strike crossings / window: **{avg_cross:.2f}**")
    lines.append(f"- Avg re-crosses / window: **{avg_recross:.2f}**\n")
    lines.append("## Both-legs-flip rate by target X (all windows)\n")
    lines.append("| X (¢/side) | both-legs-flip rate |")
    lines.append("|---|---|")
    for x in X_TARGETS:
        rate = sum(w[f"flip_x{x}"] for w in windows) / n if n else 0
        lines.append(f"| {x} | {rate:.1%} |")
    lines.append("\n## Both-legs-flip @6¢ by vol regime\n")
    lines.append("| regime | flip@6 |")
    lines.append("|---|---|")
    for reg, r in gate["flip_rate"].items():
        lines.append(f"| {reg} | {r:.1%} |")
    lines.append("\n## Recommended D4 gate thresholds\n")
    lines.append("```json")
    lines.append(json.dumps({k: v for k, v in gate.items() if not k.startswith("_")}, indent=2))
    lines.append("```")
    lines.append("\n### Reading\n")
    lines.append("- A window flips both legs only when spot swings past the strike by the")
    lines.append("  cents→USD-equivalent of X on **both** sides. Low-vol windows rarely do;")
    lines.append("  they are exactly the SAT_OUT windows the gate should refuse.")
    lines.append("- `sigma_floor`/`sigma_ceil` are the 10th/95th percentiles of realized")
    lines.append("  window sigma — below the floor is too calm to cross, above the ceil is")
    lines.append("  coin-flip territory we won't feed at rung 1.")
    lines.append("- These seed pricebrain via `flipdesk_gate.json`. The tape retunes them;")
    lines.append("  they are boot defaults, not law (the LAWS are the walls).\n")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="FLIPDESK crossing study (Deliverable 0)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="candle CSV: time,open,high,low,close")
    src.add_argument("--coinbase", action="store_true", help="pull recent 1m candles live")
    src.add_argument("--synthetic", type=int, metavar="MINUTES", help="generate N minutes of demo candles")
    ap.add_argument("--days", type=int, default=0,
                    help="with --coinbase: paginate this many days back (e.g. 180)")
    ap.add_argument("--out", default=".", help="output directory")
    ap.add_argument("--sigma-low", type=float, default=6.0)
    ap.add_argument("--sigma-high", type=float, default=18.0)
    args = ap.parse_args(argv)

    manifest = None
    if args.csv:
        rows, source = load_csv(args.csv), f"csv:{os.path.basename(args.csv)}"
    elif args.coinbase:
        if args.days and args.days > 0:
            rows, manifest = load_coinbase_paginated(args.days)
            source = f"coinbase:{args.days}d:1m"
        else:
            rows, source = load_coinbase(), "coinbase:1m"
    else:
        rows, source = gen_synthetic(args.synthetic), f"synthetic:{args.synthetic}m"

    windows = [w for w in (analyze_window(win) for win in slice_windows(rows)) if w]
    if not windows:
        print("no analyzable windows in input", file=sys.stderr)
        return 2

    gate = build_gate(windows, args.sigma_low, args.sigma_high)
    os.makedirs(args.out, exist_ok=True)
    write_csv(windows, os.path.join(args.out, "crossing_study.csv"))
    write_summary(windows, gate, os.path.join(args.out, "crossing_study_summary.md"), source)
    with open(os.path.join(args.out, "flipdesk_gate.json"), "w") as f:
        json.dump(gate, f, indent=2)
    if manifest is not None:
        with open(os.path.join(args.out, "crossing_study_manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"fetch manifest: {manifest['candles']}/{manifest['expected_candles']} candles "
              f"({manifest['coverage_pct']}%), {manifest['gaps']} gaps, "
              f"{manifest['requests']} requests, {manifest['errors']} errors")
        if manifest["coverage_pct"] < 90.0:
            print("WARNING: coverage < 90% — the study is holey; do not seed the gate from it",
                  file=sys.stderr)

    print(f"analyzed {len(windows)} windows -> {args.out}/crossing_study.csv, "
          f"crossing_study_summary.md, flipdesk_gate.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
