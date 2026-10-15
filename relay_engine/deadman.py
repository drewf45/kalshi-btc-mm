"""WO-ROT-1 Chunk 1 — the local heartbeat and the disk guard.

HEARTBEAT (ruled 2026-10-14: local file + hourly Telegram line, no outside
ping for now). Every completed cycle writes HEARTBEAT_PATH atomically
(tmp + rename, so a watcher never reads half a file) with the wall-clock ts,
cycle count, open positions, deploy %, and last error. A killed process stops
writing; the file goes stale HEARTBEAT_STALE_S later. Any local agent can watch
it: `python -m relay_engine.deadman --check` exits 0 fresh, 1 stale, 2 missing.
HEARTBEAT_PING_URL stays in code, unset and off.

DISK GUARD. At boot and hourly: log free disk and DB size. Under
DISK_MIN_FREE_BYTES free: page and halt new entries (ledger halt DISK_LOW,
cleared automatically once free space is back above the floor; the
DURABLE_WRITE halt, by contrast, clears only on restart).
"""

import json
import logging
import os
import shutil
import sys
import time
from typing import Optional

from . import config

log = logging.getLogger("relay.deadman")

DISK_HALT = "DISK_LOW"


# ── heartbeat ────────────────────────────────────────────────────

def write_heartbeat(path: str, *, cycles: int, open_positions: int,
                    deploy_pct: Optional[float], last_error: str = "",
                    now: Optional[float] = None) -> dict:
    """Atomically write the heartbeat file. Returns the payload written.
    deploy_pct None means unknown (book unreadable) — written as null, never 0."""
    payload = {
        "ts": time.time() if now is None else now,
        "pid": os.getpid(),
        "cycles": int(cycles),
        "open_positions": int(open_positions),
        "deploy_pct": deploy_pct,
        "last_error": last_error or "",
        "run_mode": config.RUN_MODE,
        "desk_mode": config.DESK_MODE,
    }
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    return payload


def read_heartbeat(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def heartbeat_age(path: str, now: Optional[float] = None) -> Optional[float]:
    hb = read_heartbeat(path)
    if hb is None or "ts" not in hb:
        return None
    return (time.time() if now is None else now) - float(hb["ts"])


def is_stale(path: str, now: Optional[float] = None,
             max_age_s: float = config.HEARTBEAT_STALE_S) -> bool:
    """True when the heartbeat is missing or older than max_age_s."""
    age = heartbeat_age(path, now)
    return age is None or age > max_age_s


def hourly_line(cycles: int, trips: int, net_cents: int, open_positions: int,
                deploy_pct: Optional[float]) -> str:
    """Unknown values print n/a — never 0 (No Silent Fallbacks)."""
    dep = "n/a" if deploy_pct is None else f"{deploy_pct:.0%}"
    tr = "n/a" if trips is None else str(trips)
    if net_cents is None:
        net = "n/a"
    else:
        net = f"{'-' if net_cents < 0 else '+'}${abs(net_cents) / 100:.2f}"
    return (f"💓 alive · cycles {cycles} · trips {tr} · net {net} · "
            f"open {open_positions} · deploy {dep}")


# ── disk guard ───────────────────────────────────────────────────

def disk_status(db_path: str) -> dict:
    """Free bytes on the DB's filesystem and the DB file's size (+ WAL/journal)."""
    probe = db_path if os.path.isdir(db_path) else (os.path.dirname(os.path.abspath(db_path)) or ".")
    free = shutil.disk_usage(probe).free
    size = 0
    for suffix in ("", "-wal", "-journal", "-shm"):
        try:
            size += os.path.getsize(db_path + suffix)
        except OSError:
            pass
    return {"free_bytes": free, "db_bytes": size, "probe": probe}


def disk_line(st: dict) -> str:
    return (f"DISK: free {st['free_bytes'] / 2**20:,.0f} MB on {st['probe']} · "
            f"DB {st['db_bytes'] / 2**20:,.1f} MB · floor "
            f"{config.DISK_MIN_FREE_BYTES / 2**20:,.0f} MB")


def check_disk(ledger, min_free_bytes: int = None, status: dict = None) -> dict:
    """Log the disk line; under the floor, halt entries (ledger DISK_LOW) and page
    once per episode; back above it, lift DISK_LOW. Returns the status dict
    with `low` set. An in-memory DB has no file: probed at the CWD."""
    floor = config.DISK_MIN_FREE_BYTES if min_free_bytes is None else min_free_bytes
    st = status if status is not None else disk_status(
        ledger.db_path if ledger.db_path != ":memory:" else ".")
    st["low"] = st["free_bytes"] < floor
    log.info(disk_line(st))
    if st["low"]:
        if DISK_HALT not in ledger.entry_halts:
            ledger.entry_halts.add(DISK_HALT)
            from . import failures
            failures.fail(
                "DISK_LOW",
                f"free disk {st['free_bytes'] / 2**20:,.0f} MB < floor "
                f"{floor / 2**20:,.0f} MB (DB {st['db_bytes'] / 2**20:,.1f} MB) — "
                "NO NEW ENTRIES until space is freed; exits and custody continue",
                fatal=False, alert=True, free_bytes=st["free_bytes"],
                db_bytes=st["db_bytes"])
    elif DISK_HALT in ledger.entry_halts:
        ledger.entry_halts.discard(DISK_HALT)
        log.warning("DISK_LOW cleared: free %d bytes", st["free_bytes"])
    return st


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = config.HEARTBEAT_PATH
    if "--path" in argv:
        path = argv[argv.index("--path") + 1]
    age = heartbeat_age(path)
    if age is None:
        print(f"MISSING {path}")
        return 2
    stale = age > config.HEARTBEAT_STALE_S
    print(f"{'STALE' if stale else 'FRESH'} age {age:.1f}s {path}")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
