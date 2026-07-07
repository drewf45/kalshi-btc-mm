"""Chunk 7 — Scoreboard + treasury.

Daily scoreboard from store data — realized only, projections banned.
Headline metric: edge margin per band = win% - breakeven% - friction%
with N and Wilson bounds.

Statistical gates:
- Tradeable-at-size: Wilson 95% lower bound > breakeven + friction
- Band lock: Wilson upper bound < breakeven at N >= 200
- Project kill: all bands locked -> thesis is dead
"""

import math
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Optional

from . import store, notify

log = logging.getLogger("k_worker.scoreboard")

COST_BANDS = [
    (95, 95), (96, 96), (97, 97), (98, 98), (99, 99),
]


def wilson_bounds(wins: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score interval. Returns (lower, upper)."""
    if n == 0:
        return 0.0, 1.0
    p_hat = wins / n
    denom = 1 + z * z / n
    centre = (p_hat + z * z / (2 * n)) / denom
    spread = z * math.sqrt((p_hat * (1 - p_hat) + z * z / (4 * n)) / n) / denom
    return max(0.0, centre - spread), min(1.0, centre + spread)


def build_scoreboard() -> str:
    """Build the daily scoreboard string."""
    lines = []
    lines.append("=== K-WORKER SCOREBOARD ===")
    lines.append(f"Time: {time.strftime('%Y-%m-%d %H:%M ET')}")

    daily = store.daily_stats("live-traded")
    lines.append(f"\nToday: {daily['n']} trades, "
                 f"W/L={daily['wins']}/{daily['losses']}, "
                 f"net=${daily['net_pnl']:.2f}, fees=${daily['total_fees']:.2f}")

    # --- TRADED bands ---
    lines.append("\n TRADED")
    lines.append(" Band  |  N  | Win%  | BE%      | Wilson LB | Margin")
    lines.append("-------|-----|-------|----------|-----------|----------")

    all_locked = True
    for lo, hi in COST_BANDS:
        stats = store.query_band_stats(lo, hi, "live-traded")
        n = stats["n"]
        wp = stats["win_pct"]
        be = lo / 100.0
        w_lo, w_hi = wilson_bounds(stats["wins"], n)

        friction = store.query_band_friction(lo, hi, "live-traded")

        if friction is not None:
            friction_str = f"{friction:.1%}"
            wilson_margin = w_lo - be - friction if n > 0 else 0.0
            be_display = f"{be:.1%}+{friction_str}"
        else:
            friction_str = "unmeasured"
            wilson_margin = w_lo - be if n > 0 else 0.0
            be_display = f"{be:.1%}+?"

        status = ""
        if n == 0:
            status = "waiting"
            all_locked = False
        elif friction is None:
            status = "unmeasured friction"
            all_locked = False
        elif w_lo > be + friction:
            status = "CLEAR"
            all_locked = False
        elif n >= 200 and w_hi < be:
            status = "LOCKED"
        else:
            status = "MEASURING"
            all_locked = False

        lines.append(
            f" {lo}-{hi}c | {n:3d} | {wp:5.1%} | {be_display:>8s} | {w_lo:7.1%}   | {wilson_margin:+7.1%} {status}"
        )

    lines.append("")

    if all_locked and any(store.query_band_stats(lo, hi, "live-traded")["n"] >= 200
                          for lo, hi in COST_BANDS):
        lines.append("PROJECT KILL: All bands locked. Thesis is dead.")

    # --- S1: SHADOW bands (live-observed with obs_win/obs_loss) ---
    has_shadow = False
    shadow_lines = []
    shadow_lines.append(" SHADOW (observed)")
    shadow_lines.append(" Band  |  N  | Win%  | Wilson LB")
    shadow_lines.append("-------|-----|-------|----------")
    for lo, hi in COST_BANDS:
        combined = store.query_band_stats_combined(lo, hi)
        obs_n = combined["obs_n"]
        if obs_n > 0:
            has_shadow = True
        obs_wp = combined["obs_win_pct"]
        obs_w_lo, _ = wilson_bounds(combined["obs_wins"], obs_n)
        shadow_lines.append(
            f" {lo}-{hi}c | {obs_n:3d} | {obs_wp:5.1%} | {obs_w_lo:7.1%}"
        )
    shadow_lines.append(f" Combined N per band used for trade-at-size gate")
    if has_shadow:
        lines.extend(shadow_lines)
        lines.append("")

    # --- S4: Weekly drift review (Sunday only) ---
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() == 6:  # Sunday
        drift = store.query_drift_stats()
        if drift["win_count"] + drift["loss_count"] > 0:
            lines.append(" WEEKLY DRIFT REVIEW")
            lines.append(f" Winners: mean drift={drift['win_mean_drift']:+.1f}c (N={drift['win_count']})")
            lines.append(f" Losers:  mean drift={drift['loss_mean_drift']:+.1f}c (N={drift['loss_count']})")
            lines.append(f" Loss by time bucket:")
            for bucket, count in drift["loss_buckets"].items():
                lines.append(f"   T-{bucket}s: {count} losses")
            lines.append("")

    # --- Daily shadow summary ---
    shadow = store.daily_stats("live-observed")
    if shadow["n"] > 0:
        lines.append(f"Shadow: {shadow['n']} observed, "
                     f"would-be net=${shadow['net_pnl']:.2f}")

    return "\n".join(lines)


def send_scoreboard() -> None:
    """Build and send scoreboard to Telegram."""
    text = build_scoreboard()
    log.info(f"[SCOREBOARD]\n{text}")
    notify.send(f"<pre>{text}</pre>")
