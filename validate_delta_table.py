"""Validate delta_table.csv — hard gates for deployment.

Five checks:
  A1: Bounds (p_cross in [0,1], wilson_ub >= p_cross, sanity ranges)
  A2: Statistical monotonicity (P decreases with distance, increases with time)
  A3: Brute-force recompute of random cells from raw candles
  A4: Coverage >= 150 days (from manifest)
  A5: Session-map hash from BOTH the generator and engine's session_tag, matching

Schema: distance_usd, secs_remaining, p_cross, n, effective_n, wilson_ub, session

Exit 0 = all gates pass. Exit 1 = any gate fails.
"""

import csv
import json
import hashlib
import math
import os
import sys
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Try both locations: /var/data (production) and repo root (dev)
DATA_DIR = os.environ.get("K_WORKER_DATA_DIR",
    os.path.dirname(os.environ.get("K_WORKER_DB", ""))) or BASE_DIR

CSV_PATH = os.path.join(DATA_DIR, "delta_table.csv")
MANIFEST_PATH = os.path.join(DATA_DIR, "candles_manifest.json")
CANDLES_PATH = os.path.join(DATA_DIR, "btc_candles_raw.json")

GRANULARITY_SEC = 60
INDEPENDENT_WINDOW_SEC = 900

PASS = 0
FAIL = 0


def gate(name, ok, detail=""):
    global PASS, FAIL
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    return ok


def _session_tag_standalone(ts):
    """Standalone session tagger for A5 comparison."""
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York"))
    except ImportError:
        dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=-4)))
    WINDOWS = [
        ("ASIA",        20, 0,  2, 29),
        ("LONDON_OPEN",  2, 30, 4, 0),
        ("EU",           4, 0,  8, 0),
        ("NY_PRE",       8, 0,  9, 29),
        ("NY_OPEN",      9, 30, 10, 30),
        ("NY",          10, 30, 15, 29),
        ("NY_CLOSE",    15, 30, 16, 30),
        ("EVENING",     16, 30, 20, 0),
    ]
    t = dt.hour * 60 + dt.minute
    for name, sh, sm, eh, em in WINDOWS:
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= end:
            if start <= t < end:
                return name
        else:
            if t >= start or t < end:
                return name
    return "UNKNOWN"


def _session_hash_standalone():
    WINDOWS = [
        ("ASIA",        20, 0,  2, 29),
        ("LONDON_OPEN",  2, 30, 4, 0),
        ("EU",           4, 0,  8, 0),
        ("NY_PRE",       8, 0,  9, 29),
        ("NY_OPEN",      9, 30, 10, 30),
        ("NY",          10, 30, 15, 29),
        ("NY_CLOSE",    15, 30, 16, 30),
        ("EVENING",     16, 30, 20, 0),
    ]
    raw = repr(WINDOWS).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def main():
    global PASS, FAIL
    print("=== DELTA TABLE VALIDATOR ===\n")

    # Files exist
    csv_exists = os.path.exists(CSV_PATH)
    manifest_exists = os.path.exists(MANIFEST_PATH)
    gate("CSV file exists", csv_exists, CSV_PATH)
    gate("Manifest file exists", manifest_exists, MANIFEST_PATH)
    if not csv_exists or not manifest_exists:
        print(f"\nRESULT: {PASS} passed, {FAIL} failed — ABORT (missing files)")
        sys.exit(1)

    # Load manifest
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    # A4: Coverage >= 150 days
    days = manifest.get("days_covered", 0)
    gate("A4: >=150 days coverage", days >= 150, f"{days:.1f} days")

    # SHA-256 match
    actual_sha = hashlib.sha256(open(CSV_PATH, "rb").read()).hexdigest()
    expected_sha = manifest.get("csv_sha256", "")
    gate("SHA-256 match", actual_sha == expected_sha,
         f"actual={actual_sha[:16]}... expected={expected_sha[:16]}...")

    # Source check
    source = manifest.get("source", "")
    is_real = "Coinbase" in source and "Synthetic" not in source
    gate("Source is real Coinbase data", is_real, source[:60])

    # Load CSV
    with open(CSV_PATH) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    print(f"\n  Loaded {len(rows)} rows from CSV")

    # Schema check
    required_cols = {"distance_usd", "secs_remaining", "p_cross", "n", "effective_n", "wilson_ub", "session"}
    actual_cols = set(rows[0].keys()) if rows else set()
    gate("Schema has required columns", required_cols.issubset(actual_cols),
         f"missing={required_cols - actual_cols}" if not required_cols.issubset(actual_cols) else "OK")

    # Parse data
    by_session_time = defaultdict(list)
    by_session_dist = defaultdict(list)
    sessions_found = set()
    for r in rows:
        d = int(r["distance_usd"])
        t = int(r["secs_remaining"])
        p = float(r["p_cross"])
        w = float(r["wilson_ub"])
        sess = r["session"]
        sessions_found.add(sess)
        by_session_time[(sess, t)].append((d, p, w))
        by_session_dist[(sess, d)].append((t, p, w))

    # A5: Session hash parity
    validator_hash = _session_hash_standalone()
    manifest_hash = manifest.get("session_hash", "")
    gate("A5: Session hash — validator", validator_hash == manifest_hash,
         f"validator={validator_hash} manifest={manifest_hash}")

    # Try importing engine's hash too
    try:
        sys.path.insert(0, BASE_DIR)
        from k_worker.sessions import session_hash as engine_hash_fn
        engine_hash = engine_hash_fn()
        gate("A5: Session hash — engine parity", engine_hash == manifest_hash,
             f"engine={engine_hash} manifest={manifest_hash}")
    except ImportError:
        gate("A5: Session hash — engine import", False, "could not import k_worker.sessions")

    # Sessions coverage
    ENGINE_SESSIONS = {"ASIA", "LONDON_OPEN", "EU", "NY_PRE", "NY_OPEN", "NY", "NY_CLOSE", "EVENING", "UNKNOWN"}
    gate("ALL session present", "ALL" in sessions_found)
    missing = ENGINE_SESSIONS - sessions_found
    gate("Engine sessions covered", len(missing) == 0,
         f"missing={sorted(missing)}" if missing else f"all {len(ENGINE_SESSIONS)} found")

    # A1: Bounds — wilson_ub >= p_cross
    ub_violations = sum(1 for r in rows if float(r["wilson_ub"]) < float(r["p_cross"]) - 1e-6)
    gate("A1: wilson_ub >= p_cross", ub_violations == 0, f"{ub_violations} violations")

    # A1: p_cross in [0, 1]
    bound_violations = sum(1 for r in rows if not (-1e-9 <= float(r["p_cross"]) <= 1.0 + 1e-9))
    gate("A1: p_cross in [0,1]", bound_violations == 0, f"{bound_violations} violations")

    # A1: effective_n < n
    eff_violations = sum(1 for r in rows if int(r["effective_n"]) > int(r["n"]))
    gate("A1: effective_n <= n", eff_violations == 0, f"{eff_violations} violations")

    # A2: Monotonicity — P decreases with distance (ALL)
    mono_d_violations = 0
    mono_d_total = 0
    for (sess, t), entries in by_session_time.items():
        if sess != "ALL":
            continue
        entries.sort(key=lambda x: x[0])
        for i in range(1, len(entries)):
            mono_d_total += 1
            if entries[i][1] > entries[i - 1][1] + 1e-9:
                mono_d_violations += 1
    gate("A2: P decreases with distance (ALL)", mono_d_violations == 0,
         f"{mono_d_violations}/{mono_d_total}")

    # A2: P increases with time (ALL)
    mono_t_violations = 0
    mono_t_total = 0
    for (sess, d), entries in by_session_dist.items():
        if sess != "ALL":
            continue
        entries.sort(key=lambda x: x[0])
        for i in range(1, len(entries)):
            mono_t_total += 1
            if entries[i][1] < entries[i - 1][1] - 1e-9:
                mono_t_violations += 1
    gate("A2: P increases with time (ALL)", mono_t_violations == 0,
         f"{mono_t_violations}/{mono_t_total}")

    # A1: Sanity bounds
    all_data = {(int(r["distance_usd"]), int(r["secs_remaining"])): float(r["p_cross"])
                for r in rows if r["session"] == "ALL"}
    p_50_900 = all_data.get((50, 900), -1)
    gate("A1: P($50, 900s) > 0.01", p_50_900 > 0.01, f"P={p_50_900:.4f}")
    p_2000_10 = all_data.get((2000, 10), 1)
    gate("A1: P($2000, 10s) < 0.01", p_2000_10 < 0.01, f"P={p_2000_10:.6f}")
    p_100_60 = all_data.get((100, 60), -1)
    gate("A1: P($100, 60s) reasonable", 0.0 < p_100_60 < 0.5, f"P={p_100_60:.4f}")

    # Min sample size
    all_ns = [int(r["effective_n"]) for r in rows if r["session"] == "ALL"]
    min_eff_n = min(all_ns) if all_ns else 0
    gate("Min effective_n >= 100", min_eff_n >= 100, f"min effective_n={min_eff_n}")

    # A3: Brute-force recompute from raw candles
    candles_exist = os.path.exists(CANDLES_PATH)
    if candles_exist:
        print(f"\n  Loading raw candles for A3 recompute...")
        with open(CANDLES_PATH) as f:
            candles = json.load(f)
        print(f"  Loaded {len(candles)} candles")

        closes = [c[4] for c in candles]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]
        n_candles = len(candles)

        a3_cells = [(100, 300), (200, 600), (500, 900), (150, 180), (75, 60)]
        for d, t_secs in a3_cells:
            t_candles = max(1, t_secs // GRANULARITY_SEC)
            sub_scale = math.sqrt(t_secs / GRANULARITY_SEC) if t_secs < GRANULARITY_SEC else 1.0
            crosses = 0
            total = 0
            for i in range(n_candles - t_candles):
                sc = closes[i]
                we = min(i + t_candles + 1, n_candles)
                mh = max(highs[i + 1:we])
                ml = min(lows[i + 1:we])
                mm = max(mh - sc, sc - ml) * sub_scale
                total += 1
                if mm >= d:
                    crosses += 1
            recomputed_p = crosses / total if total > 0 else 0.0
            csv_p = all_data.get((d, t_secs))
            if csv_p is not None:
                diff = abs(recomputed_p - csv_p)
                gate(f"A3: Recompute d=${d} t={t_secs}s", diff < 1e-5,
                     f"csv={csv_p:.6f} recomputed={recomputed_p:.6f} diff={diff:.8f}")
            else:
                gate(f"A3: Recompute d=${d} t={t_secs}s", False, "cell not found")
    else:
        print(f"\n  Raw candles not found ({CANDLES_PATH}) — A3 skipped")
        print("  (A3 runs automatically during self-provisioning build)")

    # Summary
    print(f"\n=== RESULT: {PASS} passed, {FAIL} failed ===")
    if FAIL > 0:
        print("VALIDATION FAILED")
        sys.exit(1)
    else:
        print("VALIDATION PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
