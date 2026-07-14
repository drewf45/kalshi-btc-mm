"""Hourly Telegram pack builder + message grammar + flood budget.

Exactly 8 sections, ≤30 lines, prefixed 🅳.
"""

import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from k_worker import notify

from . import dstore

log = logging.getLogger("d_worker.pack")

NY = ZoneInfo("America/New_York")
MSG_BUDGET_PER_HOUR = int(os.environ.get("DW_MSG_BUDGET_PER_HOUR", "20"))
PACK_MINUTE = int(os.environ.get("DW_PACK_MINUTE", "0"))

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
    """Send an event message if within budget."""
    _reset_hour()
    global _msg_count_this_hour
    _msg_count_this_hour += 1
    if _msg_count_this_hour > MSG_BUDGET_PER_HOUR:
        log.info(f"[PACK] Message over budget, queuing: {text[:60]}")
        return
    notify.send(text)


def is_pack_time() -> bool:
    now = datetime.now(NY)
    return now.minute == PACK_MINUTE


def pack_sent_this_hour() -> bool:
    now = datetime.now(NY)
    key = f"pack_sent_{now.strftime('%Y%m%d_%H')}"
    return dstore.get_state(key) == "1"


def mark_pack_sent() -> None:
    now = datetime.now(NY)
    key = f"pack_sent_{now.strftime('%Y%m%d_%H')}"
    dstore.set_state(key, "1")


def build_pack() -> str:
    """Build the 8-section hourly pack. ≤30 lines."""
    now = datetime.now(NY)
    hour_str = now.strftime("%H")
    hour_start = now.replace(minute=0, second=0, microsecond=0).timestamp()
    midnight = dstore.et_midnight_ts()

    # Determine quiet/active
    seed_stats = dstore.seed_stats_since(hour_start)
    settled_hour = seed_stats["today_settled"]
    alerts = _get_alerts()
    is_quiet = (seed_stats["new"] == 0 and settled_hour == 0
                and not alerts)
    mode = "quiet" if is_quiet else "active"

    lines = [f"🅳 === KAL-D HOURLY — {hour_str}:00 ET "
             f"(hour {int(now.strftime('%H'))} · {mode}) ==="]

    # §1 SCAN
    cycles = dstore.latest_cycles(24)
    hour_cycles = [c for c in cycles if c["started_ts"] >= hour_start]
    total_mkts = sum(c.get("markets_seen", 0) for c in hour_cycles)
    total_seeds = sum(c.get("seeds", 0) for c in hour_cycles)
    total_skips = sum(c.get("skips", 0) for c in hour_cycles)
    total_errs = sum(c.get("errs", 0) for c in hour_cycles)
    top_skips = dstore.top_skip_reasons(hour_start, 2)
    top_str = ""
    if top_skips:
        parts = []
        total_v = sum(s[1] for s in top_skips)
        for reason, count in top_skips[:2]:
            pct = count / total_v * 100 if total_v > 0 else 0
            parts.append(f"{reason} {pct:.0f}%")
        top_str = " | top: " + ", ".join(parts)
    lines.append(f"1. SCAN: {total_mkts} mkts in {len(hour_cycles)} sweeps | "
                 f"SEED {total_seeds} / SKIP {total_skips} / ERR {total_errs}{top_str}")

    # §2 SEEDS
    all_stats = dstore.seed_stats_since(midnight)
    sim_used = dstore.sim_capital_used()
    import os
    sim_cap = float(os.environ.get("DW_SIM_CAPITAL_USD", "10.00"))
    lines.append(f"2. SEEDS: {seed_stats['new']} new | {all_stats['open']} open | "
                 f"sim ${sim_used:.2f}/${sim_cap:.2f}")

    # §3 SETTLED
    lt_settled = all_stats["lifetime_settled"]
    lt_correct = all_stats["lifetime_correct"]
    lt_pct = lt_correct / lt_settled * 100 if lt_settled > 0 else 0
    inv_label = "INVARIANT" if lt_pct == 100 or lt_settled == 0 else "⚠ BROKEN"
    lines.append(f"3. SETTLED: {all_stats['today_settled']} | "
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
                 f"maker-est-filled {fill_stats['maker_filled']}/{fill_stats['total']}")

    # §6 REGISTRY
    all_reg = dstore.list_registry()
    approved = [r for r in all_reg if r.get("approved_ts")]
    drafted = [r for r in all_reg if not r.get("approved_ts")]
    fees = dstore.list_fees()
    lines.append(f"6. REGISTRY: fees {len(fees)} cached | "
                 f"settle: {len(approved)} approved / "
                 f"{len(drafted)} awaiting /approve")

    # §7 HEALTH
    from . import scanner
    gov = scanner._get_governor()
    feed_status = _feed_health()
    last_cycle = cycles[0] if cycles else None
    sweep_sec = 0
    if last_cycle and last_cycle.get("finished_ts") and last_cycle.get("started_ts"):
        sweep_sec = last_cycle["finished_ts"] - last_cycle["started_ts"]
    lines.append(f"7. HEALTH: api {gov.total_consumed}/min (cap {gov.rate}) | "
                 f"{feed_status} | sweep {sweep_sec:.0f}s")

    # §8 ALERTS
    _reset_hour()
    if alerts:
        lines.append(f"8. ALERTS: {'; '.join(alerts[:3])}")
    elif _overflow_count > 0:
        lines.append(f"8. ALERTS: {_overflow_count} messages suppressed (budget)")
    else:
        lines.append("8. ALERTS: none")

    return "\n".join(lines)


def send_pack() -> None:
    """Build and send the hourly pack."""
    if pack_sent_this_hour():
        return
    text = build_pack()
    log.info(f"[PACK]\n{text}")
    import html
    notify.send(f"<pre>{html.escape(text)}</pre>")
    mark_pack_sent()


def _feed_health() -> str:
    stale_key = dstore.get_state("feed_stale_count")
    disagree_key = dstore.get_state("feed_disagree_count")
    stale = int(stale_key) if stale_key else 0
    disagree = int(disagree_key) if disagree_key else 0
    if stale > 0:
        return f"feeds STALE {stale}"
    parts = ["feeds fresh ✓"]
    if disagree > 0:
        parts.append(f"disagree {disagree}")
    return " | ".join(parts)


def _get_alerts() -> list:
    halt = dstore.get_state("halt_promotion")
    alerts = []
    if halt == "1":
        alerts.append("halt_promotion active (invariant break)")
    breaks = dstore.invariant_break_seeds()
    if breaks:
        alerts.append(f"{len(breaks)} invariant break(s)")
    return alerts
