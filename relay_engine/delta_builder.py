"""Self-provisioning delta table builder — borrowed WHOLE from the live tree
(B1: k_worker/delta_table_builder.py, vendored byte-identical at
reference/live_k_worker/delta_table_builder.py). Adapted ONLY in module wiring
(imports, notify hook). Every grid, threshold, validator gate, and the Coinbase
candle fetcher are the live code's.

Background thread that fetches 180d BTC 1-min candles from Coinbase,
computes P(cross) + wilson_ub for each (distance, time, session) cell,
validates, and hot-loads into the running engine. Weekly refresh on timer.

The engine trades normally on static gates while the table builds
(TABLE_ABSENT path). Never blocks or delays a market cycle.

AUTH EXEMPTION (WO-P1 §2.3): the Coinbase candle fetch below is a PUBLIC
endpoint — it is explicitly exempt from the venue signing scheme in
relay_engine/auth.py. Every Kalshi call goes through auth.signed_request.
"""

import csv
import json
import hashlib
import math
import os
import threading
import time
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional, List, Tuple

import requests

from .sessions import session_tag, session_hash, SESSION_WINDOWS

log = logging.getLogger("relay.delta_builder")

COINBASE_CANDLE_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
MAX_CANDLES_PER_REQUEST = 300
GRANULARITY_SEC = 60

DISTANCE_GRID = list(range(50, 2001, 5))
TIME_GRID = [10, 30, 60, 120, 180, 300, 600, 900]
INDEPENDENT_WINDOW_SEC = 900

REFRESH_INTERVAL_SEC = 7 * 86400

_builder_thread: Optional[threading.Thread] = None
_building = False


def _data_dir() -> str:
    db_path = os.environ.get("RELAY_DB_PATH", "")
    if db_path and os.path.dirname(db_path):
        return os.path.dirname(db_path)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def csv_path() -> str:
    return os.path.join(_data_dir(), "delta_table.csv")


def manifest_path() -> str:
    return os.path.join(_data_dir(), "candles_manifest.json")


def candles_path() -> str:
    return os.path.join(_data_dir(), "btc_candles_raw.json")


def fetch_candles_coinbase(days: int = 180) -> list:
    """Fetch 1-min BTC-USD candles from Coinbase public API (no auth)."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    all_candles = []
    cursor = start

    session = requests.Session()
    session.headers.update({"User-Agent": "k-worker-delta/1.0"})

    batch = 0
    while cursor < end:
        batch += 1
        chunk_end = min(
            cursor + timedelta(seconds=GRANULARITY_SEC * MAX_CANDLES_PER_REQUEST),
            end,
        )
        params = {
            "start": cursor.isoformat(),
            "end": chunk_end.isoformat(),
            "granularity": GRANULARITY_SEC,
        }

        for attempt in range(5):
            try:
                resp = session.get(COINBASE_CANDLE_URL, params=params, timeout=15)
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt == 4:
                    raise RuntimeError(f"Coinbase fetch failed after 5 attempts at {cursor}: {e}")
                time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"Exhausted retries at {cursor}")

        # Coinbase format: [[time, low, high, open, close, volume], ...]
        for row in data:
            ts, low, high, opn, close, vol = row
            all_candles.append((int(ts), float(opn), float(high), float(low), float(close), float(vol)))

        if batch % 200 == 0:
            log.info(f"[BUILDER] Fetched {len(all_candles)} candles through "
                     f"{chunk_end.strftime('%Y-%m-%d')}...")

        cursor = chunk_end
        time.sleep(0.12)

    all_candles.sort(key=lambda c: c[0])

    # Deduplicate
    seen = set()
    deduped = []
    for c in all_candles:
        if c[0] not in seen:
            seen.add(c[0])
            deduped.append(c)

    log.info(f"[BUILDER] Total candles: {len(deduped)} ({len(deduped) / 1440:.1f} days)")
    return deduped


def _wilson_ub(p: float, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 1.0
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return min(1.0, centre + spread)


def _wilson_lb(p: float, n: int, z: float = 1.96) -> float:
    """WO-2026-07-24-J P1: the Wilson LOWER bound — HUNT's settle gate reads the
    conservative bound (an edge that clears fees at the LB, never the point)."""
    if n == 0:
        return 0.0
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return max(0.0, centre - spread)


def compute_delta_table(candles: list) -> list:
    """Compute P(cross) + wilson_ub for each (distance, time, session) cell.

    p_cross measures ANY-TOUCH: the probability that the max intraperiod
    excursion (candle highs/lows vs the start close) reaches distance d at
    ANY moment within the remaining time — NOT the at-close outcome.

    wilson_ub uses effective_n = distinct 15-minute windows, not overlapping
    minute observations. This prevents overstated confidence from correlated
    samples.
    """
    n_candles = len(candles)
    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    timestamps = [c[0] for c in candles]

    independent_windows = max(1, n_candles // (INDEPENDENT_WINDOW_SEC // GRANULARITY_SEC))

    log.info(f"[BUILDER] Computing: {len(DISTANCE_GRID)} distances x "
             f"{len(TIME_GRID)} times x {n_candles} candles "
             f"(effective_n={independent_windows})")

    results = []
    all_sessions = set()

    for t_secs in TIME_GRID:
        t_candles = max(1, t_secs // GRANULARITY_SEC)
        sub_minute_scale = math.sqrt(t_secs / GRANULARITY_SEC) if t_secs < GRANULARITY_SEC else 1.0

        session_crosses = defaultdict(lambda: defaultdict(int))
        session_ends = defaultdict(lambda: defaultdict(int))
        session_n = defaultdict(int)
        total_crosses = defaultdict(int)
        total_ends = defaultdict(int)
        total_n = 0

        for i in range(n_candles - t_candles):
            start_close = closes[i]
            window_end = min(i + t_candles + 1, n_candles)

            max_high = max(highs[i + 1:window_end])
            min_low = min(lows[i + 1:window_end])
            max_move = max(max_high - start_close, start_close - min_low) * sub_minute_scale
            # WO-2026-07-24-J P1: the SETTLE move — where the window ENDED, not
            # what it touched. |close_at_window_end − start| is the settle-
            # question analog of the touch max_move (same sub-minute scaling).
            end_close = closes[window_end - 1]
            end_move = abs(end_close - start_close) * sub_minute_scale

            sess = session_tag(timestamps[i])
            all_sessions.add(sess)
            session_n[sess] += 1
            total_n += 1

            for d in DISTANCE_GRID:
                if max_move >= d:
                    total_crosses[d] += 1
                    session_crosses[sess][d] += 1
                if end_move >= d:
                    total_ends[d] += 1
                    session_ends[sess][d] += 1
                elif max_move < d:
                    break   # both are monotone in d — nothing larger can hit

        # ALL session rows
        for d in DISTANCE_GRID:
            p = total_crosses[d] / total_n if total_n > 0 else 0.0
            pe = total_ends[d] / total_n if total_n > 0 else 0.0
            eff_n = independent_windows
            results.append({
                "distance_usd": d,
                "secs_remaining": t_secs,
                "p_cross": round(p, 6),
                "n": total_n,
                "effective_n": eff_n,
                "wilson_ub": round(_wilson_ub(p, eff_n), 6),
                "p_end": round(pe, 6),
                "p_end_n": total_n,
                "p_end_wilson_lb": round(_wilson_lb(pe, eff_n), 6),
                "session": "ALL",
            })

        # Per-session rows
        for sess in sorted(all_sessions):
            n_s = session_n[sess]
            eff_n_s = max(1, n_s // (INDEPENDENT_WINDOW_SEC // GRANULARITY_SEC))
            for d in DISTANCE_GRID:
                p = session_crosses[sess][d] / n_s if n_s > 0 else 0.0
                pe = session_ends[sess][d] / n_s if n_s > 0 else 0.0
                results.append({
                    "distance_usd": d,
                    "secs_remaining": t_secs,
                    "p_cross": round(p, 6),
                    "n": n_s,
                    "effective_n": eff_n_s,
                    "wilson_ub": round(_wilson_ub(p, eff_n_s), 6),
                    "p_end": round(pe, 6),
                    "p_end_n": n_s,
                    "p_end_wilson_lb": round(_wilson_lb(pe, eff_n_s), 6),
                    "session": sess,
                })

        log.info(f"[BUILDER]   T={t_secs:4d}s: N={total_n} eff_N={independent_windows} "
                 f"P($100)={total_crosses.get(100, 0) / max(total_n, 1):.4f} "
                 f"P($500)={total_crosses.get(500, 0) / max(total_n, 1):.6f}")

    return results


CSV_FIELDS = ["distance_usd", "secs_remaining", "p_cross", "n", "effective_n",
              "wilson_ub", "p_end", "p_end_n", "p_end_wilson_lb", "session"]


def write_csv(rows: list, path: str) -> str:
    """Write delta table CSV, return SHA-256."""
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    sha = hashlib.sha256(open(tmp, "rb").read()).hexdigest()
    os.replace(tmp, path)
    return sha


def write_manifest(candles: list, csv_sha: str, path: str) -> dict:
    """Write candles manifest JSON."""
    timestamps = [c[0] for c in candles]
    date_min = datetime.fromtimestamp(min(timestamps), tz=timezone.utc).strftime("%Y-%m-%d")
    date_max = datetime.fromtimestamp(max(timestamps), tz=timezone.utc).strftime("%Y-%m-%d")
    days_covered = (max(timestamps) - min(timestamps)) / 86400

    manifest = {
        "source": "Coinbase BTC-USD 1-min candles (public)",
        "date_range": f"{date_min} to {date_max}",
        "days_covered": round(days_covered, 1),
        "candle_count": len(candles),
        "granularity_sec": GRANULARITY_SEC,
        "distance_grid": f"${min(DISTANCE_GRID)}-${max(DISTANCE_GRID)} step $5",
        "time_grid_sec": TIME_GRID,
        "effective_n_method": "distinct 15-min windows (n / 15)",
        "session_hash": session_hash(),
        "csv_sha256": csv_sha,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def save_candles(candles: list, path: str) -> None:
    """Save raw candles to JSON for validator A3 recompute."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(candles, f)
    os.replace(tmp, path)
    log.info(f"[BUILDER] Saved {len(candles)} raw candles to {path}")


def validate_in_process(csv_file: str, manifest_file: str,
                        candles: list) -> Tuple[bool, str]:
    """Run validator checks in-process. Returns (pass, transcript)."""
    lines = ["=== DELTA TABLE VALIDATOR (in-process) ==="]
    passes = 0
    fails = 0

    def gate(name, ok, detail=""):
        nonlocal passes, fails
        status = "PASS" if ok else "FAIL"
        if ok:
            passes += 1
        else:
            fails += 1
        line = f"  [{status}] {name}" + (f" — {detail}" if detail else "")
        lines.append(line)
        return ok

    # A4: Coverage
    with open(manifest_file) as f:
        manifest = json.load(f)
    days = manifest.get("days_covered", 0)
    gate("A4: >=150 days coverage", days >= 150, f"{days:.1f} days")

    # SHA match
    actual_sha = hashlib.sha256(open(csv_file, "rb").read()).hexdigest()
    gate("SHA-256 match", actual_sha == manifest.get("csv_sha256", ""),
         f"actual={actual_sha[:16]}...")

    # Source check
    source = manifest.get("source", "")
    gate("Source is Coinbase (not synthetic)", "Coinbase" in source and "Synthetic" not in source,
         source[:60])

    # Load CSV
    with open(csv_file) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    lines.append(f"  Loaded {len(rows)} rows")

    # Check schema
    required_cols = {"distance_usd", "secs_remaining", "p_cross", "n", "effective_n", "wilson_ub", "session"}
    actual_cols = set(rows[0].keys()) if rows else set()
    gate("Schema has all required columns", required_cols.issubset(actual_cols),
         f"missing={required_cols - actual_cols}" if not required_cols.issubset(actual_cols) else "OK")

    # WO-2026-07-24-J P1: the SETTLE surface — question-tagged (ADVERSARY ii: a
    # table that cannot say WHICH question it answers fails). A tape built after
    # this WO carries the p_end column family; validate it alongside the touch
    # surface (a legacy touch-only tape passes without it and HUNT stays BLIND).
    settle_cols = {"p_end", "p_end_n", "p_end_wilson_lb"}
    has_settle = settle_cols.issubset(actual_cols)
    if has_settle:
        # A1: p_end in [0,1]; the settle LB never exceeds the point (LOWER bound)
        pe_bound = sum(1 for r in rows
                       if not (-1e-9 <= float(r["p_end"]) <= 1.0 + 1e-9))
        gate("A1: p_end in [0,1]", pe_bound == 0, f"{pe_bound} violations")
        lb_viol = sum(1 for r in rows
                      if float(r["p_end_wilson_lb"]) > float(r["p_end"]) + 1e-6)
        gate("A1: p_end_wilson_lb <= p_end", lb_viol == 0, f"{lb_viol} violations")
        # SCIENTIST falsifiability: the settle can NEVER exceed the touch (a
        # window that closes beyond d necessarily touched d) — the physical law
        # that proves the two surfaces answer DIFFERENT questions, not the same
        # number mislabeled (ADVERSARY ii). p_end <= p_cross everywhere.
        pe_le_pc = sum(1 for r in rows
                       if float(r["p_end"]) > float(r["p_cross"]) + 1e-6)
        gate("A1: p_end <= p_cross (settle ⊆ touch)", pe_le_pc == 0,
             f"{pe_le_pc} violations")
    else:
        lines.append("  (legacy touch-only tape — no settle surface; HUNT BLIND)")

    # A5: Session hash parity
    builder_hash = session_hash()
    manifest_hash = manifest.get("session_hash", "")
    gate("A5: Session hash parity (builder)", builder_hash == manifest_hash,
         f"builder={builder_hash} manifest={manifest_hash}")

    # A1: Bounds
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

    gate("ALL session present", "ALL" in sessions_found)

    engine_sessions = {s[0] for s in SESSION_WINDOWS} | {"UNKNOWN"}
    missing = engine_sessions - sessions_found
    gate("Engine sessions covered", len(missing) == 0,
         f"missing={sorted(missing)}" if missing else f"all {len(engine_sessions)} found")

    # A1: wilson_ub >= p_cross everywhere
    ub_violations = sum(1 for r in rows if float(r["wilson_ub"]) < float(r["p_cross"]) - 1e-6)
    gate("A1: wilson_ub >= p_cross", ub_violations == 0, f"{ub_violations} violations")

    # A1: p_cross in [0, 1]
    bound_violations = sum(1 for r in rows if not (-1e-9 <= float(r["p_cross"]) <= 1.0 + 1e-9))
    gate("A1: p_cross in [0,1]", bound_violations == 0, f"{bound_violations} violations")

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

    # A3: Brute-force recompute of random cells
    a3_cells = [(100, 300), (200, 600), (500, 900), (150, 180), (75, 60)]
    closes = [c[4] for c in candles]
    highs_arr = [c[2] for c in candles]
    lows_arr = [c[3] for c in candles]
    n_candles = len(candles)
    a3_pass = True

    for d, t_secs in a3_cells:
        t_candles_val = max(1, t_secs // GRANULARITY_SEC)
        sub_scale = math.sqrt(t_secs / GRANULARITY_SEC) if t_secs < GRANULARITY_SEC else 1.0
        crosses = 0
        total = 0
        for i in range(n_candles - t_candles_val):
            sc = closes[i]
            we = min(i + t_candles_val + 1, n_candles)
            mh = max(highs_arr[i + 1:we])
            ml = min(lows_arr[i + 1:we])
            mm = max(mh - sc, sc - ml) * sub_scale
            total += 1
            if mm >= d:
                crosses += 1
        recomputed_p = crosses / total if total > 0 else 0.0
        csv_p = all_data.get((d, t_secs))
        if csv_p is not None:
            diff = abs(recomputed_p - csv_p)
            ok = diff < 1e-5
            if not ok:
                a3_pass = False
            detail = f"d={d} t={t_secs}: csv={csv_p:.6f} recomputed={recomputed_p:.6f} diff={diff:.8f}"
            gate(f"A3: Recompute d=${d} t={t_secs}s", ok, detail)
        else:
            gate(f"A3: Recompute d=${d} t={t_secs}s", False, "cell not found in CSV")
            a3_pass = False

    # Effective N check
    eff_ns = [int(r["effective_n"]) for r in rows if r["session"] == "ALL"]
    if eff_ns:
        eff_n = eff_ns[0]
        expected_eff = n_candles // (INDEPENDENT_WINDOW_SEC // GRANULARITY_SEC)
        gate("Effective N matches expected", abs(eff_n - expected_eff) <= 1,
             f"eff_n={eff_n} expected={expected_eff}")

    lines.append(f"\n=== RESULT: {passes} passed, {fails} failed ===")
    lines.append("VALIDATION PASSED" if fails == 0 else "VALIDATION FAILED")

    transcript = "\n".join(lines)
    return fails == 0, transcript


def build_table(notify_fn=None) -> bool:
    """Full build: fetch, compute, validate, hot-load. Returns True on success."""
    global _building
    _building = True
    transcript_lines = []

    def tlog(msg):
        log.info(msg)
        transcript_lines.append(msg)

    try:
        tlog("[BUILDER] Starting delta table build...")
        data_d = _data_dir()
        os.makedirs(data_d, exist_ok=True)

        tlog(f"[BUILDER] Fetching 180d BTC-USD 1-min candles from Coinbase...")
        candles = fetch_candles_coinbase(180)
        days_covered = (candles[-1][0] - candles[0][0]) / 86400

        if days_covered < 150:
            tlog(f"[BUILDER] FAIL: Only {days_covered:.1f} days (need >=150)")
            if notify_fn:
                notify_fn("\n".join(transcript_lines))
            return False

        tlog(f"[BUILDER] Got {len(candles)} candles ({days_covered:.1f} days)")

        save_candles(candles, candles_path())

        tlog("[BUILDER] Computing delta table...")
        rows = compute_delta_table(candles)

        csv_p = csv_path()
        sha = write_csv(rows, csv_p)
        tlog(f"[BUILDER] Wrote {len(rows)} rows, SHA-256: {sha[:16]}...")

        manifest = write_manifest(candles, sha, manifest_path())
        tlog(f"[BUILDER] Manifest: {manifest['date_range']}, session_hash={manifest['session_hash']}")

        tlog("[BUILDER] Running validator...")
        ok, validator_transcript = validate_in_process(csv_p, manifest_path(), candles)
        tlog(validator_transcript)

        if ok:
            from . import delta as delta_table_loader
            delta_table_loader.load(csv_p)
            tlog("[BUILDER] Hot-loaded into running engine")

            if notify_fn:
                notify_fn(f"DELTA TABLE BUILT: {days_covered:.0f}d, "
                          f"{len(candles)} candles, "
                          f"eff_N={len(candles) // 15}, "
                          f"validator PASS\n{validator_transcript}")
            return True
        else:
            tlog("[BUILDER] Validator FAILED — table NOT loaded")
            if notify_fn:
                notify_fn(f"DELTA TABLE BUILD FAILED:\n{validator_transcript}")
            return False

    except Exception as e:
        msg = f"[BUILDER] Build error: {e}"
        tlog(msg)
        log.error(msg, exc_info=True)
        if notify_fn:
            notify_fn(f"DELTA TABLE BUILD ERROR: {e}")
        return False
    finally:
        _building = False


def is_building() -> bool:
    return _building


def start_background_build(notify_fn=None) -> None:
    """Start the builder in a background daemon thread."""
    global _builder_thread
    if _building:
        log.info("[BUILDER] Already building, skipping")
        return

    def _run():
        build_table(notify_fn=notify_fn)
        _schedule_next_refresh(notify_fn)

    _builder_thread = threading.Thread(target=_run, daemon=True, name="delta-builder")
    _builder_thread.start()
    log.info("[BUILDER] Background build started")


def _schedule_next_refresh(notify_fn=None) -> None:
    """Schedule the next weekly rebuild."""
    def _refresh():
        time.sleep(REFRESH_INTERVAL_SEC)
        log.info("[BUILDER] Weekly refresh triggered")
        build_table(notify_fn=notify_fn)
        _schedule_next_refresh(notify_fn)

    t = threading.Thread(target=_refresh, daemon=True, name="delta-refresh")
    t.start()
