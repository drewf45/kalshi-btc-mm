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

from . import store, notify, treasury, gateway

log = logging.getLogger("k_worker.scoreboard")

F_COST_BANDS = [
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


def _lane_daily(lane: str) -> dict:
    """Today's W/L for a specific lane."""
    today_start = store.et_midnight_ts()
    with store._lock:
        rows = store._conn.execute(
            """SELECT resolution, pnl_net FROM surface
               WHERE lane=? AND action='ENTER' AND decision_ts >= ?
               AND resolution IN ('win', 'loss')""",
            (lane, today_start),
        ).fetchall()
    wins = sum(1 for r in rows if r[0] == "win")
    losses = sum(1 for r in rows if r[0] == "loss")
    pnl = sum(r[1] or 0 for r in rows)
    return {"wins": wins, "losses": losses, "n": wins + losses, "pnl": pnl}


def build_scoreboard() -> str:
    """Build the daily scoreboard string."""
    lines = []
    lines.append("=== Kal SCOREBOARD ===")
    lines.append(f"Time: {datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M ET')}")

    daily = store.daily_stats("live-traded")
    lines.append(f"\nToday: {daily['n']} trades, "
                 f"W/L={daily['wins']}/{daily['losses']}, "
                 f"net=${daily['net_pnl']:.2f}, fees=${daily['total_fees']:.2f}")

    # === LANE F (LIVE) ===
    f_daily = _lane_daily("F")
    kill_f = store.query_lane_losses_recent("F", 3600)
    lines.append(f"\n LANE F (LIVE) W/L={f_daily['wins']}/{f_daily['losses']} "
                 f"net=${f_daily['pnl']:.2f} | kill {kill_f}/3")
    lines.append(" Band  |  N  | Win%  | BE%      | Wilson LB | Margin")
    lines.append("-------|-----|-------|----------|-----------|----------")

    all_locked = True
    for lo, hi in F_COST_BANDS:
        stats = store.query_band_stats(lo, hi, "live-traded", "F")
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

    if all_locked and any(store.query_band_stats(lo, hi, "live-traded", "F")["n"] >= 200
                          for lo, hi in F_COST_BANDS):
        lines.append("PROJECT KILL: All F bands locked. Thesis is dead.")

    # === LANE H8 (LIVE) ===
    h8_daily = _lane_daily("H8")
    kill_h8 = store.query_lane_losses_recent("H8", 3600)
    h8_cells = store.query_h8_grid()
    if h8_cells or h8_daily["n"] > 0:
        lines.append(f" LANE H8 (LIVE) W/L={h8_daily['wins']}/{h8_daily['losses']} "
                     f"net=${h8_daily['pnl']:.2f} | kill {kill_h8}/3 | "
                     f"budget ${store.query_h8_probe_daily()['at_risk']:.2f}/"
                     f"${gateway.LANES['H8']['daily_budget']:.2f}")
        if h8_cells:
            lines.append(" Cost     | Dist  | Time     |  N  | Win%  | BE%  | WLB")
            lines.append("----------|-------|----------|-----|-------|------|------")
            for c in h8_cells:
                w_lo, _ = wilson_bounds(c["wins"], c["n"])
                lines.append(
                    f" {c['cost']:>8s} | {c['dist']:>5s} | {c['time']:>8s} | "
                    f"{c['n']:3d} | {c['win_pct']:5.1%} | {c['be']:.0%} | {w_lo:5.1%}"
                )
        lines.append("")

    # === LANE MM (SHADOW) — placeholder ===
    # === LANE D (SHADOW) — placeholder ===

    # --- F SHADOW (observed) ---
    has_shadow = False
    shadow_lines = []
    shadow_lines.append(" F SHADOW (observed)")
    shadow_lines.append(" Band  |  N  | Win%  | Wilson LB")
    shadow_lines.append("-------|-----|-------|----------")
    for lo, hi in F_COST_BANDS:
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

    # --- CLIP BY TIME BAND (H10/Yogi-Berra) ---
    clip_bands = store.query_clip_by_time_band()
    if any(c["traded_n"] > 0 for c in clip_bands):
        lines.append(" CLIP BY TIME BAND")
        lines.append(" Band      |  N  | Win%  | Clip   | Obs")
        lines.append("-----------|-----|-------|--------|-----")
        for c in clip_bands:
            if c["traded_n"] > 0 or c["obs_n"] > 0:
                wp = c["traded_win_pct"] if c["traded_n"] > 0 else 0
                lines.append(
                    f" {c['band']:<10s}| {c['traded_n']:3d} | {wp:5.1%} "
                    f"| ${c['traded_clip']:+.3f} | {c['obs_n']}"
                )
        lines.append("")

    # --- MEDIAN DEPTH AT TOUCH ---
    med_depth = store.query_median_depth()
    if med_depth is not None:
        lines.append(f" Median depth at touch: {med_depth} contracts")
        lines.append("")

    # --- CAUTION LEDGER ---
    caution = store.query_caution_ledger()
    if caution["n"] > 0:
        lines.append(" CAUTION LEDGER (today)")
        lines.append(f"  Captured:       ${caution['captured']:.2f}")
        lines.append(f"  Cost of caution: ${caution['cost_of_caution']:.2f}")
        lines.append(f"  Caution savings: ${caution['caution_savings']:.2f}")
        lines.append(f"  Net caution:     ${caution['net_caution']:+.2f}")
        lines.append(f"  Took loss:       ${caution['took_loss']:.2f}")
        lines.append("")

    # --- CONTEXT TABLE ---
    ctx = store.query_context_stats()
    if ctx["sessions"]:
        lines.append(" CONTEXT: Sessions")
        lines.append(" Session     |  N  | Win%  | Net PnL")
        lines.append("-------------|-----|-------|--------")
        for tag in ["ASIA", "LONDON_OPEN", "EU", "NY_PRE", "NY_OPEN", "NY", "NY_CLOSE", "EVENING"]:
            s = ctx["sessions"].get(tag)
            if s and s["n"] > 0:
                lines.append(f" {tag:<12s}| {s['n']:3d} | {s['win_pct']:5.1%} | ${s['net_pnl']:+.2f}")
        lines.append("")
    if ctx["vol_regimes"]:
        lines.append(" CONTEXT: Vol Regime")
        lines.append(" Regime |  N  | Win%  | Net PnL")
        lines.append("--------|-----|-------|--------")
        for tag in ["LOW", "MED", "HIGH", "UNKNOWN"]:
            s = ctx["vol_regimes"].get(tag)
            if s and s["n"] > 0:
                lines.append(f" {tag:<7s}| {s['n']:3d} | {s['win_pct']:5.1%} | ${s['net_pnl']:+.2f}")
        lines.append("")

    # --- S4: Weekly drift review (Sunday only) ---
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() == 6:
        drift = store.query_drift_stats()
        if drift["win_count"] + drift["loss_count"] > 0:
            lines.append(" WEEKLY DRIFT REVIEW")
            lines.append(f" Winners: mean drift={drift['win_mean_drift']:+.1f}c (N={drift['win_count']})")
            lines.append(f" Losers:  mean drift={drift['loss_mean_drift']:+.1f}c (N={drift['loss_count']})")
            lines.append(f" Loss by time bucket:")
            for bucket, count in drift["loss_buckets"].items():
                lines.append(f"   T-{bucket}s: {count} losses")
            lines.append("")

    # --- TREASURY ---
    lines.append(treasury.format_scoreboard())
    lines.append("")

    # --- Conflicting resolution check ---
    conflicts = store.check_conflicting_resolutions()
    if conflicts:
        lines.append(f" ALERT: Conflicting resolutions: {', '.join(conflicts[:5])}")
        lines.append("")

    # --- CENSUS COVERAGE ---
    census_covered = store.get_state("census_covered")
    census_missed = store.get_state("census_missed")
    if census_covered:
        seen = int(census_covered)
        missed = int(census_missed) if census_missed else 0
        total = seen + missed
        cov_line = f" Coverage: {seen} seen-live / {total} total"
        if missed > 0:
            cov_line += f" ({missed} explained-missed)"
        lines.append(cov_line)
        lines.append("")

    # --- Daily shadow summary ---
    shadow = store.daily_stats("live-observed")
    if shadow["n"] > 0:
        lines.append(f"Shadow: {shadow['n']} observed, "
                     f"would-be net=${shadow['net_pnl']:.2f}")

    return "\n".join(lines)


def send_scoreboard() -> None:
    """Build and send scoreboard to Telegram."""
    import html
    text = build_scoreboard()
    log.info(f"[SCOREBOARD]\n{text}")
    notify.send(f"<pre>{html.escape(text)}</pre>")
