r"""WO-A repair: fix stale seconds_to_expiry on ladder-pass rows.

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
    if store._conn is None:
        store.init_db()

    with store._lock:
        rows = store._conn.execute(
            """SELECT id, why_tag, seconds_to_expiry
               FROM surface
               WHERE why_tag LIKE '%_confirms_%'
               AND seconds_to_expiry IS NOT NULL"""
        ).fetchall()

    updates = []
    for row_id, why_tag, stored_tte in rows:
        m = re.search(r'_T-(\d+)', why_tag)
        if not m:
            continue
        true_tte = int(m.group(1))
        diff = abs(stored_tte - true_tte)
        if diff > 5:
            new_tag = f"{why_tag}|TS_REPAIRED"
            if dry_run:
                print(f"  WOULD REPAIR id={row_id}: stored={stored_tte:.0f} -> "
                      f"{true_tte} (d{diff:.0f}s) tag={why_tag[:60]}")
            updates.append((true_tte, new_tag, row_id))

    if updates and not dry_run:
        with store._lock:
            store._conn.executemany(
                "UPDATE surface SET seconds_to_expiry=?, why_tag=? WHERE id=?",
                updates,
            )
            store._conn.commit()

    repaired = len(updates)
    print(f"{'Would repair' if dry_run else 'Repaired'} "
          f"{repaired}/{len(rows)} ladder rows")
    return repaired


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    dry = "--dry-run" in sys.argv
    repair(dry_run=dry)
