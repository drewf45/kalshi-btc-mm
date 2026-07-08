"""Validate delta_table.csv — hard gates for deployment.

Gates:
1. File exists and parses
2. ≥150 days coverage (from manifest)
3. Monotonicity: P(cross) decreases as distance increases (for fixed time)
4. Monotonicity: P(cross) increases as time increases (for fixed distance)
5. Sanity bounds: P(cross $50 in 900s) should be meaningful, P(cross $2000 in 10s) ≈ 0
6. Session tags match engine's SESSION_WINDOWS
7. SHA-256 match between manifest and actual CSV

Exit 0 = all gates pass. Exit 1 = any gate fails.
"""

import csv
import json
import hashlib
import os
import sys
from collections import defaultdict

BASE_DIR = os.path.dirname(__file__)
CSV_PATH = os.path.join(BASE_DIR, "delta_table.csv")
MANIFEST_PATH = os.path.join(BASE_DIR, "candles_manifest.json")

ENGINE_SESSIONS = {"ASIA", "LONDON_OPEN", "EU", "NY_PRE", "NY_OPEN", "NY", "NY_CLOSE", "EVENING", "UNKNOWN"}

PASS = 0
FAIL = 0


def gate(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    status = "PASS" if ok else "FAIL"
    if not ok:
        FAIL += 1
    else:
        PASS += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    return ok


def main():
    global PASS, FAIL
    print("=== DELTA TABLE VALIDATOR ===\n")

    # Gate 1: Files exist
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

    # Gate 2: ≥150 days
    days = manifest.get("days_covered", 0)
    gate("≥150 days coverage", days >= 150, f"{days:.1f} days")

    # Gate 3: SHA-256 match
    actual_sha = hashlib.sha256(open(CSV_PATH, "rb").read()).hexdigest()
    expected_sha = manifest.get("csv_sha256", "")
    gate("SHA-256 match", actual_sha == expected_sha,
         f"actual={actual_sha[:16]}... expected={expected_sha[:16]}...")

    # Load CSV
    with open(CSV_PATH) as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"\n  Loaded {len(rows)} rows from CSV")

    # Parse into structured data
    # Group by (session, secs_remaining) -> sorted list of (distance, p_cross)
    by_session_time = defaultdict(list)
    # Group by (session, distance) -> sorted list of (secs_remaining, p_cross)
    by_session_dist = defaultdict(list)
    sessions_found = set()

    for r in rows:
        d = int(r["distance_usd"])
        t = int(r["secs_remaining"])
        p = float(r["p_cross"])
        n = int(r["n"])
        sess = r["session"]
        sessions_found.add(sess)
        by_session_time[(sess, t)].append((d, p, n))
        by_session_dist[(sess, d)].append((t, p, n))

    # Gate 4: Session tags — ALL must be present, plus engine sessions
    gate("ALL session present", "ALL" in sessions_found, f"sessions: {sorted(sessions_found)}")
    engine_covered = ENGINE_SESSIONS & sessions_found
    engine_missing = ENGINE_SESSIONS - sessions_found - {"UNKNOWN"}
    gate("Engine sessions covered", len(engine_missing) == 0,
         f"covered={sorted(engine_covered)}, missing={sorted(engine_missing)}" if engine_missing else
         f"all {len(engine_covered)} engine sessions found")

    # Gate 5: Monotonicity — P(cross) decreases as distance increases (ALL session)
    mono_dist_violations = 0
    mono_dist_total = 0
    for (sess, t), entries in by_session_time.items():
        if sess != "ALL":
            continue
        entries.sort(key=lambda x: x[0])
        for i in range(1, len(entries)):
            mono_dist_total += 1
            if entries[i][1] > entries[i-1][1] + 1e-9:
                mono_dist_violations += 1
    gate("Monotonicity: P decreases with distance (ALL)",
         mono_dist_violations == 0,
         f"{mono_dist_violations}/{mono_dist_total} violations")

    # Gate 6: Monotonicity — P(cross) increases as time increases (ALL session)
    mono_time_violations = 0
    mono_time_total = 0
    for (sess, d), entries in by_session_dist.items():
        if sess != "ALL":
            continue
        entries.sort(key=lambda x: x[0])
        for i in range(1, len(entries)):
            mono_time_total += 1
            if entries[i][1] < entries[i-1][1] - 1e-9:
                mono_time_violations += 1
    gate("Monotonicity: P increases with time (ALL)",
         mono_time_violations == 0,
         f"{mono_time_violations}/{mono_time_total} violations")

    # Gate 7: Sanity bounds
    all_data = {(int(r["distance_usd"]), int(r["secs_remaining"])): float(r["p_cross"])
                for r in rows if r["session"] == "ALL"}

    p_50_900 = all_data.get((50, 900), -1)
    gate("P(cross $50, 900s) > 0.01", p_50_900 > 0.01, f"P={p_50_900:.4f}")
    gate("P(cross $50, 900s) < 1.0", p_50_900 < 1.0, f"P={p_50_900:.4f}")

    p_2000_10 = all_data.get((2000, 10), 1)
    gate("P(cross $2000, 10s) < 0.01", p_2000_10 < 0.01, f"P={p_2000_10:.6f}")

    p_100_60 = all_data.get((100, 60), -1)
    gate("P(cross $100, 60s) reasonable", 0.0 < p_100_60 < 0.5, f"P={p_100_60:.4f}")

    # Gate 8: Minimum sample size
    all_ns = [int(r["n"]) for r in rows if r["session"] == "ALL"]
    min_n = min(all_ns) if all_ns else 0
    gate("Min sample size ≥ 1000", min_n >= 1000, f"min N={min_n}")

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
