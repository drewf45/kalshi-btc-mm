"""Inbound Telegram commands for KAL-D.

Polls the same bot as k_worker; ignores non-KAL-D commands.
Commands: /approve, /dstatus, /dclear, /dblacklist, /dunblacklist.
"""

import os
import time
import logging
import threading

import requests

from . import dstore, registry, pack

log = logging.getLogger("d_worker.tg")

_BOT_TOKEN = ""
_CHAT_ID = ""
_SESSION = requests.Session()
_DW_COMMANDS = {"/approve", "/dstatus", "/dclear", "/dblacklist", "/dunblacklist"}


def init() -> None:
    global _BOT_TOKEN, _CHAT_ID
    _BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    _CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def _reply(text: str) -> None:
    if not _BOT_TOKEN or not _CHAT_ID:
        return
    try:
        _SESSION.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json={"chat_id": _CHAT_ID, "text": f"🅳 {text}",
                  "disable_web_page_preview": True},
            timeout=10)
    except Exception as e:
        log.warning(f"[TG] Reply failed: {e}")


def poll_loop() -> None:
    """Long-poll getUpdates, dispatch KAL-D commands."""
    if not _BOT_TOKEN or not _CHAT_ID:
        log.warning("[TG] Not configured — listener disabled")
        return

    raw_offset = dstore.get_state("dw_tg_update_offset")
    offset = int(raw_offset) if raw_offset else 0

    while True:
        try:
            resp = _SESSION.get(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30,
                        "allowed_updates": '["message"]'},
                timeout=35)
            if resp.status_code != 200:
                log.warning(f"[TG] getUpdates HTTP {resp.status_code}")
                time.sleep(5)
                continue
            updates = resp.json().get("result", [])
            for update in updates:
                uid = update.get("update_id", 0)
                offset = uid + 1
                msg = update.get("message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                if str(chat.get("id", "")) != _CHAT_ID:
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                cmd = text.split()[0].lower()
                if cmd in _DW_COMMANDS:
                    _handle(cmd, text)
            if updates:
                dstore.set_state("dw_tg_update_offset", str(offset))
        except Exception as e:
            log.warning(f"[TG] Listener error: {e}")
            time.sleep(10)


def _handle(cmd: str, text: str) -> None:
    parts = text.split()

    if cmd == "/approve":
        if len(parts) < 2:
            _reply("Usage: /approve <series_ticker>")
            return
        series = parts[1].upper()
        reg = dstore.get_registry(series)
        if not reg:
            _reply(f"No registry entry for {series}. "
                   f"It will be auto-drafted when first seen in a sweep.")
            return
        if reg.get("approved_ts"):
            _reply(f"{series} already approved (v{reg.get('version')})")
            return
        dstore.approve_registry(series, "drew-telegram")
        _reply(f"APPROVED: {series} v{reg.get('version')}\n"
               f"  source: {reg.get('settle_source')}\n"
               f"  station: {reg.get('station_or_ref')}\n"
               f"  units: {reg.get('units')} rounding: {reg.get('rounding')}")
        return

    if cmd == "/dstatus":
        _send_mini_status()
        return

    if cmd == "/dclear":
        halt = dstore.get_state("halt_promotion")
        if halt != "1":
            _reply("No halt_promotion active.")
            return
        dstore.set_state("halt_promotion", "0")
        _reply("halt_promotion cleared — SEED verdicts resume.")
        return

    if cmd == "/dblacklist":
        if len(parts) < 2:
            _reply("Usage: /dblacklist <series_ticker>")
            return
        series = parts[1].upper()
        registry.add_blacklist(series)
        _reply(f"Blacklisted: {series}")
        return

    if cmd == "/dunblacklist":
        if len(parts) < 2:
            _reply("Usage: /dunblacklist <series_ticker>")
            return
        series = parts[1].upper()
        registry.remove_blacklist(series)
        _reply(f"Unblacklisted: {series}")
        return


def _send_mini_status() -> None:
    """Sections 2,3,6,7 from the pack."""
    midnight = dstore.et_midnight_ts()
    stats = dstore.seed_stats_since(midnight)
    lt = stats["lifetime_settled"]
    lc = stats["lifetime_correct"]
    pct = lc / lt * 100 if lt > 0 else 0

    all_reg = dstore.list_registry()
    approved = sum(1 for r in all_reg if r.get("approved_ts"))
    drafted = sum(1 for r in all_reg if not r.get("approved_ts"))
    fees = len(dstore.list_fees())

    sim_used = dstore.sim_capital_used()
    import os
    sim_cap = float(os.environ.get("DW_SIM_CAPITAL_USD", "10.00"))

    from . import scanner
    gov = scanner._get_governor()

    lines = [
        f"SEEDS: {stats['new']} new | {stats['open']} open | sim ${sim_used:.2f}/${sim_cap:.2f}",
        f"SETTLED: {stats['today_settled']} | classifier {lc}/{lt} ({pct:.1f}%)",
        f"REGISTRY: fees {fees} | {approved} approved / {drafted} awaiting",
        f"HEALTH: api reqs={gov.total_consumed} (cap {gov.rate}/min)",
    ]
    _reply("\n".join(lines))
