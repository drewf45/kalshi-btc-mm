"""WO-A repair: fix stale seconds_to_expiry on ladder-pass rows.

The watch ladder computed secs_to_expiry before waiting; the surface row
recorded the pre-wait value instead of the true decision-time value.
The correct value is embedded in the why_tag as _T-(\d+).

Usage: python -m k_worker.repair_ts [--dry-run]
"""

import re
import sys
import logging

from . import store

log = logging.getLogger("k_worker.repair_ts")


def repair(dry_run: bool = False) -> int:
    store.init_db()

    with store._lock:
        rows = store._conn.execute(
            """SELECT id, why_tag, seconds_to_expiry
               FROM surface
               WHERE why_tag LIKE '%_confirms_%'
               AND seconds_to_expiry IS NOT NULL"""
        ).fetchall()

    repaired = 0
    for row_id, why_tag, stored_tte in rows:
        m = re.search(r'_T-(\d+)', why_tag)
        if not m:
            continue
        true_tte = int(m.group(1))
        diff = abs(stored_tte - true_tte)
        if diff > 5:
            if dry_run:
                print(f"  WOULD REPAIR id={row_id}: stored={stored_tte:.0f} → "
                      f"{true_tte} (Δ{diff:.0f}s) tag={why_tag[:60]}")
            else:
                new_tag = f"{why_tag}|TS_REPAIRED"
                with store._lock:
                    store._conn.execute(
                        "UPDATE surface SET seconds_to_expiry=?, why_tag=? WHERE id=?",
                        (true_tte, new_tag, row_id),
                    )
                    store._conn.commit()
                print(f"  REPAIRED id={row_id}: {stored_tte:.0f} → "
                      f"{true_tte} (Δ{diff:.0f}s)")
            repaired += 1

    print(f"{'Would repair' if dry_run else 'Repaired'} "
          f"{repaired}/{len(rows)} ladder rows")
    return repaired


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    dry = "--dry-run" in sys.argv
    repair(dry_run=dry)
