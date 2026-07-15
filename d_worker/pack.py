"""Pack builder — hourly to Telegram + DB, flood budget for events.

Message policy: hourly pack SENT to Telegram every hour, always.
Events (seed/settle/failure) sent individually within flood budget.
No daily pack — the hourly cadence replaces it.
"""

import os
import json
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from k_worker import notify

from . import dstore

log = logging.getLogger("d_worker.pack")

NY = ZoneInfo("America/New_York")
MSG_BUDGET_PER_HOUR = int(os.environ.get("DW_MSG_BUDGET_PER_HOUR", "20"))

_msg_count_this_hour: int = 0
_msg_hour: int = -1
_overflow_count: int = 0


def _reset_hour() -> None:
    global _msg_count_this_hour, _msg_hour, _overflow_count
    now = datetime.now(NY)
    current_hour = now.hour
    if current_hour != _msg_hour:
        _overflow_count = _msg_count_this_hour - MSG_BUDGET_PER_HOUR \
            if _msg_count_this_hour > MSG_BUDGET_PER_HOUR else 0
        _msg_count_this_hour = 0
        _msg_hour = current_hour


def send_event(text: str) -> None:
    """Send a seed/settle/failure event if within flood budget."""
    _reset_hour()
    global _msg_count_this_hour
    _msg_count_this_hour += 1
    if _msg_count_this_hour > MSG_BUDGET_PER_HOUR:
        log.info(f"[PACK] Message over budget, suppressed: {text[:60]}")
        return
    notify.send(text)


def hourly_pack_sent_this_hour() -> bool:
    now = datetime.now(NY)
    key = f"pack_sent_{now.strftime('%Y%m%d_%H')}"
    return dstore.get_state(key) == "1"


def build_pack() -> str:
    """Build the 8-section pack. Covers the full day (since midnight). ≤30 lines."""
    now = datetime.now(NY)
    date_str = now.strftime("%Y-%m-%d")
    midnight = dstore.et_midnight_ts()

    seed_stats = dstore.seed_stats_since(midnight)
    alerts = _get_alerts()
    is_quiet = (seed_stats["new"] == 0 and seed_stats["today_settled"] == 0
                and not alerts)
    mode = "quiet" if is_quiet else "active"

    lines = [f"🅳 === KAL-D HOURLY — {date_str} {now.strftime('%H:%M')} ET ({mode}) ==="]

    # §1 SCAN
    cycles = dstore.latest_cycles(100)
    day_cycles = [c for c in cycles if c["started_ts"] >= midnight]
    total_mkts = sum(c.get("markets_seen", 0) for c in day_cycles)
    total_seeds = sum(c.get("seeds", 0) for c in day_cycles)
    total_skips = sum(c.get("skips", 0) for c in day_cycles)
    total_errs = sum(c.get("errs", 0) for c in day_cycles)
    top_skips = dstore.top_skip_reasons(midnight, 2)
    top_str = ""
    if top_skips:
        parts = []
        total_v = sum(s[1] for s in top_skips)
        for reason, count in top_skips[:2]:
            pct = count / total_v * 100 if total_v > 0 else 0
            parts.append(f"{reason} {pct:.0f}%")
        top_str = " | top: " + ", ".join(parts)
    lines.append(f"1. SCAN: {total_mkts} mkts in {len(day_cycles)} sweeps | "
                 f"SEED {total_seeds} / SKIP {total_skips} / ERR {total_errs}{top_str}")

    # §2 SEEDS
    sim_used = dstore.sim_capital_used()
    sim_cap = float(os.environ.get("DW_SIM_CAPITAL_USD", "10.00"))
    watch = dstore.watch_stats_since(midnight)
    watch_str = ""
    if seed_stats["open"] > 0:
        watch_str = f" | watch ✓ {watch['held']}/{watch['total']}"
        if watch["broken"] > 0:
            watch_str = f" | watch ⚠ {watch['broken']} BROKEN"
    lines.append(f"2. SEEDS: {seed_stats['new']} new | {seed_stats['open']} open | "
                 f"sim ${sim_used:.2f}/${sim_cap:.2f}{watch_str}")

    # §3 SETTLED
    lt_settled = seed_stats["lifetime_settled"]
    lt_correct = seed_stats["lifetime_correct"]
    lt_pct = lt_correct / lt_settled * 100 if lt_settled > 0 else 0
    inv_label = "INVARIANT" if lt_pct == 100 or lt_settled == 0 else "⚠ BROKEN"
    lines.append(f"3. SETTLED: {seed_stats['today_settled']} today | "
                 f"classifier {lt_correct}/{lt_settled} ✓ "
                 f"(lifetime {lt_pct:.1f}% — {inv_label})")

    # §4 CLASSES
    daily_stats = dstore.get_daily_stats()
    if daily_stats:
        class_parts = []
        for ds in daily_stats[:4]:
            class_parts.append(
                f"{ds['dclass']} N={ds['n_settled']} "
                f"LB(taker)={ds.get('wilson_lb_net_taker', 0):+.1f}¢ "
                f"LB(maker)={ds.get('wilson_lb_net_maker', 0):+.1f}¢")
        lines.append("4. CLASSES: " + " | ".join(class_parts))
    else:
        lines.append("4. CLASSES: building")

    # §5 FILLS
    fill_stats = dstore.seed_fill_stats_since(midnight)
    lines.append(f"5. FILLS: taker-viable {fill_stats['taker_viable']}/{fill_stats['total']} | "
                 f"maker UNINSTRUMENTED")

    # §6 REGISTRY
    all_reg = dstore.list_registry()
    approved = [r for r in all_reg if r.get("approved_ts")]
    drafted = [r for r in all_reg if not r.get("approved_ts")]
    fees = dstore.list_fees()
    lines.append(f"6. REGISTRY: fees {len(fees)} cached | "
                 f"settle: {len(approved)} approved / "
                 f"{len(drafted)} drafted")

    # §7 HEALTH
    from . import scanner
    gov = scanner._get_governor()
    feed_status = _feed_health()
    last_cycle = day_cycles[0] if day_cycles else None
    sweep_sec = 0
    last_reqs = 0
    if last_cycle:
        if last_cycle.get("finished_ts") and last_cycle.get("started_ts"):
            sweep_sec = last_cycle["finished_ts"] - last_cycle["started_ts"]
        last_reqs = last_cycle.get("req_count", 0) or 0
    rpm = last_reqs / (sweep_sec / 60) if sweep_sec > 0 else 0
    lines.append(f"7. HEALTH: api {last_reqs} reqs/{sweep_sec:.0f}s "
                 f"({rpm:.0f}/min, cap {gov.rate}) | "
                 f"{feed_status}")

    # §8 ALERTS
    _reset_hour()
    if alerts:
        lines.append(f"8. ALERTS: {'; '.join(alerts[:3])}")
    elif _overflow_count > 0:
        lines.append(f"8. ALERTS: {_overflow_count} messages suppressed (budget)")
    else:
        lines.append("8. ALERTS: none")

    return "\n".join(lines)


def send_hourly_pack() -> None:
    """Build the hourly pack, store to DB, and send to Telegram."""
    if hourly_pack_sent_this_hour():
        return
    text = build_pack()
    now = datetime.now(NY)
    hour_key = now.strftime("%Y%m%d_%H")
    midnight = dstore.et_midnight_ts()
    seed_stats = dstore.seed_stats_since(midnight)
    stats = {
        "seeds_new": seed_stats["new"],
        "seeds_open": seed_stats["open"],
        "settled_today": seed_stats["today_settled"],
    }
    dstore.insert_pack(hour_key, text, json.dumps(stats))
    notify.send(text)
    dstore.set_state(f"pack_sent_{hour_key}", "1")
    log.info(f"[PACK] Hourly pack sent: {hour_key}")


def _feed_health() -> str:
    stale_key = dstore.get_state("feed_stale_count")
    disagree_key = dstore.get_state("feed_disagree_count")
    stale = int(stale_key) if stale_key else 0
    disagree = int(disagree_key) if disagree_key else 0
    if stale > 0:
        return f"feeds STALE {stale}"
    parts = ["feeds fresh"]
    if disagree > 0:
        parts.append(f"disagree {disagree}")
    return " | ".join(parts)


def _get_alerts() -> list:
    halt = dstore.get_state("halt_promotion")
    alerts = []
    if halt == "1":
        alerts.append("halt_promotion active (invariant break)")
    midnight = dstore.et_midnight_ts()
    watch = dstore.watch_stats_since(midnight)
    if watch["broken"] > 0:
        alerts.append(f"{watch['broken']} EVIDENCE_BROKEN watch event(s)")
    breaks = dstore.invariant_break_seeds()
    if breaks:
        alerts.append(f"{len(breaks)} invariant break(s)")
    return alerts
