"""WO-ROT-HOMIE-1 — the Homie pager: a second alert path the LIVE boot accepts.

RULED (Drew, 2026-10-15): the desk's accounting/alert channel on this Mac is
Homie (the OpenClaw agent watching this machine), not Telegram. Telegram stays
supported but is no longer the ONLY thing that satisfies the LIVE pager guard.

THE SAFETY INTENT IS UNCHANGED. The original guard (shadow_runner, R6) refuses
to boot LIVE without a wired pager, because "the ledger of record cannot be
silent" — real money must never run with no one watching. This module keeps
that intent: Homie only counts as the pager when Homie is PROVABLY alive.

THE PROOF IS A HEARTBEAT, mirrored on deadman.py's design. Homie's watch loop
writes HOMIE_PAGER_PATH atomically every cycle with a wall-clock ts. The LIVE
boot (and, later, the running engine) reads it:

  - fresh  (age <= HOMIE_PAGER_MAX_AGE_S)  -> the pager is satisfied, LIVE proceeds.
  - stale/missing                          -> Homie is NOT watching; the engine
                                              DOWNGRADES to SHADOW (Drew's ruling
                                              2026-10-15: boot, do not die; resume
                                              LIVE when the pager is fresh again).

A hardcoded "Homie is here" would defeat the guard — a dead watch loop would
boot LIVE blind. The freshness check is the whole point: no proof, no real money.
"""

import json
import os
import time
from typing import Optional

from . import config


def write_pager(path: str = None, *, note: str = "",
                now: Optional[float] = None) -> dict:
    """Atomically stamp the Homie pager file (tmp + rename, never a half read).
    Homie's watch loop calls this each cycle to prove it is alive."""
    path = path or config.HOMIE_PAGER_PATH
    payload = {
        "ts": time.time() if now is None else now,
        "writer": "homie",
        "note": note or "",
    }
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    return payload


def pager_age(path: str = None, now: Optional[float] = None) -> Optional[float]:
    """Seconds since the pager was last stamped, or None if missing/unreadable."""
    path = path or config.HOMIE_PAGER_PATH
    try:
        with open(path) as f:
            hb = json.load(f)
    except (OSError, ValueError):
        return None
    if "ts" not in hb:
        return None
    return (time.time() if now is None else now) - float(hb["ts"])


def is_fresh(path: str = None, now: Optional[float] = None,
             max_age_s: float = None) -> bool:
    """True when the pager exists and is newer than max_age_s — i.e. Homie is
    provably watching right now."""
    max_age_s = config.HOMIE_PAGER_MAX_AGE_S if max_age_s is None else max_age_s
    age = pager_age(path, now)
    return age is not None and age <= max_age_s


def pager_satisfies_live() -> bool:
    """The guard's question: may Homie stand in for the Telegram pager at THIS
    boot? Only when enabled AND provably fresh."""
    return config.HOMIE_PAGER_ENABLED and is_fresh()


def status_line(path: str = None, now: Optional[float] = None) -> str:
    """One-line human status for the boot tape."""
    if not config.HOMIE_PAGER_ENABLED:
        return "HOMIE PAGER: disabled (HOMIE_PAGER unset) — Telegram is the only LIVE pager"
    age = pager_age(path, now)
    if age is None:
        return (f"HOMIE PAGER: MISSING {path or config.HOMIE_PAGER_PATH} — "
                f"stale counts as no pager (LIVE would downgrade to SHADOW)")
    state = "FRESH" if age <= config.HOMIE_PAGER_MAX_AGE_S else "STALE"
    return (f"HOMIE PAGER: {state} age {age:.0f}s "
            f"(max {config.HOMIE_PAGER_MAX_AGE_S:.0f}s)")
