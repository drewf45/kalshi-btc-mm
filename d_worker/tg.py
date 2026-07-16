"""Env-var-driven configuration for KAL-D.

Applied once at boot:
  DW_APPROVED_SERIES: comma-separated series tickers to approve
  DW_BLACKLIST_EXTRA: comma-separated series tickers to blacklist
  DW_CLEAR_HALT: set to "1" to clear halt_promotion on this boot
  DW_CLEAR_LIVE_HALT: set to "1" to clear live_halt on this boot
  DW_RUNG: set current go-live rung (0-3, Drew-only)
"""

import os
import logging

from . import dstore, registry

log = logging.getLogger("d_worker.tg")


def apply_env_approvals() -> list:
    """Approve series listed in DW_APPROVED_SERIES. Returns list of newly approved."""
    raw = os.environ.get("DW_APPROVED_SERIES", "").strip()
    if not raw:
        return []
    approved = []
    for series in raw.split(","):
        series = series.strip().upper()
        if not series:
            continue
        reg = dstore.get_registry(series)
        if not reg:
            log.info(f"[BOOT] DW_APPROVED_SERIES: {series} not yet drafted — skipping")
            continue
        if reg.get("approved_ts"):
            log.info(f"[BOOT] DW_APPROVED_SERIES: {series} already approved "
                     f"(v{reg.get('version')})")
            continue
        station = (reg.get("station_or_ref") or "").strip()
        if not station or station == "UNKNOWN":
            log.error(f"[BOOT] DW_APPROVED_SERIES: REFUSED {series} — "
                      f"station UNKNOWN; correct the registry row first")
            from k_worker import notify
            notify.alert(f"🅳 🚨 APPROVAL REFUSED: {series} — station UNKNOWN. "
                         f"Verify the contract's settlement station, update the "
                         f"registry, then re-approve.")
            continue
        dstore.approve_registry(series, "env")
        log.info(f"[BOOT] DW_APPROVED_SERIES: APPROVED {series} v{reg.get('version')}")
        approved.append(series)
    return approved


def apply_env_blacklist() -> list:
    """Add series listed in DW_BLACKLIST_EXTRA to blacklist. Returns list of newly added."""
    raw = os.environ.get("DW_BLACKLIST_EXTRA", "").strip()
    if not raw:
        return []
    added = []
    for series in raw.split(","):
        series = series.strip().upper()
        if not series:
            continue
        if not registry.is_blacklisted(series):
            registry.add_blacklist(series)
            log.info(f"[BOOT] DW_BLACKLIST_EXTRA: blacklisted {series}")
            added.append(series)
    return added


def apply_env_clear_halt() -> bool:
    """If DW_CLEAR_HALT=1, clear halt_promotion once. Returns True if cleared."""
    raw = os.environ.get("DW_CLEAR_HALT", "").strip()
    if raw != "1":
        return False
    halt = dstore.get_state("halt_promotion")
    if halt != "1":
        log.info("[BOOT] DW_CLEAR_HALT=1 but no halt_promotion active — ignored")
        return False
    dstore.set_state("halt_promotion", "0")
    log.warning("[BOOT] DW_CLEAR_HALT=1 — halt_promotion cleared. "
                "Remove DW_CLEAR_HALT from env to avoid silent re-clears.")
    return True


def apply_env_rung() -> bool:
    """Set current go-live rung from DW_RUNG. Returns True if set."""
    raw = os.environ.get("DW_RUNG", "").strip()
    if not raw:
        return False
    try:
        rung = int(raw)
    except ValueError:
        log.warning(f"[BOOT] DW_RUNG={raw!r} not a valid integer — ignored")
        return False
    if rung < 0 or rung > 3:
        log.warning(f"[BOOT] DW_RUNG={rung} out of range [0,3] — ignored")
        return False
    current = int(dstore.get_state("current_rung") or "0")
    if rung != current:
        dstore.set_state("current_rung", str(rung))
        log.warning(f"[BOOT] DW_RUNG={rung} — rung changed from {current} to {rung}")
    return True


def apply_env_clear_live_halt() -> bool:
    """If DW_CLEAR_LIVE_HALT=1, clear live_halt. Returns True if cleared."""
    raw = os.environ.get("DW_CLEAR_LIVE_HALT", "").strip()
    if raw != "1":
        return False
    halt = dstore.get_state("live_halt")
    if halt != "1":
        log.info("[BOOT] DW_CLEAR_LIVE_HALT=1 but no live_halt active — ignored")
        return False
    dstore.set_state("live_halt", "0")
    dstore.set_state("live_halt_reason", "")
    log.warning("[BOOT] DW_CLEAR_LIVE_HALT=1 — live_halt cleared.")
    return True
