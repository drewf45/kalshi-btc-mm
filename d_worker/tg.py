"""Env-var-driven approval + blacklist + halt-clear for KAL-D.

No Telegram polling — k_worker owns the bot token exclusively.
Configuration via env vars, applied once at boot:
  DW_APPROVED_SERIES: comma-separated series tickers to approve
  DW_BLACKLIST_EXTRA: comma-separated series tickers to blacklist
  DW_CLEAR_HALT: set to "1" to clear halt_promotion on this boot
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
