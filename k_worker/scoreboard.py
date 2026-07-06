"""Chunk 7 — Scoreboard + treasury.

Daily scoreboard from store data — realized only, projections banned.
Headline metric: edge margin per band = win% - breakeven% - friction%
with N and Wilson bounds.

Statistical gates:
- Tradeable-at-size: Wilson 95% lower bound > breakeven + friction
- Band lock: Wilson upper bound < breakeven at N >= 200
- Project kill: all bands locked → thesis is dead
"""

import math
import time
import logging
from typing import Dict, Optional

from . import store, notify

log = logging.getLogger("k_worker.scoreboard")

COST_BANDS = [
    (95, 95), (96, 96), (97, 97), (98, 98), (99, 99),
]
FEE_FRICTION_PCT = 0.01  # estimated 1% friction from fees/slippage


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
    lines.append("═══ K-WORKER SCOREBOARD ═══")
    lines.append(f"Time: {time.strftime('%Y-%m-%d %H:%M ET')}")

    # Overall daily stats
    daily = store.daily_stats("live-traded")
    lines.append(f"\n📊 Today: {daily['n']} trades, "
                 f"W/L={daily['wins']}/{daily['losses']}, "
                 f"net=${daily['net_pnl']:.2f}, fees=${daily['total_fees']:.2f}")

    # Per-band breakdown
    lines.append("\n╔═══════╦═════╦═══════╦══════════╦═══════════╦═══════════╗")
    lines.append("║ Band  ║  N  ║ Win%  ║ BE%      ║ Wilson LB ║ Margin    ║")
    lines.append("╠═══════╬═════╬═══════╬══════════╬═══════════╬═══════════╣")

    all_locked = True
    for lo, hi in COST_BANDS:
        stats = store.query_band_stats(lo, hi, "live-traded")
        n = stats["n"]
        wp = stats["win_pct"]
        be = lo / 100.0  # breakeven = cost
        w_lo, w_hi = wilson_bounds(stats["wins"], n)
        margin = wp - be - FEE_FRICTION_PCT if n > 0 else 0.0
        wilson_margin = w_lo - be - FEE_FRICTION_PCT if n > 0 else 0.0

        status = ""
        if n == 0:
            status = "⏳"
            all_locked = False
        elif w_lo > be + FEE_FRICTION_PCT:
            status = "✅ CLEAR"
            all_locked = False
        elif n >= 200 and w_hi < be:
            status = "🔒 LOCKED"
        else:
            status = "📊 MEASURING"
            all_locked = False

        lines.append(
            f"║ {lo}-{hi}¢ ║ {n:3d} ║ {wp:5.1%} ║ {be:6.1%}+1% ║ {w_lo:7.1%}   ║ {wilson_margin:+7.1%} {status} ║"
        )

    lines.append("╚═══════╩═════╩═══════╩══════════╩═══════════╩═══════════╝")

    if all_locked and any(store.query_band_stats(lo, hi, "live-traded")["n"] >= 200
                          for lo, hi in COST_BANDS):
        lines.append("\n🪦 PROJECT KILL: All bands locked. Thesis is dead.")

    # Shadow report
    shadow = store.daily_stats("live-observed")
    if shadow["n"] > 0:
        lines.append(f"\n👁 Shadow: {shadow['n']} observed, "
                     f"would-be net=${shadow['net_pnl']:.2f}")

    return "\n".join(lines)


def send_scoreboard() -> None:
    """Build and send scoreboard to Telegram."""
    text = build_scoreboard()
    log.info(f"[SCOREBOARD]\n{text}")
    notify.send(f"<pre>{text}</pre>")
