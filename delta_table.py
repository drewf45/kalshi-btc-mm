"""CLI tool for building the delta table locally (development/testing).

In production, the table self-provisions via k_worker.delta_table_builder
at boot. This script is for offline testing and validation.

Usage:
  python delta_table.py --source synthetic --days 180   (offline, no network)
  python delta_table.py --source coinbase --days 180    (needs Coinbase access)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from k_worker.sessions import session_tag, session_hash, SESSION_WINDOWS
from k_worker.delta_table_builder import (
    compute_delta_table, write_csv, write_manifest, CSV_FIELDS,
    DISTANCE_GRID, TIME_GRID, GRANULARITY_SEC,
)

import csv
import json
import hashlib
import math
import random
import time
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("delta_table")

BTC_ANNUAL_VOL = 0.55
BTC_PRICE_ANCHOR = 100000.0
PER_MINUTE_SIGMA = BTC_ANNUAL_VOL / math.sqrt(365.25 * 24 * 60)

SESSION_VOL_MULT = {
    "ASIA": 0.8, "LONDON_OPEN": 1.1, "EU": 0.95,
    "NY_PRE": 1.05, "NY_OPEN": 1.3, "NY": 1.1,
    "NY_CLOSE": 1.0, "EVENING": 0.85, "UNKNOWN": 1.0,
}


def generate_synthetic_candles(days=180, seed=42):
    rng = random.Random(seed)
    total_minutes = days * 24 * 60
    candles = []
    start_ts = int(datetime(2026, 1, 9, 0, 0, tzinfo=timezone.utc).timestamp())
    price = BTC_PRICE_ANCHOR

    for i in range(total_minutes):
        ts = start_ts + i * 60
        sess = session_tag(ts)
        vol_mult = SESSION_VOL_MULT.get(sess, 1.0)
        sigma = PER_MINUTE_SIGMA * vol_mult * price
        opn = price
        high = price
        low = price
        sub_sigma = sigma / math.sqrt(10)
        p = price
        for _ in range(10):
            p += rng.gauss(0, sub_sigma)
            p = max(p, 1.0)
            high = max(high, p)
            low = min(low, p)
        close = p
        price = close
        candles.append((ts, round(opn, 2), round(high, 2), round(low, 2), round(close, 2), 1.0))

    return candles


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Build BTC delta table")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--source", choices=["coinbase", "synthetic"], default="coinbase")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    out_csv = os.path.join(base_dir, "delta_table.csv")
    out_manifest = os.path.join(base_dir, "candles_manifest.json")

    if args.source == "coinbase":
        from k_worker.delta_table_builder import fetch_candles_coinbase
        candles = fetch_candles_coinbase(args.days)
        source_label = "Coinbase BTC-USD 1-min candles (public)"
    else:
        candles = generate_synthetic_candles(args.days, args.seed)
        source_label = f"Synthetic GBM (annual_vol={BTC_ANNUAL_VOL}, seed={args.seed})"

    days_covered = (candles[-1][0] - candles[0][0]) / 86400
    if days_covered < 150:
        log.error(f"FAIL: {days_covered:.1f} days < 150")
        sys.exit(1)

    log.info(f"Session hash: {session_hash()}")
    rows = compute_delta_table(candles)
    sha = write_csv(rows, out_csv)
    manifest = write_manifest(candles, sha, out_manifest)
    # Override source label for synthetic
    if args.source == "synthetic":
        manifest["source"] = source_label
        with open(out_manifest, "w") as f:
            json.dump(manifest, f, indent=2)

    log.info(f"Done: {len(rows)} rows, {days_covered:.1f}d, SHA={sha[:16]}")


if __name__ == "__main__":
    main()
